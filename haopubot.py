import asyncio
import io
import datetime, qrcode, socket, struct, threading, hashlib, uuid
import inspect
import random
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import telegram
import os
import sys
import subprocess
import logging, os, shutil
from dotenv import load_dotenv, dotenv_values
import requests
import urllib.parse
import html
import warnings
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Process
from telegram import helpers

try:
    from pygtrans import Translate
except Exception:
    Translate = None

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'), override=True)

from mongo import *
from account_health_check import check_account_inventory_item, get_account_check_runtime_status
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackContext, MessageHandler, CallbackQueryHandler, \
    InlineQueryHandler, filters
from telegram import InlineKeyboardMarkup,ForceReply, InlineKeyboardButton as TGInlineKeyboardButton, Update, ChatMemberRestricted, ChatPermissions, \
    ChatMemberRestricted, ChatMember, ChatMemberAdministrator, KeyboardButton as TGKeyboardButton, ReplyKeyboardMarkup, \
    InlineQueryResultArticle, InputTextMessageContent,InputMediaPhoto, MessageEntity
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut
import time, json, pickle, re
from threading import Timer
from decimal import Decimal
from datetime import timedelta
import zipfile
from pathlib import Path
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

warnings.filterwarnings(
    'ignore',
    message=r'Duplicate name: .*',
    category=UserWarning,
    module=r'zipfile',
)


def patch_zipfile_duplicate_name_warning():
    if getattr(zipfile.ZipFile, '_duplicate_name_warning_patched', False):
        return

    def _writecheck_no_duplicate_warning(self, zinfo):
        if self.mode not in ('w', 'x', 'a'):
            raise ValueError("write() requires mode 'w', 'x', or 'a'")
        if not self.fp:
            raise ValueError("Attempt to write ZIP archive that was already closed")
        zipfile._check_compression(zinfo.compress_type)
        if not self._allowZip64:
            requires_zip64 = None
            if len(self.filelist) >= zipfile.ZIP_FILECOUNT_LIMIT:
                requires_zip64 = 'Files count'
            elif zinfo.file_size > zipfile.ZIP64_LIMIT:
                requires_zip64 = 'Filesize'
            elif zinfo.header_offset > zipfile.ZIP64_LIMIT:
                requires_zip64 = 'Zipfile size'
            if requires_zip64:
                raise zipfile.LargeZipFile(requires_zip64 + ' would require ZIP64 extensions')

    zipfile.ZipFile._writecheck = _writecheck_no_duplicate_warning
    zipfile.ZipFile._duplicate_name_warning_patched = True


patch_zipfile_duplicate_name_warning()


REFERRAL_RATE_TIERS = (
    (Decimal('1000'), Decimal('0.05')),
    (Decimal('500'), Decimal('0.03')),
    (Decimal('100'), Decimal('0.01')),
)

REFERRAL_FIELD_DEFAULTS = {
    'referrer_user_id': 0,
    'ref_bind_time': '',
    'invite_count': 0,
    'invite_commission_total': 0,
    'referred_recharge_total': 0,
}


def to_db_number(value):
    normalized = standard_num(value)
    return float(normalized) if str(normalized).count('.') > 0 else int(normalized)


def ensure_referral_fields(user_id):
    user_doc = user.find_one({'user_id': user_id})
    if user_doc is None:
        return None
    updates = {}
    for field, default in REFERRAL_FIELD_DEFAULTS.items():
        if field not in user_doc:
            updates[field] = default
    if updates:
        user.update_one({'user_id': user_id}, {'$set': updates})
        user_doc.update(updates)
    return user_doc


def get_referral_rate(total_recharge_before):
    total_recharge_before = Decimal(str(total_recharge_before or 0))
    for threshold, rate in REFERRAL_RATE_TIERS:
        if total_recharge_before >= threshold:
            return rate
    return Decimal('0')


def user_has_recharge_history(user_id):
    if topup.find_one({'user_id': user_id, 'state': TOPUP_STATE_PAID}, {'_id': 1}) is not None:
        return True
    if user_log.find_one({'user_id': user_id, 'projectname': {'$regex': '充值'}}, {'_id': 1}) is not None:
        return True
    return False


def build_referral_text(bot_username, user_doc):
    referral_link = f'https://t.me/{bot_username}?start=ref_{user_doc["user_id"]}'
    invite_count = user_doc.get('invite_count', 0)
    commission_total = format_usdt_2(user_doc.get('invite_commission_total', 0))
    return f'''
<b>我的推广链接</b>

推广链接：
<code>{html.escape(referral_link, quote=False)}</code>

已邀请人数：<b>{invite_count}</b>
累计返佣：<b>{commission_total} USDT</b>

当前规则：
• 累计充值满100U返1%
• 累计充值满500U返3%
• 累计充值满1000U返5%
    '''


def format_user_ref(user_doc):
    if user_doc is None:
        return '无'
    fullname = str(user_doc.get('fullname') or '').replace('<', '').replace('>', '')
    username = str(user_doc.get('username') or '').strip().lstrip('@')
    if username:
        safe_username = html.escape(username, quote=False)
        safe_name = html.escape(fullname or username, quote=False)
        return f'<a href="https://t.me/{safe_username}">{safe_name}</a> (<code>{user_doc["user_id"]}</code>)'
    return f'{html.escape(fullname or str(user_doc["user_id"]), quote=False)} (<code>{user_doc["user_id"]}</code>)'


def build_admin_referral_text(target_user_doc):
    target_user_doc = ensure_referral_fields(target_user_doc['user_id'])
    inviter_doc = None
    inviter_user_id = int(target_user_doc.get('referrer_user_id', 0) or 0)
    if inviter_user_id:
        inviter_doc = ensure_referral_fields(inviter_user_id)

    invitees = list(
        user.find({'referrer_user_id': target_user_doc['user_id']}, {'user_id': 1, 'fullname': 1, 'ref_bind_time': 1})
        .sort('ref_bind_time', -1)
        .limit(5)
    )
    invitee_preview = []
    for invitee in invitees:
        invitee_name = html.escape(str(invitee.get('fullname') or invitee.get('user_id') or '').replace('<', '').replace('>', ''), quote=False)
        invitee_preview.append(f'• {invitee_name} (<code>{invitee["user_id"]}</code>)')
    invitee_preview_text = '\n'.join(invitee_preview) if invitee_preview else '• 暂无'

    commission_rows = list(
        commission_log.find({'inviter_user_id': target_user_doc['user_id']}, {'invitee_user_id': 1, 'recharge_amount': 1, 'commission_amount': 1, 'rate': 1})
        .sort('created_at', -1)
        .limit(5)
    )
    commission_preview = []
    for row in commission_rows:
        percent = int(Decimal(str(row.get('rate', 0) or 0)) * 100)
        commission_preview.append(
            f'• 下级 <code>{row["invitee_user_id"]}</code> 充值 {format_usdt_2(row.get("recharge_amount", 0))}U '
            f'返 {format_usdt_2(row.get("commission_amount", 0))}U ({percent}%)'
        )
    commission_preview_text = '\n'.join(commission_preview) if commission_preview else '• 暂无'

    return f'''
<b>推广关系查询</b>

<b>用户ID:</b> <code>{target_user_doc["user_id"]}</code>
<b>邀请人:</b> {format_user_ref(inviter_doc)}
<b>绑定时间:</b> {html.escape(str(target_user_doc.get("ref_bind_time") or "未绑定"), quote=False)}

<b>已邀请人数:</b> {target_user_doc.get("invite_count", 0)}
<b>累计返佣:</b> {format_usdt_2(target_user_doc.get("invite_commission_total", 0))} USDT
<b>本人累计有效充值:</b> {format_usdt_2(target_user_doc.get("referred_recharge_total", 0))} USDT

<b>最近邀请的下级:</b>
{invitee_preview_text}

<b>最近返佣流水:</b>
{commission_preview_text}
    '''


def bind_referrer_if_possible(user_id, referral_code, timer):
    referral_code = str(referral_code or '').strip()
    if not referral_code.startswith('ref_'):
        return False
    referrer_part = referral_code.replace('ref_', '', 1).strip()
    if not referrer_part.isdigit():
        return False
    referrer_user_id = int(referrer_part)
    if referrer_user_id == user_id:
        return False

    user_doc = ensure_referral_fields(user_id)
    referrer_doc = ensure_referral_fields(referrer_user_id)
    if user_doc is None or referrer_doc is None:
        return False
    if int(user_doc.get('referrer_user_id', 0) or 0) != 0:
        return False
    if Decimal(str(user_doc.get('referred_recharge_total', 0) or 0)) > 0:
        return False
    if Decimal(str(user_doc.get('zgje', 0) or 0)) > 0 or int(user_doc.get('zgsl', 0) or 0) > 0:
        return False
    if Decimal(str(user_doc.get('USDT', 0) or 0)) > 0:
        return False
    if user_has_recharge_history(user_id):
        return False

    user.update_one(
        {'user_id': user_id},
        {'$set': {'referrer_user_id': referrer_user_id, 'ref_bind_time': timer}}
    )
    user.update_one({'user_id': referrer_user_id}, {'$inc': {'invite_count': 1}})
    return True


def apply_referral_commission(bot, invitee_user_id, recharge_amount, order_id, source, timer):
    invitee_doc = ensure_referral_fields(invitee_user_id)
    if invitee_doc is None:
        return
    if commission_log.find_one({'order_id': order_id, 'invitee_user_id': invitee_user_id}, {'_id': 1}) is not None:
        return

    recharge_amount = Decimal(str(recharge_amount or 0))
    if recharge_amount <= 0:
        return

    recharge_before = Decimal(str(invitee_doc.get('referred_recharge_total', 0) or 0))
    recharge_after = to_db_number(recharge_before + recharge_amount)
    inviter_user_id = int(invitee_doc.get('referrer_user_id', 0) or 0)
    rate = get_referral_rate(recharge_before)
    commission_amount = Decimal('0')

    user.update_one({'user_id': invitee_user_id}, {'$set': {'referred_recharge_total': recharge_after}})

    if inviter_user_id and rate > 0:
        inviter_doc = ensure_referral_fields(inviter_user_id)
        if inviter_doc is not None:
            raw_commission = recharge_amount * rate
            commission_amount = Decimal(str(to_db_number(raw_commission)))
            if commission_amount > 0:
                inviter_balance = Decimal(str(inviter_doc.get('USDT', 0) or 0))
                inviter_commission_total = Decimal(str(inviter_doc.get('invite_commission_total', 0) or 0))
                user.update_one(
                    {'user_id': inviter_user_id},
                    {'$set': {
                        'USDT': to_db_number(inviter_balance + commission_amount),
                        'invite_commission_total': to_db_number(inviter_commission_total + commission_amount),
                    }}
                )
                if bot is not None:
                    try:
                        bot.send_message(
                            chat_id=inviter_user_id,
                            text=(
                                f'🎉 推广返佣到账 {format_usdt_2(commission_amount)} USDT\n'
                                f'下级用户充值：{format_usdt_2(recharge_amount)} USDT\n'
                                f'返佣比例：{int(rate * 100)}%'
                            ),
                        )
                    except Exception as exc:
                        print(f'推广返佣通知失败: {exc}')

    commission_logging(
        order_id,
        inviter_user_id,
        invitee_user_id,
        to_db_number(recharge_amount),
        float(rate),
        to_db_number(commission_amount),
        source,
        timer,
    )

BASE_DIR = Path(__file__).resolve().parent
VERSION_FILE = BASE_DIR / 'VERSION'

TELEGRAM_TRANSIENT_WINDOW_SECONDS = 120
TELEGRAM_TRANSIENT_OPEN_THRESHOLD = 12
TELEGRAM_TRANSIENT_COOLDOWN_SECONDS = 180
TELEGRAM_TRANSIENT_LOG_SUPPRESS_SECONDS = 30

_telegram_transient_lock = threading.Lock()
_telegram_transient_events = []
_telegram_transient_cooldown_until = 0.0
_telegram_transient_last_log_at = {}


def _configured_storage_roots(folder_name):
    folder_name = str(folder_name or '').strip()
    configured = []
    if folder_name == '协议号':
        protocol_root = str(os.getenv('BASE_PROTOCOL_PATH', '') or '').strip()
        if protocol_root:
            configured.append(Path(protocol_root))
    elif folder_name == '号包':
        direct_root = str(
            os.getenv('BASE_ACCOUNT_BAG_PATH', '')
            or os.getenv('BASE_DIRECT_LOGIN_PATH', '')
            or os.getenv('BASE_TDATA_PATH', '')
            or ''
        ).strip()
        if direct_root:
            configured.append(Path(direct_root))
        protocol_root = str(os.getenv('BASE_PROTOCOL_PATH', '') or '').strip()
        if protocol_root:
            protocol_parent = Path(protocol_root).expanduser().parent
            if str(protocol_parent) not in {'', '.'}:
                configured.append(protocol_parent / '号包')
    elif folder_name == '协议号发货':
        delivery_root = str(os.getenv('BASE_PROTOCOL_DELIVERY_PATH', '') or '').strip()
        if delivery_root:
            configured.append(Path(delivery_root))
    elif folder_name == '发货':
        delivery_root = str(os.getenv('BASE_DELIVERY_PATH', '') or '').strip()
        if delivery_root:
            configured.append(Path(delivery_root))
    return configured


def candidate_storage_roots(folder_name):
    roots = [*_configured_storage_roots(folder_name), BASE_DIR / folder_name, Path(folder_name)]
    deduped = []
    seen = set()
    for root in roots:
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def find_existing_storage_path(folder_name, *parts):
    for root in candidate_storage_roots(folder_name):
        candidate = root.joinpath(*[str(part) for part in parts])
        if candidate.exists():
            return candidate
    return candidate_storage_roots(folder_name)[0].joinpath(*[str(part) for part in parts])


def unique_preserve_order(values):
    result = []
    seen = set()
    for value in values or []:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


try:
    APP_VERSION = VERSION_FILE.read_text(encoding='utf-8').strip()
except Exception:
    APP_VERSION = '0.1.0'


def get_sifa_delay_seconds(send_type='text'):
    send_type = str(send_type or 'text').strip().lower()
    if send_type == 'text':
        default_value = '0.6'
        env_key = 'SIFA_TEXT_DELAY_SECONDS'
    else:
        default_value = '1.0'
        env_key = 'SIFA_MEDIA_DELAY_SECONDS'
    raw_value = str(os.getenv(env_key, default_value) or default_value).strip()
    try:
        value = float(raw_value)
    except Exception:
        value = float(default_value)
    return max(0.0, value)


DEFAULT_CLONE_WELCOME_TEXT = (
    '<b>🔥 欢迎使用号铺机器人\n\n'
    '‼️请先由管理员在后台完成以下配置：\n\n'
    '😃 欢迎语 / 联系方式\n'
    '😄 菜单按钮\n'
    '😃商品分类与商品内容\n'
    '😄TRC20 充值地址\n'
    '😃 其他支付配置\n\n'
    '⚙️ /start ⬅️ 点击命令打开菜单</b>'
)

DEFAULT_CLONE_WELCOME_TEXT_EN = (
    '💎 Our Services 💎\n'
    'Telegram Accounts, API Accounts, and Direct-Login Accounts (.tdata) — Wholesale & Retail!\n'
    'Telegram Premium Activation, Established Accounts, Groups, and Channels!\n\n'
    '❗️ If you are new to our products, please purchase a small quantity for testing first to avoid any unnecessary disputes. Thank you for your cooperation!\n\n'
    '❗️ Disclaimer: All products sold in this store are intended solely for entertainment and testing purposes; they must not be used for any illegal activities. Please comply with all local laws and regulations!\n\n'
    '🚀 /start ⬅️ Click this command to open the bottom menu!'
)

DEFAULT_LANG = 'zh'
SUPPORTED_LANGS = {'zh', 'en'}
TRANSLATION_TARGET_LANG = {'zh': 'zh-CN', 'en': 'en'}
TRANSLATION_EXACT_FALLBACKS = {
    'en': {
        '🛒商品列表': '🛒 Product Catalog',
        '👤个人中心': '👤 Profile',
        '💸我要充值': '💸 Recharge',
        '🧧红包': '🧧 Red Packets',
        '🌐 English': '🌐 中文',
        '🌐 中文': '🌐 English',
        '🏠主菜单': '🏠 Home',
        '⬅️返回': '⬅️ Back',
        '❌关闭': '❌ Close',
        '关闭': 'Close',
        '[emoji:5397916757333654639:➕]提醒补货': '[emoji:5397916757333654639:➕] Restock Alert',
        '#g [emoji:5287684458881756303:🤖]一键克隆同款': '#g [emoji:5287684458881756303:🤖] Clone This Bot',
        '🤖一键克隆同款': '🤖 Clone This Bot',
        '✅购买': '✅ Buy Now',
        '⚠️暂无库存': '⚠️ Out of Stock',
        '🛒购买记录': '🛒 Purchase History',
        '下一页': 'Next',
        '上一页': 'Previous',
        '返回个人中心': 'Back to Profile',
        '自定义充值金额': 'Custom Amount',
        '返回支付方式': 'Back to Payment Methods',
        '取消充值': 'Cancel Recharge',
        '请输入充值金额': 'Please enter the recharge amount',
        '请输入OKPay充值金额': 'Please enter the OKPay recharge amount',
        '请输入数字': 'Please enter a number',
        '确认购买✅': 'Confirm ✅',
        '全新注册 | 一手协议号（session+json）': 'NEW | Fresh Session (session+json)',
        '全新注册 | 一手协议号(session+json)': 'NEW | Fresh Session (session+json)',
        '全球TG | 二次协议号（session+json）': 'Global TG | Secondary (session+json)',
        '全球TG | 二次协议号(session+json)': 'Global TG | Secondary (session+json)',
        '【1-8年】老协议号（session+json）': '[1-8Y] Old Protocol (session+json)',
        '【1-8年】老协议号(session+json)': '[1-8Y] Old Protocol (session+json)',
        '【1-8年】协议老号（session+json）': '[1-8Y] Old Protocol (session+json)',
        '【1-8年】协议老号(session+json)': '[1-8Y] Old Protocol (session+json)',
        'TG周会员号 | 凭证 接预定': 'TG Premium | Voucher Presale',
        'TG周会员号｜凭证 接预定': 'TG Premium | Voucher Presale',
    }
}
TRANSLATION_REPLACEMENT_FALLBACKS = {
    'en': OrderedDict([
        ('【1-8年】', '[1-8Y] '),
        ('（session+json）', ' (session+json)'),
        ('全新注册', 'NEW'),
        ('一手协议号', 'Fresh Session'),
        ('全球TG', 'Global TG'),
        ('二次协议号', 'Secondary'),
        ('老协议号', 'Old Protocol'),
        ('协议老号', 'Old Protocol'),
        ('TG周会员号', 'TG Premium'),
        ('凭证 接预定', 'Voucher Presale'),
        ('欢迎使用号铺机器人', 'Welcome to BotShop'),
        ('点击命令打开菜单', 'Tap the command to open the menu'),
        ('请选择充值方式', 'Please choose a recharge method'),
        ('请选择支付方式', 'Please choose a payment method'),
        ('商品列表', 'Product Catalog'),
        ('个人中心', 'Profile'),
        ('我要充值', 'Recharge'),
        ('红包', 'Red Packets'),
        ('购买记录', 'Purchase History'),
        ('返回个人中心', 'Back to Profile'),
        ('主菜单', 'Home'),
        ('返回', 'Back'),
        ('关闭', 'Close'),
        ('购买', 'Buy Now'),
        ('暂无库存', 'Out of Stock'),
        ('提醒补货', 'Restock Alert'),
        ('联系客服', 'Contact Support'),
        ('使用教程', 'Tutorial'),
        ('查询库存', 'Check Stock'),
        ('充值金额', 'Recharge Amount'),
        ('价格', 'Price'),
        ('库存', 'Stock'),
        ('数量', 'Quantity'),
    ])
}
TRANSLATION_UI_TEXTS = {
    'language_toggle': {'zh': '[emoji:5298584437338946835:🌐]English', 'en': '[emoji:5298584437338946835:🌐]中文'},
    'language_switch_prompt': {'zh': '请选择语言', 'en': 'Please choose your language'},
    'language_switch_done': {'zh': '语言已切换为中文', 'en': 'Language switched to English'},
    'language_switch_zh': {'zh': '中文服务', 'en': '中文服务'},
    'language_switch_en': {'zh': 'English', 'en': 'English'},
    'menu_goods_list': {'zh': '🛒商品列表', 'en': '🛒 Product Catalog'},
    'menu_profile': {'zh': '👤个人中心', 'en': '👤 Profile'},
    'menu_recharge': {'zh': '💸我要充值', 'en': '💸 Recharge'},
    'menu_redpacket': {'zh': '🧧红包', 'en': '🧧 Red Packets'},
    'menu_clone_same': {'zh': '#g [emoji:5287684458881756303:🤖]一键克隆同款', 'en': '#g [emoji:5287684458881756303:🤖]Clone This Bot'},
    'purchase_history_button': {'zh': '🛒购买记录', 'en': '🛒 Purchase History'},
    'close': {'zh': '关闭', 'en': 'Close'},
    'close_with_icon': {'zh': '❌关闭', 'en': '❌ Close'},
    'main_menu': {'zh': '🏠主菜单', 'en': '🏠 Home'},
    'back': {'zh': '⬅️返回', 'en': '⬅️ Back'},
    'buy_now': {'zh': '✅购买', 'en': '✅ Buy Now'},
    'out_of_stock_button': {'zh': '⚠️暂无库存', 'en': '⚠️ Out of Stock'},
    'restock_notice_button': {'zh': '[emoji:5397916757333654639:➕]提醒补货', 'en': '[emoji:5397916757333654639:➕] Restock Alert'},
    'profile_text': {
        'zh': '<b>[emoji:5929391996408959380:🏞] 您的ID: <code>{user_id}</code>\n\n[emoji:6323075330189826977:😃] 您的用户名: {username_html}\n\n[emoji:5028418466000930064:📆] 注册日期: {creation_time}\n\n[emoji:6273995106810863535:🌑] 总购数量: {zgsl}\n\n[emoji:5028746137645876535:📈] 总购金额: {zgje} USDT\n\n[emoji:4972482444025398275:👛] 您的余额: {USDT} USDT</b>',
        'en': '<b>[emoji:5929391996408959380:🏞] Your ID: <code>{user_id}</code>\n\n[emoji:6323075330189826977:😃] Username: {username_html}\n\n[emoji:5028418466000930064:📆] Joined: {creation_time}\n\n[emoji:6273995106810863535:🌑] Total Purchases: {zgsl}\n\n[emoji:5028746137645876535:📈] Total Spent: {zgje} USDT\n\n[emoji:4972482444025398275:👛] Balance: {USDT} USDT</b>'
    },
    'category_list_text': {
        'zh': '<b>🛒这是商品列表  选择你需要的商品：\n\n❗️没使用过的本店商品的，请先少量购买测试，以免造成不必要的争执！谢谢合作！\n\n❗️账户放久难免会死，有差异，请联系客服售后！望理解！</b>',
        'en': '<b>🛒 Product Catalog\n\n❗️New item? Place a small test order first.\n\n❗️Accounts may change over time. Contact support if needed.</b>'
    },
    'category_empty_text': {
        'zh': '<b>⚠️ 当前这个分类暂时没有库存\n\n你可以返回上一层看看其他商品，或者稍后再来。</b>',
        'en': '<b>⚠️ This category is temporarily out of stock.\n\nYou can go back to browse other products or check again later.</b>'
    },
    'product_purchase_text': {
        'zh': '<b>✅您正在购买:  {projectname}\n\n💰 价格： {money} USDT\n\n📊 库存： {stock_count}\n\n❗️ 未使用过的本店商品的，请先少量购买测试，以免造成不必要的争执！谢谢合作！</b>',
        'en': '<b>✅ You are buying: {projectname}\n\n💰 Price: {money} USDT\n\n📊 Stock: {stock_count}\n\n❗️ If this is your first time buying this item, please place a small test order first to avoid unnecessary disputes. Thank you.</b>'
    },
    'product_not_found': {'zh': '未找到这个商品', 'en': 'Product not found'},
    'current_no_stock': {'zh': '当前暂无库存', 'en': 'Currently out of stock'},
    'restock_notice_enabled': {'zh': '已开启补货通知，商品补货后我会提醒你', 'en': 'Restock alert enabled. I will notify you when it is available.'},
    'restock_notice_disabled': {'zh': '已取消这个商品的补货通知', 'en': 'Restock alert disabled for this product.'},
    'restock_notice_empty_text': {
        'zh': '<b>✅您正在购买:  {projectname}\n\n💰 价格： {money} USDT\n\n📊 库存： 0\n\n❗️ 当前暂时无库存，你可以先开启补货通知。</b>',
        'en': '<b>✅ You are buying: {projectname}\n\n💰 Price: {money} USDT\n\n📊 Stock: 0\n\n❗️ This product is currently out of stock. You can enable a restock alert first.</b>'
    },
    'recharge_method_title': {'zh': '[emoji:5197474438970363734:💳] 请选择充值方式', 'en': '[emoji:5197474438970363734:💳] Please choose a recharge method'},
    'recharge_method_unavailable': {'zh': '当前未开启充值方式，请联系管理员', 'en': 'Recharge is currently unavailable. Please contact the admin.'},
    'recharge_trc20_button': {'zh': '[emoji:5080312910866024090:💵] USDT 充值 | TRC20', 'en': '[emoji:5080312910866024090:💵] USDT Recharge | TRC20'},
    'recharge_okpay_button': {'zh': '[emoji:6321339712430676611:💳] OKPay充值 | 秒到账', 'en': '[emoji:6321339712430676611:💳] OKPay Recharge | Fast Credit'},
    'custom_recharge_amount': {'zh': '自定义充值金额', 'en': 'Custom Amount'},
    'back_to_recharge_method': {'zh': '返回支付方式', 'en': 'Back to Payment Methods'},
    'cancel_recharge': {'zh': '取消充值', 'en': 'Cancel Recharge'},
    'trc20_amount_menu': {'zh': '<b>请选择下面 USDT(TRC20) 充值金额</b>', 'en': '<b>Please choose a USDT (TRC20) recharge amount</b>'},
    'okpay_amount_menu': {'zh': '<b>请选择下面 OKPay 充值金额</b>', 'en': '<b>Please choose an OKPay recharge amount</b>'},
    'enter_custom_trc20_amount': {'zh': '请输入充值金额', 'en': 'Please enter the recharge amount'},
    'enter_custom_okpay_amount': {'zh': '请输入OKPay充值金额', 'en': 'Please enter the OKPay recharge amount'},
    'cancel_input': {'zh': '❌取消输入', 'en': '❌ Cancel'},
    'please_enter_number': {'zh': '请输入数字', 'en': 'Please enter a number'},
    'insufficient_balance': {'zh': '❌余额不足，请立即充值', 'en': '❌ Insufficient balance. Please recharge first.'},
    'enter_quantity_prompt': {'zh': '<b>请输入数量：\n格式：</b><code>10</code>', 'en': '<b>Please enter the quantity:\nFormat:</b><code>10</code>'},
    'quantity_positive_integer': {'zh': '购买数量只能输入大于0的整数', 'en': 'Quantity must be an integer greater than 0.'},
    'quantity_positive_integer_retry': {'zh': '购买数量只能输入大于0的整数，不购买请点击取消', 'en': 'Quantity must be an integer greater than 0. Tap cancel if you do not want to buy.'},
    'stock_insufficient_retry': {'zh': '当前库存不足【请再次输入数量】', 'en': 'Insufficient stock right now. Please enter the quantity again.'},
    'cancel_purchase': {'zh': '❌取消购买', 'en': '❌ Cancel'},
    'cancel_trade': {'zh': '❌取消交易', 'en': '❌ Cancel'},
    'confirm_purchase': {'zh': '确认购买✅', 'en': 'Confirm ✅'},
    'purchase_confirm_text': {
        'zh': '<b>[emoji:5451937962629544243:🛍]您正在购买：{projectname}\n\n[emoji:5028746137645876535:📈] 数量：{gmsl}\n\n💰价格：{zxymoney}\n\n👛您的余额：{USDT}</b>',
        'en': '<b>[emoji:5451937962629544243:🛍] You are buying: {projectname}\n\n[emoji:5028746137645876535:📈] Quantity: {gmsl}\n\n💰 Price: {zxymoney}\n\n👛 Your Balance: {USDT}</b>'
    },
    'purchase_history_title': {'zh': '🛒购买记录', 'en': '🛒 Purchase History'},
    'next_page': {'zh': '下一页', 'en': 'Next'},
    'prev_page': {'zh': '上一页', 'en': 'Previous'},
    'back_profile': {'zh': '返回个人中心', 'en': 'Back to Profile'},
    'area_search_title': {'zh': '<b>[emoji:5220064167356025824:⭐️] 区号搜索结果</b>', 'en': '<b>[emoji:5220064167356025824:⭐️] Area Code Search Results</b>'},
    'area_search_keyword': {'zh': '[emoji:5217818964612108191:✨] 搜索关键词：<code>{area_code}</code>', 'en': '[emoji:5217818964612108191:✨] Search Keyword: <code>{area_code}</code>'},
    'area_search_total': {'zh': '[emoji:5028746137645876535:📈] 匹配商品：<code>{total}</code> 个', 'en': '[emoji:5028746137645876535:📈] Matched Products: <code>{total}</code>'},
    'area_search_tail_in_stock': {'zh': '请从下面列表中选择要查看的商品。', 'en': 'Please choose a product from the list below.'},
    'area_search_tail_no_stock': {'zh': '当前相关商品暂无库存，你可以点击底部按钮提醒补货。', 'en': 'Matching products are currently out of stock. You can tap the button below to request restock.'},
    'area_search_empty': {'zh': '[emoji:5301246586918024418:⚠️] 暂时没有找到 {area_code} 相关商品。\n\n你可以点击下方按钮提醒补货，或者稍后再来看看。', 'en': '[emoji:5301246586918024418:⚠️] No products related to {area_code} were found for now.\n\nYou can tap the button below to request restock or check again later.'},
    'area_request_invalid': {'zh': '提醒补货失败：区号格式无效', 'en': 'Restock request failed: invalid area code format.'},
    'area_request_exists': {'zh': '这个区号你已经提醒过补货啦，请等管理员上新 [emoji:5222044641200720562:🌸]', 'en': 'You have already requested restock for this area code. Please wait for the admin to add stock. [emoji:5222044641200720562:🌸]'},
    'area_request_done': {'zh': '已帮你提醒管理员补货，请稍后留意上新消息 [emoji:5222044641200720562:🌸]', 'en': 'I have notified the admin for restock. Please watch for new stock updates. [emoji:5222044641200720562:🌸]'},
    'stock_count_label': {'zh': '库存', 'en': 'Stock'},
    'redpacket_menu_title': {'zh': '从下面的列表中选择一个红包', 'en': 'Choose a red packet from the list below'},
    'redpacket_ongoing_active': {'zh': '◾️进行中', 'en': '◾️ Ongoing'},
    'redpacket_ended_tab': {'zh': '已结束', 'en': 'Ended'},
    'redpacket_ongoing_tab': {'zh': '进行中', 'en': 'Ongoing'},
    'redpacket_ended_active': {'zh': '◾️已结束', 'en': '◾️ Ended'},
    'redpacket_add': {'zh': '➕添加', 'en': '➕ Add'},
}

_translation_memory_cache = {}
_translation_client = None
_user_lang_cache = {}
_localized_button_cache = {}
_translation_warm_jobs = {}
_translation_warm_done = set()


ADMIN_EMOJI_USERLIST = '[emoji:6321041414067068140:👤]'
ADMIN_EMOJI_DM = '[emoji:5456535802429330837:💬]'
ADMIN_EMOJI_TRC20 = '[emoji:5443127283898405358:📥]'
ADMIN_EMOJI_OKPAY = '[emoji:5445353829304387411:💳]'
ADMIN_EMOJI_GOODS = '[emoji:5312361253610475399:🛒]'
ADMIN_EMOJI_WELCOME = '[emoji:5458382591121964689:✍️]'
ADMIN_EMOJI_MENU = '[emoji:5341715473882955310:⚙️]'
ADMIN_EMOJI_BUY_NOTICE = '[emoji:5235511932064129087:🎁]'
ADMIN_EMOJI_RESTOCK = '[emoji:5220214598585568818:🚨]'
ADMIN_EMOJI_CLONE = '#g [emoji:5287684458881756303:🤖]'
ADMIN_EMOJI_CLONE_LIST = '[emoji:5132131004097496494:🧩]'
ADMIN_EMOJI_CLOSE = '[emoji:5210952531676504517:❌]'

MOOD_EMOJI_SOFT = '[emoji:5222044641200720562:🌸]'
MOOD_EMOJI_SPARKLE = '[emoji:5217818964612108191:✨]'
MOOD_EMOJI_STAR = '[emoji:5220064167356025824:⭐️]'
MOOD_EMOJI_FAST = '[emoji:5220195537520711716:⚡️]'
MOOD_EMOJI_FIRE = '[emoji:5220166546491459639:🔥]'

ACCOUNT_CHECK_EMOJI_PROGRESS = '[emoji:5296562641613897196:🕜]'
ACCOUNT_CHECK_EMOJI_CHECKED = '[emoji:5429381339851796035:✅]'
ACCOUNT_CHECK_EMOJI_IN_PROGRESS = '[emoji:5834734348884075863:👍]'
ACCOUNT_CHECK_EMOJI_QUEUED = '[emoji:5255944891082492462:🟥]'
ACCOUNT_CHECK_EMOJI_ELAPSED = '[emoji:5269337080147748373:⏰]'
ACCOUNT_CHECK_EMOJI_ALIVE = '[emoji:5260463209562776385:✅]'
ACCOUNT_CHECK_EMOJI_INVALID = '[emoji:5273914604752216432:❌]'
ACCOUNT_CHECK_EMOJI_FROZEN = '[emoji:5449449325434266744:❄️]'
ACCOUNT_CHECK_EMOJI_TIMEOUT = '[emoji:5382194935057372936:⏱️]'
ACCOUNT_CHECK_EMOJI_TOTAL = '[emoji:5352625743081775722:🎚️]'


def parse_admin_user_ids(value):
    admin_ids = set()
    for item in (value or '').split(','):
        item = item.strip()
        if not item:
            continue
        try:
            admin_ids.add(int(item))
        except ValueError:
            pass
    return admin_ids


def build_admin_dashboard_keyboard(user_id):
    base_buttons = [
        InlineKeyboardButton(f'{ADMIN_EMOJI_USERLIST}用户列表', callback_data='yhlist'),
        InlineKeyboardButton(f'{ADMIN_EMOJI_DM}对话用户私发', callback_data='sifa'),
        InlineKeyboardButton(f'{ADMIN_EMOJI_TRC20}充值地址设置', callback_data='settrc20'),
        InlineKeyboardButton(f'{ADMIN_EMOJI_OKPAY}OKPay配置', callback_data='okpaycfg'),
        InlineKeyboardButton(f'{ADMIN_EMOJI_GOODS}商品管理', callback_data='spgli'),
        InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}欢迎语修改', callback_data='startupdate'),
        InlineKeyboardButton(f'{ADMIN_EMOJI_BUY_NOTICE}购买提醒', callback_data='buynoticecfg'),
        InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}菜单按钮', callback_data='addzdykey'),
        InlineKeyboardButton(f'{ADMIN_EMOJI_RESTOCK}补货通知', callback_data='restockpushcfg'),
    ]

    keyboard = []
    for index in range(0, len(base_buttons), 3):
        keyboard.append(base_buttons[index:index + 3])

    if BOT_CLONE_ENABLED:
        keyboard.append([
            InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}一键克隆同款', callback_data='clonebot'),
            InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE_LIST}克隆列表', callback_data='clonelist 0'),
        ])
        keyboard.append([
            InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}创建代理Bot', callback_data='cloneagent'),
            InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE_LIST}代理列表', callback_data='agentlist 0'),
        ])

    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
    return keyboard


def welcome_uses_html_parse(text, entities):
    return not entities and isinstance(text, str) and '<' in text and '>' in text


ADMIN_USER_IDS = parse_admin_user_ids(os.getenv('ADMIN_USER_IDS', ''))


def parse_env_bool(value, default=True):
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in ('1', 'true', 'yes', 'y', 'on'):
        return True
    if value in ('0', 'false', 'no', 'n', 'off'):
        return False
    return default


def normalize_menu_text(text):
    if not isinstance(text, str):
        return ''
    text = text.replace('\ufe0f', '').strip()
    text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
    text = re.sub(r'^[\W_]+', '', text)
    text = re.sub(r'\s+', '', text)
    return text


def normalize_lang_code(value):
    value = str(value or '').strip().lower()
    if value.startswith('en'):
        return 'en'
    if value.startswith('zh'):
        return 'zh'
    return DEFAULT_LANG


def get_translation_client():
    global _translation_client
    if _translation_client is not None or Translate is None:
        return _translation_client
    try:
        _translation_client = Translate()
    except Exception:
        logging.warning('Failed to initialize translation client', exc_info=True)
        _translation_client = False
    return _translation_client or None


def mask_translation_tokens(text):
    mapping = {}
    if not isinstance(text, str) or not text:
        return text, mapping
    patterns = [r'\[emoji:[^\]]+\]', r'<[^>]+>']
    masked = text
    counter = 0
    for pattern in patterns:
        def repl(match):
            nonlocal counter
            key = f'__BOTSHOP_TOKEN_{counter}__'
            counter += 1
            mapping[key] = match.group(0)
            return key
        masked = re.sub(pattern, repl, masked)
    return masked, mapping


def unmask_translation_tokens(text, mapping):
    restored = str(text or '')
    for key, value in mapping.items():
        restored = restored.replace(key, value)
    return restored


def apply_translation_fallbacks(text, target_lang):
    text = str(text or '')
    target_lang = normalize_lang_code(target_lang)
    exact = TRANSLATION_EXACT_FALLBACKS.get(target_lang, {})
    if text in exact:
        return exact[text]

    replacements = TRANSLATION_REPLACEMENT_FALLBACKS.get(target_lang, OrderedDict())
    translated = text
    for source, target in replacements.items():
        translated = translated.replace(source, target)
    return translated


def translate_text_via_http(text, target_lang='en'):
    text = str(text or '')
    target_lang = normalize_lang_code(target_lang)
    if not text or target_lang == 'zh':
        return text

    masked_text, mapping = mask_translation_tokens(text)
    response = requests.get(
        'https://translate.googleapis.com/translate_a/single',
        params={
            'client': 'gtx',
            'sl': 'auto',
            'tl': TRANSLATION_TARGET_LANG.get(target_lang, target_lang),
            'dt': 't',
            'q': masked_text,
        },
        timeout=12,
    )
    response.raise_for_status()
    data = response.json()
    translated = ''.join(
        str(part[0] or '')
        for part in (data[0] or [])
        if isinstance(part, list) and part
    ).strip()
    translated = unmask_translation_tokens(translated, mapping).strip()
    return translated or text


def translate_text(text, target_lang='en'):
    text = str(text or '')
    target_lang = normalize_lang_code(target_lang)
    if not text or target_lang == 'zh':
        return text

    fallback_text = apply_translation_fallbacks(text, target_lang)
    if fallback_text != text:
        return fallback_text

    cache_key = f'{target_lang}:{text}'
    cached = _translation_memory_cache.get(cache_key)
    if cached:
        return cached

    override_doc = translation_overrides.find_one({'text': text, 'lang': target_lang})
    if override_doc and override_doc.get('fanyi'):
        translated = str(override_doc.get('fanyi'))
        if translated and translated != text:
            _translation_memory_cache[cache_key] = translated
            return translated

    cache_doc = translation_cache.find_one({'text': text, 'lang': target_lang})
    if cache_doc and cache_doc.get('fanyi'):
        translated = str(cache_doc.get('fanyi'))
        if translated and translated != text:
            _translation_memory_cache[cache_key] = translated
            return translated

    client = get_translation_client()
    if client is None:
        try:
            translated = translate_text_via_http(text, target_lang)
            if translated and translated != text:
                translation_cache.update_one(
                    {'text': text, 'lang': target_lang},
                    {'$set': {'text': text, 'lang': target_lang, 'fanyi': translated, 'updated_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}},
                    upsert=True
                )
                _translation_memory_cache[cache_key] = translated
                return translated
        except Exception:
            logging.warning('HTTP translation failed for lang=%s text=%r', target_lang, text, exc_info=True)
        _translation_memory_cache[cache_key] = fallback_text
        return fallback_text

    masked_text, mapping = mask_translation_tokens(text)
    try:
        result = client.translate(masked_text.replace('\n', '\\n'), target=TRANSLATION_TARGET_LANG.get(target_lang, target_lang))
        translated = getattr(result, 'translatedText', None) or getattr(result, 'text', None) or str(result or '')
        translated = unmask_translation_tokens(translated.replace('\\n', '\n'), mapping).strip() or text
        if translated == text:
            translated = translate_text_via_http(text, target_lang)
        translation_cache.update_one(
            {'text': text, 'lang': target_lang},
            {'$set': {'text': text, 'lang': target_lang, 'fanyi': translated, 'updated_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}},
            upsert=True
        )
        _translation_memory_cache[cache_key] = translated
        return translated
    except Exception:
        logging.warning('Auto translation failed for lang=%s text=%r', target_lang, text, exc_info=True)
        fallback_text = apply_translation_fallbacks(text, target_lang)
        _translation_memory_cache[cache_key] = fallback_text
        return fallback_text


def get_cached_translation_text(text, target_lang='en'):
    text = str(text or '')
    target_lang = normalize_lang_code(target_lang)
    if not text or target_lang == 'zh':
        return text

    fallback_text = apply_translation_fallbacks(text, target_lang)
    if fallback_text != text:
        return fallback_text

    cache_key = f'{target_lang}:{text}'
    cached = _translation_memory_cache.get(cache_key)
    if cached:
        return cached

    override_doc = translation_overrides.find_one({'text': text, 'lang': target_lang})
    if override_doc and override_doc.get('fanyi'):
        translated = str(override_doc.get('fanyi'))
        if translated and translated != text:
            _translation_memory_cache[cache_key] = translated
            return translated

    cache_doc = translation_cache.find_one({'text': text, 'lang': target_lang})
    if cache_doc and cache_doc.get('fanyi'):
        translated = str(cache_doc.get('fanyi'))
        if translated and translated != text:
            _translation_memory_cache[cache_key] = translated
            return translated

    return fallback_text


def contains_cjk(text):
    return bool(re.search(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]', str(text or '')))


def get_ui_text(key, viewer_user_id=None, lang=None, **kwargs):
    lang = normalize_lang_code(lang or (get_user_lang(viewer_user_id) if viewer_user_id is not None else DEFAULT_LANG))
    bucket = TRANSLATION_UI_TEXTS.get(key, {})
    template = bucket.get(lang) or bucket.get(DEFAULT_LANG) or key
    return template.format(**kwargs) if kwargs else template


def get_user_lang(user_id=None, fallback=None):
    lang = normalize_lang_code(fallback)
    if user_id is None:
        return lang
    cached = _user_lang_cache.get(user_id)
    if cached:
        return cached
    row = user.find_one({'user_id': user_id}, {'lang': 1}) or {}
    stored = row.get('lang')
    if stored:
        normalized = normalize_lang_code(stored)
        _user_lang_cache[user_id] = normalized
        return normalized
    user.update_one({'user_id': user_id}, {'$set': {'lang': lang}})
    _user_lang_cache[user_id] = lang
    return lang


def set_user_lang(user_id, lang):
    lang = normalize_lang_code(lang)
    user.update_one({'user_id': user_id}, {'$set': {'lang': lang}})
    _user_lang_cache[user_id] = lang
    _localized_button_cache.clear()
    return lang


def toggle_user_lang(user_id):
    lang = 'en' if get_user_lang(user_id) == 'zh' else 'zh'
    if lang == 'en':
        warm_storefront_translation_cache(user_id=user_id, lang=lang, wait=True)
    lang = set_user_lang(user_id, lang)
    if lang == 'en':
        warm_storefront_translation_cache(user_id=user_id, lang=lang)
    return lang


def localize_dynamic_text(text, user_id=None, lang=None):
    lang = normalize_lang_code(lang or (get_user_lang(user_id) if user_id is not None else DEFAULT_LANG))
    return translate_text(text, lang) if lang == 'en' else str(text or '')


def localize_dynamic_text_fast(text, user_id=None, lang=None):
    lang = normalize_lang_code(lang or (get_user_lang(user_id) if user_id is not None else DEFAULT_LANG))
    return get_cached_translation_text(text, lang) if lang == 'en' else str(text or '')


def collect_storefront_translation_texts(limit=800):
    texts = []
    seen = set()

    def add_text(value):
        value = str(value or '').strip()
        if not value or value in seen:
            return
        seen.add(value)
        texts.append(value)

    try:
        for item in get_key.find({}, {'projectname': 1}).limit(limit):
            add_text(item.get('projectname'))
        for item in fenlei.find({}, {'projectname': 1}).limit(limit):
            add_text(item.get('projectname'))
        for item in ejfl.find({}, {'projectname': 1}).limit(limit):
            add_text(item.get('projectname'))
    except Exception:
        logging.warning('Failed to collect storefront translation texts', exc_info=True)

    return texts


def warm_storefront_translation_cache(user_id=None, lang=None, wait=False, force=False):
    lang = normalize_lang_code(lang or (get_user_lang(user_id) if user_id is not None else DEFAULT_LANG))
    if lang != 'en':
        return

    job_key = f'storefront:{lang}'
    if not force and job_key in _translation_warm_done:
        return

    existing_event = _translation_warm_jobs.get(job_key)
    if existing_event is not None:
        if wait:
            existing_event.wait()
        return

    done_event = threading.Event()
    _translation_warm_jobs[job_key] = done_event

    def worker():
        try:
            for text in collect_storefront_translation_texts():
                try:
                    translate_text(text, lang)
                except Exception:
                    logging.warning('Warm translation failed for text=%r', text, exc_info=True)
            _translation_warm_done.add(job_key)
        finally:
            done_event.set()
            _translation_warm_jobs.pop(job_key, None)

    if wait:
        worker()
        return

    threading.Thread(target=worker, daemon=True).start()


def matches_menu_text(user_id, incoming_text, source_text):
    candidates = {
        normalize_menu_text(get_button_match_text(source_text)),
        normalize_menu_text(get_button_match_text(localize_dynamic_text(source_text, user_id=user_id))),
    }
    return normalize_menu_text(get_button_match_text(incoming_text)) in {item for item in candidates if item}


def matches_ui_text(incoming_text, key):
    normalized = normalize_menu_text(get_button_match_text(incoming_text))
    candidates = {
        normalize_menu_text(get_button_match_text(get_ui_text(key, lang='zh'))),
        normalize_menu_text(get_button_match_text(get_ui_text(key, lang='en'))),
    }
    return normalized in {item for item in candidates if item}


def get_fixed_frontend_text_key(source_text):
    normalized = normalize_menu_text(get_button_match_text(str(source_text or '')))
    mapping = {
        normalize_menu_text('🛒商品列表'): 'menu_goods_list',
        normalize_menu_text('👤个人中心'): 'menu_profile',
        normalize_menu_text('💸我要充值'): 'menu_recharge',
        normalize_menu_text('🧧红包'): 'menu_redpacket',
        normalize_menu_text('🏠主菜单'): 'main_menu',
        normalize_menu_text('⬅️返回'): 'back',
        normalize_menu_text('❌关闭'): 'close_with_icon',
        normalize_menu_text('关闭'): 'close',
        normalize_menu_text('✅购买'): 'buy_now',
        normalize_menu_text('⚠️暂无库存'): 'out_of_stock_button',
        normalize_menu_text('🛒购买记录'): 'purchase_history_button',
        normalize_menu_text('一键克隆同款'): 'menu_clone_same',
        normalize_menu_text('一键克隆Bot'): 'menu_clone_same',
    }
    return mapping.get(normalized)


def strip_button_label_decoration(text):
    text = str(text or '')
    _, text = parse_button_style_prefix(text)
    emoji_id, alt, emoji_style, rest = parse_dynamic_emoji_prefix(text)
    if emoji_id:
        return str(rest or '').strip()
    _, emoji_text, clean_text = extract_known_button_icon(text)
    if emoji_text:
        return str(clean_text or '').strip()
    return str(text or '').strip()


def cache_localized_button_result(cache_key, original_text, result, lang, fixed_key=None):
    if lang == 'en' and not fixed_key:
        normalized_original = normalize_menu_text(get_button_match_text(original_text))
        normalized_result = normalize_menu_text(get_button_match_text(result))
        if normalized_original and normalized_original == normalized_result:
            return result
    _localized_button_cache[cache_key] = result
    return result


def localize_button_body_text(text, user_id=None, lang=None):
    lang = normalize_lang_code(lang or (get_user_lang(user_id) if user_id is not None else DEFAULT_LANG))
    source_text = str(text or '')
    if lang != 'en' or not source_text:
        return source_text

    localized = localize_dynamic_text_fast(source_text, user_id=user_id, lang=lang)
    source_has_cjk = contains_cjk(source_text)
    if not source_has_cjk:
        return localized

    normalized_source = normalize_menu_text(get_button_match_text(source_text))
    normalized_localized = normalize_menu_text(get_button_match_text(localized))
    if contains_cjk(localized) or (normalized_source and normalized_source == normalized_localized):
        localized = localize_dynamic_text(source_text, user_id=user_id, lang=lang)
    return localized


def localize_button_label(source_text, user_id=None, lang=None):
    lang = normalize_lang_code(lang or (get_user_lang(user_id) if user_id is not None else DEFAULT_LANG))
    fixed_key = get_fixed_frontend_text_key(source_text)
    original_text = str(source_text or '')
    if lang == 'zh':
        return original_text

    cache_key = (lang, original_text)
    cached = _localized_button_cache.get(cache_key)
    if cached:
        return cached

    style_prefix = ''
    body_text = original_text
    style, stripped_body = parse_button_style_prefix(original_text)
    if style:
        style_prefix = next((prefix for prefix, mapped in BUTTON_STYLE_PREFIX_MAP.items() if mapped == style), '') + ' '
        body_text = stripped_body

    emoji_id, alt, emoji_style, rest = parse_dynamic_emoji_prefix(body_text)
    if emoji_id:
        translated_body = strip_button_label_decoration(get_ui_text(fixed_key, lang=lang)) if fixed_key else localize_button_body_text(rest, user_id=user_id, lang=lang)
        emoji_prefix = f'[emoji:{emoji_id}:{alt}'
        if emoji_style:
            emoji_prefix += f':{emoji_style}'
        emoji_prefix += ']'
        result = f'{style_prefix}{emoji_prefix}{translated_body}'.strip()
        return cache_localized_button_result(cache_key, original_text, result, lang, fixed_key=fixed_key)

    known_emoji_id, emoji_text, clean_text = extract_known_button_icon(body_text)
    if emoji_text:
        translated_body = strip_button_label_decoration(get_ui_text(fixed_key, lang=lang)) if fixed_key else localize_button_body_text(clean_text, user_id=user_id, lang=lang)
        if body_text.strip().startswith(emoji_text):
            result = f'{style_prefix}{emoji_text}{translated_body}'.strip()
            return cache_localized_button_result(cache_key, original_text, result, lang, fixed_key=fixed_key)
        if body_text.strip().endswith(emoji_text):
            result = f'{style_prefix}{translated_body}{emoji_text}'.strip()
            return cache_localized_button_result(cache_key, original_text, result, lang, fixed_key=fixed_key)

    if fixed_key:
        result = f'{style_prefix}{get_ui_text(fixed_key, lang=lang)}'.strip()
        return cache_localized_button_result(cache_key, original_text, result, lang, fixed_key=fixed_key)
    result = f'{style_prefix}{localize_button_body_text(body_text, user_id=user_id, lang=lang)}'.strip()
    return cache_localized_button_result(cache_key, original_text, result, lang, fixed_key=fixed_key)


def sanitize_service_name(value):
    value = re.sub(r'[^a-zA-Z0-9_.-]+', '-', str(value or '').strip()).strip('-').lower()
    return value or 'bot'


def sanitize_db_name(value):
    value = re.sub(r'[^a-zA-Z0-9_]+', '_', str(value or '').strip()).strip('_').lower()
    return value or 'botshop_clone'


def run_system_command(args, cwd=None, timeout=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or '命令执行失败')
    return result.stdout.strip()


def get_systemd_unit_state(service_unit):
    result = subprocess.run(['systemctl', 'is-active', service_unit], capture_output=True, text=True, timeout=10)
    state = (result.stdout or result.stderr or '').strip()
    return state or 'unknown'


def get_systemd_unit_logs(service_unit, lines=20):
    commands = [
        ['journalctl', '-u', service_unit, '-n', str(lines), '--no-pager'],
        ['systemctl', 'status', service_unit, '--no-pager', '--lines', str(lines)],
    ]
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except Exception:
            continue
        output = (result.stdout or result.stderr or '').strip()
        if output:
            return output[-1200:]
    return ''


def ensure_systemd_unit_active(service_unit, label='服务', wait_seconds=8):
    deadline = time.time() + max(wait_seconds, 1)
    last_state = 'unknown'
    while time.time() < deadline:
        state = get_systemd_unit_state(service_unit)
        last_state = state
        if state == 'active':
            return
        if state in ('activating', 'reloading', 'deactivating'):
            time.sleep(2)
            continue
        time.sleep(1)

    logs = get_systemd_unit_logs(service_unit)
    detail = f'当前状态：{last_state}'
    if logs:
        detail += f'\n\n最近日志：\n{logs}'
    raise RuntimeError(f'{label} 启动失败：{service_unit}\n\n{detail}')


def restart_systemd_unit(service_unit, label='服务', wait_seconds=120):
    try:
        run_system_command(['systemctl', 'restart', '--no-block', service_unit], timeout=15)
    except subprocess.TimeoutExpired:
        pass
    ensure_systemd_unit_active(service_unit, label=label, wait_seconds=wait_seconds)


def get_clone_repo_url():
    if BOT_CLONE_REPO_URL:
        return BOT_CLONE_REPO_URL
    try:
        repo_url = run_system_command(['git', 'config', '--get', 'remote.origin.url'], cwd=str(BASE_DIR))
        if repo_url:
            return repo_url
    except Exception:
        pass
    return 'https://github.com/marinlarabel717-stack/botshop.git'


def get_python_exec_path():
    return os.path.realpath(sys.executable or shutil.which('python3.10') or shutil.which('python3') or 'python3')


def get_bot_profile(bot_token):
    token = str(bot_token or '').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
        raise RuntimeError('Bot Token 格式不正确')
    response = requests.get(f'https://api.telegram.org/bot{token}/getMe', timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not payload.get('ok'):
        raise RuntimeError(payload.get('description') or 'Bot Token 无效')
    result = payload.get('result') or {}
    if not result.get('is_bot'):
        raise RuntimeError('提供的 Token 不是机器人 Token')
    return result


def render_env_lines(env_map):
    preferred_keys = [
        'BOT_TOKEN', 'ADMIN_USER_IDS',
        'MONGO_URI', 'MONGO_USER', 'MONGO_PASSWORD', 'MONGO_AUTH_DB', 'MONGO_DB_NAME', 'MONGO_CHAIN_DB_NAME',
        'OKPAY_API_URL', 'OKPAY_SHOP_ID', 'OKPAY_SHOP_TOKEN', 'OKPAY_NAME', 'OKPAY_BOT_USERNAME', 'OKPAY_CALLBACK_URL',
        'OKPAY_CALLBACK_HOST', 'OKPAY_CALLBACK_PORT',
        'SHOW_TRC20_RECHARGE_ENTRY', 'SHOW_OKPAY_RECHARGE_ENTRY',
        'ACCOUNT_CHECK_ENABLED', 'ACCOUNT_CHECK_TIMEOUT_SECONDS', 'ACCOUNT_CHECK_PROGRESS_INTERVAL_SECONDS',
        'ACCOUNT_CHECK_PROGRESS_STEP', 'ACCOUNT_CHECK_API_ID', 'ACCOUNT_CHECK_API_HASH',
        'TRONGRID_API_BASE', 'TRONGRID_API_KEY', 'TRONGRID_API_KEYS', 'TRC20_USDT_CONTRACT', 'TRONGRID_POLL_SECONDS',
        'TRONGRID_REQUEST_TIMEOUT', 'TRONGRID_MAX_PAGES', 'TRONGRID_LOOKBACK_MINUTES', 'TRONGRID_MONITOR_ADDRESSES',
        'BASE_PROTOCOL_PATH', 'BASE_ACCOUNT_BAG_PATH',
        'BOT_CLONE_ROOT', 'BOT_CLONE_REPO_URL'
    ]
    lines = []
    used = set()
    for key in preferred_keys:
        if key in env_map:
            used.add(key)
            lines.append(f'{key}={env_map.get(key, "") or ""}')
    for key in sorted(env_map.keys()):
        if key in used:
            continue
        lines.append(f'{key}={env_map.get(key, "") or ""}')
    return '\n'.join(lines) + '\n'


def get_default_storage_env_map(existing_env=None):
    existing_env = existing_env or {}
    protocol_path = str(existing_env.get('BASE_PROTOCOL_PATH') or os.getenv('BASE_PROTOCOL_PATH') or (BASE_DIR / '协议号')).strip()
    direct_path = str(existing_env.get('BASE_ACCOUNT_BAG_PATH') or os.getenv('BASE_ACCOUNT_BAG_PATH') or (BASE_DIR / '号包')).strip()
    return {
        'BASE_PROTOCOL_PATH': protocol_path,
        'BASE_ACCOUNT_BAG_PATH': direct_path,
    }


def backfill_clone_storage_env(clone_dir, clone_kind=''):
    clone_dir = Path(str(clone_dir or '').strip())
    if not clone_dir:
        return
    root_env_path = clone_dir / '.env'
    root_env = {k: v for k, v in (dotenv_values(root_env_path) or {}).items() if k} if root_env_path.exists() else {}
    root_env.update({k: v for k, v in get_default_storage_env_map(root_env).items() if v})
    root_env_path.write_text(render_env_lines(root_env), encoding='utf-8')
    if str(clone_kind or '').strip() == 'agent':
        agent_env_path = clone_dir / 'agent_service' / '.env'
        agent_env = {k: v for k, v in (dotenv_values(agent_env_path) or {}).items() if k} if agent_env_path.exists() else {}
        agent_env.update({k: v for k, v in get_default_storage_env_map(agent_env).items() if v})
        agent_env_path.parent.mkdir(parents=True, exist_ok=True)
        agent_env_path.write_text(render_env_lines(agent_env), encoding='utf-8')


def write_clone_env(clone_dir, bot_token, admin_user_id, bot_info):
    source_env_path = BASE_DIR / '.env'
    env_map = {}
    if source_env_path.exists():
        env_map.update({k: v for k, v in (dotenv_values(source_env_path) or {}).items() if k})

    bot_id = str(bot_info.get('id'))
    bot_username = str(bot_info.get('username') or f'bot{bot_id}').strip()
    db_name = sanitize_db_name(bot_username)

    env_map['BOT_TOKEN'] = bot_token
    env_map['ADMIN_USER_IDS'] = str(admin_user_id)
    env_map['MONGO_DB_NAME'] = db_name
    env_map['MONGO_CHAIN_DB_NAME'] = db_name
    env_map['OKPAY_NAME'] = ''
    env_map['OKPAY_BOT_USERNAME'] = ''
    env_map['OKPAY_SHOP_ID'] = ''
    env_map['OKPAY_SHOP_TOKEN'] = ''
    env_map['OKPAY_CALLBACK_URL'] = ''
    env_map['TRONGRID_MONITOR_ADDRESSES'] = ''
    env_map['SHOW_OKPAY_RECHARGE_ENTRY'] = 'false'
    env_map['BOT_CLONE_ENABLED'] = 'false'
    env_map['ALLOW_PUBLIC_BOT_CLONE'] = 'false'
    env_map['BOT_CLONE_ROOT'] = BOT_CLONE_ROOT
    env_map['BOT_CLONE_REPO_URL'] = get_clone_repo_url()
    env_map.update({k: v for k, v in get_default_storage_env_map(env_map).items() if v})

    (clone_dir / '.env').write_text(render_env_lines(env_map), encoding='utf-8')
    return db_name


def write_agent_clone_env(clone_dir, admin_user_id, bot_info, bot_token):
    source_env_path = BASE_DIR / '.env'
    env_map = {}
    if source_env_path.exists():
        env_map.update({k: v for k, v in (dotenv_values(source_env_path) or {}).items() if k})

    bot_id = str(bot_info.get('id'))
    bot_username = str(bot_info.get('username') or f'bot{bot_id}').strip().lstrip('@')
    agent_name = str(bot_info.get('first_name') or bot_username or f'agent{bot_id}').strip()
    agent_trc20_address = get_trc20_address()

    env_map['BOT_CLONE_ENABLED'] = 'false'
    env_map['ALLOW_PUBLIC_BOT_CLONE'] = 'false'
    env_map['BOT_CLONE_ROOT'] = BOT_CLONE_ROOT
    env_map['BOT_CLONE_REPO_URL'] = get_clone_repo_url()
    env_map.update({k: v for k, v in get_default_storage_env_map(env_map).items() if v})
    (clone_dir / '.env').write_text(render_env_lines(env_map), encoding='utf-8')

    customer_service = str(OKPAY_BOT_USERNAME or os.getenv('CUSTOMER_SERVICE_USERNAME', '') or '').strip()
    agent_env = {
        'AGENT_BOT_ID': bot_id,
        'AGENT_BOT_TOKEN': str(bot_token or '').strip(),
        'AGENT_NAME': agent_name,
        'AGENT_USERNAME': bot_username,
        'AGENT_CUSTOMER_SERVICE': customer_service,
        'AGENT_ADMIN_IDS': str(admin_user_id),
        'AGENT_TRC20_ADDRESS': agent_trc20_address,
        'AGENT_RECHARGE_AMOUNTS': '10,30,50,100',
        'AGENT_DEFAULT_LANG': 'zh',
        **get_default_storage_env_map(env_map),
    }
    agent_env_dir = clone_dir / 'agent_service'
    agent_env_dir.mkdir(parents=True, exist_ok=True)
    (agent_env_dir / '.env').write_text(render_env_lines(agent_env), encoding='utf-8')
    return sanitize_db_name(f'agent_{bot_username}')


def build_clone_service_content(description, working_directory, exec_start):
    return f'''[Unit]
Description={description}
After=network.target

[Service]
Type=simple
WorkingDirectory={working_directory}
ExecStart={exec_start}
Restart=always
RestartSec=3
TimeoutStopSec=15
KillSignal=SIGKILL
User=root
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
'''


def sync_clone_repo_code(clone_dir):
    clone_dir = Path(str(clone_dir or '').strip())
    if not clone_dir or not clone_dir.exists() or not (clone_dir / '.git').exists():
        return
    run_system_command(['git', 'pull', '--ff-only'], cwd=str(clone_dir), timeout=60)


def refresh_clone_service_files(record):
    clone_dir = Path(str(record.get('clone_dir') or '').strip())
    if not clone_dir:
        return
    clone_kind = str(record.get('clone_kind') or '').strip()
    backfill_clone_storage_env(clone_dir, clone_kind=clone_kind)
    python_exec = get_python_exec_path()
    bot_username = str(record.get('bot_username') or record.get('bot_id') or 'bot').strip()
    service_name = str(record.get('service_name') or '').strip()
    listener_service_name = str(record.get('listener_service_name') or '').strip()
    if service_name:
        service_path = Path('/etc/systemd/system') / f'{service_name}.service'
        exec_start = f'{python_exec} {clone_dir / "haopubot.py"}'
        description = f'botshop cloned telegram bot {bot_username}'
        if clone_kind == 'agent':
            exec_start = f'{python_exec} {clone_dir / "agent_service" / "service.py"}'
            description = f'botshop agent service {bot_username}'
        service_path.write_text(
            build_clone_service_content(
                description,
                str(clone_dir),
                exec_start
            ),
            encoding='utf-8'
        )
    if listener_service_name and clone_kind != 'agent':
        listener_service_path = Path('/etc/systemd/system') / f'{listener_service_name}.service'
        listener_service_path.write_text(
            build_clone_service_content(
                f'botshop cloned TRC20 listener {bot_username}',
                str(clone_dir),
                f'{python_exec} {clone_dir / "trc20_listener.py"}'
            ),
            encoding='utf-8'
        )
    run_system_command(['systemctl', 'daemon-reload'], timeout=20)


def clone_bot_instance(bot_token, admin_user_id, source_bot_id=None):
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        raise RuntimeError('当前进程不是 root，无法自动安装 systemd 服务')

    bot_info = get_bot_profile(bot_token)
    bot_id = str(bot_info.get('id'))
    if source_bot_id is not None and str(source_bot_id) == bot_id:
        raise RuntimeError('不能克隆当前源机器人本体，请发送一个新的 Bot Token')
    bot_username = str(bot_info.get('username') or f'bot{bot_id}')
    slug = sanitize_service_name(f'{bot_username}-{bot_id}')
    clone_root = Path(BOT_CLONE_ROOT)
    clone_root.mkdir(parents=True, exist_ok=True)
    clone_dir = clone_root / slug
    repo_url = get_clone_repo_url()

    if not clone_dir.exists():
        run_system_command(['git', 'clone', '--depth', '1', repo_url, str(clone_dir)])

    db_name = write_clone_env(clone_dir, bot_token.strip(), admin_user_id, bot_info)
    python_exec = get_python_exec_path()
    service_name = f'botshop-clone-{bot_id}'
    listener_service_name = f'botshop-clone-{bot_id}-trc20'
    service_path = Path('/etc/systemd/system') / f'{service_name}.service'
    listener_service_path = Path('/etc/systemd/system') / f'{listener_service_name}.service'

    service_path.write_text(
        build_clone_service_content(
            f'botshop cloned telegram bot {bot_username}',
            str(clone_dir),
            f'{python_exec} {clone_dir / "haopubot.py"}'
        ),
        encoding='utf-8'
    )
    listener_service_path.write_text(
        build_clone_service_content(
            f'botshop cloned TRC20 listener {bot_username}',
            str(clone_dir),
            f'{python_exec} {clone_dir / "trc20_listener.py"}'
        ),
        encoding='utf-8'
    )

    run_system_command(['systemctl', 'daemon-reload'])
    run_system_command(['systemctl', 'enable', '--now', f'{service_name}.service'])
    run_system_command(['systemctl', 'enable', '--now', f'{listener_service_name}.service'])
    ensure_systemd_unit_active(f'{service_name}.service', label='克隆 Bot 服务', wait_seconds=10)

    return {
        'bot_id': bot_id,
        'bot_username': bot_username,
        'clone_dir': str(clone_dir),
        'db_name': db_name,
        'service_name': service_name,
        'listener_service_name': listener_service_name,
    }


def clone_agent_instance(bot_token, admin_user_id, source_bot_id=None):
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        raise RuntimeError('当前进程不是 root，无法自动安装 systemd 服务')

    bot_info = get_bot_profile(bot_token)
    bot_id = str(bot_info.get('id'))
    if source_bot_id is not None and str(source_bot_id) == bot_id:
        raise RuntimeError('不能把当前源机器人本体直接当成代理机器人')
    bot_username = str(bot_info.get('username') or f'bot{bot_id}').strip()
    agent_trc20_address = get_trc20_address()
    slug = sanitize_service_name(f'agent-{bot_username}-{bot_id}')
    clone_root = Path(BOT_CLONE_ROOT)
    clone_root.mkdir(parents=True, exist_ok=True)
    clone_dir = clone_root / slug
    repo_url = get_clone_repo_url()

    if not clone_dir.exists():
        run_system_command(['git', 'clone', '--depth', '1', repo_url, str(clone_dir)])

    db_name = write_agent_clone_env(clone_dir, admin_user_id, bot_info, bot_token.strip())
    python_exec = get_python_exec_path()
    service_name = f'botshop-agent-{bot_id}'
    service_path = Path('/etc/systemd/system') / f'{service_name}.service'
    service_path.write_text(
        build_clone_service_content(
            f'botshop agent service {bot_username}',
            str(clone_dir),
            f'{python_exec} {clone_dir / "agent_service" / "service.py"}'
        ),
        encoding='utf-8'
    )
    run_system_command(['systemctl', 'daemon-reload'])
    run_system_command(['systemctl', 'enable', '--now', f'{service_name}.service'])
    ensure_systemd_unit_active(f'{service_name}.service', label='代理 Bot 服务', wait_seconds=15)

    agent_bots.update_one(
        {'agent_bot_id': bot_id},
        {'$set': {
            'agent_bot_id': bot_id,
            'tenant_id': bot_id,
            'bot_username': bot_username,
            'bot_name': str(bot_info.get('first_name') or bot_username),
            'admin_ids': [int(admin_user_id)],
            'service_name': service_name,
            'clone_dir': str(clone_dir),
            'state': 'active',
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
            'customer_service': str(OKPAY_BOT_USERNAME or os.getenv('CUSTOMER_SERVICE_USERNAME', '') or '').strip(),
            'trc20_address': agent_trc20_address,
        }},
        upsert=True
    )
    return {
        'bot_id': bot_id,
        'bot_username': bot_username,
        'clone_dir': str(clone_dir),
        'db_name': db_name,
        'service_name': service_name,
        'listener_service_name': '',
        'clone_kind': 'agent',
    }


OKPAY_API_URL = os.getenv('OKPAY_API_URL', 'https://api.okaypay.me/shop/')
OKPAY_SHOP_ID = os.getenv('OKPAY_SHOP_ID', '')
OKPAY_SHOP_TOKEN = os.getenv('OKPAY_SHOP_TOKEN', '')
OKPAY_NAME = os.getenv('OKPAY_NAME', '号铺')
OKPAY_BOT_USERNAME = os.getenv('OKPAY_BOT_USERNAME', '')
OKPAY_CALLBACK_URL = os.getenv('OKPAY_CALLBACK_URL', '')
OKPAY_CALLBACK_HOST = os.getenv('OKPAY_CALLBACK_HOST', '0.0.0.0')
OKPAY_CALLBACK_PORT = int(os.getenv('OKPAY_CALLBACK_PORT', '8088'))
SHOW_TRC20_RECHARGE_ENTRY = parse_env_bool(os.getenv('SHOW_TRC20_RECHARGE_ENTRY', 'true'))
SHOW_OKPAY_RECHARGE_ENTRY = parse_env_bool(os.getenv('SHOW_OKPAY_RECHARGE_ENTRY', 'true'))
BOT_CLONE_ENABLED = parse_env_bool(os.getenv('BOT_CLONE_ENABLED', 'true'))
ALLOW_PUBLIC_BOT_CLONE = parse_env_bool(os.getenv('ALLOW_PUBLIC_BOT_CLONE', 'true'))
BOT_CLONE_ROOT = os.getenv('BOT_CLONE_ROOT', '/www/wwwroot/botshop-clones').strip() or '/www/wwwroot/botshop-clones'
BOT_CLONE_REPO_URL = os.getenv('BOT_CLONE_REPO_URL', '').strip()
ACCOUNT_CHECK_ENABLED = parse_env_bool(os.getenv('ACCOUNT_CHECK_ENABLED', 'true'))
ACCOUNT_CHECK_TIMEOUT_SECONDS = max(5, int(os.getenv('ACCOUNT_CHECK_TIMEOUT_SECONDS', '25') or '25'))
ACCOUNT_CHECK_MAX_RETRIES = 1
ACCOUNT_CHECK_PROGRESS_INTERVAL_SECONDS = max(3, int(os.getenv('ACCOUNT_CHECK_PROGRESS_INTERVAL_SECONDS', '10') or '10'))
ACCOUNT_CHECK_PROGRESS_STEP = max(1, int(os.getenv('ACCOUNT_CHECK_PROGRESS_STEP', '3') or '3'))
ACCOUNT_CHECK_PROGRESS_HEARTBEAT_SECONDS = 2.0
ACCOUNT_CHECK_SUPPORTED_TYPES = {'协议号', '直登号'}
ACCOUNT_BAN_ROOT = BASE_DIR / 'ban'
TRC20_USDT_CONTRACT = os.getenv('TRC20_USDT_CONTRACT', 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t').strip()
OKPAY_BOT = None
OKPAY_HTTPD = None
APP_EVENT_LOOP = None
TOPUP_LOOP_STARTED = False


def ensure_topup_indexes():
    try:
        topup.create_index(
            [('type', 1), ('to_address', 1), ('pay_amount_text', 1), ('state', 1)],
            name='uniq_active_trc20_amount',
            unique=True,
            partialFilterExpression={'type': 'trc20', 'state': 0}
        )
    except Exception:
        pass


def ensure_user_indexes():
    try:
        user.create_index([('user_id', 1)], name='uniq_user_id', unique=True)
    except Exception:
        pass
    try:
        user.create_index([('count_id', 1)], name='user_count_id')
    except Exception:
        pass
    try:
        user.create_index([('USDT', -1), ('count_id', 1)], name='user_balance_rank')
    except Exception:
        pass


ensure_topup_indexes()
ensure_user_indexes()

clone_instances = mydb['clone_instances']
restock_notices = mydb['restock_notices']
restock_requests = mydb['restock_requests']
translation_cache = mydb['fyb']
translation_overrides = mydb['fyb_override']
account_check_refunds = mydb['account_check_refunds']


def ensure_clone_indexes():
    try:
        clone_instances.create_index([('bot_id', 1)], name='uniq_clone_bot_id', unique=True)
    except Exception:
        pass
    try:
        clone_instances.create_index([('requester_user_id', 1), ('created_at', -1)], name='clone_requester_created')
    except Exception:
        pass


def ensure_restock_notice_indexes():
    try:
        restock_notices.create_index([('nowuid', 1), ('user_id', 1)], name='uniq_restock_notice', unique=True)
    except Exception:
        pass
    try:
        restock_notices.create_index([('nowuid', 1), ('created_at', -1)], name='restock_notice_nowuid_created')
    except Exception:
        pass


def ensure_restock_request_indexes():
    try:
        restock_requests.create_index(
            [('request_type', 1), ('keyword', 1), ('user_id', 1)],
            name='uniq_restock_request',
            unique=True
        )
    except Exception:
        pass


def ensure_account_check_refund_indexes():
    try:
        account_check_refunds.create_index([('order_id', 1)], name='uniq_account_check_refund_order', unique=True)
    except Exception:
        pass
    try:
        account_check_refunds.create_index([('user_id', 1), ('created_at', -1)], name='account_check_refund_user_created')
    except Exception:
        pass
    try:
        restock_requests.create_index([('created_at', -1)], name='restock_request_created')
    except Exception:
        pass


def ensure_translation_indexes():
    try:
        translation_cache.create_index([('text', 1), ('lang', 1)], name='uniq_translation_text_lang', unique=True)
    except Exception:
        pass
    try:
        translation_overrides.create_index([('text', 1), ('lang', 1)], name='uniq_translation_override_text_lang', unique=True)
    except Exception:
        pass


ensure_clone_indexes()
ensure_restock_notice_indexes()
ensure_restock_request_indexes()
ensure_account_check_refund_indexes()
ensure_translation_indexes()


DYNAMIC_EMOJI_RE = re.compile(r'\[(?:emoji|ce|custom_emoji):([0-9]+)(?::([^:\]]+))?(?::(danger|success|primary))?\]')
DYNAMIC_EMOJI_PREFIX_RE = re.compile(r'^\s*\[(?:emoji|ce|custom_emoji):([0-9]+)(?::([^:\]]+))?(?::(danger|success|primary))?\]\s*(.*)$', re.S)
KNOWN_DYNAMIC_EMOJI_IDS = OrderedDict([
    ('🔔', '5458603043203327669'),
    ('📱', '5330237710655306682'),
    ('👑', '5217822164362739968'),
    ('📊', '5231200819986047254'),
    ('👛', '4972482444025398275'),
    ('❌', '5210952531676504517'),
    ('😃', '6323075330189826977'),
    ('😀', '5080312910866024090'),
    ('😄', '6321339712430676611'),
    ('💰', '4965219701572503640'),
    ('✅', '5350486389806868244'),
    ('🛒', '5312361253610475399'),
    ('⚠️', '5447644880824181073'),
    ('⚠', '5447644880824181073'),
    ('❗️', '5274099962655816924'),
    ('❗', '5274099962655816924'),
    ('⁉️', '5219866512961062330'),
    ('⁉', '5219866512961062330'),
    ('➕', '6320823470246600333'),
    ('💸', '5424925715009118244'),
    ('💳', '5445353829304387411'),
    ('🔋', '5370715226209525171'),
    ('🪫', '5370688996844249600'),
    ('🚫', '5240241223632954241'),
    ('🏠', '5416041192905265756'),
    ('💡', '5190691070702279446'),
    ('📥', '5443127283898405358'),
    ('🔴', '5411225014148014586'),
    ('🟢', '5416081784641168838'),
    ('👤', '6321041414067068140'),
    ('💬', '5456535802429330837'),
    ('♥️', '6273982526851652490'),
    ('♥', '6273982526851652490'),
    ('🛫', '5201691993775818138'),
    ('🎉', '5193209274452425995'),
    ('🥳', '5458824569026532353'),
    ('💫', '5469744063815102906'),
    ('✈️', '5300866598276450274'),
    ('✈', '5300866598276450274'),
    ('➡️', '5416117059207572332'),
    ('➡', '5416117059207572332'),
    ('✍️', '5458382591121964689'),
    ('✍', '5458382591121964689'),
    ('🌎', '5224450179368767019'),
    ('🥇', '5440539497383087970'),
    ('🥈', '5447203607294265305'),
    ('🥉', '5453902265922376865'),
    ('🇨🇳', '5224435456220868088'),
    ('🌹', '5363938656874673963'),
    ('💎', '5427168083074628963'),
    ('🏦', '5332455502917949981'),
    ('⚙️', '5341715473882955310'),
    ('⚙', '5341715473882955310'),
    ('⬅️', '5253955286137338977'),
    ('⬅', '5253955286137338977'),
    ('📝', '6321175945327680619'),
])
KNOWN_DYNAMIC_EMOJI_PATTERN = re.compile('|'.join(sorted((re.escape(k) for k in KNOWN_DYNAMIC_EMOJI_IDS.keys()), key=len, reverse=True)))
PROTECTED_DYNAMIC_EMOJI_SEGMENT_RE = re.compile(r'(<tg-emoji\b[^>]*>.*?</tg-emoji>|\[(?:emoji|ce|custom_emoji):[0-9]+(?::[^:\]]+)?(?::(?:danger|success|primary))?\])', re.S)
KNOWN_DYNAMIC_EMOJI_KEYS = sorted(KNOWN_DYNAMIC_EMOJI_IDS.keys(), key=len, reverse=True)
BUTTON_STYLE_PREFIX_MAP = {
    '#r': 'danger',
    '#g': 'success',
    '#b': 'primary',
}


def parse_button_style_prefix(text):
    if not isinstance(text, str):
        return None, text
    stripped = text.strip()
    for prefix, style in BUTTON_STYLE_PREFIX_MAP.items():
        if stripped.lower().startswith(prefix):
            return style, stripped[len(prefix):].strip()
    return None, text


def extract_known_button_icon(text):
    if not isinstance(text, str):
        return None, None, text
    stripped = text.strip()
    if not stripped:
        return None, None, text

    for emoji_text in KNOWN_DYNAMIC_EMOJI_KEYS:
        if stripped.startswith(emoji_text):
            clean_text = stripped[len(emoji_text):].strip()
            return KNOWN_DYNAMIC_EMOJI_IDS[emoji_text], emoji_text, clean_text or stripped
        if stripped.endswith(emoji_text):
            clean_text = stripped[:-len(emoji_text)].strip()
            return KNOWN_DYNAMIC_EMOJI_IDS[emoji_text], emoji_text, clean_text or stripped
    return None, None, text


def known_plain_emoji_to_dynamic_html(text):
    if not isinstance(text, str) or not text:
        return text
    if not KNOWN_DYNAMIC_EMOJI_PATTERN.search(text):
        return text

    def repl(m):
        emoji_text = m.group(0)
        emoji_id = KNOWN_DYNAMIC_EMOJI_IDS.get(emoji_text)
        if not emoji_id:
            return emoji_text
        return f'<tg-emoji emoji-id="{emoji_id}">{emoji_text}</tg-emoji>'

    parts = []
    last = 0
    for m in PROTECTED_DYNAMIC_EMOJI_SEGMENT_RE.finditer(text):
        if m.start() > last:
            parts.append(KNOWN_DYNAMIC_EMOJI_PATTERN.sub(repl, text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(KNOWN_DYNAMIC_EMOJI_PATTERN.sub(repl, text[last:]))
    return ''.join(parts)


def parse_dynamic_emoji_prefix(text):
    """Parse button prefix: [emoji:custom_emoji_id:📱]按钮文字 or [emoji:id:📱:primary]按钮文字"""
    if not isinstance(text, str):
        return None, None, None, text
    m = DYNAMIC_EMOJI_PREFIX_RE.match(text)
    if not m:
        return None, None, None, text
    emoji_id = m.group(1)
    alt = m.group(2) or '✨'
    style = m.group(3)
    rest = m.group(4) or ''
    return emoji_id, alt, style, rest


def dynamic_emoji_to_html(text):
    """Convert [emoji:id:alt] in message text to Telegram HTML custom emoji tags."""
    if not isinstance(text, str):
        return text

    if '[emoji:' in text or '[ce:' in text or '[custom_emoji:' in text:
        def repl(m):
            emoji_id = m.group(1)
            alt = m.group(2) or '✨'
            return f'<tg-emoji emoji-id="{emoji_id}">{alt}</tg-emoji>'

        text = DYNAMIC_EMOJI_RE.sub(repl, text)

    return known_plain_emoji_to_dynamic_html(text)


def needs_dynamic_emoji_parse(text):
    if not isinstance(text, str):
        return False
    return (
        '[emoji:' in text or '[ce:' in text or '[custom_emoji:' in text or
        bool(KNOWN_DYNAMIC_EMOJI_PATTERN.search(text))
    )


def utf16_len(text):
    if not isinstance(text, str):
        return 0
    return len(text.encode('utf-16-le')) // 2


def build_custom_emoji_text_entities(text):
    if not isinstance(text, str) or not text:
        return text or '', []
    plain_parts = []
    entities = []
    last = 0
    utf16_offset = 0
    for m in DYNAMIC_EMOJI_RE.finditer(text):
        prefix = text[last:m.start()]
        if prefix:
            plain_parts.append(prefix)
            utf16_offset += utf16_len(prefix)
        alt = m.group(2) or '?'
        custom_emoji_id = str(m.group(1) or '').strip()
        plain_parts.append(alt)
        entities.append(MessageEntity(type='custom_emoji', offset=utf16_offset, length=utf16_len(alt), custom_emoji_id=custom_emoji_id))
        utf16_offset += utf16_len(alt)
        last = m.end()
    tail = text[last:]
    if tail:
        plain_parts.append(tail)
    return ''.join(plain_parts), entities


def get_entity_custom_emoji_id(entity):
    custom_emoji_id = getattr(entity, 'custom_emoji_id', None)
    if custom_emoji_id:
        return str(custom_emoji_id)
    api_kwargs = getattr(entity, 'api_kwargs', None) or {}
    if isinstance(api_kwargs, dict) and api_kwargs.get('custom_emoji_id'):
        return str(api_kwargs['custom_emoji_id'])
    return None


def extract_custom_emoji_from_message(message):
    if not message:
        return None, None
    for entity_attr, text_attr in (('entities', 'text'), ('caption_entities', 'caption')):
        entities = getattr(message, entity_attr, None) or []
        source_text = getattr(message, text_attr, None) or ''
        for entity in entities:
            custom_emoji_id = get_entity_custom_emoji_id(entity)
            if getattr(entity, 'type', None) == 'custom_emoji' or custom_emoji_id:
                alt = ''
                try:
                    alt = source_text[entity.offset:entity.offset + entity.length]
                except Exception:
                    alt = ''
                return custom_emoji_id, (alt or '✨')
    return None, None


def utf16_index_to_py_index(text, utf16_index):
    if not isinstance(text, str):
        return utf16_index
    if utf16_index <= 0:
        return 0
    current_utf16 = 0
    for py_index, char in enumerate(text):
        if current_utf16 >= utf16_index:
            return py_index
        current_utf16 += len(char.encode('utf-16-le')) // 2
        if current_utf16 >= utf16_index:
            return py_index + 1
    return len(text)


def strip_custom_emoji_entities(source_text, entities):
    if not isinstance(source_text, str) or not entities:
        return source_text
    cut_ranges = []
    for entity in entities:
        custom_emoji_id = get_entity_custom_emoji_id(entity)
        if getattr(entity, 'type', None) == 'custom_emoji' or custom_emoji_id:
            start = getattr(entity, 'offset', None)
            length = getattr(entity, 'length', None)
            if isinstance(start, int) and isinstance(length, int):
                py_start = utf16_index_to_py_index(source_text, start)
                py_end = utf16_index_to_py_index(source_text, start + length)
                cut_ranges.append((py_start, py_end))
    if not cut_ranges:
        return source_text

    parts = []
    last = 0
    for start, end in sorted(cut_ranges):
        if start > last:
            parts.append(source_text[last:start])
        last = max(last, end)
    parts.append(source_text[last:])
    return ''.join(parts)


def get_button_match_text(text):
    _, text = parse_button_style_prefix(text)
    emoji_id, _, _, clean_text = parse_dynamic_emoji_prefix(text)
    return clean_text if emoji_id else text


def get_message_match_text(message):
    if not message:
        return ''
    text = getattr(message, 'text', None) or ''
    if not text:
        return ''
    text = strip_custom_emoji_entities(text, getattr(message, 'entities', None) or [])
    return get_button_match_text(text).strip()


def get_message_storage_text(message):
    if not message:
        return ''
    source_text = getattr(message, 'text', None) or ''
    entities = getattr(message, 'entities', None) or []
    return build_storage_text_from_entities(source_text, entities)


def build_storage_text_from_entities(source_text, entities):
    if not source_text or not entities:
        return source_text

    parts = []
    last = 0
    custom_entities = []
    for entity in entities:
        custom_emoji_id = get_entity_custom_emoji_id(entity)
        if getattr(entity, 'type', None) == 'custom_emoji' or custom_emoji_id:
            start = getattr(entity, 'offset', None)
            length = getattr(entity, 'length', None)
            if isinstance(start, int) and isinstance(length, int) and custom_emoji_id:
                py_start = utf16_index_to_py_index(source_text, start)
                py_end = utf16_index_to_py_index(source_text, start + length)
                alt = source_text[py_start:py_end] or '✨'
                custom_entities.append((py_start, py_end, custom_emoji_id, alt))

    if not custom_entities:
        return source_text

    for py_start, py_end, custom_emoji_id, alt in sorted(custom_entities):
        if py_start > last:
            parts.append(source_text[last:py_start])
        parts.append(f'[emoji:{custom_emoji_id}:{alt}]')
        last = max(last, py_end)
    parts.append(source_text[last:])
    return ''.join(parts)


def send_key_content_preview(context, chat_id, text='', file_type='text', file_id='', entities=None, keyboard=None):
    entities = entities or []
    keyboard = keyboard or []
    if needs_dynamic_emoji_parse(text):
        entities = []
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    if file_type == 'photo':
        return context.bot.send_photo(chat_id=chat_id, caption=text or '', photo=file_id,
                                      reply_markup=reply_markup, caption_entities=entities)
    if file_type == 'animation':
        return context.bot.sendAnimation(chat_id=chat_id, caption=text or '', animation=file_id,
                                         reply_markup=reply_markup, caption_entities=entities)
    if file_type == 'video':
        return context.bot.sendVideo(chat_id=chat_id, caption=text or '', video=file_id,
                                     reply_markup=reply_markup, caption_entities=entities)
    return context.bot.send_message(chat_id=chat_id, text=text or '', reply_markup=reply_markup,
                                    entities=entities)


def has_custom_emoji_entities(entities):
    for entity in entities or []:
        if getattr(entity, 'type', None) == 'custom_emoji' or get_entity_custom_emoji_id(entity):
            return True
    return False


def send_key_save_success_notice(context, chat_id):
    return context.bot.send_message(
        chat_id=chat_id,
        text='[emoji:5312028599803460968:🆗] 图文设置已保存\n\n[emoji:5217818964612108191:✨] 当前回复内容如下：',
        parse_mode='HTML'
    )


def _prune_telegram_transient_events(now):
    global _telegram_transient_events
    _telegram_transient_events = [ts for ts in _telegram_transient_events if now - ts <= TELEGRAM_TRANSIENT_WINDOW_SECONDS]


def note_telegram_transient_error(label, exc):
    global _telegram_transient_cooldown_until
    now = time.monotonic()
    open_notice = None
    should_log_error = False

    with _telegram_transient_lock:
        _prune_telegram_transient_events(now)
        _telegram_transient_events.append(now)
        error_count = len(_telegram_transient_events)

        if error_count >= TELEGRAM_TRANSIENT_OPEN_THRESHOLD and now >= _telegram_transient_cooldown_until:
            _telegram_transient_cooldown_until = now + TELEGRAM_TRANSIENT_COOLDOWN_SECONDS
            open_notice = (error_count, TELEGRAM_TRANSIENT_WINDOW_SECONDS, TELEGRAM_TRANSIENT_COOLDOWN_SECONDS)

        last_log_at = _telegram_transient_last_log_at.get(label, 0.0)
        if now - last_log_at >= TELEGRAM_TRANSIENT_LOG_SUPPRESS_SECONDS:
            _telegram_transient_last_log_at[label] = now
            should_log_error = True

    if open_notice:
        count, window_seconds, cooldown_seconds = open_notice
        logging.warning(
            'Telegram transient circuit opened after %s errors in %ss; optional sends/deletes will cool down for %ss',
            count,
            window_seconds,
            cooldown_seconds,
        )

    if should_log_error:
        logging.warning('Telegram transient error on %s: %s', label, exc)


def should_skip_optional_telegram_action(label):
    now = time.monotonic()
    should_log_skip = False
    remaining_seconds = 0
    with _telegram_transient_lock:
        if now >= _telegram_transient_cooldown_until:
            return False
        remaining_seconds = max(1, int(_telegram_transient_cooldown_until - now))
        last_log_at = _telegram_transient_last_log_at.get(f'{label}:skip', 0.0)
        if now - last_log_at >= TELEGRAM_TRANSIENT_LOG_SUPPRESS_SECONDS:
            _telegram_transient_last_log_at[f'{label}:skip'] = now
            should_log_skip = True

    if should_log_skip:
        logging.warning('Telegram transient circuit active, skip optional %s for %ss', label, remaining_seconds)
    return True


def safe_send_message(context, chat_id, text='', **kwargs):
    if should_skip_optional_telegram_action('send_message'):
        return None
    try:
        return context.bot.send_message(chat_id=chat_id, text=text or '', **kwargs)
    except (TimedOut, NetworkError) as exc:
        note_telegram_transient_error('send_message', exc)
        return None
    except BadRequest as exc:
        if 'message is not modified' in str(exc).lower():
            return None
        raise


def safe_delete_message(bot, chat_id, message_id, log_label='delete_message'):
    if not chat_id or not message_id:
        return False
    if should_skip_optional_telegram_action(log_label):
        return False
    try:
        bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except (TimedOut, NetworkError) as exc:
        note_telegram_transient_error(log_label, exc)
        return False
    except BadRequest as exc:
        exc_text = str(exc).lower()
        if (
            'message to delete not found' in exc_text
            or "message can't be deleted" in exc_text
            or 'message can\'t be deleted' in exc_text
            or 'message identifier is not specified' in exc_text
            or 'message id invalid' in exc_text
        ):
            return False
        raise
    except Forbidden:
        return False


def should_preserve_sign_on_menu_match(sign):
    if not sign:
        return False
    sign = str(sign)
    editable_prefixes = (
        'startupdate',
        'upejflname ',
        'upspname ',
        'setkeyname ',
        'settuwenset ',
        'setkeyboard ',
        'setrestocktarget',
        'setbuynotice',
        'update_sysm ',
        'update_wbts ',
        'settrc20',
    )
    return sign.startswith(editable_prefixes)


def emojiid(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if chat.type != 'private':
        return
    message = update.effective_message
    args = context.args or []

    reply_custom_id, reply_alt = extract_custom_emoji_from_message(message.reply_to_message)
    own_custom_id, own_alt = extract_custom_emoji_from_message(message)

    custom_emoji_id = reply_custom_id or own_custom_id

    if reply_custom_id:
        alt = args[0] if args else (reply_alt or '✨')
        label = ' '.join(args[1:]).strip() if len(args) > 1 else ''
    else:
        alt = args[0] if args else (own_alt or '✨')
        label = ' '.join(args[1:]).strip() if len(args) > 1 else ''

    if not custom_emoji_id:
        fstext = (
            '用法：\n'
            '1. 直接发送：/emojiid 自定义emoji 商品列表\n'
            '2. 或先发一个自定义 emoji，再回复那条消息发送：/emojiid 💬 商品列表\n'
            '注意：这里必须是 Telegram 自定义 emoji，不是普通系统 emoji。'
        )
        context.bot.send_message(chat_id=chat.id, text=fstext)
        return

    result = f'[emoji:{custom_emoji_id}:{alt}]'
    if label:
        result += label
    context.bot.send_message(chat_id=chat.id, text=result)


def InlineKeyboardButton(text, *args, **kwargs):
    """Backward-compatible inline button with optional dynamic emoji icon.

    Usage: InlineKeyboardButton('[emoji:5368324170671202286:📱]商品列表', callback_data='...')
    """
    _, styled_text = parse_button_style_prefix(text)
    emoji_id, alt, style, clean_text = parse_dynamic_emoji_prefix(styled_text)
    if not emoji_id:
        emoji_id, alt, clean_text = extract_known_button_icon(styled_text)
    if emoji_id:
        try:
            api_kwargs = dict(kwargs.pop('api_kwargs', {}) or {})
            api_kwargs['icon_custom_emoji_id'] = emoji_id
            if style:
                api_kwargs['style'] = style
            return TGInlineKeyboardButton(clean_text, *args, api_kwargs=api_kwargs, **kwargs)
        except TypeError:
            return TGInlineKeyboardButton(f'{alt}{clean_text}', *args, **kwargs)
    return TGInlineKeyboardButton(styled_text, *args, **kwargs)


def KeyboardButton(text, *args, **kwargs):
    """Backward-compatible reply keyboard button with optional dynamic emoji icon."""
    style_prefix, styled_text = parse_button_style_prefix(text)
    emoji_id, alt, style, clean_text = parse_dynamic_emoji_prefix(styled_text)
    if not emoji_id:
        emoji_id, alt, clean_text = extract_known_button_icon(styled_text)
    final_style = style or style_prefix
    if emoji_id:
        try:
            api_kwargs = dict(kwargs.pop('api_kwargs', {}) or {})
            api_kwargs['icon_custom_emoji_id'] = emoji_id
            api_kwargs['style'] = final_style or 'primary'
            return TGKeyboardButton(clean_text, *args, api_kwargs=api_kwargs, **kwargs)
        except TypeError:
            return TGKeyboardButton(f'{alt}{clean_text}', *args, **kwargs)
    if final_style:
        try:
            api_kwargs = dict(kwargs.pop('api_kwargs', {}) or {})
            api_kwargs['style'] = final_style
            return TGKeyboardButton(styled_text, *args, api_kwargs=api_kwargs, **kwargs)
        except TypeError:
            return TGKeyboardButton(styled_text, *args, **kwargs)
    return TGKeyboardButton(styled_text, *args, **kwargs)


def patch_bot_dynamic_emoji(bot):
    """Patch common bot send/edit methods to understand [emoji:id:alt] in text/caption."""
    if getattr(bot, '_dynamic_emoji_patched', False):
        return

    def wrap_text_method(method_name, text_key):
        original = getattr(bot, method_name)

        def wrapped(*args, **kwargs):
            # Positional text/caption support for common Bot methods.
            args = list(args)
            value = kwargs.get(text_key)
            entity_key = 'entities' if text_key == 'text' else 'caption_entities'
            if kwargs.get(entity_key) and not kwargs.get('parse_mode'):
                return original(*args, **kwargs)
            arg_index = 1 if method_name.startswith('send_') else None
            if value is None and arg_index is not None and len(args) > arg_index:
                value = args[arg_index]
                if needs_dynamic_emoji_parse(value):
                    args[arg_index] = dynamic_emoji_to_html(value)
            elif needs_dynamic_emoji_parse(value):
                kwargs[text_key] = dynamic_emoji_to_html(value)

            if needs_dynamic_emoji_parse(value):
                if not kwargs.get('parse_mode') and not kwargs.get(entity_key):
                    kwargs['parse_mode'] = 'HTML'
            return original(*args, **kwargs)

        setattr(bot, method_name, wrapped)

    for name, key in [
        ('send_message', 'text'),
        ('edit_message_text', 'text'),
        ('send_photo', 'caption'),
        ('send_animation', 'caption'),
        ('send_video', 'caption'),
        ('sendAnimation', 'caption'),
        ('sendVideo', 'caption'),
        ('edit_message_caption', 'caption')
    ]:
        if hasattr(bot, name):
            wrap_text_method(name, key)
    bot._dynamic_emoji_patched = True


class SyncTelegramProxy:
    METHOD_ALIASES = {
        'sendAnimation': 'send_animation',
        'sendVideo': 'send_video',
        'sendPhoto': 'send_photo',
        'editMessageText': 'edit_message_text',
        'editMessageCaption': 'edit_message_caption',
        'deleteMessage': 'delete_message',
        'download': 'download_to_drive',
    }
    TEXT_METHOD_KEYS = {
        'send_message': 'text',
        'edit_message_text': 'text',
        'reply_text': 'text',
        'reply_html': 'text',
        'send_photo': 'caption',
        'reply_photo': 'caption',
        'send_animation': 'caption',
        'reply_animation': 'caption',
        'send_video': 'caption',
        'reply_video': 'caption',
        'edit_message_caption': 'caption',
    }
    TEXT_METHOD_ARG_INDEX = {
        'send_message': 1,
        'edit_message_text': 0,
        'reply_text': 0,
        'reply_html': 0,
        'send_photo': 1,
        'reply_photo': 0,
        'send_animation': 1,
        'reply_animation': 0,
        'send_video': 1,
        'reply_video': 0,
        'edit_message_caption': 0,
    }

    def __init__(self, obj, loop_ref):
        self._obj = obj
        self._loop_ref = loop_ref

    def _get_loop(self):
        loop = self._loop_ref() if callable(self._loop_ref) else self._loop_ref
        if loop is None:
            raise RuntimeError('Telegram event loop 尚未初始化')
        return loop

    def _prepare_dynamic_emoji_args(self, method_name, args, kwargs):
        text_key = self.TEXT_METHOD_KEYS.get(method_name)
        if not text_key:
            return args, kwargs

        args = list(args)
        kwargs = dict(kwargs)
        entity_key = 'entities' if text_key == 'text' else 'caption_entities'
        if kwargs.get(entity_key) and not kwargs.get('parse_mode'):
            return args, kwargs

        value = kwargs.get(text_key)
        arg_index = self.TEXT_METHOD_ARG_INDEX.get(method_name)
        if value is None and arg_index is not None and len(args) > arg_index:
            value = args[arg_index]
            if needs_dynamic_emoji_parse(value):
                args[arg_index] = dynamic_emoji_to_html(value)
        elif needs_dynamic_emoji_parse(value):
            kwargs[text_key] = dynamic_emoji_to_html(value)

        if needs_dynamic_emoji_parse(value) and not kwargs.get('parse_mode') and not kwargs.get(entity_key):
            kwargs['parse_mode'] = 'HTML'
        return args, kwargs

    def __getitem__(self, key):
        if hasattr(self._obj, '__getitem__'):
            try:
                return wrap_sync_telegram_value(self._obj[key], self._loop_ref)
            except Exception:
                pass
        return wrap_sync_telegram_value(getattr(self._obj, key), self._loop_ref)

    def get(self, key, default=None):
        try:
            return self[key]
        except Exception:
            return default

    def __contains__(self, key):
        sentinel = object()
        return self.get(key, sentinel) is not sentinel

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)

        target_name = self.METHOD_ALIASES.get(name, name)
        attr = getattr(self._obj, target_name)

        if callable(attr):
            def wrapped(*args, **kwargs):
                args, kwargs = self._prepare_dynamic_emoji_args(target_name, args, kwargs)
                transient_methods = {
                    'send_message', 'send_photo', 'send_document', 'send_animation', 'send_media_group',
                    'edit_message_text', 'edit_message_caption', 'edit_message_reply_markup',
                    'answer', 'answer_callback_query', 'delete_message'
                }
                timeout_defaults = {
                    'send_document': {'connect_timeout': 20, 'read_timeout': 120, 'write_timeout': 120, 'pool_timeout': 20},
                    'send_photo': {'connect_timeout': 20, 'read_timeout': 60, 'write_timeout': 60, 'pool_timeout': 20},
                    'send_animation': {'connect_timeout': 20, 'read_timeout': 120, 'write_timeout': 120, 'pool_timeout': 20},
                    'send_media_group': {'connect_timeout': 20, 'read_timeout': 120, 'write_timeout': 120, 'pool_timeout': 20},
                    'send_message': {'connect_timeout': 15, 'read_timeout': 45, 'write_timeout': 45, 'pool_timeout': 15},
                    'edit_message_text': {'connect_timeout': 15, 'read_timeout': 45, 'write_timeout': 45, 'pool_timeout': 15},
                    'edit_message_caption': {'connect_timeout': 15, 'read_timeout': 45, 'write_timeout': 45, 'pool_timeout': 15},
                    'edit_message_reply_markup': {'connect_timeout': 15, 'read_timeout': 45, 'write_timeout': 45, 'pool_timeout': 15},
                    'answer': {'connect_timeout': 10, 'read_timeout': 20, 'write_timeout': 20, 'pool_timeout': 10},
                    'answer_callback_query': {'connect_timeout': 10, 'read_timeout': 20, 'write_timeout': 20, 'pool_timeout': 10},
                    'delete_message': {'connect_timeout': 10, 'read_timeout': 20, 'write_timeout': 20, 'pool_timeout': 10},
                }
                last_exc = None
                max_attempts = 3 if target_name in {'send_document', 'send_photo', 'send_animation', 'send_media_group', 'answer', 'answer_callback_query', 'delete_message'} else (2 if target_name in transient_methods else 1)
                for timeout_key, timeout_value in timeout_defaults.get(target_name, {}).items():
                    kwargs.setdefault(timeout_key, timeout_value)

                def rewind_retry_streams():
                    candidates = list(args) + list(kwargs.values())
                    for candidate in candidates:
                        file_obj = getattr(candidate, 'fp', None)
                        if file_obj is not None and hasattr(file_obj, 'seek'):
                            try:
                                file_obj.seek(0)
                            except Exception:
                                pass
                        if hasattr(candidate, 'seek'):
                            try:
                                candidate.seek(0)
                            except Exception:
                                pass

                for attempt in range(max_attempts):
                    try:
                        if attempt > 0:
                            rewind_retry_streams()
                        result = attr(*args, **kwargs)
                        if inspect.isawaitable(result):
                            result = asyncio.run_coroutine_threadsafe(result, self._get_loop()).result()
                        return wrap_sync_telegram_value(result, self._loop_ref)
                    except BadRequest as exc:
                        exc_text = str(exc)
                        if target_name in ('answer', 'answer_callback_query') and (
                            'Query is too old' in exc_text or 'query id is invalid' in exc_text
                        ):
                            return None
                        if 'Message is not modified' in exc_text:
                            return None
                        raise
                    except Forbidden as exc:
                        if 'bot was blocked by the user' in str(exc):
                            return None
                        raise
                    except (TimedOut, NetworkError) as exc:
                        last_exc = exc
                        if attempt + 1 < max_attempts:
                            time.sleep(min(3, 1 + attempt))
                            continue
                        note_telegram_transient_error(target_name, exc)
                        return None
                if last_exc is not None:
                    raise last_exc

            return wrapped

        return wrap_sync_telegram_value(attr, self._loop_ref)


class SyncCallbackContextProxy:
    def __init__(self, context, loop_ref):
        self._context = context
        self._loop_ref = loop_ref
        self.bot = SyncTelegramProxy(context.bot, loop_ref)

    def __getattr__(self, name):
        return wrap_sync_telegram_value(getattr(self._context, name), self._loop_ref)


def wrap_sync_telegram_value(value, loop_ref):
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return value
    if isinstance(value, list):
        return [wrap_sync_telegram_value(item, loop_ref) for item in value]
    if isinstance(value, tuple):
        return tuple(wrap_sync_telegram_value(item, loop_ref) for item in value)
    module_name = getattr(value.__class__, '__module__', '')
    if module_name.startswith('telegram'):
        return SyncTelegramProxy(value, loop_ref)
    return value


def unwrap_sync_value(value):
    if isinstance(value, SyncTelegramProxy):
        return unwrap_sync_value(value._obj)
    if isinstance(value, list):
        return [unwrap_sync_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(unwrap_sync_value(item) for item in value)
    if isinstance(value, dict):
        return {unwrap_sync_value(k): unwrap_sync_value(v) for k, v in value.items()}
    return value


def safe_pickle_loads(value, default=None):
    raw_value = unwrap_sync_value(value)
    try:
        loaded = pickle.loads(raw_value)
        return unwrap_sync_value(loaded)
    except Exception:
        return [] if default is None else default


def sync_handler(callback):
    async def wrapped(update, context):
        loop = asyncio.get_running_loop()
        sync_update = wrap_sync_telegram_value(update, loop)
        sync_context = SyncCallbackContextProxy(context, loop)
        return await asyncio.to_thread(callback, sync_update, sync_context)

    return wrapped


def sync_job(callback):
    async def wrapped(context):
        loop = asyncio.get_running_loop()
        sync_context = SyncCallbackContextProxy(context, loop)
        return await asyncio.to_thread(callback, sync_context)

    return wrapped


async def global_error_handler(update, context):
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        note_telegram_transient_error('global_error_handler', err)
        return
    logging.exception('Unhandled bot error', exc_info=err)


def make_directory(path):
    if not os.path.exists(path):
        os.makedirs(path)
        print(f"Folder '{path}' created successfully")
    else:
        print(f"Folder '{path}' already exists")


def rename_directory(old_path, new_path):
    if os.path.exists(old_path):
        os.rename(old_path, new_path)
        print(f"Folder '{old_path}' renamed to '{new_path}'")
    else:
        print(f"Folder '{old_path}' does not exist")


TRANSFER_CLAIM_EXPIRE_SECONDS = 12 * 60 * 60


def get_transfer_expire_ts(transfer_row):
    row = transfer_row or {}
    created_ts = 0
    try:
        created_ts = int(row.get('created_ts') or 0)
    except Exception:
        created_ts = 0

    computed_expire_ts = 0
    if created_ts > 0:
        computed_expire_ts = created_ts + TRANSFER_CLAIM_EXPIRE_SECONDS
    else:
        timer_text = str(row.get('timer') or '').strip()
        if timer_text:
            try:
                computed_expire_ts = int(time.mktime(time.strptime(timer_text, '%Y-%m-%d %H:%M:%S'))) + TRANSFER_CLAIM_EXPIRE_SECONDS
            except Exception:
                computed_expire_ts = 0

    try:
        expire_ts = int(row.get('expire_ts') or 0)
    except Exception:
        expire_ts = 0

    if expire_ts > 0 and computed_expire_ts > 0:
        return max(expire_ts, computed_expire_ts)
    if expire_ts > 0:
        return expire_ts
    return computed_expire_ts


def is_transfer_expired(transfer_row, now_ts=None):
    expire_ts = get_transfer_expire_ts(transfer_row)
    if expire_ts <= 0:
        return False
    now_ts = int(now_ts or time.time())
    return now_ts >= expire_ts


def inline_query(update: Update, context: CallbackContext):
    """Handle the inline query. This is run when you type: @botusername <query>"""
    query = update.inline_query.query
    if not query:  # empty query should not be handled
        update.inline_query.answer(results=[], cache_time=0)
        return

    inline_user = update.inline_query.from_user
    user_id = inline_user.id
    username = inline_user.username
    fullname = (inline_user.full_name or '').replace('<', '').replace('>', '')
    lastname = inline_user.last_name
    user_list = ensure_user_exists(user_id, username, fullname, lastname, getattr(inline_user, 'language_code', None))
    if user_list is None:
        logging.error('inline_query failed to ensure user exists: user_id=%s username=%r', user_id, username)
        update.inline_query.answer(results=[], cache_time=0)
        return

    if is_number(query):
        money = query
        money = float(money) if str(money).count('.') > 0 else int(money)
        USDT = user_list.get('USDT', 0)
        if USDT >= money:
            if money <= 0:
                url = helpers.create_deep_linked_url(context.bot.username, str(user_id))
                keyboard = [
                    [InlineKeyboardButton(context.bot.first_name, url=url)]
                ]
                fstext = f'''
⚠️操作失败，转账金额必须大于0
                '''

                hyy, entities = get_welcome_content()

                input_message_content = (
                    InputTextMessageContent(hyy, parse_mode='HTML')
                    if welcome_uses_html_parse(hyy, entities)
                    else InputTextMessageContent(hyy, entities=entities)
                )

                results = [
                    InlineQueryResultArticle(
                        id=str(uuid.uuid4()),
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        title=fstext,
                        input_message_content=input_message_content
                    ),
                ]

                update.inline_query.answer(results=results, cache_time=0)
                return
            uid = generate_24bit_uid()
            created_ts = int(time.time())
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created_ts))
            zhuanz.insert_one({
                'uid': uid,
                'user_id': user_id,
                'fullname': fullname,
                'money': money,
                'timer': timer,
                'created_ts': created_ts,
                'expire_ts': created_ts + TRANSFER_CLAIM_EXPIRE_SECONDS,
                'state': 0
            })
            # keyboard = [[InlineKeyboardButton("📥收款", callback_data=f'shokuan {user_id}:{money}')]]
            keyboard = [[InlineKeyboardButton("📥收款", callback_data=f'shokuan {uid}')]]
            fstext = f'''
转账 {query} U
            '''

            zztext = f'''
<b>转账给你 {query} U</b>

请在12小时内领取
            '''
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    title=fstext,
                    description='⚠️您正在向对方转账U并立即生效',
                    input_message_content=InputTextMessageContent(
                        zztext, parse_mode='HTML'
                    )
                ),
            ]

            update.inline_query.answer(results=results, cache_time=0)
            return
        else:
            url = helpers.create_deep_linked_url(context.bot.username, str(user_id))
            keyboard = [
                [InlineKeyboardButton(context.bot.first_name, url=url)]
            ]
            fstext = f'''
⚠️操作失败，余额不足，💰当前余额：{format_usdt_2(USDT)}U
            '''

            hyy, entities = get_welcome_content()

            input_message_content = (
                InputTextMessageContent(hyy, parse_mode='HTML')
                if welcome_uses_html_parse(hyy, entities)
                else InputTextMessageContent(hyy, entities=entities)
            )

            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    title=fstext,
                    input_message_content=input_message_content
                ),
            ]

            update.inline_query.answer(results=results, cache_time=0)
            return
    uid = query.replace('redpacket ', '')
    hongbao_list = hongbao.find_one({'uid': uid})
    if hongbao_list is None:
        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="参数错误",
                input_message_content=InputTextMessageContent(
                    f"<b>错误</b>", parse_mode='HTML'
                )),
        ]

        update.inline_query.answer(results=results, cache_time=0)
        return
    yh_id = hongbao_list['user_id']
    if yh_id != user_id:

        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="🧧这不是你的红包",
                input_message_content=InputTextMessageContent(
                    f"<b>🧧这不是你的红包</b>", parse_mode='HTML'
                )),
        ]

        update.inline_query.answer(results=results, cache_time=0)
    else:
        hbmoney = hongbao_list['hbmoney']
        hbsl = hongbao_list['hbsl']
        state = hongbao_list['state']
        if state == 1:
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    title="🧧红包已领取完",
                    input_message_content=InputTextMessageContent(
                        f"<b>🧧红包已领取完</b>", parse_mode='HTML'
                    )),
            ]

            update.inline_query.answer(results=results, cache_time=0)
        else:
            qbrtext = []
            jiangpai = {'0': '🥇', '1': '🥈', '2': '🥉'}
            count = 0
            qb_list = list(qb.find({'uid': uid}, sort=[('money', -1)]))
            for i in qb_list:
                qbid = i['user_id']
                qbname = i['fullname'].replace('<', '').replace('>', '')
                qbtimer = i['timer'][-8:]
                qbmoney = i['money']
                safe_qbname = html.escape(qbname, quote=False)
                if str(count) in jiangpai.keys():

                    qbrtext.append(
                        f'{jiangpai[str(count)]} <code>{qbmoney}</code>({qbtimer}) USDT💰 - {safe_qbname}')
                else:
                    qbrtext.append(
                        f'<code>{qbmoney}</code>({qbtimer}) USDT💰 - {safe_qbname}')
                count += 1
            qbrtext = '\n'.join(qbrtext)

            syhb = hbsl - len(qb_list)

            safe_fullname = html.escape(fullname, quote=False)
            fstext = f'''
🧧 {safe_fullname} 发送了一个红包
💵总金额:{hbmoney} USDT💰 剩余:{syhb}/{hbsl}

{qbrtext}
            '''

            url = helpers.create_deep_linked_url(context.bot.username, str(user_id))
            keyboard = [
                [InlineKeyboardButton('领取红包', callback_data=f'lqhb {uid}')],
                [InlineKeyboardButton(context.bot.first_name, url=url)]
            ]

            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    title=f"💵总金额:{hbmoney} USDT💰 剩余:{syhb}/{hbsl}",
                    input_message_content=InputTextMessageContent(
                        fstext, parse_mode='HTML'
                    )
                ),
            ]

            update.inline_query.answer(results=results, cache_time=0)


def shokuan(update: Update, context: CallbackContext):
    query = update.callback_query
    uid = query.data.replace('shokuan ', '')
    now_ts = int(time.time())

    fb_list = zhuanz.find_one({'uid': uid})
    if fb_list is None:
        logging.warning('transfer claim target missing: uid=%s claimer=%s', uid, getattr(query.from_user, 'id', 0))
        query.answer('❌ 领取失败，转账记录不存在或已失效', show_alert=bool("true"))
        return

    fb_state = int(fb_list.get('state', 0) or 0)
    if fb_state == 1:
        query.answer('❌ 领取失败，该转账已被领取', show_alert=bool("true"))
        return
    if fb_state == 2 or is_transfer_expired(fb_list, now_ts):
        zhuanz.update_one({'uid': uid, 'state': {'$ne': 1}}, {"$set": {"state": 2, 'expired_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now_ts))}})
        query.answer('❌ 领取失败，该转账已超过12小时，已自动失效', show_alert=bool("true"))
        try:
            query.edit_message_text('❌ 该转账已超过12小时，已自动失效')
        except Exception:
            pass
        return

    claimed_row = zhuanz.find_one_and_update(
        {'uid': uid, 'state': 0},
        {'$set': {'state': 1, 'claimed_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now_ts)), 'claimed_user_id': int(query.from_user.id)}},
        return_document=ReturnDocument.AFTER,
    )
    if claimed_row is None:
        query.answer('❌ 领取失败，该转账已被领取或已失效', show_alert=bool("true"))
        return

    fb_id = claimed_row['user_id']
    fb_money = claimed_row['money']
    yh_list = user.find_one({'user_id': fb_id})
    yh_usdt = float((yh_list or {}).get('USDT', 0) or 0)
    if yh_usdt < fb_money:
        fstext = f'''
❌ 领取失败.USDT 操作失败，余额不足
        '''
        query.answer(fstext, show_alert=bool("true"))
        return

    now_money = standard_num(yh_usdt - fb_money)
    now_money = float(now_money) if str((now_money)).count('.') > 0 else int(standard_num(now_money))
    user.update_one({'user_id': fb_id}, {"$set": {'USDT': now_money}})

    user_id = query.from_user.id
    username = query.from_user.username
    fullname = query.from_user.full_name.replace('<', '').replace('>', '')
    lastname = query.from_user.last_name
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())

    if user.find_one({'user_id': user_id}) is None:
        try:
            key_id = user.find_one({}, sort=[('count_id', -1)])['count_id']
        except:
            key_id = 0
        try:
            key_id += 1
            user_data(key_id, user_id, username, fullname, lastname, str(1), creation_time=timer,
                      last_contact_time=timer)
        except:
            for i in range(100):
                try:
                    key_id += 1
                    user_data(key_id, user_id, username, fullname, lastname, str(1), creation_time=timer,
                              last_contact_time=timer)
                    break
                except:
                    continue
    elif user.find_one({'user_id': user_id})['username'] != username:
        user.update_one({'user_id': user_id}, {'$set': {'username': username}})

    elif user.find_one({'user_id': user_id})['fullname'] != fullname:
        user.update_one({'user_id': user_id}, {'$set': {'fullname': fullname}})

    user_list = user.find_one({"user_id": user_id})
    USDT = user_list['USDT']

    now_money = standard_num(USDT + fb_money)
    now_money = float(now_money) if str((now_money)).count('.') > 0 else int(standard_num(now_money))
    user.update_one({'user_id': user_id}, {"$set": {'USDT': now_money}})
    safe_fullname = html.escape(fullname, quote=False)
    fstext = f'''
{safe_fullname} 已领取 <b>{fb_money}</b> USDT
    '''
    url = helpers.create_deep_linked_url(context.bot.username, str(user_id))
    keyboard = [[InlineKeyboardButton(f"{context.bot.first_name}", url=url)]]
    try:
        query.edit_message_text(fstext, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except:
        pass


def lqhb(update: Update, context: CallbackContext):
    query = update.callback_query
    uid = query.data.replace('lqhb ', '')
    user_id = query.from_user.id
    username = query.from_user.username
    fullname = query.from_user.full_name.replace('<', '').replace('>', '')
    lastname = query.from_user.last_name
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())

    if user.find_one({'user_id': user_id}) is None:
        try:
            key_id = user.find_one({}, sort=[('count_id', -1)])['count_id']
        except:
            key_id = 0
        try:
            key_id += 1
            user_data(key_id, user_id, username, fullname, lastname, str(1), creation_time=timer,
                      last_contact_time=timer)
        except:
            for i in range(100):
                try:
                    key_id += 1
                    user_data(key_id, user_id, username, fullname, lastname, str(1), creation_time=timer,
                              last_contact_time=timer)
                    break
                except:
                    continue
    elif user.find_one({'user_id': user_id})['username'] != username:
        user.update_one({'user_id': user_id}, {'$set': {'username': username}})

    elif user.find_one({'user_id': user_id})['fullname'] != fullname:
        user.update_one({'user_id': user_id}, {'$set': {'fullname': fullname}})

    user_list = user.find_one({"user_id": user_id})
    USDT = user_list['USDT']

    hongbao_list = hongbao.find_one({'uid': uid})
    fb_id = hongbao_list['user_id']
    fb_fullname = hongbao_list['fullname']
    hbmoney = hongbao_list['hbmoney']
    hbsl = hongbao_list['hbsl']
    state = hongbao_list['state']
    if state == 1:
        query.answer('红包已抢完', show_alert=bool("true"))
        return

    qhb_list = qb.find_one({"uid": uid, 'user_id': user_id})
    if qhb_list is not None:
        query.answer('你已领取该红包', show_alert=bool("true"))
        return
    qb_list = list(qb.find({'uid': uid}, sort=[('money', -1)]))

    syhb = hbsl - len(qb_list)
    # 以下是随机分配金额的代码
    remaining_money = hbmoney - sum(q['money'] for q in qb_list)  # 计算剩余红包总额
    if syhb > 1:
        # 多于一个红包剩余时，使用正态分布随机生成金额
        mean_money = remaining_money / syhb  # 计算每个红包的平均金额
        std_dev = mean_money / 3  # 标准差设定为平均金额的1/3
        money = standard_num(max(0.01, round(random.normalvariate(mean_money, std_dev), 2)))  # 使用正态分布生成金额，并保留两位小数
        money = float(money) if str(money).count('.') > 0 else int(money)
    else:
        # 如果只有一个红包剩余，直接将剩余金额分配给该红包
        money = round(remaining_money, 2)  # 将剩余金额保留两位小数
        money = float(money) if str(money).count('.') > 0 else int(money)

    # 将金额保存到数据库
    qb.insert_one({
        'uid': uid,
        'user_id': user_id,
        'fullname': fullname,
        'money': money,
        'timer': timer
    })

    user_money = standard_num(USDT + money)
    user_money = float(user_money) if str(user_money).count('.') > 0 else int(user_money)
    user.update_one({'user_id': user_id}, {"$set": {'USDT': user_money}})

    query.answer(f'领取红包成功，金额:{money}', show_alert=bool("true"))

    jiangpai = {'0': '🥇', '1': '🥈', '2': '🥉'}

    qb_list = list(qb.find({'uid': uid}, sort=[('money', -1)]))

    syhb = hbsl - len(qb_list)
    qbrtext = []
    count = 0
    for i in qb_list:
        qbid = i['user_id']
        qbname = i['fullname'].replace('<', '').replace('>', '')
        qbtimer = i['timer'][-8:]
        qbmoney = i['money']
        safe_qbname = html.escape(qbname, quote=False)
        if str(count) in jiangpai.keys():

            qbrtext.append(
                f'{jiangpai[str(count)]} <code>{qbmoney}</code>({qbtimer}) USDT💰 - {safe_qbname}')
        else:
            qbrtext.append(f'<code>{qbmoney}</code>({qbtimer}) USDT💰 - {safe_qbname}')
        count += 1
    qbrtext = '\n'.join(qbrtext)

    safe_fb_fullname = html.escape(fb_fullname, quote=False)
    fstext = f'''
🧧 {safe_fb_fullname} 发送了一个红包
💵总金额:{hbmoney} USDT💰 剩余:{syhb}/{hbsl}

{qbrtext}
    '''
    if syhb == 0:
        url = helpers.create_deep_linked_url(context.bot.username, str(user_id))
        keyboard = [
            [InlineKeyboardButton(context.bot.first_name, url=url)]
        ]
        hongbao.update_one({'uid': uid}, {"$set": {'state': 1}})
    else:
        url = helpers.create_deep_linked_url(context.bot.username, str(user_id))
        keyboard = [
            [InlineKeyboardButton('领取红包', callback_data=f'lqhb {uid}')],
            [InlineKeyboardButton(context.bot.first_name, url=url)]
        ]
    try:
        query.edit_message_text(text=fstext, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    except:
        pass

def xzhb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    uid = query.data.replace('xzhb ', '')
    hongbao_list = hongbao.find_one({'uid': uid})
    fb_id = hongbao_list['user_id']
    fb_fullname = hongbao_list['fullname']
    state = hongbao_list['state']
    hbmoney = hongbao_list['hbmoney']
    hbsl = hongbao_list['hbsl']
    timer = hongbao_list['timer']
    jiangpai = {'0': '🥇', '1': '🥈', '2': '🥉'}
    if state == 0:

        qb_list = list(qb.find({'uid': uid}, sort=[('money', -1)]))

        syhb = hbsl - len(qb_list)

        qbrtext = []
        count = 0
        for i in qb_list:
            qbid = i['user_id']
            qbname = i['fullname'].replace('<', '').replace('>', '')
            qbtimer = i['timer'][-8:]
            qbmoney = i['money']
            safe_qbname = html.escape(qbname, quote=False)
            if str(count) in jiangpai.keys():

                qbrtext.append(
                    f'{jiangpai[str(count)]} <code>{qbmoney}</code>({qbtimer}) USDT💰 - {safe_qbname}')
            else:
                qbrtext.append(f'<code>{qbmoney}</code>({qbtimer}) USDT💰 - {safe_qbname}')
            count += 1
        qbrtext = '\n'.join(qbrtext)

        safe_fb_fullname = html.escape(fb_fullname, quote=False)
        fstext = f'''
🧧 {safe_fb_fullname} 发送了一个红包
🕦 时间:{timer}
💵 总金额:{hbmoney} USDT
状态:进行中
剩余:{syhb}/{hbsl}

{qbrtext}
        '''
        keyboard = [[InlineKeyboardButton('发送红包', switch_inline_query=f'redpacket {uid}')],
                    [InlineKeyboardButton('⭕️关闭', callback_data=f'close {user_id}')]]
        context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                 reply_markup=InlineKeyboardMarkup(keyboard))
    else:

        qb_list = list(qb.find({'uid': uid}, sort=[('money', -1)]))

        qbrtext = []
        count = 0
        for i in qb_list:
            qbid = i['user_id']
            qbname = i['fullname'].replace('<', '').replace('>', '')
            qbtimer = i['timer'][-8:]
            qbmoney = i['money']
            safe_qbname = html.escape(qbname, quote=False)
            if str(count) in jiangpai.keys():

                qbrtext.append(
                    f'{jiangpai[str(count)]} <code>{qbmoney}</code>({qbtimer}) USDT💰 - {safe_qbname}')
            else:
                qbrtext.append(f'<code>{qbmoney}</code>({qbtimer}) USDT💰 - {safe_qbname}')
            count += 1
        qbrtext = '\n'.join(qbrtext)

        safe_fb_fullname = html.escape(fb_fullname, quote=False)
        fstext = f'''
🧧 {safe_fb_fullname} 发送了一个红包
🕦 时间:{timer}
💵 总金额:{hbmoney} USDT
状态:已结束
剩余:0/{hbsl}

{qbrtext}
        '''

        keyboard = [[InlineKeyboardButton('⭕️关闭', callback_data=f'close {user_id}')]]
        context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                 reply_markup=InlineKeyboardMarkup(keyboard))


def jxzhb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    lang = get_user_lang(user_id)

    keyboard = [
        [InlineKeyboardButton(get_ui_text('redpacket_ongoing_active', lang=lang), callback_data='jxzhb'),
         InlineKeyboardButton(get_ui_text('redpacket_ended_tab', lang=lang), callback_data='yjshb')],

    ]

    for i in list(hongbao.find({'user_id': user_id, 'state': 0})):
        timer = i['timer'][-14:-3]
        hbsl = i['hbsl']
        uid = i['uid']
        qb_list = list(qb.find({'uid': uid}, sort=[('money', -1)]))
        syhb = hbsl - len(qb_list)
        hbmoney = i['hbmoney']
        keyboard.append(
            [InlineKeyboardButton(f'🧧[{timer}] {syhb}/{hbsl} - {hbmoney} USDT', callback_data=f'xzhb {uid}')])

    keyboard.append([InlineKeyboardButton(get_ui_text('redpacket_add', lang=lang), callback_data='addhb')])
    keyboard.append([InlineKeyboardButton(get_ui_text('close', lang=lang), callback_data=f'close {user_id}')])

    query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))


def yjshb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    lang = get_user_lang(user_id)

    keyboard = [
        [InlineKeyboardButton(get_ui_text('redpacket_ongoing_tab', lang=lang), callback_data='jxzhb'),
         InlineKeyboardButton(get_ui_text('redpacket_ended_active', lang=lang), callback_data='yjshb')],

    ]

    for i in list(hongbao.find({'user_id': user_id, 'state': 1})):
        timer = i['timer'][-14:-3]
        hbsl = i['hbsl']
        uid = i['uid']
        hbmoney = i['hbmoney']
        keyboard.append(
            [InlineKeyboardButton(f'🧧[{timer}] 0/{hbsl} - {hbmoney} USDT (over)', callback_data=f'xzhb {uid}')])

    keyboard.append([InlineKeyboardButton(get_ui_text('redpacket_add', lang=lang), callback_data='addhb')])
    keyboard.append([InlineKeyboardButton(get_ui_text('close', lang=lang), callback_data=f'close {user_id}')])

    query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))


def addhb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    fstext = f'''
💡 请回复你要发送的总金额()? 例如: <code>8.88</code>
    '''
    keyboard = [[InlineKeyboardButton('🚫取消', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {'sign': 'addhb'}})
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard),
                             parse_mode='HTML')


def ensure_user_exists(user_id, username, fullname, lastname, language_code=None):
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    initial_lang = normalize_lang_code(language_code)
    user_list = user.find_one({'user_id': user_id})
    if user_list is None:
        try:
            key_id = user.find_one({}, sort=[('count_id', -1)])['count_id']
        except:
            key_id = 0
        try:
            key_id += 1
            user_data(key_id, user_id, username, fullname, lastname, str(1), creation_time=timer,
                      last_contact_time=timer, lang=initial_lang)
        except:
            for i in range(100):
                try:
                    key_id += 1
                    user_data(key_id, user_id, username, fullname, lastname, str(1), creation_time=timer,
                              last_contact_time=timer, lang=initial_lang)
                    break
                except:
                    continue
        user_list = user.find_one({'user_id': user_id})
    else:
        updates = {'last_contact_time': timer}
        if user_list.get('username') != username:
            updates['username'] = username
        if user_list.get('fullname') != fullname:
            updates['fullname'] = fullname
        if user_list.get('lastname') != lastname:
            updates['lastname'] = lastname
        if not user_list.get('lang'):
            updates['lang'] = initial_lang
        if updates:
            user.update_one({'user_id': user_id}, {'$set': updates})
            user_list = user.find_one({'user_id': user_id})
    if user_id in ADMIN_USER_IDS:
        user.update_one({'user_id': user_id}, {'$set': {'state': '4'}})
        user_list = user.find_one({'user_id': user_id})
    return ensure_referral_fields(user_id) or user_list


def sum_user_log_amount_by_day(day_text):
    total = Decimal('0')
    for row in user_log.find({'today_time': {'$regex': f'^{day_text}'}}):
        try:
            money = Decimal(str(row.get('today_money', 0) or 0))
        except Exception:
            continue
        if money > 0:
            total += money
    return standard_num(total)


def build_admin_dashboard_text(user_count, total_balance):
    today_text = time.strftime('%Y-%m-%d', time.localtime())
    yesterday_text = (datetime.datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    today_income = sum_user_log_amount_by_day(today_text)
    yesterday_income = sum_user_log_amount_by_day(yesterday_text)
    return f'''
[emoji:5287684458881756303:🤖] 机器人使用人数：{user_count}
[emoji:4972482444025398275:👛] 机器人总余额：{standard_num(total_balance)} USDT
[emoji:5220195537520711716:⚡️] 今日收入：{today_income} USDT
[emoji:5222097061276566531:🍃] 昨日收入：{yesterday_income} USDT
        '''


DEFAULT_BUY_NOTICE_TEXT = '<b>您购买的商品已打包完成，请查收！ 欢迎下次光临 ❣️</b>'


def ensure_buy_notice_bold(text):
    value = str(text or '').strip()
    if not value:
        return DEFAULT_BUY_NOTICE_TEXT
    if re.search(r'<\s*/?\s*[a-zA-Z][^>]*>', value):
        return value
    return f'<b>{html.escape(value, quote=False)}</b>'


def normalize_buy_notice_compare_text(text):
    text = str(text or '')
    text = text.replace('\xa0', ' ')
    text = re.sub(r'\s+', '', text)
    return text.strip()


def get_buy_notice_text(product_text=''):
    global_text = ensure_buy_notice_bold(get_text_config('购买提醒', DEFAULT_BUY_NOTICE_TEXT))
    product_text = str(product_text or '').strip()
    if not product_text:
        return global_text
    if normalize_buy_notice_compare_text(product_text) == normalize_buy_notice_compare_text(DEFAULT_BUY_NOTICE_TEXT):
        return global_text
    return ensure_buy_notice_bold(product_text)


def build_purchase_success_header(deducted_amount, remaining_amount, user_id=None):
    deducted_text = standard_num(deducted_amount)
    remaining_text = standard_num(remaining_amount)
    lang = get_user_lang(user_id) if user_id is not None else 'zh'
    if lang == 'en':
        return (
            '<b>[emoji:5193209274452425995:🎉] Purchase Successful</b>\n\n'
            f'<b>[emoji:4965219701572503640:💰] Deducted from Balance:</b> {deducted_text} USDT\n'
            f'<b>[emoji:4972482444025398275:👛] Remaining Balance:</b> {remaining_text} USDT'
        )
    return (
        '<b>[emoji:5193209274452425995:🎉] 购买成功</b>\n\n'
        f'<b>[emoji:4965219701572503640:💰] 从余额中扣除：</b> {deducted_text} USDT\n'
        f'<b>[emoji:4972482444025398275:👛] 您的剩余金额：</b> {remaining_text} USDT'
    )


def format_account_check_elapsed(elapsed_seconds):
    total_seconds = max(0, int(elapsed_seconds or 0))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f'{hours}h {minutes}m {seconds}s'
    if minutes > 0:
        return f'{minutes}m {seconds}s'
    return f'{seconds}s'


def build_account_check_progress_text(total_count, checked_count, alive_count=0, invalid_count=0, frozen_count=0, timeout_count=0, in_progress_count=0, queued_count=0, elapsed_seconds=None, user_id=None):
    lang = get_user_lang(user_id) if user_id is not None else 'zh'
    elapsed_text = format_account_check_elapsed(elapsed_seconds)
    if lang == 'en':
        lines = [
            f'<b>{ACCOUNT_CHECK_EMOJI_PROGRESS} Checking account status, please wait...</b>',
            '',
            f'<b>{ACCOUNT_CHECK_EMOJI_CHECKED} Checked:</b> {checked_count} / {total_count}',
            f'<b>{ACCOUNT_CHECK_EMOJI_IN_PROGRESS} In progress:</b> {max(0, int(in_progress_count or 0))}',
        ]
        if queued_count > 0:
            lines.append(f'<b>{ACCOUNT_CHECK_EMOJI_QUEUED} Queued:</b> {queued_count}')
        if elapsed_seconds is not None:
            lines.append(f'<b>{ACCOUNT_CHECK_EMOJI_ELAPSED} Elapsed:</b> {elapsed_text}')
        return '\n'.join(lines)
    lines = [
        f'<b>{ACCOUNT_CHECK_EMOJI_PROGRESS} 正在检查账号状态，请稍等！</b>',
        '',
        f'<b>{ACCOUNT_CHECK_EMOJI_CHECKED} 已检测：</b> {checked_count} / {total_count}',
        f'<b>{ACCOUNT_CHECK_EMOJI_IN_PROGRESS} 检测中：</b> {max(0, int(in_progress_count or 0))}',
    ]
    if queued_count > 0:
        lines.append(f'<b>{ACCOUNT_CHECK_EMOJI_QUEUED} 排队中：</b> {queued_count}')
    if elapsed_seconds is not None:
        lines.append(f'<b>{ACCOUNT_CHECK_EMOJI_ELAPSED} 已用时：</b> {elapsed_text}')
    return '\n'.join(lines)


def build_account_check_result_text(total_count, alive_count, invalid_count, frozen_count, timeout_count, deducted_amount, refund_amount, remaining_amount, user_id=None):
    deducted_text = standard_num(deducted_amount)
    refund_text = standard_num(refund_amount)
    remaining_text = standard_num(remaining_amount)
    lang = get_user_lang(user_id) if user_id is not None else 'zh'
    lines = []
    if alive_count == 0 and timeout_count == 0:
        lines.append(f'<b>{ACCOUNT_CHECK_EMOJI_INVALID} All checked accounts were invalid. Refund issued.</b>' if lang == 'en' else f'<b>{ACCOUNT_CHECK_EMOJI_INVALID} 本次账号检测全部失效，已退款</b>')
    else:
        lines.append('<b>[emoji:5193209274452425995:🎉] Purchase Successful</b>' if lang == 'en' else '<b>[emoji:5193209274452425995:🎉] 购买成功</b>')
    if lang == 'en':
        lines.extend([
            '',
            f'<b>{ACCOUNT_CHECK_EMOJI_TOTAL} Total Accounts:</b> {total_count}',
            f'<b>{ACCOUNT_CHECK_EMOJI_ALIVE} Valid Accounts:</b> {alive_count}',
            f'<b>{ACCOUNT_CHECK_EMOJI_INVALID} Invalid Accounts:</b> {invalid_count}',
            f'<b>{ACCOUNT_CHECK_EMOJI_FROZEN} Frozen Accounts:</b> {frozen_count}',
        ])
    else:
        lines.extend([
            '',
            f'<b>{ACCOUNT_CHECK_EMOJI_TOTAL} 账号数量：</b> {total_count}',
            f'<b>{ACCOUNT_CHECK_EMOJI_ALIVE} 存活账号：</b> {alive_count}',
            f'<b>{ACCOUNT_CHECK_EMOJI_INVALID} 无效账号：</b> {invalid_count}',
            f'<b>{ACCOUNT_CHECK_EMOJI_FROZEN} 冻结账号：</b> {frozen_count}',
        ])
    if timeout_count:
        lines.append(f'<b>{ACCOUNT_CHECK_EMOJI_TIMEOUT} Timed-out Accounts:</b> {timeout_count}' if lang == 'en' else f'<b>{ACCOUNT_CHECK_EMOJI_TIMEOUT} 超时账号：</b> {timeout_count}')
    lines.append(f'<b>[emoji:4965219701572503640:💰] Deducted from Balance:</b> {deducted_text} USDT' if lang == 'en' else f'<b>[emoji:4965219701572503640:💰] 从余额中扣除：</b> {deducted_text} USDT')
    if refund_amount:
        lines.append(f'<b>[emoji:5235511932064129087:🎁] Refunded to Balance:</b> {refund_text} USDT' if lang == 'en' else f'<b>[emoji:5235511932064129087:🎁] 已退回余额：</b> {refund_text} USDT')
    lines.append(f'<b>[emoji:4972482444025398275:👛] Remaining Balance:</b> {remaining_text} USDT' if lang == 'en' else f'<b>[emoji:4972482444025398275:👛] 您的剩余金额：</b> {remaining_text} USDT')
    if timeout_count:
        lines.extend([
            '',
            f'<b>{ACCOUNT_CHECK_EMOJI_TIMEOUT} Timed-out accounts were delivered with the file. Please contact support if needed.</b>' if lang == 'en' else f'<b>{ACCOUNT_CHECK_EMOJI_TIMEOUT} 超时账号已随文件一起发给你，请联系客服处理。</b>'
        ])
    return '\n'.join(lines)


def build_account_check_admin_notice(fullname, username, user_id, yijiprojectname, erjiprojectname, total_count, alive_count, invalid_count, frozen_count, timeout_count, order_id, deducted_amount, refund_amount):
    username_text = f'@{username}' if username else '未设置'
    lines = [
        f'用户: <a href="tg://user?id={user_id}">{fullname}</a> {username_text}',
        f'用户ID: <code>{user_id}</code>',
        f'购买商品: {yijiprojectname}/{erjiprojectname}',
        f'订单号: <code>{order_id}</code>',
        f'购买数量: {total_count}',
        f'存活账号: {alive_count}',
        f'无效账号: {invalid_count}',
        f'冻结账号: {frozen_count}',
    ]
    if timeout_count:
        lines.append(f'超时账号: {timeout_count}')
    lines.append(f'扣除金额: {standard_num(deducted_amount)}')
    if refund_amount:
        lines.append(f'退款金额: {standard_num(refund_amount)}')
    return '\n'.join(lines)


def create_delivery_order_id():
    current_time = datetime.datetime.now()
    formatted_time = current_time.strftime('%Y%m%d%H%M%S')
    timestamp = str(current_time.timestamp()).replace('.', '')
    return formatted_time + timestamp


def reserve_inventory_items(base_query, count, user_id, order_id, timer):
    reserved_docs = []
    query = dict(base_query or {})
    query['state'] = 0
    update_fields = {
        'state': 1,
        'yssj': timer,
        'gmid': user_id,
        'delivery_order_id': order_id,
    }
    for _ in range(max(0, int(count or 0))):
        reserved = hb.find_one_and_update(
            query,
            {'$set': update_fields},
            sort=[('_id', 1)],
            return_document=ReturnDocument.AFTER,
        )
        if not reserved:
            break
        reserved_docs.append(reserved)

    if len(reserved_docs) < count:
        if reserved_docs:
            hb.update_many(
                {
                    '_id': {'$in': [doc['_id'] for doc in reserved_docs]},
                    'gmid': user_id,
                    'delivery_order_id': order_id,
                },
                {
                    '$set': {'state': 0},
                    '$unset': {
                        'yssj': '',
                        'gmid': '',
                        'delivery_order_id': '',
                        'delivery_check_state': '',
                        'delivery_check_reason': '',
                        'delivery_check_timer': '',
                    }
                }
            )
        return []
    return reserved_docs


def release_reserved_inventory_items(selected_docs, user_id, order_id):
    selected_docs = list(selected_docs or [])
    if not selected_docs:
        return
    hb.update_many(
        {
            '_id': {'$in': [doc['_id'] for doc in selected_docs if doc.get('_id') is not None]},
            'gmid': user_id,
            'delivery_order_id': order_id,
            'state': 1,
        },
        {
            '$set': {'state': 0},
            '$unset': {
                'yssj': '',
                'gmid': '',
                'delivery_order_id': '',
                'delivery_check_state': '',
                'delivery_check_reason': '',
                'delivery_check_timer': '',
            }
        }
    )


def reserve_inventory_and_charge(base_query, count, user_id, order_id, timer, total_amount):
    selected_docs = reserve_inventory_items(base_query, count, user_id, order_id, timer)
    if len(selected_docs) < count:
        return [], None, 'stock'

    updated_user = charge_user_for_purchase(user_id, total_amount, count)
    if not updated_user:
        release_reserved_inventory_items(selected_docs, user_id, order_id)
        return [], None, 'balance'
    return selected_docs, updated_user, 'ok'


def charge_user_for_purchase(user_id, total_amount, quantity):
    normalized_amount = standard_num(total_amount)
    amount_value = float(normalized_amount) if str(normalized_amount).count('.') > 0 else int(normalized_amount)
    quantity = int(quantity or 0)
    if amount_value <= 0 or quantity <= 0:
        return None
    return user.find_one_and_update(
        {
            'user_id': user_id,
            'USDT': {'$gte': amount_value},
        },
        {
            '$inc': {
                'USDT': -amount_value,
                'zgje': amount_value,
                'zgsl': quantity,
            },
            '$set': {'sign': 0}
        },
        return_document=ReturnDocument.AFTER,
    )


def build_inventory_entry_file_path(leixing, nowuid, projectname):
    if leixing == '协议号':
        return find_existing_storage_path('协议号', nowuid, f'{projectname}.session')
    return find_existing_storage_path('号包', nowuid, projectname)


def collect_delivery_source_paths(leixing, nowuid, entry_name):
    paths = []
    if leixing == '协议号':
        for suffix in ('.json', '.session'):
            source_path = find_existing_storage_path('协议号', nowuid, f'{entry_name}{suffix}')
            if source_path.exists() and source_path.is_file():
                paths.append(source_path)
        return paths

    folder_path = find_existing_storage_path('号包', nowuid, entry_name)
    if not folder_path.exists() or not folder_path.is_dir():
        return paths

    for candidate in folder_path.rglob('*'):
        if candidate.is_file():
            paths.append(candidate)
    return paths


def build_delivery_zip(leixing, user_id, nowuid, entry_names):
    shijiancuo = int(time.time())
    missing_entries = []
    added_files = 0
    entry_names = unique_preserve_order(entry_names)
    archived_names = set()
    if leixing == '协议号':
        zip_filename = find_existing_storage_path('协议号发货', f'{user_id}_{shijiancuo}.zip')
        zip_filename.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_name in entry_names:
                source_paths = collect_delivery_source_paths(leixing, nowuid, file_name)
                if not source_paths:
                    missing_entries.append(str(file_name))
                    continue
                for source_path in source_paths:
                    arcname = source_path.name
                    if arcname in archived_names:
                        logging.warning(
                            'skip duplicate delivery archive member: leixing=%s nowuid=%s user_id=%s arcname=%s entry=%s',
                            leixing,
                            nowuid,
                            user_id,
                            arcname,
                            file_name,
                        )
                        continue
                    zipf.write(source_path, arcname)
                    archived_names.add(arcname)
                    added_files += 1
        return zip_filename, added_files, missing_entries

    zip_filename = find_existing_storage_path('发货', f'{user_id}_{shijiancuo}.zip')
    zip_filename.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for folder_name in entry_names:
            source_paths = collect_delivery_source_paths(leixing, nowuid, folder_name)
            if not source_paths:
                missing_entries.append(str(folder_name))
                continue
            full_folder_path = find_existing_storage_path('号包', nowuid, folder_name)
            for file_path in source_paths:
                arcname = os.path.join(str(folder_name), os.path.relpath(file_path, full_folder_path))
                if arcname in archived_names:
                    logging.warning(
                        'skip duplicate delivery archive member: leixing=%s nowuid=%s user_id=%s arcname=%s entry=%s',
                        leixing,
                        nowuid,
                        user_id,
                        arcname,
                        folder_name,
                    )
                    continue
                zipf.write(file_path, arcname)
                archived_names.add(arcname)
                added_files += 1
    return zip_filename, added_files, missing_entries


def resolve_inventory_check_target(leixing, nowuid, projectname):
    if leixing == '协议号':
        return leixing, build_inventory_entry_file_path(leixing, nowuid, projectname)

    folder_path = find_existing_storage_path('号包', nowuid, projectname)
    tdata_path = folder_path / 'tdata'
    if tdata_path.exists() and tdata_path.is_dir():
        return '直登号', tdata_path

    session_files = sorted(folder_path.glob('*.session')) if folder_path.exists() else []
    if session_files:
        return '协议号', session_files[0]

    json_files = sorted(folder_path.glob('*.json')) if folder_path.exists() else []
    if json_files:
        return '协议号', json_files[0]

    return '直登号', folder_path


def archive_invalid_inventory_item(leixing, nowuid, projectname, order_id, item_meta):
    date_text = time.strftime('%Y-%m-%d', time.localtime())
    bucket_name = 'session' if leixing == '协议号' else 'tdata'
    target_root = ACCOUNT_BAN_ROOT / date_text / str(order_id) / bucket_name
    target_root.mkdir(parents=True, exist_ok=True)

    archived_files = []
    if leixing == '协议号':
        for suffix in ('.session', '.json'):
            src_path = find_existing_storage_path('协议号', nowuid, f'{projectname}{suffix}')
            if src_path.exists():
                dst_path = target_root / src_path.name
                if dst_path.exists():
                    dst_path.unlink()
                shutil.move(str(src_path), str(dst_path))
                archived_files.append(str(dst_path))
    else:
        src_path = find_existing_storage_path('号包', nowuid, projectname)
        if src_path.exists():
            dst_path = target_root / str(projectname)
            if dst_path.exists():
                shutil.rmtree(dst_path, ignore_errors=True)
            shutil.move(str(src_path), str(dst_path))
            archived_files.append(str(dst_path))

    if archived_files:
        item_meta['archived_files'] = archived_files
    return item_meta


def write_invalid_archive_meta(order_id, payload):
    date_text = time.strftime('%Y-%m-%d', time.localtime())
    meta_path = ACCOUNT_BAN_ROOT / date_text / str(order_id) / 'meta.json'
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return meta_path


def refund_invalid_accounts(user_id, refund_amount, order_id):
    refund_amount = float(standard_num(refund_amount))
    if refund_amount <= 0:
        current_user = user.find_one({'user_id': user_id}) or {}
        return float(current_user.get('USDT', 0))
    created_at = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    try:
        account_check_refunds.insert_one({
            'order_id': str(order_id),
            'user_id': user_id,
            'refund_amount': refund_amount,
            'created_at': created_at,
            'state': 'pending',
        })
    except DuplicateKeyError:
        current_user = user.find_one({'user_id': user_id}) or {}
        return float(current_user.get('USDT', 0))

    updated_user = user.find_one_and_update(
        {'user_id': user_id},
        {'$inc': {'USDT': refund_amount}},
        return_document=ReturnDocument.AFTER,
    ) or {}
    new_balance = float(updated_user.get('USDT', 0))
    account_check_refunds.update_one(
        {'order_id': str(order_id)},
        {'$set': {'state': 'applied', 'applied_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), 'balance_after': new_balance}}
    )
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    user_logging(order_id, '账号检测退款', user_id, refund_amount, timer)
    return float(new_balance)


def send_html_message(bot, chat_id, text, **kwargs):
    return bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', disable_web_page_preview=True, **kwargs)


def edit_html_message(bot, chat_id, message_id, text, **kwargs):
    return bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode='HTML', disable_web_page_preview=True, **kwargs)


def finalize_account_check_message(bot, user_id, progress_message_id, final_text):
    try:
        edit_html_message(bot, user_id, progress_message_id, final_text)
        return 'edited'
    except Exception:
        logging.exception('Failed to edit account-check progress message for user %s', user_id)

    try:
        send_html_message(bot, user_id, final_text)
        safe_delete_message(bot, user_id, progress_message_id, 'delete_account_check_progress')
        return 'sent'
    except Exception:
        logging.exception('Failed to send account-check completion message for user %s', user_id)
        return 'failed'


def begin_account_check_progress_message(bot, query, user_id, total_count):
    progress_text = build_account_check_progress_text(total_count, 0, 0, 0, 0, user_id=user_id)
    existing_message = getattr(query, 'message', None)
    if existing_message:
        try:
            edit_html_message(bot, user_id, existing_message.message_id, progress_text)
            return existing_message.message_id, True
        except Exception:
            logging.warning('Failed to reuse purchase confirmation message for account-check progress of user %s', user_id, exc_info=True)
    try:
        progress_message = send_html_message(bot, user_id, progress_text)
        return progress_message.message_id, False
    except Exception:
        logging.exception('Failed to create account-check progress message for user %s', user_id)
        if existing_message:
            return existing_message.message_id, True
        return None, False


def update_account_check_status_message(bot, user_id, progress_message_id, text):
    if progress_message_id:
        try:
            edit_html_message(bot, user_id, progress_message_id, text)
            return 'edited'
        except Exception:
            logging.warning('Failed to update account-check status message for user %s', user_id, exc_info=True)
    try:
        send_html_message(bot, user_id, text)
        return 'sent'
    except Exception:
        logging.exception('Failed to send account-check status message for user %s', user_id)
        return 'failed'


def run_single_account_check(leixing, nowuid, item, timeout_seconds):
    projectname = item['projectname']
    check_entry_type, target_path = resolve_inventory_check_target(leixing, nowuid, projectname)
    runtime_status = get_account_check_runtime_status(check_entry_type)
    if not runtime_status.get('ready'):
        check_result = {
            'status': 'timeout',
            'reason': f"runtime_not_ready:{runtime_status.get('reason', 'unknown')}",
            'entry_type': check_entry_type,
            'path': str(target_path),
            'attempts': 1,
            'max_retries': ACCOUNT_CHECK_MAX_RETRIES,
        }
    else:
        attempts = 0
        check_result = {'status': 'timeout', 'reason': 'empty_check_result'}
        while True:
            attempts += 1
            try:
                check_result = check_account_inventory_item(check_entry_type, str(target_path), timeout_seconds)
            except Exception as exc:
                check_result = {'status': 'timeout', 'reason': str(exc) or exc.__class__.__name__}
            if check_result.get('status') != 'timeout':
                break
            if attempts > ACCOUNT_CHECK_MAX_RETRIES:
                timeout_reason = str(check_result.get('reason', '') or '')
                check_result = dict(check_result or {})
                check_result['reason'] = (
                    f'check_timeout_after_retries:{timeout_reason} | retries={attempts - 1}'
                    if timeout_reason
                    else f'check_timeout_after_retries | retries={attempts - 1}'
                )
                break
            logging.warning(
                'account check timeout, retrying: user=%s hbid=%s project=%s attempt=%s/%s path=%s reason=%s',
                nowuid,
                item.get('hbid'),
                projectname,
                attempts,
                ACCOUNT_CHECK_MAX_RETRIES + 1,
                str(target_path),
                check_result.get('reason', ''),
            )
        check_result = dict(check_result or {})
        check_result.setdefault('entry_type', check_entry_type)
        check_result.setdefault('path', str(target_path))
        check_result['attempts'] = attempts
        check_result['max_retries'] = ACCOUNT_CHECK_MAX_RETRIES
    return item, projectname, check_result


def get_account_check_concurrency(total_count):
    total_count = max(0, int(total_count or 0))
    if total_count > 500:
        return 30
    if total_count > 100:
        return 10
    if total_count > 10:
        return 5
    if total_count > 3:
        return 2
    return 1


def deliver_accounts_with_check(context, user_id, fullname, username, nowuid, erjiprojectname, yijiprojectname, leixing, selected_items, notice_text, order_id, unit_price, total_amount, progress_message_id):
    bot = context.bot
    total_count = len(selected_items)
    alive_items = []
    invalid_items = []
    frozen_items = []
    timeout_items = []
    checked_count = 0
    progress_started_at = time.monotonic()
    progress_state = {
        'running_count': 0,
        'alive_count': 0,
        'invalid_count': 0,
        'frozen_count': 0,
        'timeout_count': 0,
    }
    progress_lock = threading.Lock()
    last_progress_ts = 0.0

    def push_progress(force=False):
        nonlocal last_progress_ts
        now_ts = time.monotonic()
        if not force and now_ts - last_progress_ts < ACCOUNT_CHECK_PROGRESS_HEARTBEAT_SECONDS:
            return
        with progress_lock:
            running_count = max(0, int(progress_state['running_count']))
            alive_count = int(progress_state['alive_count'])
            invalid_count = int(progress_state['invalid_count'])
            frozen_count = int(progress_state['frozen_count'])
            timeout_count = int(progress_state['timeout_count'])
        queued_count = max(0, total_count - checked_count - running_count)
        try:
            edit_html_message(
                bot,
                user_id,
                progress_message_id,
                build_account_check_progress_text(
                    total_count,
                    checked_count,
                    alive_count,
                    invalid_count,
                    frozen_count,
                    timeout_count,
                    in_progress_count=running_count,
                    queued_count=queued_count,
                    elapsed_seconds=now_ts - progress_started_at,
                    user_id=user_id,
                )
            )
        except Exception:
            logging.warning('Failed to update account-check progress for user %s at %s/%s', user_id, checked_count, total_count, exc_info=True)
        last_progress_ts = now_ts

    def run_single_account_check_tracked(item):
        with progress_lock:
            progress_state['running_count'] += 1
        try:
            return run_single_account_check(leixing, nowuid, item, ACCOUNT_CHECK_TIMEOUT_SECONDS)
        finally:
            with progress_lock:
                progress_state['running_count'] = max(0, progress_state['running_count'] - 1)

    max_workers = get_account_check_concurrency(total_count)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='acct-check') as executor:
        future_map = {
            executor.submit(run_single_account_check_tracked, item): item
            for item in selected_items
        }
        pending_futures = set(future_map.keys())
        while pending_futures:
            done_futures, pending_futures = wait(
                pending_futures,
                timeout=ACCOUNT_CHECK_PROGRESS_HEARTBEAT_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            if not done_futures:
                push_progress(force=True)
                continue
            for future in done_futures:
                item = future_map[future]
                projectname = item['projectname']
                try:
                    _, projectname, check_result = future.result()
                except Exception as exc:
                    check_result = {'status': 'timeout', 'reason': str(exc) or exc.__class__.__name__}
                checked_count += 1

                hb.update_one(
                    {'hbid': item['hbid']},
                    {'$set': {
                        'delivery_order_id': order_id,
                        'delivery_check_state': check_result.get('status', 'timeout'),
                        'delivery_check_reason': str(check_result.get('reason', ''))[:500],
                        'delivery_check_timer': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    }}
                )

                item_meta = {
                    'hbid': item['hbid'],
                    'projectname': projectname,
                    'status': check_result.get('status', 'timeout'),
                    'reason': check_result.get('reason', ''),
                }
                if check_result.get('status') == 'alive':
                    alive_items.append(item_meta)
                    with progress_lock:
                        progress_state['alive_count'] += 1
                elif check_result.get('status') == 'frozen':
                    item_meta.update({
                        'freeze_since_date': check_result.get('freeze_since_date', 0),
                        'freeze_until_date': check_result.get('freeze_until_date', 0),
                        'freeze_since_text': check_result.get('freeze_since_text', ''),
                        'freeze_until_text': check_result.get('freeze_until_text', ''),
                        'freeze_appeal_url': check_result.get('freeze_appeal_url', ''),
                    })
                    frozen_items.append(archive_invalid_inventory_item(leixing, nowuid, projectname, order_id, item_meta))
                    with progress_lock:
                        progress_state['frozen_count'] += 1
                elif check_result.get('status') == 'invalid':
                    invalid_items.append(archive_invalid_inventory_item(leixing, nowuid, projectname, order_id, item_meta))
                    with progress_lock:
                        progress_state['invalid_count'] += 1
                else:
                    timeout_items.append(item_meta)
                    with progress_lock:
                        progress_state['timeout_count'] += 1

                should_push_progress = (
                    checked_count == total_count
                    or checked_count % ACCOUNT_CHECK_PROGRESS_STEP == 0
                    or time.monotonic() - last_progress_ts >= ACCOUNT_CHECK_PROGRESS_HEARTBEAT_SECONDS
                    or time.monotonic() - last_progress_ts >= ACCOUNT_CHECK_PROGRESS_INTERVAL_SECONDS
                )
                if should_push_progress:
                    push_progress(force=True)

    invalid_count = len(invalid_items)
    frozen_count = len(frozen_items)
    timeout_count = len(timeout_items)
    refund_amount = standard_num(unit_price * (invalid_count + frozen_count))
    refund_amount = float(refund_amount) if str(refund_amount).count('.') > 0 else int(refund_amount)
    remaining_amount = refund_invalid_accounts(user_id, refund_amount, order_id)
    charged_amount = standard_num(float(total_amount) - float(refund_amount))
    charged_amount = float(charged_amount) if str(charged_amount).count('.') > 0 else int(charged_amount)

    archive_payload = {
        'order_id': order_id,
        'user_id': user_id,
        'product_nowuid': nowuid,
        'product_name': erjiprojectname,
        'delivery_type': leixing,
        'total_count': total_count,
        'alive_count': len(alive_items),
        'invalid_count': invalid_count,
        'frozen_count': frozen_count,
        'timeout_count': timeout_count,
        'refund_amount': refund_amount,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
        'invalid_items': invalid_items,
        'frozen_items': frozen_items,
    }
    if invalid_items or frozen_items:
        write_invalid_archive_meta(order_id, archive_payload)

    final_text = build_account_check_result_text(
        total_count,
        len(alive_items),
        invalid_count,
        frozen_count,
        timeout_count,
        charged_amount,
        refund_amount,
        remaining_amount,
        user_id=user_id,
    )
    finalize_account_check_message(bot, user_id, progress_message_id, final_text)

    delivery_names = [item['projectname'] for item in alive_items + timeout_items]
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    delivery_record_text = final_text
    if delivery_names:
        zip_filename, added_files, missing_entries = build_delivery_zip(leixing, user_id, nowuid, delivery_names)
        if missing_entries:
            logging.warning(
                'delivery package missing inventory files: leixing=%s nowuid=%s user_id=%s entries=%s',
                leixing,
                nowuid,
                user_id,
                ', '.join(missing_entries),
            )

        if added_files <= 0:
            failure_text = '这批库存文件没找到，暂时没法发货，请联系客服处理。'
            logging.error(
                'delivery package is empty: leixing=%s nowuid=%s user_id=%s requested=%s',
                leixing,
                nowuid,
                user_id,
                ', '.join(map(str, delivery_names)),
            )
            goumaijilua(leixing, order_id, user_id, erjiprojectname, '', f'{delivery_record_text}\n\n{failure_text}', timer)
            bot.send_message(chat_id=user_id, text=failure_text)
        else:
            goumaijilua(leixing, order_id, user_id, erjiprojectname, str(zip_filename), delivery_record_text, timer)
            with open(zip_filename, 'rb') as document_fp:
                bot.send_document(chat_id=user_id, document=document_fp)
            if notice_text:
                send_html_message(bot, user_id, notice_text)
    else:
        goumaijilua(leixing, order_id, user_id, erjiprojectname, '', delivery_record_text, timer)

    admin_notice = build_account_check_admin_notice(
        fullname,
        username,
        user_id,
        yijiprojectname,
        erjiprojectname,
        total_count,
        len(alive_items),
        invalid_count,
        frozen_count,
        timeout_count,
        order_id,
        charged_amount,
        refund_amount,
    )
    for admin_user in list(user.find({'state': '4'})):
        try:
            send_html_message(bot, admin_user['user_id'], admin_notice)
        except Exception:
            pass


def start(update: Update, context: CallbackContext):
    us = update.effective_user
    chat_id = update.effective_chat.id
    user_id = us.id
    username = us.username
    fullname = us.full_name.replace('<', '').replace('>', '')
    lastname = us.last_name
    botusername = context.bot.username
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    user_list = ensure_user_exists(user_id, username, fullname, lastname, getattr(us, 'language_code', None))
    state = user_list['state']
    sign = user_list['sign']
    USDT = user_list['USDT']
    zgje = user_list['zgje']
    zgsl = user_list['zgsl']
    creation_time = user_list['creation_time']
    args = update.message.text.split(maxsplit=2)
    content = args[2] if len(args) == 3 else ""
    if len(args) == 2:
        start_arg = str(args[1] or '').strip()
        if start_arg.startswith('buy_'):
            nowuid = start_arg.replace('buy_', '', 1).strip()
            send_product_purchase_page(context, user_id, user_id, nowuid)
            return
        bind_referrer_if_possible(user_id, start_arg, timer)

    yyzt = shangtext.find_one({'projectname': '营业状态'})['text']
    if yyzt == 0:
        if state != '4':
            return
    send_user_home(context, user_id)
    if state == '4':
        keyboard = build_admin_dashboard_keyboard(user_id)
        jqrsyrs = len(list(user.find({})))
        numu = 0
        for i in list(user.find({"USDT": {"$gt": 0}})):
            USDT = i['USDT']

            numu += USDT

        fstext = build_admin_dashboard_text(jqrsyrs, numu)
        context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))
        # message_id = context.bot.send_photo(chat_id=user_id,  photo=open('辛迪充值图片.png', 'rb'))
        # print(message_id)



def huifu(update: Update, context: CallbackContext):
    chat = update.effective_chat
    bot_id = context.bot.id
    if chat.type == 'private':
        user_id = update.effective_user.id
        user_list = user.find_one({"user_id": user_id})
        replymessage = update.message.reply_to_message
        text = replymessage.text
        del_message(update.message)
        messagetext = update.effective_message.text
        state = user_list['state']
        if state == '4' or state == '3':
            if '回复图文或图片视频文字' == text:
                stored_message_text = get_message_storage_text(update.message)
                if not update.message.photo and update.message.animation is None:
                    r_text = stored_message_text or messagetext or ''
                    sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'text': r_text}})
                    sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'file_id': ''}})
                    sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'send_type': 'text'}})
                    sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'state': 1}})
                    message_id = context.bot.send_message(chat_id=user_id, text=r_text)
                    time.sleep(3)
                    del_message(message_id)
                    message_id = context.user_data[f'wanfapeizhi{user_id}']
                    time.sleep(3)
                    del_message(message_id)

                else:
                    r_text = stored_message_text or update.message.caption or ''
                    if update.message.photo:
                        file = update.message.photo[-1].file_id
                        sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'text': r_text}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'file_id': file}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'send_type': 'photo'}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'state': 1}})
                        message_id = context.bot.send_photo(chat_id=user_id, caption=r_text, photo=file)
                        time.sleep(3)
                        del_message(message_id)
                    elif update.message.animation is not None:
                        file = update.message.animation.file_id
                        sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'text': r_text}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'file_id': file}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'send_type': 'animation'}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'state': 1}})
                        message_id = context.bot.sendAnimation(chat_id=user_id, caption=r_text, animation=file)
                        time.sleep(3)
                        del_message(message_id)
                    else:
                        context.bot.send_message(chat_id=user_id, text='⚠️ 当前只支持文字、图片或动画')
            elif '回复按钮设置' == text:
                text = get_message_storage_text(update.message) or messagetext or ''
                message_id = context.user_data[f'wanfapeizhi{user_id}']
                del_message(message_id)
                keyboard = parse_urls(text)
                dumped = pickle.dumps(keyboard)
                sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'keyboard': dumped}})
                sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {'key_text': text}})
                try:
                    message_id = context.bot.send_message(chat_id=user_id, text='按钮设置成功',
                                                          reply_markup=InlineKeyboardMarkup(keyboard))
                    time.sleep(10)
                    del_message(message_id)

                except:
                    context.bot.send_message(chat_id=user_id, text=text)
                    message_id = context.bot.send_message(chat_id=user_id, text='按钮设置失败,请重新输入')
                    asyncio.sleep(10)
                    del_message(message_id)

def sifa(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    bot_id = context.bot.id
    fqdtw_list = sftw.find_one({'bot_id': bot_id,'projectname': f'图文1🔽'})
    if fqdtw_list is None:
        sifatuwen(bot_id, '图文1🔽','','','',b'\x80\x03]q\x00]q\x01a.','')
        fqdtw_list = sftw.find_one({'bot_id': bot_id,'projectname': f'图文1🔽'})
    state = fqdtw_list['state']
    keyboard = [
        [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}图文设置', callback_data='tuwen'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}按钮设置', callback_data='anniu')],
        [InlineKeyboardButton(f'{MOOD_EMOJI_STAR}查看图文', callback_data='cattu'),
         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}开启私发', callback_data='kaiqisifa')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')]
    ]
    if state == 1:
        context.bot.send_message(chat_id=user_id, text='私发状态:已关闭🔴', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        context.bot.send_message(chat_id=user_id, text='私发状态:已开启🟢', reply_markup=InlineKeyboardMarkup(keyboard))


def tuwen(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    context.user_data[f'key{user_id}'] = query.message
    message_id = context.bot.send_message(chat_id=user_id, text=f'回复图文或图片视频文字',
                                          reply_markup=ForceReply())
    context.user_data[f'wanfapeizhi{user_id}'] = message_id

def cattu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    bot_id = context.bot.id
    fqdtw_list = sftw.find_one({'bot_id': bot_id,'projectname': f'图文1🔽'})
    file_id = fqdtw_list['file_id']
    file_text = fqdtw_list['text']
    file_type = fqdtw_list['send_type']
    key_text = fqdtw_list['key_text']
    keyboard = load_saved_inline_keyboard(fqdtw_list.get('keyboard'), fqdtw_list.get('key_text'))
    keyboard.append([InlineKeyboardButton('✅已读（点击销毁此消息）', callback_data=f'close {user_id}')])
    if fqdtw_list['text'] == '' and fqdtw_list['file_id'] == '':
        message_id = context.bot.send_message(chat_id=user_id, text='请设置图文后点击')
        time.sleep(3)
        del_message(message_id)
    else:
        try:
            if key_text:
                send_key_content_preview(context, user_id, text=key_text, file_type='text')
        except:
            pass
        message_id = send_key_content_preview(context, user_id, text=file_text, file_type=file_type,
                                              file_id=file_id, keyboard=keyboard)
        time.sleep(3)
        del_message(message_id)

def anniu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    context.user_data[f'key{user_id}'] = query.message
    message_id = context.bot.send_message(chat_id=user_id, text=f'回复按钮设置', reply_markup=ForceReply())
    context.user_data[f'wanfapeizhi{user_id}'] = message_id

def kaiqisifa(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    bot_id = context.bot.id
    job = context.job_queue.get_jobs_by_name(f'sifa')
    if job == ():
        sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {"state": 2}})
        keyboard = [
            [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}图文设置', callback_data='tuwen'),
             InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}按钮设置', callback_data='anniu')],
            [InlineKeyboardButton(f'{MOOD_EMOJI_STAR}查看图文', callback_data='cattu'),
             InlineKeyboardButton(f'{MOOD_EMOJI_FAST}开启私发', callback_data='kaiqisifa')],
            [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')]]
        try:
            query.edit_message_text(text='私发状态:已开启🟢', reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as exc:
            if 'Message is not modified' not in str(exc):
                raise
        total_users = user.count_documents({})
        progress_message = context.bot.send_message(
            chat_id=user_id,
            text=f'私信进行中\n\n正在发送0/{total_users}（每10秒刷新一次进度）'
        )
        context.job_queue.run_once(
            sync_job(usersifa),
            1,
            data={"user_id": user_id, "progress_message_id": progress_message.message_id},
            name=f'sifa'
        )
        context.user_data['sifa'] = progress_message
    else:
        message_id = context.bot.send_message(chat_id=user_id, text='私发进行中')
        time.sleep(3)
        del_message(message_id)

def usersifa(context: CallbackContext):
    job = context.job
    bot_id = context.bot.id
    guanli_id = job.data['user_id']
    progress_message_id = job.data.get('progress_message_id')
    count = 0
    shibai = 0
    processed = 0
    fqdtw_list = sftw.find_one({'bot_id': bot_id,'projectname': f'图文1🔽'})
    file_id = fqdtw_list['file_id']
    file_text = fqdtw_list['text']
    file_type = fqdtw_list['send_type']
    key_text = fqdtw_list['key_text']
    keyboard = load_saved_inline_keyboard(fqdtw_list.get('keyboard'), fqdtw_list.get('key_text'))
    user_list = list(user.find({}))
    total_users = len(user_list)
    last_progress_at = 0
    send_delay_seconds = get_sifa_delay_seconds(file_type)

    def update_sifa_progress(final=False):
        if not progress_message_id:
            return
        if final:
            text = f'私信已完成\n成功:{count}\n失败:{shibai}'
        else:
            text = f'私信进行中\n\n正在发送{processed}/{total_users}（每10秒刷新一次进度）'
        try:
            context.bot.edit_message_text(chat_id=guanli_id, message_id=progress_message_id, text=text)
        except Exception:
            pass

    keyboard.append([InlineKeyboardButton('✅已读（点击销毁此消息）', callback_data=f'close 12321')])
    for i in user_list:
        if file_type == 'text':
            try:
                context.bot.send_message(chat_id=i['user_id'], text=file_text,
                                         reply_markup=InlineKeyboardMarkup(keyboard))
                count += 1
            except:
                shibai += 1
        else:
            if file_type == 'photo':
                try:
                    context.bot.send_photo(chat_id=i['user_id'], caption=file_text, photo=file_id,
                                           reply_markup=InlineKeyboardMarkup(keyboard))
                    count += 1
                except:
                    shibai += 1
            else:
                try:
                    context.bot.sendAnimation(chat_id=i['user_id'], caption=file_text, animation=file_id,
                                              reply_markup=InlineKeyboardMarkup(keyboard))
                    count += 1
                except:
                    shibai += 1
        processed += 1
        now_ts = time.time()
        if processed >= total_users or now_ts - last_progress_at >= 10:
            update_sifa_progress(final=False)
            last_progress_at = now_ts
        if send_delay_seconds > 0:
            time.sleep(send_delay_seconds)
    sftw.update_one({'bot_id': bot_id,'projectname': f'图文1🔽'}, {'$set': {"state": 1}})
    update_sifa_progress(final=True)
    keyboard = [
        [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}图文设置', callback_data='tuwen'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}按钮设置', callback_data='anniu')],
        [InlineKeyboardButton(f'{MOOD_EMOJI_STAR}查看图文', callback_data='cattu'),
         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}开启私发', callback_data='kaiqisifa')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {guanli_id}')]]
    context.bot.send_message(chat_id=guanli_id, text='私发状态:已关闭🔴', reply_markup=InlineKeyboardMarkup(keyboard))

def backstart(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    keyboard = build_admin_dashboard_keyboard(user_id)
    jqrsyrs = len(list(user.find({})))

    numu = 0
    for i in list(user.find({"USDT": {"$gt": 0}})): 
        USDT = i['USDT']

        numu += USDT

    fstext = build_admin_dashboard_text(jqrsyrs, numu)
    query.edit_message_text(text=fstext, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def gmaijilu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    lang = get_user_lang(user_id)
    df_id = int(query.data.replace('gmaijilu ', ''))
    jilu_list = list(gmjlu.find({'user_id': df_id}, sort=[('timer', -1)], limit=10))
    keyboard = []
    text_list = []
    count = 1
    for i in jilu_list:
        bianhao = i['bianhao']
        projectname = i['projectname']
        fhtext = i['text']

        keyboard.append([InlineKeyboardButton(localize_catalog_name(projectname, user_id, lang=lang), callback_data=f'zcfshuo {bianhao}')])
        count += 1
    if len(list(gmjlu.find({'user_id': df_id}))) > 10:
        keyboard.append([InlineKeyboardButton(get_ui_text('next_page', viewer_user_id=user_id), callback_data=f'gmainext {df_id}:10')])
    keyboard.append([InlineKeyboardButton(get_ui_text('back', viewer_user_id=user_id), callback_data=f'backgmjl {df_id}')])
    try:
        query.edit_message_text(text=get_ui_text('purchase_history_title', viewer_user_id=user_id), parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except:
        pass


def gmainext(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data.replace('gmainext ', '')
    page = data.split(":")[1]
    df_id = int(data.split(':')[0])
    user_id = query.from_user.id
    lang = get_user_lang(user_id)
    keyboard = []
    text_list = []
    jilu_list = list(gmjlu.find({"user_id": df_id}, sort=[("timer", -1)], skip=int(page), limit=10))
    count = 1
    for i in jilu_list:
        bianhao = i['bianhao']
        projectname = i['projectname']
        fhtext = i['text']

        keyboard.append([InlineKeyboardButton(localize_catalog_name(projectname, user_id, lang=lang), callback_data=f'zcfshuo {bianhao}')])
        count += 1
    if len(list(gmjlu.find({"user_id": df_id}, sort=[("timer", -1)], skip=int(page)))) > 10:
        if int(page) == 0:
            keyboard.append([InlineKeyboardButton(get_ui_text('next_page', viewer_user_id=user_id), callback_data=f'gmainext {df_id}:{int(page) + 10}')])
        else:
            keyboard.append([InlineKeyboardButton(get_ui_text('prev_page', viewer_user_id=user_id), callback_data=f'gmainext {df_id}:{int(page) - 10}'),
                             InlineKeyboardButton(get_ui_text('next_page', viewer_user_id=user_id), callback_data=f'gmainext {df_id}:{int(page) + 10}')])
    else:
        keyboard.append([InlineKeyboardButton(get_ui_text('prev_page', viewer_user_id=user_id), callback_data=f'gmainext {df_id}:{int(page) - 10}')])

    keyboard.append([InlineKeyboardButton(get_ui_text('back', viewer_user_id=user_id), callback_data=f'backgmjl {df_id}')])
    try:
        query.edit_message_text(text=get_ui_text('purchase_history_title', viewer_user_id=user_id), parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except:
        pass


def backgmjl(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    df_id = int(query.data.replace('backgmjl ', ''))
    df_list = user.find_one({'user_id': df_id})
    df_fullname = df_list['fullname']
    df_username = df_list['username']
    if df_username is None:
        df_username = df_fullname
    creation_time = df_list['creation_time']
    zgsl = df_list['zgsl']
    zgje = df_list['zgje']
    USDT = df_list['USDT']
    fstext = build_user_profile_text(df_id, df_username, creation_time, zgsl, zgje, USDT)

    keyboard = build_profile_keyboard(df_id)
    query.edit_message_text(text=fstext, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML',
                            disable_web_page_preview=True)


def tglink(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    df_id = int(query.data.replace('tglink ', ''))
    user_doc = ensure_referral_fields(df_id)
    if user_doc is None:
        query.edit_message_text(text='用户不存在')
        return
    fstext = build_referral_text(context.bot.username, user_doc)
    keyboard = [
        [InlineKeyboardButton('返回个人中心', callback_data=f'backgmjl {df_id}')],
        [InlineKeyboardButton(get_ui_text('close', viewer_user_id=df_id), callback_data=f'close {df_id}')],
    ]
    query.edit_message_text(
        text=fstext,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML',
        disable_web_page_preview=True,
    )


def resolve_purchase_record_file_path(record_path, leixing=None):
    record_path = str(record_path or '').strip()
    if not record_path:
        return None

    candidate = Path(record_path).expanduser()
    if candidate.exists() and candidate.is_file():
        return candidate

    file_name = candidate.name
    if not file_name:
        return None

    folder_candidates = []
    leixing = str(leixing or '').strip()
    if leixing == '协议号':
        folder_candidates.append('协议号发货')
    elif leixing == '直登号':
        folder_candidates.append('发货')
    elif leixing == '谷歌':
        folder_candidates.append('谷歌发货')
    elif leixing == 'API链接':
        folder_candidates.append('手机接码发货')

    folder_candidates.extend(['协议号发货', '发货', '谷歌发货', '手机接码发货'])

    for folder_name in unique_preserve_order(folder_candidates):
        resolved = find_existing_storage_path(folder_name, file_name)
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def zcfshuo(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    lang = get_user_lang(user_id)
    bianhao = query.data.replace('zcfshuo ', '')
    gmjlu_list = gmjlu.find_one({'bianhao': bianhao})
    leixing = gmjlu_list['leixing']
    if leixing == '会员链接':
        text = gmjlu_list['text']
        if lang == 'en':
            text = translate_text(text, 'en')

        context.bot.send_message(chat_id=user_id, text=text, disable_web_page_preview=True)

    else:
        zip_filename = gmjlu_list['text']
        fstext = gmjlu_list['ts']
        if lang == 'en':
            fstext = translate_text(fstext, 'en')
        keyboard = [[InlineKeyboardButton(translate_text('✅已读（点击销毁此消息）', lang), callback_data=f'close {user_id}')]]
        context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML', disable_web_page_preview=True,
                                 reply_markup=InlineKeyboardMarkup(keyboard))
        resolved_path = resolve_purchase_record_file_path(zip_filename, leixing=leixing)
        if not resolved_path:
            missing_text = '该订单的发货文件已不在本地，请联系客服处理。'
            if lang == 'en':
                missing_text = 'The delivery file for this order is no longer available locally. Please contact support.'
            context.bot.send_message(chat_id=user_id, text=missing_text)
            logging.warning(
                'purchase history document missing: bianhao=%s user_id=%s leixing=%s raw_path=%s',
                bianhao,
                user_id,
                leixing,
                zip_filename,
            )
            return

        with open(resolved_path, "rb") as document_fp:
            query.message.reply_document(document_fp)


USER_LIST_PAGE_SIZE = 10
USER_LIST_PROJECTION = {
    '_id': 0,
    'user_id': 1,
    'username': 1,
    'fullname': 1,
    'lastname': 1,
    'USDT': 1,
}


def _format_user_list_row(index, row):
    df_id = row.get('user_id', 0)
    raw_username = str(row.get('username') or '').strip()
    raw_fullname = str(row.get('fullname') or row.get('lastname') or raw_username or df_id)
    df_username = html.escape(raw_username, quote=False) if raw_username else '无用户名'
    df_fullname = html.escape(raw_fullname, quote=False)
    usdt = row.get('USDT', 0)
    username_part = f'@{df_username}' if raw_username else '无用户名'
    return f'{index}. <a href="tg://user?id={df_id}">{df_fullname}</a> ID:<code>{df_id}</code>-{username_part}-余额:{usdt}'


def _fetch_user_page(page):
    page = max(int(page), 0)
    rows = list(user.find({}, USER_LIST_PROJECTION, sort=[('USDT', -1), ('count_id', 1)], skip=page, limit=USER_LIST_PAGE_SIZE + 1))
    has_next = len(rows) > USER_LIST_PAGE_SIZE
    return rows[:USER_LIST_PAGE_SIZE], has_next


def yhlist(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    jilu_list, has_next = _fetch_user_page(0)
    keyboard = []
    text_list = []
    count = 1
    for i in jilu_list:
        text_list.append(_format_user_list_row(count, i))
        count += 1
    if has_next:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}下一页', callback_data=f'yhnext {USER_LIST_PAGE_SIZE}:{count}')])

    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data='backstart')])

    text_list = '\n'.join(text_list) or '暂无用户数据'
    try:
        query.edit_message_text(text=text_list, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except:
        pass


def yhnext(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data.replace('yhnext ', '')
    page = max(int(data.split(":")[0]), 0)
    count = max(int(data.split(":")[1]), 1)
    keyboard = []
    text_list = []
    jilu_list, has_next = _fetch_user_page(page)
    for i in jilu_list:
        text_list.append(_format_user_list_row(count, i))
        count += 1

    has_prev = page > 0
    if has_prev and has_next:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}上一页', callback_data=f'yhnext {max(page - USER_LIST_PAGE_SIZE, 0)}:{max(count - USER_LIST_PAGE_SIZE * 2, 1)}'),
                         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}下一页', callback_data=f'yhnext {page + USER_LIST_PAGE_SIZE}:{count}')])
    elif has_prev:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}上一页', callback_data=f'yhnext {max(page - USER_LIST_PAGE_SIZE, 0)}:{max(count - USER_LIST_PAGE_SIZE * 2, 1)}')])
    elif has_next:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}下一页', callback_data=f'yhnext {page + USER_LIST_PAGE_SIZE}:{count}')])

    text_list = '\n'.join(text_list) or '这一页没有用户数据'
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data='backstart')])
    query.edit_message_text(text=text_list,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode='HTML')


def tjbaobiao(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id


def spgli(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    sp_list = list(fenlei.find({}))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]

    for i in sp_list:
        uid = i['uid']
        projectname = i['projectname']
        row = i['row']
        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}', callback_data=f'flxxi {uid}'))
    if sp_list == []:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新建一行', callback_data='newfl')])
    else:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新建一行', callback_data='newfl'),
                         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整行排序', callback_data='paixufl'),
                         InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除一行', callback_data='delfl')])
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data='backstart'), InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
    text = f'''
商品管理
    '''
    query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')



def generate_24bit_uid():
    # 生成一个UUID
    uid = uuid.uuid4()

    # 将UUID转换为字符串
    uid_str = str(uid)

    # 使用MD5哈希算法将字符串哈希为一个128位的值
    hashed_uid = hashlib.md5(uid_str.encode()).hexdigest()

    # 取哈希值的前24位作为我们的24位UID
    return hashed_uid[:24]


def build_product_detail_keyboard(nowuid, uid, user_id):
    return [
        [InlineKeyboardButton(f'{MOOD_EMOJI_FIRE}取出所有库存', callback_data=f'qchuall {nowuid}'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除该分类', callback_data=f'delcurconfirm {nowuid}')],
        [InlineKeyboardButton('📄上传谷歌账户', callback_data=f'update_gg {nowuid}'),
         InlineKeyboardButton('💡购买提示', callback_data=f'update_wbts {nowuid}')],
        [InlineKeyboardButton('🔗上传链接', callback_data=f'update_hy {nowuid}'),
         InlineKeyboardButton('📝上传txt文件', callback_data=f'update_txt {nowuid}')],
        [InlineKeyboardButton('📦上传号包', callback_data=f'update_hb {nowuid}'),
         InlineKeyboardButton('🧩上传协议号', callback_data=f'update_xyh {nowuid}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}修改二级分类名', callback_data=f'upejflname {nowuid}'),
         InlineKeyboardButton('💰修改价格', callback_data=f'upmoney {nowuid}')],
        [InlineKeyboardButton('⬅️返回分类详情', callback_data=f'flxxi {uid}'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')]
    ]


def newfl(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    del_message(query.message)
    bot_id = context.bot.id
    maxrow = fenlei.find_one({}, sort=[('row', -1)])
    if maxrow is None:
        maxrow = 1
    else:
        maxrow = maxrow['row'] + 1
    uid = generate_24bit_uid()
    fenleibiao(uid, '点击按钮修改', maxrow)
    keylist = list(fenlei.find({}, sort=[('row', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    for i in keylist:
        uid = i['uid']
        projectname = i['projectname']
        row = i['row']
        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}', callback_data=f'flxxi {uid}'))
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新建一行', callback_data='newfl'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整行排序', callback_data='paixufl'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除一行', callback_data='delfl')])
    context.bot.send_message(chat_id=user_id, text='商品管理', reply_markup=InlineKeyboardMarkup(keyboard))


def flxxi(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    uid = query.data.replace('flxxi ', '')
    fl_pro = fenlei.find_one({'uid': uid})['projectname']
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    ej_list = ejfl.find({'uid': uid})
    for i in ej_list:
        nowuid = i['nowuid']
        projectname = i['projectname']
        row = i['row']
        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}', callback_data=f'fejxxi {nowuid}'))

    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}修改分类名', callback_data=f'upspname {uid}'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新增二级分类', callback_data=f'newejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整二级分类排序', callback_data=f'paixuejfl {uid}'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除二级分类', callback_data=f'delejfl {uid}')])
    keyboard.append([InlineKeyboardButton('⬅️返回商品管理', callback_data='spgli')])
    fstext = f'''
分类: {fl_pro}
    '''
    query.edit_message_text(text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def fejxxi(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('fejxxi ', '')

    ej_list = ejfl.find_one({'nowuid': nowuid})
    uid = ej_list['uid']
    ej_projectname = ej_list['projectname']
    money = ej_list['money']
    fl_pro = fenlei.find_one({'uid': uid})['projectname']
    keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
    ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
    fstext = f'''
主分类: {fl_pro}
二级分类: {ej_projectname}

价格: {money}U
库存: {kc}
已售: {ys}
    '''
    query.edit_message_text(text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def update_xyh(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_xyh ', '')
    fstext = f'''
发送协议号压缩包，自动识别里面的json或session格式
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_xyh {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def update_gg(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_gg ', '')
    fstext = f'''
发送txt文件
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_gg {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def update_txt(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_txt ', '')
    fstext = f'''
api号码链接专用，请正确上传，发送txt文件，一行一个
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_txt {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def update_sysm(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_sysm ', '')
    dqts = ejfl.find_one({'nowuid': nowuid})['sysm']

    context.bot.send_message(chat_id=user_id, text=dqts, parse_mode='HTML')

    fstext = f'''
当前使用说明为上面
输入新的文字更改
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_sysm {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def update_wbts(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_wbts ', '')
    dqts = ejfl.find_one({'nowuid': nowuid})['text']

    context.bot.send_message(chat_id=user_id, text=dqts, parse_mode='HTML')

    fstext = f'''
当前分类提示为上面
输入新的文字更改
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_wbts {nowuid}'}})
    keyboard = [[InlineKeyboardButton('取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def update_hy(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_hy ', '')
    fstext = f'''
发送链接，换行代表多个
单个
https://t.me/giftcode/IApV5cqF2FCzAQAA5aDXkeEqQrQ
多个
https://t.me/giftcode/IApV5cqF2FCzAQAA5aDXkeEqQrQ
https://t.me/giftcode/wI_oG9K2oFBSAQAA-Z2W0Fb3ng8
https://t.me/giftcode/_xSoPUXMgVBmAQAAiKBPNxWWIpY
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_hy {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard),
                             disable_web_page_preview=True)


def update_hb(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_hb ', '')
    fstext = f'''
发送号包
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_hb {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def upmoney(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    uid = query.data.replace('upmoney ', '')
    fstext = f'''
输入新的价格
    '''

    user.update_one({"user_id": user_id}, {"$set": {"sign": f'upmoney {uid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def upejflname(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    uid = query.data.replace('upejflname ', '')
    fstext = f'''
输入新的名字
例如 🇨🇳+86中国~直登号(tadta)
    '''

    user.update_one({"user_id": user_id}, {"$set": {"sign": f'upejflname {uid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def upspname(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    uid = query.data.replace('upspname ', '')
    fstext = f'''
输入新的名字
例如 🌎亚洲国家~✈直登号(tadta)
    '''

    user.update_one({"user_id": user_id}, {"$set": {"sign": f'upspname {uid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def newejfl(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    uid = query.data.replace('newejfl ', '')

    maxrow = ejfl.find_one({'uid': uid}, sort=[('row', -1)])
    if maxrow is None:
        maxrow = 1
    else:
        maxrow = maxrow['row'] + 1
    nowuid = generate_24bit_uid()
    erjifenleibiao(uid, nowuid, '点击按钮修改', maxrow)
    fl_pro = fenlei.find_one({'uid': uid})['projectname']
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    ej_list = ejfl.find({'uid': uid})
    for i in ej_list:
        nowuid = i['nowuid']
        projectname = i['projectname']
        row = i['row']
        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}', callback_data=f'fejxxi {nowuid}'))

    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}修改分类名', callback_data=f'upspname {uid}'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新增二级分类', callback_data=f'newejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整二级分类排序', callback_data=f'paixuejfl {uid}'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除二级分类', callback_data=f'delejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
    fstext = f'''
分类: {fl_pro}
    '''
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def addzdykey(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    keylist = get_key.find({}, sort=[('Row', 1), ('first', 1)])
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    for i in keylist:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
    if keylist == []:
        keyboard = [[InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新建一行', callback_data='newrow')]]
    else:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新建一行', callback_data='newrow'),
                         InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除一行', callback_data='delrow'),
                         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整行排序', callback_data='paixurow')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}修改按钮', callback_data='newkey')])
        
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
    text = f'''
自定义按钮
    '''
    query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


def newkey(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    keylist = list(get_key.find({}, sort=[('Row', 1), ('first', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    count = []
    for i in keylist:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
        count.append(row)
    if count == []:
        context.bot.send_message(chat_id=user_id, text='请先新建一行')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_STAR}第{i + 1}行', callback_data=f'dddd'),
                             InlineKeyboardButton('➕', callback_data=f'addhangkey {i + 1}'),
                             InlineKeyboardButton('➖', callback_data=f'delhangkey {i + 1}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
        keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
        query.edit_message_text(text='自定义按钮', reply_markup=InlineKeyboardMarkup(keyboard))


def newrow(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    del_message(query.message)
    bot_id = context.bot.id
    maxrow = get_key.find_one({}, sort=[('Row', -1)])
    if maxrow is None:
        maxrow = 1
    else:
        maxrow = maxrow['Row'] + 1
    keybutton(maxrow, 1)
    keylist = list(get_key.find({}, sort=[('Row', 1), ('first', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    for i in keylist:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新建一行', callback_data='newrow'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除一行', callback_data='delrow'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整行排序', callback_data='paixurow')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}修改按钮', callback_data='newkey')])
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
    context.bot.send_message(chat_id=user_id, text='自定义按钮', reply_markup=InlineKeyboardMarkup(keyboard))


def close(update: Update, context: CallbackContext):
    query = update.callback_query
    chat = query.message.chat
    query.answer()
    yh_id = query.data.replace("close ", '')
    bot_id = context.bot.id
    chat_id = chat.id
    user_id = query.from_user.id

    user.update_one({'user_id': user_id}, {'$set': {'sign': 0}})
    safe_delete_message(context.bot, query.from_user.id, query.message.message_id, 'delete_close_panel_message')


def paixurow(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    keylist = list(get_key.find({}, sort=[('Row', 1), ('first', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    count = []
    for i in keylist:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
        count.append(row)
    if count == []:
        context.bot.send_message(chat_id=user_id, text='没有按钮存在')
    else:
        maxrow = max(count)
        if maxrow == 1:
            context.bot.send_message(chat_id=user_id, text='只有一行按钮无法调整')
        else:
            for i in range(0, maxrow):
                if i == 0:
                    keyboard.append(
                        [InlineKeyboardButton(f'{MOOD_EMOJI_FAST}第{i + 1}行下移', callback_data=f'paixuyidong xiayi:{i + 1}')])
                elif i == maxrow - 1:
                    keyboard.append(
                        [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}第{i + 1}行上移', callback_data=f'paixuyidong shangyi:{i + 1}')])
                else:
                    keyboard.append(
                        [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}第{i + 1}行上移', callback_data=f'paixuyidong shangyi:{i + 1}'),
                         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}第{i + 1}行下移', callback_data=f'paixuyidong xiayi:{i + 1}')])
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
            keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
            query.edit_message_text(text='自定义按钮', reply_markup=InlineKeyboardMarkup(keyboard))


def paixuyidong(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    qudata = query.data.replace('paixuyidong ', '')
    qudataall = qudata.split(':')
    yidongtype = qudataall[0]
    row = int(qudataall[1])
    if yidongtype == 'shangyi':
        get_key.update_many({"Row": row - 1}, {"$set": {'Row': 99}})
        get_key.update_many({"Row": row}, {"$set": {'Row': row - 1}})
        get_key.update_many({"Row": 99}, {"$set": {'Row': row}})
    else:
        get_key.update_many({"Row": row + 1}, {"$set": {'Row': 99}})
        get_key.update_many({"Row": row}, {"$set": {'Row': row + 1}})
        get_key.update_many({"Row": 99}, {"$set": {'Row': row}})
    keylist = list(get_key.find({}, sort=[('Row', 1), ('first', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    for i in keylist:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新建一行', callback_data='newrow'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除一行', callback_data='delrow'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整行排序', callback_data='paixurow')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}修改按钮', callback_data='newkey')])
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
    query.edit_message_text(text='自定义按钮', reply_markup=InlineKeyboardMarkup(keyboard))


def delrow(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    keylist = list(get_key.find({}, sort=[('Row', 1), ('first', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    count = []
    for i in keylist:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
        count.append(row)
    if count == []:
        context.bot.send_message(chat_id=user_id, text='没有按钮存在')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除第{i + 1}行', callback_data=f'qrscdelrow {i + 1}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
        keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
        query.edit_message_text(text='自定义按钮', reply_markup=InlineKeyboardMarkup(keyboard))


def qrscdelrow(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    del_message(query.message)
    row = int(query.data.replace('qrscdelrow ', ''))
    bot_id = context.bot.id
    get_key.delete_many({"Row": row})
    max_list = list(get_key.find({'Row': {"$gt": row}}))
    for i in max_list:
        max_row = i['Row']
        get_key.update_many({'Row': max_row}, {"$set": {"Row": max_row - 1}})
    maxrow = get_key.find_one({}, sort=[('Row', -1)])
    if maxrow is None:
        maxrow = 1
    else:
        maxrow = maxrow['Row'] + 1
    # keybutton(maxrow,1)
    keylist = list(get_key.find({}, sort=[('Row', 1), ('first', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    for i in keylist:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新建一行', callback_data='newrow'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除一行', callback_data='delrow'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整行排序', callback_data='paixurow')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}修改按钮', callback_data='newkey')])
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
    context.bot.send_message(chat_id=user_id,text='自定义按钮', reply_markup=InlineKeyboardMarkup(keyboard))


def delhangkey(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    row = int(query.data.replace('delhangkey ', ''))
    bot_id = context.bot.id
    key_list = list(get_key.find({'Row': row}, sort=[('first', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    count = []
    for i in key_list:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
        count.append(row)
    if count == []:
        context.bot.send_message(chat_id=user_id, text='没有按钮存在')
    else:

        # maxrow = max(count)
        for i in range(0, len(count)):
            keyboard[count[i]].append(InlineKeyboardButton('➖', callback_data=f'qrdelliekey {row}:{i + 1}'))
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
        keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
        query.edit_message_text(text='自定义按钮', reply_markup=InlineKeyboardMarkup(keyboard))


def keyxq(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    qudata = query.data.replace('keyxq ', '')
    qudataall = qudata.split(':')
    row = int(qudataall[0])
    first = int(qudataall[1])
    key_list = get_key.find_one({'Row': row, 'first': first})
    projectname = key_list['projectname']
    text = key_list['text']
    print_text = f'''
这是第{row}行第{first}个按钮

按钮名称: {projectname}
    '''

    keyboard = [
        [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}图文设置', callback_data=f'settuwenset {row}:{first}'),
         InlineKeyboardButton(f'{MOOD_EMOJI_STAR}查看图文设置', callback_data=f'cattuwenset {row}:{first}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}修改尾随按钮', callback_data=f'setkeyboard {row}:{first}'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}修改按钮名字', callback_data=f'setkeyname {row}:{first}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')]
    ]

    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
    query.edit_message_text(text=print_text, reply_markup=InlineKeyboardMarkup(keyboard))


def setkeyname(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    qudata = query.data.replace('setkeyname ', '')
    qudataall = qudata.split(':')
    row = int(qudataall[0])
    first = int(qudataall[1])
    text = f'''
输入要修改的名字

颜色前缀：
#r = 红色
#g = 绿色
#b = 蓝色

例如：
#r 😃商品列表
#g 👤个人中心
#b 💸我要充值
    '''
    user.update_one({'user_id': user_id}, {"$set": {"sign": f'setkeyname {row}:{first}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')]]
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
    query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def setkeyboard(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    qudata = query.data.replace('setkeyboard ', '')
    qudataall = qudata.split(':')
    row = int(qudataall[0])
    first = int(qudataall[1])
    text = f'''
按以下格式设置按钮，填入◈之间，同一行用 | 隔开
按钮名称&https://t.me/... | 按钮名称&https://t.me/...
按钮名称&https://t.me/... | 按钮名称&https://t.me/... | 按钮名称&https://t.me/....
    '''
    key_list = get_key.find_one({'Row': row, 'first': first})
    key_text = key_list['key_text']
    if key_text != '':
        context.bot.send_message(chat_id=user_id, text=key_text)
    user.update_one({'user_id': user_id}, {"$set": {"sign": f'setkeyboard {row}:{first}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')]]
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data=f'backstart')])
    query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def settuwenset(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    qudata = query.data.replace('settuwenset ', '')
    qudataall = qudata.split(':')
    row = int(qudataall[0])
    first = int(qudataall[1])
    key_list = get_key.find_one({'Row': row, 'first': first})
    key_text = key_list['key_text']
    text = key_list['text']
    file_type = key_list['file_type']
    file_id = key_list['file_id']
    entities = safe_pickle_loads(key_list['entities'])
    keyboard = load_saved_inline_keyboard(key_list.get('keyboard'), key_list.get('key_text'))
    if text == '' and file_id == '':
        pass
    else:
        send_key_content_preview(context, user_id, text=text, file_type=file_type, file_id=file_id,
                                 entities=entities, keyboard=keyboard)
    text = f'''
✍️ 发送你的图文设置

文字、视频、图片、gif、图文
    '''
    user.update_one({'user_id': user_id}, {"$set": {"sign": f'settuwenset {row}:{first}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def cattuwenset(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    qudata = query.data.replace('cattuwenset ', '')
    qudataall = qudata.split(':')
    row = int(qudataall[0])
    first = int(qudataall[1])
    key_list = get_key.find_one({'Row': row, 'first': first})
    key_text = key_list['key_text']
    text = key_list['text']
    file_type = key_list['file_type']
    file_id = key_list['file_id']
    entities = safe_pickle_loads(key_list['entities'])
    keyboard = load_saved_inline_keyboard(key_list.get('keyboard'), key_list.get('key_text'))
    if text == '' and file_id == '':
        message_id = context.bot.send_message(chat_id=user_id, text='请设置图文后点击')
        timer11 = Timer(3, del_message, args=[message_id])
        timer11.start()
    else:
        message_id = send_key_content_preview(context, user_id, text=text, file_type=file_type, file_id=file_id,
                                              entities=entities, keyboard=keyboard)
        timer11 = Timer(3, del_message, args=[message_id])
        timer11.start()


def qrdelliekey(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    qudata = query.data.replace('qrdelliekey ', '')
    qudataall = qudata.split(':')
    row = int(qudataall[0])
    first = int(qudataall[1])
    get_key.delete_one({"Row": row, 'first': first})
    max_list = list(get_key.find({'Row': row, 'first': {"$gt": first}}))
    for i in max_list:
        max_lie = i['first']
        get_key.update_one({'Row': row, 'first': max_lie}, {"$set": {"first": max_lie - 1}})

    keylist = list(get_key.find({}, sort=[('Row', 1), ('first', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    count = []
    for i in keylist:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
        count.append(row)
    if count == []:
        context.bot.send_message(chat_id=user_id, text='请先新建一行')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_STAR}第{i + 1}行', callback_data=f'dddd'),
                             InlineKeyboardButton('➕', callback_data=f'addhangkey {i + 1}'),
                             InlineKeyboardButton('➖', callback_data=f'delhangkey {i + 1}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
        context.bot.send_message(chat_id=user_id, text='自定义按钮', reply_markup=InlineKeyboardMarkup(keyboard))


def addhangkey(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    del_message(query.message)
    row = int(query.data.replace('addhangkey ', ''))
    bot_id = context.bot.id
    lie = get_key.find_one({'Row': row}, sort=[('first', -1)])['first']
    keybutton(row, lie + 1)

    keylist = list(get_key.find({}, sort=[('Row', 1), ('first', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    count = []
    for i in keylist:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
        count.append(row)
    if count == []:
        context.bot.send_message(chat_id=user_id, text='请先新建一行')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_STAR}第{i + 1}行', callback_data=f'dddd'),
                             InlineKeyboardButton('➕', callback_data=f'addhangkey {i + 1}'),
                             InlineKeyboardButton('➖', callback_data=f'delhangkey {i + 1}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
        context.bot.send_message(chat_id=user_id, text='自定义按钮', reply_markup=InlineKeyboardMarkup(keyboard))


def settrc20(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    text = f'''
请输入以 T 开头、共 34 位的 TRC20-USDT 收款地址
'''
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'settrc20'}})
    context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def restockpushcfg(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    context.bot.send_message(
        chat_id=user_id,
        text=build_restock_push_config_text(),
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(build_restock_push_config_keyboard(user_id))
    )


def build_buy_notice_config_text():
    notice_text = get_buy_notice_text('')
    return (
        f'{ADMIN_EMOJI_BUY_NOTICE}购买提醒配置\n\n'
        '[emoji:5217818964612108191:✨] 支持 HTML 和会员 emoji\n'
        '[emoji:5220064167356025824:⭐️] 当前文案预览如下：'
    ), notice_text


def build_buy_notice_config_keyboard(user_id):
    return [
        [InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}修改购买提醒', callback_data='setbuynotice')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}'),
         InlineKeyboardButton('返回后台', callback_data='backstart')]
    ]


def buynoticecfg(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    title_text, notice_text = build_buy_notice_config_text()
    context.bot.send_message(chat_id=user_id, text=title_text, parse_mode='HTML')
    context.bot.send_message(
        chat_id=user_id,
        text=notice_text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(build_buy_notice_config_keyboard(user_id))
    )


def setbuynotice(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    user.update_one({'user_id': user_id}, {'$set': {'sign': 'setbuynotice'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')]]
    context.bot.send_message(
        chat_id=user_id,
        text='请直接发送新的购买提醒文案\n\n支持 HTML，也支持会员 emoji。',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def setrestocktarget(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    user.update_one({'user_id': user_id}, {'$set': {'sign': 'setrestocktarget'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    context.bot.send_message(
        chat_id=user_id,
        text='请发送补货通知要推送到的群组/频道\n\n例如：@yourchannel 或 -1001234567890',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def build_okpay_config_text():
    shop_id = get_okpay_shop_id()
    token = get_okpay_shop_token()
    name = get_okpay_name()
    enabled = '已开启' if refresh_okpay_entry_status() else '未开启'
    masked_token = (token[:6] + '******' + token[-4:]) if len(token) >= 12 else ('已设置' if token else '未设置')
    return f'''
<b>OKPay 当前配置</b>

商户ID：<code>{shop_id or '未设置'}</code>
Token：<code>{masked_token}</code>
名称：<code>{name or '未设置'}</code>
充值入口：<code>{enabled}</code>

当 商户ID / Token / 名称 三项都配置完成后，会自动开启 OKPay 充值入口。
'''


def build_okpay_config_keyboard(user_id):
    return [
        [InlineKeyboardButton(f'{ADMIN_EMOJI_OKPAY}设置商户ID', callback_data='setokpayid'), InlineKeyboardButton(f'{MOOD_EMOJI_STAR}设置Token', callback_data='setokpaytoken')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}设置名称', callback_data='setokpayname')],
        [InlineKeyboardButton('⬅️返回主界面', callback_data='backstart')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')]
    ]


def okpaycfg(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    keyboard = build_okpay_config_keyboard(user_id)
    query.edit_message_text(text=build_okpay_config_text(), parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def setokpayid(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'setokpayid'}})
    context.bot.send_message(chat_id=user_id, text='请输入 OKPay 商户ID', reply_markup=InlineKeyboardMarkup(keyboard))


def setokpaytoken(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'setokpaytoken'}})
    context.bot.send_message(chat_id=user_id, text='请输入 OKPay Token', reply_markup=InlineKeyboardMarkup(keyboard))


def setokpayname(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'setokpayname'}})
    context.bot.send_message(chat_id=user_id, text='请输入 OKPay 名称（例如：号铺）', reply_markup=InlineKeyboardMarkup(keyboard))


def can_use_clonebot(state):
    if not BOT_CLONE_ENABLED:
        return False
    return ALLOW_PUBLIC_BOT_CLONE or str(state) == '4'


def get_clone_unavailable_text(user_id):
    return 'Clone This Bot is currently unavailable' if get_user_lang(user_id) == 'en' else '当前未开放一键克隆功能'


def get_clone_purchase_button_text(user_id, fee):
    return f'{ADMIN_EMOJI_OKPAY}Pay {format_clone_price(fee)} USDT and Continue' if get_user_lang(user_id) == 'en' else f'{ADMIN_EMOJI_OKPAY}支付 {format_clone_price(fee)} USDT 并继续'


def get_clone_recharge_button_text(user_id):
    return f'{ADMIN_EMOJI_OKPAY}Recharge First' if get_user_lang(user_id) == 'en' else f'{ADMIN_EMOJI_OKPAY}余额不足，先去充值'


def get_clone_cancel_text(user_id):
    return f'{ADMIN_EMOJI_CLOSE}Cancel' if get_user_lang(user_id) == 'en' else f'{ADMIN_EMOJI_CLOSE}取消'


def build_clone_purchase_prompt_text(user_id, fee, balance):
    if get_user_lang(user_id) == 'en':
        return f'''
[emoji:5445353829304387411:💳] Clone This Bot is in paid mode

[emoji:4965219701572503640:💰] Clone Price: <code>{format_clone_price(fee)} USDT</code>
[emoji:4972482444025398275:👛] Current Balance: <code>{format_clone_price(balance)} USDT</code>

[emoji:5301246586918024418:⚠️] Payment must be completed before you can continue sending the new Bot Token.
        '''
    return f'''
[emoji:5445353829304387411:💳] 当前一键克隆为付费模式

[emoji:4965219701572503640:💰] 克隆价格：<code>{format_clone_price(fee)} USDT</code>
[emoji:4972482444025398275:👛] 当前余额：<code>{format_clone_price(balance)} USDT</code>

[emoji:5301246586918024418:⚠️] 支付成功后，才能继续发送新 Bot Token 进行克隆。
        '''


def build_clone_token_prompt_text(user_id):
    if get_user_lang(user_id) == 'en':
        return '''
[emoji:5287684458881756303:🤖] Please send the new Bot Token you want to clone

[emoji:5217818964612108191:✨] Example:
123456789:ABCdefGhIJKlmNoPQRsTUVwxyz123456789

[emoji:5220195537520711716:⚡️] The current user will be set as the new Bot admin by default, and the new Bot will be started automatically.
'''
    return '''
[emoji:5287684458881756303:🤖] 请发送你要克隆的新 Bot Token

[emoji:5217818964612108191:✨] 例如：
123456789:ABCdefGhIJKlmNoPQRsTUVwxyz123456789

[emoji:5220195537520711716:⚡️] 默认会把当前操作用户设为新 Bot 管理员，并自动拉起新 Bot。
'''


def build_clone_balance_shortage_text(user_id, fee, balance):
    if get_user_lang(user_id) == 'en':
        return f'Insufficient balance. You need to pay {format_clone_price(fee)} USDT, and your current balance is {format_clone_price(balance)} USDT.'
    return f'余额不足，当前需支付 {format_clone_price(fee)} USDT，您现在余额为 {format_clone_price(balance)} USDT。'


def build_clone_purchase_keyboard(user_id, user_balance, fee):
    keyboard = []
    if Decimal(str(user_balance)) >= fee:
        keyboard.append([InlineKeyboardButton(get_clone_purchase_button_text(user_id, fee), callback_data='clonepay')])
    else:
        keyboard.append([InlineKeyboardButton(get_clone_recharge_button_text(user_id), callback_data='recharge_menu')])
    keyboard.append([InlineKeyboardButton(get_clone_cancel_text(user_id), callback_data=f'close {user_id}')])
    return keyboard


def send_clonebot_prompt(context, user_id):
    user_list = user.find_one({'user_id': user_id}) or {}
    state = user_list.get('state')
    if not can_use_clonebot(state):
        context.bot.send_message(chat_id=user_id, text=get_clone_unavailable_text(user_id))
        return
    fee = get_clone_price_decimal()
    clone_credit = get_user_clone_credit(user_id)
    if fee > 0 and not is_clone_fee_exempt(user_id, state) and clone_credit <= 0:
        balance = Decimal(str(user_list.get('USDT', 0) or 0)).quantize(Decimal('0.01'))
        text = build_clone_purchase_prompt_text(user_id, fee, balance)
        keyboard = build_clone_purchase_keyboard(user_id, balance, fee)
        context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        return
    text = build_clone_token_prompt_text(user_id)
    keyboard = [[InlineKeyboardButton(get_clone_cancel_text(user_id), callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'clonebottoken'}})
    context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def send_cloneagent_prompt(context, user_id):
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消输入', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'agentbottoken'}})
    context.bot.send_message(
        chat_id=user_id,
        text='[emoji:5287684458881756303:🤖] 请发送新的代理 Bot Token\n\n[emoji:5220195537520711716:⚡️] 系统会自动克隆 agent_service、写入配置、注册 systemd 并直接启动。',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def clonepay(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    user_list = user.find_one({'user_id': user_id}) or {}
    state = user_list.get('state')
    if not can_use_clonebot(state):
        context.bot.send_message(chat_id=user_id, text=get_clone_unavailable_text(user_id))
        return
    fee = get_clone_price_decimal()
    if fee <= 0 or is_clone_fee_exempt(user_id, state):
        send_clonebot_prompt(context, user_id)
        return

    balance = Decimal(str(user_list.get('USDT', 0) or 0)).quantize(Decimal('0.01'))
    if balance < fee:
        text = build_clone_balance_shortage_text(user_id, fee, balance)
        keyboard = build_clone_purchase_keyboard(user_id, balance, fee)
        try:
            query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    order_id = 'CLONEPAY' + time.strftime('%Y%m%d%H%M%S', time.localtime()) + str(user_id)
    new_balance = (balance - fee).quantize(Decimal('0.01'))
    user.update_one({'user_id': user_id}, {'$set': {'USDT': float(new_balance)}, '$inc': {'clone_credit': 1}})
    user_logging(order_id, '克隆同款付费', user_id, float(fee), time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
    send_clonebot_prompt(context, user_id)


def clonebot(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='Clone This Bot is disabled on this bot' if get_user_lang(user_id) == 'en' else '当前机器人未开放克隆功能')
        return
    send_clonebot_prompt(context, user_id)


def cloneagent(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有后台管理员可以创建代理 Bot')
        return
    send_cloneagent_prompt(context, user_id)


def clonelist(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='当前机器人未开放克隆管理')
        return
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有源机器人管理员可以查看克隆列表')
        return
    data = str(query.data or '').replace('clonelist', '', 1).strip()
    try:
        page = max(int(data), 0) if data else 0
    except Exception:
        page = 0
    keyboard, total = build_clone_list_keyboard(user_id, page)
    price_text = format_clone_price()
    text = f'''
<b>[emoji:5287684458881756303:🤖] 克隆列表</b>

当前付费价格：<code>{price_text} USDT</code>
活跃克隆数：<code>{total}</code>

点下面机器人可查看详情或删除。
    '''
    try:
        query.edit_message_text(text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def agentlist(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有源机器人管理员可以查看代理列表')
        return
    data = str(query.data or '').replace('agentlist', '', 1).strip()
    try:
        page = max(int(data), 0) if data else 0
    except Exception:
        page = 0
    keyboard, total = build_agent_list_keyboard(user_id, page)
    text = f'''
<b>{ADMIN_EMOJI_CLONE_LIST}代理列表</b>

活跃代理数：<code>{total}</code>

点下面代理可查看销售数据、用户列表、重启或删除。
    '''
    try:
        query.edit_message_text(text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def agentinfo(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有源机器人管理员可以查看代理详情')
        return
    bot_id = str(query.data.replace('agentinfo ', '', 1)).strip()
    row = clone_instances.find_one({'bot_id': bot_id, 'clone_kind': 'agent', 'state': {'$ne': 'deleted'}})
    if row is None:
        context.bot.send_message(chat_id=user_id, text='未找到这个代理实例')
        return
    runtime = agent_bots.find_one({'agent_bot_id': bot_id}) or {}
    stats = build_agent_runtime_stats(bot_id)
    requester_user_id = row.get('requester_user_id')
    requester_name = str(row.get('requester_name') or requester_user_id or '')
    requester_username = str(row.get('requester_username') or '').strip()
    service_name = str(row.get('service_name') or '').strip()
    service_state = get_systemd_unit_state(f'{service_name}.service') if service_name else 'unknown'
    text = f'''
<b>{ADMIN_EMOJI_CLONE}代理详情</b>

机器人：@{row.get('bot_username')}
代理ID：<code>{bot_id}</code>
管理员：<code>{requester_user_id}</code>
用户：{requester_name} @{requester_username}
创建时间：<code>{row.get('created_at', '')}</code>
服务：<code>{service_name}.service</code>
状态：<code>{service_state}</code>

累计销售额：<code>{standard_num(stats.get('total_spent', 0))} USDT</code>
累计销售件数：<code>{stats.get('total_items', 0)}</code>
订单数：<code>{stats.get('order_count', 0)}</code>
发货完成：<code>{stats.get('delivered_count', 0)}</code>
部分退款：<code>{stats.get('partial_refund_count', 0)}</code>
全额退款：<code>{stats.get('refunded_count', 0)}</code>
用户数：<code>{stats.get('user_count', 0)}</code>
代理总余额：<code>{standard_num(stats.get('total_balance', 0))} USDT</code>
待充值：<code>{stats.get('pending_topups', 0)}</code>
已到账：<code>{stats.get('paid_topups', 0)}</code>
待提现：<code>{stats.get('pending_withdrawals', 0)}</code>
已打款：<code>{stats.get('paid_withdrawals', 0)}</code>

目录：<code>{row.get('clone_dir', '')}</code>
客服：<code>{runtime.get('customer_service', '')}</code>
    '''
    keyboard = [
        [InlineKeyboardButton(f'{ADMIN_EMOJI_USERLIST}查看代理用户列表', callback_data=f'agentusers {bot_id}:0')],
        [InlineKeyboardButton(f'{MOOD_EMOJI_FAST}重启代理机器人', callback_data=f'agentrestart {bot_id}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除代理机器人', callback_data=f'agentdelete {bot_id}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE_LIST}返回代理列表', callback_data='agentlist 0')],
    ]
    try:
        query.edit_message_text(text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def agentusers(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有源机器人管理员可以查看代理用户列表')
        return
    data = str(query.data.replace('agentusers ', '', 1)).strip()
    bot_id, _, page_text = data.partition(':')
    try:
        page = max(int(page_text or '0'), 0)
    except Exception:
        page = 0
    text, total = build_agent_users_text(bot_id, page)
    keyboard = build_agent_users_keyboard(user_id, bot_id, page, total)
    try:
        query.edit_message_text(text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def cloneinfo(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='当前机器人未开放克隆管理')
        return
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有源机器人管理员可以查看克隆详情')
        return
    bot_id = str(query.data.replace('cloneinfo ', '', 1)).strip()
    row = clone_instances.find_one({'bot_id': bot_id})
    if row is None:
        context.bot.send_message(chat_id=user_id, text='未找到这个克隆实例')
        return
    requester_user_id = row.get('requester_user_id')
    requester_name = str(row.get('requester_name') or requester_user_id or '')
    requester_username = str(row.get('requester_username') or '').strip()
    text = f'''
<b>[emoji:5287684458881756303:🤖] 克隆详情</b>

机器人：@{row.get('bot_username')}
管理员：<code>{requester_user_id}</code>
用户：{requester_name} @{requester_username}
支付金额：<code>{format_clone_price(row.get('fee_paid', 0))} USDT</code>
创建时间：<code>{row.get('created_at', '')}</code>

目录：<code>{row.get('clone_dir', '')}</code>
数据库：<code>{row.get('db_name', '')}</code>
Bot服务：<code>{row.get('service_name', '')}.service</code>
监听服务：<code>{row.get('listener_service_name', '')}.service</code>
    '''
    keyboard = [
        [InlineKeyboardButton(f'{MOOD_EMOJI_FAST}重启这个克隆', callback_data=f'clonerestart {bot_id}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除这个克隆', callback_data=f'clonedelete {bot_id}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}返回克隆列表', callback_data='clonelist 0')]
    ]
    try:
        query.edit_message_text(text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def clonedelete(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    try:
        query.answer('正在删除，请稍候...')
    except Exception:
        pass
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='当前机器人未开放克隆管理')
        return
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有源机器人管理员可以删除克隆实例')
        return
    bot_id = str(query.data.replace('clonedelete ', '', 1)).strip()
    preview_record = clone_instances.find_one({'bot_id': bot_id, 'state': {'$ne': 'deleted'}}) or {}
    if not preview_record:
        context.bot.send_message(chat_id=user_id, text='未找到这个克隆实例，可能已经删除了')
        return
    if str(preview_record.get('state') or '') == 'deleting':
        context.bot.send_message(chat_id=user_id, text='这个克隆实例正在删除中，请稍候查看结果')
        return
    bot_username = str(preview_record.get('bot_username') or '').strip()
    claimed = clone_instances.update_one(
        {'_id': preview_record['_id'], 'state': {'$nin': ['deleted', 'deleting']}},
        {'$set': {'state': 'deleting', 'deleting_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}}
    )
    if claimed.modified_count == 0:
        context.bot.send_message(chat_id=user_id, text='这个克隆实例正在删除中，请稍候查看结果')
        return
    waiting_text = f'[emoji:5220195537520711716:⚡️] 正在删除克隆实例，请稍候…\n\n[emoji:5287684458881756303:🤖] 机器人：@{bot_username}' if bot_username else f'[emoji:5220195537520711716:⚡️] 正在删除克隆实例，请稍候…\n\n[emoji:5287684458881756303:🤖] 机器人：<code>{bot_id}</code>'
    try:
        query.edit_message_text(text=waiting_text, parse_mode='HTML')
    except Exception:
        pass
    threading.Thread(
        target=finish_clone_delete_in_background,
        args=(context, user_id, bot_id, context.bot.id, MONGO_DB_NAME),
        daemon=True
    ).start()


def finish_clone_restart_in_background(context, user_id, bot_id):
    row = clone_instances.find_one({'bot_id': str(bot_id), 'state': {'$ne': 'deleted'}}) or {}
    if not row:
        try:
            context.bot.send_message(chat_id=user_id, text='未找到这个克隆实例，可能已经删除了')
        except Exception:
            pass
        return

    service_name = str(row.get('service_name') or '').strip()
    listener_service_name = str(row.get('listener_service_name') or '').strip()
    bot_username = str(row.get('bot_username') or '').strip()
    display_bot = f'@{bot_username}' if bot_username else str(bot_id)
    try:
        if not service_name:
            raise RuntimeError('未找到 Bot 服务名')
        refresh_clone_service_files(row)
        restart_systemd_unit(f'{service_name}.service', label='克隆 Bot 服务', wait_seconds=120)
        if listener_service_name:
            restart_systemd_unit(f'{listener_service_name}.service', label='监听服务', wait_seconds=120)
    except Exception as exc:
        try:
            context.bot.send_message(chat_id=user_id, text=f'重启克隆失败：{exc}')
        except Exception:
            pass
        return

    text = f'[emoji:5312028599803460968:🆗] 已重启克隆实例\n\n[emoji:5287684458881756303:🤖] 机器人：{display_bot}'
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}返回克隆详情', callback_data=f'cloneinfo {bot_id}')]])
    try:
        context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
    except Exception:
        pass


def clonerestart(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    try:
        query.answer('正在重启，请稍候...')
    except Exception:
        pass
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='当前机器人未开放克隆管理')
        return
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有源机器人管理员可以重启克隆实例')
        return
    bot_id = str(query.data.replace('clonerestart ', '', 1)).strip()
    row = clone_instances.find_one({'bot_id': bot_id, 'state': {'$ne': 'deleted'}}) or {}
    if not row:
        context.bot.send_message(chat_id=user_id, text='未找到这个克隆实例，可能已经删除了')
        return
    bot_username = str(row.get('bot_username') or '').strip()
    waiting_text = f'[emoji:5220195537520711716:⚡️] 正在重启克隆实例，请稍候…\n\n[emoji:5287684458881756303:🤖] 机器人：@{bot_username}' if bot_username else f'[emoji:5220195537520711716:⚡️] 正在重启克隆实例，请稍候…\n\n[emoji:5287684458881756303:🤖] 机器人：<code>{bot_id}</code>'
    try:
        query.edit_message_text(text=waiting_text, parse_mode='HTML')
    except Exception:
        pass
    threading.Thread(target=finish_clone_restart_in_background, args=(context, user_id, bot_id), daemon=True).start()


def finish_agent_restart_in_background(context, user_id, bot_id):
    row = clone_instances.find_one({'bot_id': str(bot_id), 'clone_kind': 'agent', 'state': {'$ne': 'deleted'}}) or {}
    if not row:
        try:
            context.bot.send_message(chat_id=user_id, text='未找到这个代理实例，可能已经删除了')
        except Exception:
            pass
        return
    service_name = str(row.get('service_name') or '').strip()
    bot_username = str(row.get('bot_username') or '').strip()
    clone_dir = Path(str(row.get('clone_dir') or '').strip())
    display_bot = f'@{bot_username}' if bot_username else str(bot_id)
    try:
        if not service_name:
            raise RuntimeError('未找到代理服务名')
        sync_clone_repo_code(clone_dir)
        refresh_clone_service_files(row)
        restart_systemd_unit(f'{service_name}.service', label='代理 Bot 服务', wait_seconds=120)
        final_state = get_systemd_unit_state(f'{service_name}.service')
    except Exception as exc:
        try:
            context.bot.send_message(chat_id=user_id, text=f'重启代理失败：{exc}')
        except Exception:
            pass
        return
    text = (
        f'[emoji:5312028599803460968:🆗] 已重启代理机器人\n\n'
        f'[emoji:5287684458881756303:🤖] 机器人：{display_bot}\n'
        f'[emoji:5954227490179255253:🔵] 服务：{service_name}.service\n'
        f'[emoji:5217818964612108191:✨] 当前状态：{final_state}'
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE_LIST}返回代理详情', callback_data=f'agentinfo {bot_id}')]])
    try:
        context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
    except Exception:
        pass


def agentrestart(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    try:
        query.answer('正在重启代理，请稍候...')
    except Exception:
        pass
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有源机器人管理员可以重启代理实例')
        return
    bot_id = str(query.data.replace('agentrestart ', '', 1)).strip()
    row = clone_instances.find_one({'bot_id': bot_id, 'clone_kind': 'agent', 'state': {'$ne': 'deleted'}}) or {}
    if not row:
        context.bot.send_message(chat_id=user_id, text='未找到这个代理实例，可能已经删除了')
        return
    bot_username = str(row.get('bot_username') or '').strip()
    waiting_text = f'[emoji:5220195537520711716:⚡️] 正在重启代理机器人，请稍候…\n\n[emoji:5287684458881756303:🤖] 机器人：@{bot_username}' if bot_username else f'[emoji:5220195537520711716:⚡️] 正在重启代理机器人，请稍候…\n\n[emoji:5287684458881756303:🤖] 机器人：<code>{bot_id}</code>'
    try:
        query.edit_message_text(text=waiting_text, parse_mode='HTML')
    except Exception:
        pass
    threading.Thread(target=finish_agent_restart_in_background, args=(context, user_id, bot_id), daemon=True).start()


def remove_agent_instance(bot_id):
    record = clone_instances.find_one({'bot_id': str(bot_id), 'clone_kind': 'agent', 'state': {'$ne': 'deleted'}})
    if record is None:
        raise RuntimeError('未找到这个代理实例')

    for service_name in [record.get('service_name'), record.get('listener_service_name')]:
        if not service_name:
            continue
        service_unit = f'{service_name}.service'
        try:
            run_system_command(['systemctl', 'disable', '--now', service_unit], timeout=25)
        except Exception:
            try:
                run_system_command(['systemctl', 'stop', service_unit], timeout=20)
            except Exception:
                pass
        service_path = Path('/etc/systemd/system') / service_unit
        try:
            if service_path.exists():
                service_path.unlink()
        except Exception:
            pass
        try:
            run_system_command(['systemctl', 'reset-failed', service_unit], timeout=15)
        except Exception:
            pass
    try:
        run_system_command(['systemctl', 'daemon-reload'], timeout=20)
    except Exception:
        pass

    clone_dir = str(record.get('clone_dir') or '').strip()
    if clone_dir:
        shutil.rmtree(clone_dir, ignore_errors=True)

    tenant_id = normalize_tenant_id(record.get('bot_id'))
    agent_bots.delete_one({'agent_bot_id': tenant_id})
    agent_product_prices.delete_many({'agent_bot_id': tenant_id})
    agent_orders.delete_many({'agent_bot_id': tenant_id})
    agent_withdrawals.delete_many({'agent_bot_id': tenant_id})
    tenant_orders.delete_many({'tenant_id': tenant_id})
    topup_orders.delete_many({'tenant_id': tenant_id})
    refund_records.delete_many({'tenant_id': tenant_id})
    settlement_ledger.delete_many({'tenant_id': tenant_id})
    tenant_wallets.delete_many({'tenant_id': tenant_id})
    wallet_ledger.delete_many({'tenant_id': tenant_id})
    tenant_products.delete_many({'tenant_id': tenant_id})
    tenant_users.delete_many({'tenant_id': tenant_id})
    for coll in [get_agent_bot_user_collection(tenant_id), get_agent_bot_topup_collection(tenant_id), get_agent_bot_gmjlu_collection(tenant_id)]:
        try:
            coll.drop()
        except Exception:
            pass

    clone_instances.delete_one({'_id': record['_id']})
    return record


def finish_agent_delete_in_background(context, user_id, bot_id):
    try:
        record = remove_agent_instance(bot_id)
    except Exception as exc:
        clone_instances.update_one(
            {'bot_id': str(bot_id), 'clone_kind': 'agent', 'state': 'deleting'},
            {'$set': {'state': 'active'}, '$unset': {'deleting_at': ''}}
        )
        try:
            context.bot.send_message(chat_id=user_id, text=f'删除代理失败：{exc}')
        except Exception:
            pass
        return
    requester_user_id = record.get('requester_user_id')
    bot_username = str(record.get('bot_username') or '').strip()
    display_bot = f'@{bot_username}' if bot_username else str(record.get('bot_id'))
    text = f'[emoji:5312028599803460968:🆗] 已删除代理机器人\n\n[emoji:5287684458881756303:🤖] 机器人：{display_bot}\n[emoji:6321041414067068140:👤] 管理员：{requester_user_id}'
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE_LIST}返回代理列表', callback_data='agentlist 0')]])
    try:
        context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
    except Exception:
        pass


def agentdelete(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    try:
        query.answer('正在删除代理，请稍候...')
    except Exception:
        pass
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有源机器人管理员可以删除代理实例')
        return
    bot_id = str(query.data.replace('agentdelete ', '', 1)).strip()
    preview_record = clone_instances.find_one({'bot_id': bot_id, 'clone_kind': 'agent', 'state': {'$ne': 'deleted'}}) or {}
    if not preview_record:
        context.bot.send_message(chat_id=user_id, text='未找到这个代理实例，可能已经删除了')
        return
    if str(preview_record.get('state') or '') == 'deleting':
        context.bot.send_message(chat_id=user_id, text='这个代理实例正在删除中，请稍候查看结果')
        return
    bot_username = str(preview_record.get('bot_username') or '').strip()
    claimed = clone_instances.update_one(
        {'_id': preview_record['_id'], 'state': {'$nin': ['deleted', 'deleting']}},
        {'$set': {'state': 'deleting', 'deleting_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}}
    )
    if claimed.modified_count == 0:
        context.bot.send_message(chat_id=user_id, text='这个代理实例正在删除中，请稍候查看结果')
        return
    waiting_text = f'[emoji:5220195537520711716:⚡️] 正在删除代理机器人，请稍候…\n\n[emoji:5287684458881756303:🤖] 机器人：@{bot_username}' if bot_username else f'[emoji:5220195537520711716:⚡️] 正在删除代理机器人，请稍候…\n\n[emoji:5287684458881756303:🤖] 机器人：<code>{bot_id}</code>'
    try:
        query.edit_message_text(text=waiting_text, parse_mode='HTML')
    except Exception:
        pass
    threading.Thread(target=finish_agent_delete_in_background, args=(context, user_id, bot_id), daemon=True).start()


def setcloneprice(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='当前机器人未开放克隆管理')
        return
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='只有源机器人管理员可以设置克隆价格')
        return
    text = f'请输入一键克隆价格（USDT）\n\n当前价格：{format_clone_price()}\n输入 0 表示免费。'
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {'$set': {'sign': 'setcloneprice'}})
    context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def startupdate(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    data = str(query.data or '').strip()

    if data == 'startupdate_zh':
        user.update_one({'user_id': user_id}, {"$set": {"sign": 'startupdate_zh'}})
        text = '请输入新的中文欢迎语'
        keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
        context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == 'startupdate_en':
        user.update_one({'user_id': user_id}, {"$set": {"sign": 'startupdate_en'}})
        text = '请输入新的英文欢迎语'
        keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
        context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    text = '请选择要修改的欢迎语版本：'
    keyboard = [
        [InlineKeyboardButton('🇨🇳 中文欢迎语', callback_data='startupdate_zh'),
         InlineKeyboardButton('🇺🇸 英文欢迎语', callback_data='startupdate_en')],
        [InlineKeyboardButton('⬅️返回主界面', callback_data='backstart')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]
    ]
    query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def build_recharge_method_keyboard(user_id):
    keyboard = []
    if SHOW_TRC20_RECHARGE_ENTRY:
        keyboard.append([InlineKeyboardButton(get_ui_text('recharge_trc20_button', viewer_user_id=user_id), callback_data='recharge_trc20')])
    if okpay_entry_enabled():
        keyboard.append([InlineKeyboardButton(get_ui_text('recharge_okpay_button', viewer_user_id=user_id), callback_data='recharge_okpay')])
    keyboard.append([InlineKeyboardButton(get_ui_text('cancel_recharge', viewer_user_id=user_id), callback_data=f'close {user_id}')])
    return keyboard


def send_recharge_method_menu(context, user_id):
    if not SHOW_TRC20_RECHARGE_ENTRY and not okpay_entry_enabled():
        context.bot.send_message(chat_id=user_id, text=get_ui_text('recharge_method_unavailable', viewer_user_id=user_id))
        return
    fstext = get_ui_text('recharge_method_title', viewer_user_id=user_id)
    keyboard = build_recharge_method_keyboard(user_id)
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def recharge_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    send_recharge_method_menu(context, user_id)


def zdycz(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    text = get_ui_text('enter_custom_trc20_amount', viewer_user_id=user_id)
    keyboard = [[InlineKeyboardButton(get_ui_text('cancel_input', viewer_user_id=user_id), callback_data=f'close {user_id}')]]

    message_id = context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))

    user.update_one({'user_id': user_id}, {"$set": {"sign": f'zdycz {message_id.message_id}'}})


def okzdycz(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    text = get_ui_text('enter_custom_okpay_amount', viewer_user_id=user_id)
    keyboard = [[InlineKeyboardButton(get_ui_text('cancel_input', viewer_user_id=user_id), callback_data=f'close {user_id}')]]

    message_id = context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))

    user.update_one({'user_id': user_id}, {"$set": {"sign": f'okzdycz {message_id.message_id}'}})


def yuecz(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    money = int(query.data.replace('yuecz ', ''))
    user_id = query.from_user.id
    create_trc20_deposit_order(context, user_id, money)


def okyuecz(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    money = int(query.data.replace('okyuecz ', ''))
    user_id = query.from_user.id
    create_okpay_deposit_order(context, user_id, money)


def recharge_trc20(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    fstext = get_ui_text('trc20_amount_menu', viewer_user_id=user_id)
    keyboard = [
        [InlineKeyboardButton('10USDT', callback_data='yuecz 10'),
         InlineKeyboardButton('30USDT', callback_data='yuecz 30'),
         InlineKeyboardButton('50USDT', callback_data='yuecz 50')],
        [InlineKeyboardButton('100USDT', callback_data='yuecz 100'),
         InlineKeyboardButton('200USDT', callback_data='yuecz 200'),
         InlineKeyboardButton('500USDT', callback_data='yuecz 500')],
        [InlineKeyboardButton('1000USDT', callback_data='yuecz 1000'),
         InlineKeyboardButton('1500USDT', callback_data='yuecz 1500'),
         InlineKeyboardButton('2000USDT', callback_data='yuecz 2000')],
        [InlineKeyboardButton(get_ui_text('custom_recharge_amount', viewer_user_id=user_id), callback_data='zdycz')],
        [InlineKeyboardButton(get_ui_text('back_to_recharge_method', viewer_user_id=user_id), callback_data='recharge_menu')],
        [InlineKeyboardButton(get_ui_text('cancel_recharge', viewer_user_id=user_id), callback_data=f'close {user_id}')]
    ]
    context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                             reply_markup=InlineKeyboardMarkup(keyboard))


def recharge_okpay(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    fstext = get_ui_text('okpay_amount_menu', viewer_user_id=user_id)
    keyboard = [
        [InlineKeyboardButton('10USDT', callback_data='okyuecz 10'),
         InlineKeyboardButton('30USDT', callback_data='okyuecz 30'),
         InlineKeyboardButton('50USDT', callback_data='okyuecz 50')],
        [InlineKeyboardButton('100USDT', callback_data='okyuecz 100'),
         InlineKeyboardButton('200USDT', callback_data='okyuecz 200'),
         InlineKeyboardButton('500USDT', callback_data='okyuecz 500')],
        [InlineKeyboardButton('1000USDT', callback_data='okyuecz 1000'),
         InlineKeyboardButton('1500USDT', callback_data='okyuecz 1500'),
         InlineKeyboardButton('2000USDT', callback_data='okyuecz 2000')],
        [InlineKeyboardButton(get_ui_text('custom_recharge_amount', viewer_user_id=user_id), callback_data='okzdycz')],
        [InlineKeyboardButton(get_ui_text('back_to_recharge_method', viewer_user_id=user_id), callback_data='recharge_menu')],
        [InlineKeyboardButton(get_ui_text('cancel_recharge', viewer_user_id=user_id), callback_data=f'close {user_id}')]
    ]
    context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                             reply_markup=InlineKeyboardMarkup(keyboard))

def catejflsp(update: Update, context: CallbackContext):
    query = update.callback_query

    uid = query.data.replace('catejflsp ', '').split(':')[0]
    zhsl = int(query.data.replace('catejflsp ', '').split(':')[1])
    #     if zhsl == 0:
    #         fstext =f'''
    # 🚫暂无商品，联系客服上架
    # 客服@momoziziya
    #         '''
    #         query.answer(fstext, show_alert=bool("true"))
    #         return
    query.answer()
    bot_id = context.bot.id
    user_id = query.from_user.id
    lang = get_user_lang(user_id)

    product_rows = []
    ej_list = list(ejfl.find({'uid': uid}, sort=[('row', 1)]))
    for i in ej_list:
        nowuid = i['nowuid']
        projectname = i['projectname']
        row = i['row']
        money = i.get('money', 0)
        hsl = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
        if hsl <= 0:
            continue
        product_rows.append({
            'nowuid': nowuid,
            'projectname': projectname,
            'row': row,
            'money': money,
            'stock': hsl
        })

    product_rows.sort(key=lambda item: (-int(item['stock']), int(item['row']), str(item['projectname'])))

    keyboard = []
    for item in product_rows:
        price_text = standard_num(item['money'])
        catalog_name = localize_catalog_name(item['projectname'], user_id, lang=lang)
        button_name = shorten_catalog_button_label(catalog_name, lang=lang)
        button_text = f"{button_name} （{item['stock']}） - ${price_text}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"gmsp {item['nowuid']}:{item['stock']}")])

    fstext = get_ui_text('category_list_text', viewer_user_id=user_id)

    if not keyboard:
        fstext = get_ui_text('category_empty_text', viewer_user_id=user_id)

    keyboard.append([InlineKeyboardButton(get_ui_text('main_menu', viewer_user_id=user_id), callback_data='backzcd'),
                     InlineKeyboardButton(get_ui_text('back', viewer_user_id=user_id), callback_data='backzcd')])
    query.edit_message_text(fstext, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


def get_product_purchase_payload(nowuid):
    nowuid = str(nowuid)
    ejfl_list = ejfl.find_one({'nowuid': nowuid}) or {}
    if not ejfl_list:
        return None
    uid = ejfl_list.get('uid')
    projectname = str(ejfl_list.get('projectname') or '商品')
    money = ejfl_list.get('money', 0)
    stock_count = get_stock_count(nowuid)
    category_name = ''
    if uid:
        fl_list = fenlei.find_one({'uid': uid}) or {}
        category_name = str(fl_list.get('projectname') or '')
    return {
        'nowuid': nowuid,
        'uid': uid,
        'projectname': projectname,
        'money': money,
        'stock_count': stock_count,
        'category_name': category_name,
    }


def build_product_purchase_text(projectname, money, stock_count, user_id=None):
    lang = get_user_lang(user_id) if user_id is not None else DEFAULT_LANG
    return get_ui_text(
        'product_purchase_text',
        viewer_user_id=user_id,
        projectname=localize_catalog_name(projectname, user_id, lang=lang) if user_id is not None else projectname,
        money=standard_num(money),
        stock_count=stock_count,
    )


def is_area_code_search_text(text):
    return isinstance(text, str) and re.fullmatch(r'\+\d{1,4}', text.strip()) is not None


def search_products_by_area_code(area_code):
    area_code = str(area_code or '').strip()
    if not area_code:
        return []
    pattern = re.escape(area_code)
    matched = []
    seen_nowuids = set()
    matched_uid_map = {}

    for fl_item in fenlei.find({'projectname': {'$regex': pattern}}):
        uid = fl_item.get('uid')
        if uid:
            matched_uid_map[str(uid)] = str(fl_item.get('projectname') or '')

    query = {'projectname': {'$regex': pattern}}
    for ej_item in ejfl.find(query, sort=[('uid', 1), ('row', 1)]):
        nowuid = str(ej_item.get('nowuid') or '')
        if not nowuid or nowuid in seen_nowuids:
            continue
        seen_nowuids.add(nowuid)
        uid = str(ej_item.get('uid') or '')
        category_name = matched_uid_map.get(uid)
        if not category_name and uid:
            fl_item = fenlei.find_one({'uid': uid}) or {}
            category_name = str(fl_item.get('projectname') or '')
        matched.append({
            'nowuid': nowuid,
            'uid': uid,
            'projectname': str(ej_item.get('projectname') or '商品'),
            'category_name': category_name,
            'money': ej_item.get('money', 0),
            'stock_count': get_stock_count(nowuid)
        })

    if matched_uid_map:
        for ej_item in ejfl.find({'uid': {'$in': list(matched_uid_map.keys())}}, sort=[('uid', 1), ('row', 1)]):
            nowuid = str(ej_item.get('nowuid') or '')
            if not nowuid or nowuid in seen_nowuids:
                continue
            seen_nowuids.add(nowuid)
            uid = str(ej_item.get('uid') or '')
            matched.append({
                'nowuid': nowuid,
                'uid': uid,
                'projectname': str(ej_item.get('projectname') or '商品'),
                'category_name': matched_uid_map.get(uid, ''),
                'money': ej_item.get('money', 0),
                'stock_count': get_stock_count(nowuid)
            })

    return matched


def build_area_code_search_text(area_code, results, user_id=None):
    total = len(results)
    in_stock_count = sum(1 for item in results if int(item.get('stock_count') or 0) > 0)
    tail_key = 'area_search_tail_in_stock' if in_stock_count > 0 else 'area_search_tail_no_stock'
    return (
        f"{get_ui_text('area_search_title', viewer_user_id=user_id)}\n\n"
        f"{get_ui_text('area_search_keyword', viewer_user_id=user_id, area_code=area_code)}\n"
        f"{get_ui_text('area_search_total', viewer_user_id=user_id, total=total)}\n\n"
        f"{get_ui_text(tail_key, viewer_user_id=user_id)}"
    )


def build_area_code_restock_request_keyboard(area_code, user_id):
    return [
        [InlineKeyboardButton(get_ui_text('restock_notice_button', viewer_user_id=user_id), callback_data=f'restockrequestarea {area_code}')],
        [InlineKeyboardButton(get_ui_text('main_menu', viewer_user_id=user_id), callback_data='backzcd'), InlineKeyboardButton(get_ui_text('close_with_icon', viewer_user_id=user_id), callback_data=f'close {user_id}')]
    ]


def build_area_code_search_keyboard(results, user_id):
    keyboard = []
    has_stock = any(int(item.get('stock_count') or 0) > 0 for item in results)
    for item in results[:40]:
        projectname = localize_catalog_name(item.get('projectname'), user_id)
        money = standard_num(item.get('money', 0))
        stock_count = int(item.get('stock_count') or 0)
        label = f'{projectname} （{stock_count}） - ${money}'
        if len(label) > 60:
            label = label[:57] + '...'
        keyboard.append([InlineKeyboardButton(label, callback_data=f'gmsp {item["nowuid"]}:{stock_count}')])
    if not has_stock:
        keyboard.append([InlineKeyboardButton(get_ui_text('restock_notice_button', viewer_user_id=user_id), callback_data=f'restockrequestarea {results[0].get("search_keyword", "")}')])
    keyboard.append([InlineKeyboardButton(get_ui_text('main_menu', viewer_user_id=user_id), callback_data='backzcd'), InlineKeyboardButton(get_ui_text('close_with_icon', viewer_user_id=user_id), callback_data=f'close {user_id}')])
    return keyboard


def handle_area_code_search(context, user_id, fullname, username, area_code):
    results = search_products_by_area_code(area_code)
    if results:
        for item in results:
            item['search_keyword'] = area_code
        context.bot.send_message(
            chat_id=user_id,
            text=build_area_code_search_text(area_code, results, user_id=user_id),
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(build_area_code_search_keyboard(results, user_id))
        )
        return True

    tip_text = get_ui_text('area_search_empty', viewer_user_id=user_id, area_code=area_code)
    context.bot.send_message(
        chat_id=user_id,
        text=tip_text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(build_area_code_restock_request_keyboard(area_code, user_id))
    )
    return True


def restockrequestarea(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    username = query.from_user.username
    fullname = (query.from_user.full_name or '').replace('<', '').replace('>', '')
    area_code = str(query.data.replace('restockrequestarea ', '', 1)).strip()
    query.answer()
    if not is_area_code_search_text(area_code):
        context.bot.send_message(chat_id=user_id, text=get_ui_text('area_request_invalid', viewer_user_id=user_id))
        return
    created_at = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    result = restock_requests.update_one(
        {'request_type': 'area_code_search', 'keyword': area_code, 'user_id': user_id},
        {'$setOnInsert': {
            'request_type': 'area_code_search',
            'keyword': area_code,
            'user_id': user_id,
            'username': username,
            'fullname': fullname,
            'created_at': created_at
        }},
        upsert=True
    )
    if result.upserted_id is None:
        context.bot.send_message(chat_id=user_id, text=get_ui_text('area_request_exists', viewer_user_id=user_id), parse_mode='HTML')
        return
    display_name = fullname or username or str(user_id)
    at_text = f'@{username}' if username else '无用户名'
    notify_text = (
        f'[emoji:5301246586918024418:⚠️] 用户请求补货\n\n'
        f'[emoji:6321041414067068140:👤] 用户：<a href="tg://user?id={user_id}">{display_name}</a> {at_text}\n'
        f'[emoji:5217818964612108191:✨] 搜索区号：<code>{area_code}</code>\n\n'
        '用户点击了提醒补货按钮，可留意是否需要补货相关商品。'
    )
    notify_source_admins(context, notify_text, exclude_user_ids=[user_id])
    context.bot.send_message(chat_id=user_id, text=get_ui_text('area_request_done', viewer_user_id=user_id), parse_mode='HTML')


def send_product_purchase_page(context, chat_id, user_id, nowuid):
    payload = get_product_purchase_payload(nowuid)
    if not payload:
        context.bot.send_message(chat_id=chat_id, text=get_ui_text('product_not_found', viewer_user_id=user_id))
        return None
    keyboard = build_product_purchase_keyboard(payload['nowuid'], payload['uid'], user_id, payload['stock_count'])
    return context.bot.send_message(
        chat_id=chat_id,
        text=build_product_purchase_text(payload['projectname'], payload['money'], payload['stock_count'], user_id=user_id),
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def build_product_purchase_deep_link(bot_username, nowuid):
    bot_username = str(bot_username or '').strip().lstrip('@')
    nowuid = str(nowuid or '').strip()
    if not bot_username or not nowuid:
        return ''
    start_param = urllib.parse.quote(f'buy_{nowuid}')
    return f'https://t.me/{bot_username}?start={start_param}'


def gmsp(update: Update, context: CallbackContext):
    query = update.callback_query

    data = query.data.replace('gmsp ', '')
    nowuid = data.split(':')[0]
    hsl = data.split(':')[1]

    bot_id = context.bot.id
    user_id = query.from_user.id

    payload = get_product_purchase_payload(nowuid)
    if not payload:
        query.answer(get_ui_text('product_not_found', viewer_user_id=user_id), show_alert=bool("true"))
        return
    hsl = payload['stock_count']
    projectname = payload['projectname']
    money = payload['money']
    uid = payload['uid']
    #     if hsl == 0:
    #         fstext =f'''
    # 🚫暂无商品，联系客服上架
    # 客服@momoziziya
    #         '''
    #         query.answer(fstext, show_alert=bool("true"))
    #         return
    # else:
    query.answer()
    fstext = build_product_purchase_text(projectname, money, hsl, user_id=user_id)

    keyboard = build_product_purchase_keyboard(nowuid, uid, user_id, hsl)
    query.edit_message_text(fstext, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def restocknotice(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    nowuid = str(query.data.replace('restocknotice ', '', 1)).strip()
    ejfl_list = ejfl.find_one({'nowuid': nowuid}) or {}
    if not ejfl_list:
        context.bot.send_message(chat_id=user_id, text=get_ui_text('product_not_found', viewer_user_id=user_id))
        return
    uid = ejfl_list.get('uid')
    projectname = str(ejfl_list.get('projectname') or '商品')
    money = ejfl_list.get('money', 0)
    stock_count = get_stock_count(nowuid)
    if stock_count > 0:
        keyboard = build_product_purchase_keyboard(nowuid, uid, user_id, stock_count)
        text = build_product_purchase_text(projectname, money, stock_count, user_id=user_id)
        try:
            query.edit_message_text(text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            pass
        return
    if is_restock_notice_subscribed(nowuid, user_id):
        restock_notices.delete_one({'nowuid': nowuid, 'user_id': user_id})
        notice_tip = get_ui_text('restock_notice_disabled', viewer_user_id=user_id)
    else:
        restock_notices.update_one(
            {'nowuid': nowuid, 'user_id': user_id},
            {'$set': {'nowuid': nowuid, 'user_id': user_id, 'created_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}},
            upsert=True
        )
        notice_tip = get_ui_text('restock_notice_enabled', viewer_user_id=user_id)
    keyboard = build_product_purchase_keyboard(nowuid, uid, user_id, 0)
    text = get_ui_text('restock_notice_empty_text', viewer_user_id=user_id, projectname=localize_catalog_name(projectname, user_id), money=standard_num(money))
    try:
        query.edit_message_text(text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        pass
    context.bot.send_message(chat_id=user_id, text=notice_tip)


def gmqq(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id

    nowuid = query.data.replace('gmqq ', '')

    if get_stock_count(nowuid) <= 0:
        query.answer(get_ui_text('current_no_stock', viewer_user_id=user_id), show_alert=bool("true"))
        return

    ejfl_list = ejfl.find_one({'nowuid': nowuid})
    projectname = ejfl_list['projectname']
    money = ejfl_list['money']
    uid = ejfl_list['uid']

    user_list = user.find_one({'user_id': user_id})
    USDT = user_list['USDT']
    if USDT < money:
        query.answer(get_ui_text('insufficient_balance', viewer_user_id=user_id), show_alert=bool("true"))
        return
    else:
        query.answer()
        # del_message(query.message)
        fstext = get_ui_text('enter_quantity_prompt', viewer_user_id=user_id)

        message_id = context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML')
        user.update_one({'user_id': user_id}, {"$set": {"sign": f"gmqq {nowuid}:{message_id.message_id}"}})


def paixuejfl(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    uid = query.data.replace('paixuejfl ', '')
    bot_id = context.bot.id
    fl_pro = fenlei.find_one({'uid': uid})['projectname']
    keylist = list(ejfl.find({'uid': uid}, sort=[('row', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    count = []
    for i in keylist:
        projectname = i['projectname']
        row = i['row']
        nowuid = i['nowuid']
        keyboard[i["row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'fejxxi {nowuid}'))
        count.append(row)
    if count == []:
        context.bot.send_message(chat_id=user_id, text='没有按钮存在')
    else:
        maxrow = max(count)
        if maxrow == 1:
            context.bot.send_message(chat_id=user_id, text='只有一行按钮无法调整')
        else:
            for i in range(0, maxrow):
                pxuid = ejfl.find_one({'uid': uid, 'row': i + 1})['nowuid']
                if i == 0:
                    keyboard.append(
                        [InlineKeyboardButton(f'第{i + 1}行下移', callback_data=f'ejfpaixu xiayi:{i + 1}:{pxuid}')])
                elif i == maxrow - 1:
                    keyboard.append(
                        [InlineKeyboardButton(f'第{i + 1}行上移', callback_data=f'ejfpaixu shangyi:{i + 1}:{pxuid}')])
                else:
                    keyboard.append(
                        [InlineKeyboardButton(f'第{i + 1}行上移', callback_data=f'ejfpaixu shangyi:{i + 1}:{pxuid}'),
                         InlineKeyboardButton(f'第{i + 1}行下移', callback_data=f'ejfpaixu xiayi:{i + 1}:{pxuid}')])
            keyboard.append([InlineKeyboardButton('❌关闭', callback_data=f'close {user_id}')])
            context.bot.send_message(chat_id=user_id, text=f'分类: {fl_pro}',
                                     reply_markup=InlineKeyboardMarkup(keyboard))


def ejfpaixu(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    qudata = query.data.replace('ejfpaixu ', '')
    qudataall = qudata.split(':')
    yidongtype = qudataall[0]
    row = int(qudataall[1])
    nowuid = qudataall[2]
    uid = ejfl.find_one({'nowuid': nowuid})['uid']
    if yidongtype == 'shangyi':
        ejfl.update_many({"row": row - 1, 'uid': uid}, {"$set": {'row': 99}})
        ejfl.update_many({"row": row, 'uid': uid}, {"$set": {'row': row - 1}})
        ejfl.update_many({"row": 99, 'uid': uid}, {"$set": {'row': row}})
    else:
        ejfl.update_many({"row": row + 1, 'uid': uid}, {"$set": {'row': 99}})
        ejfl.update_many({"row": row, 'uid': uid}, {"$set": {'row': row + 1}})
        ejfl.update_many({"row": 99, 'uid': uid}, {"$set": {'row': row}})

    fl_pro = fenlei.find_one({'uid': uid})['projectname']
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    ej_list = ejfl.find({'uid': uid})
    for i in ej_list:
        nowuid = i['nowuid']
        projectname = i['projectname']
        row = i['row']
        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}', callback_data=f'fejxxi {nowuid}'))

    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}修改分类名', callback_data=f'upspname {uid}'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新增二级分类', callback_data=f'newejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整二级分类排序', callback_data=f'paixuejfl {uid}'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除二级分类', callback_data=f'delejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
    fstext = f'''
分类: {fl_pro}
    '''
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def paixufl(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    keylist = list(fenlei.find({}, sort=[('row', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    count = []
    for i in keylist:
        projectname = i['projectname']
        row = i['row']
        uid = i['uid']
        keyboard[i["row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'flxxi {uid}'))
        count.append(row)
    if count == []:
        context.bot.send_message(chat_id=user_id, text='没有按钮存在')
    else:
        maxrow = max(count)
        if maxrow == 1:
            context.bot.send_message(chat_id=user_id, text='只有一行按钮无法调整')
        else:
            for i in range(0, maxrow):
                if i == 0:
                    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}第{i + 1}行下移', callback_data=f'flpxyd xiayi:{i + 1}')])
                elif i == maxrow - 1:
                    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}第{i + 1}行上移', callback_data=f'flpxyd shangyi:{i + 1}')])
                else:
                    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}第{i + 1}行上移', callback_data=f'flpxyd shangyi:{i + 1}'),
                                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}第{i + 1}行下移', callback_data=f'flpxyd xiayi:{i + 1}')])
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
            context.bot.send_message(chat_id=user_id, text='商品管理', reply_markup=InlineKeyboardMarkup(keyboard))


def flpxyd(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    qudata = query.data.replace('flpxyd ', '')
    qudataall = qudata.split(':')
    yidongtype = qudataall[0]
    row = int(qudataall[1])
    if yidongtype == 'shangyi':
        fenlei.update_many({"row": row - 1}, {"$set": {'row': 99}})
        fenlei.update_many({"row": row}, {"$set": {'row': row - 1}})
        fenlei.update_many({"row": 99}, {"$set": {'row': row}})
    else:
        fenlei.update_many({"row": row + 1}, {"$set": {'row': 99}})
        fenlei.update_many({"row": row}, {"$set": {'row': row + 1}})
        fenlei.update_many({"row": 99}, {"$set": {'row': row}})
    keylist = list(fenlei.find({}, sort=[('row', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    for i in keylist:
        uid = i['uid']
        projectname = i['projectname']
        row = i['row']
        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}', callback_data=f'flxxi {uid}'))
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新建一行', callback_data='newfl'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整行排序', callback_data='paixufl'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除一行', callback_data='delfl')])
    context.bot.send_message(chat_id=user_id, text='商品管理', reply_markup=InlineKeyboardMarkup(keyboard))


def send_category_detail_page(context: CallbackContext, user_id, uid):
    fl_pro = fenlei.find_one({'uid': uid})['projectname']
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    ej_list = ejfl.find({'uid': uid})
    for i in ej_list:
        nowuid = i['nowuid']
        projectname = i['projectname']
        row = i['row']
        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}', callback_data=f'fejxxi {nowuid}'))

    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}修改分类名', callback_data=f'upspname {uid}'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新增二级分类', callback_data=f'newejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整二级分类排序', callback_data=f'paixuejfl {uid}'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除二级分类', callback_data=f'delejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
    fstext = f'''
分类: {fl_pro}
    '''
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def delete_secondary_category_by_nowuid(context: CallbackContext, user_id, nowuid):
    ej_item = ejfl.find_one({'nowuid': nowuid})
    if ej_item is None:
        context.bot.send_message(chat_id=user_id, text='该分类不存在或已删除')
        return
    uid = ej_item['uid']
    row = int(ej_item['row'])
    ejfl.delete_many({'uid': uid, 'row': row})
    max_list = list(ejfl.find({'uid': uid, 'row': {'$gt': row}}))
    for i in max_list:
        max_row = i['row']
        ejfl.update_many({'uid': uid, 'row': max_row}, {"$set": {"row": max_row - 1}})
    send_category_detail_page(context, user_id, uid)


def delejfl(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    uid = query.data.replace('delejfl ', '')
    fl_pro = fenlei.find_one({'uid': uid})['projectname']
    keylist = list(ejfl.find({'uid': uid}, sort=[('row', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    count = []
    for i in keylist:
        projectname = i['projectname']
        row = i['row']
        nowuid = i['nowuid']
        keyboard[i["row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'fejxxi {nowuid}'))
        count.append(row)
    if count == []:
        context.bot.send_message(chat_id=user_id, text='没有按钮存在')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            pxuid = ejfl.find_one({'uid': uid, 'row': i + 1})['nowuid']
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除第{i + 1}行', callback_data=f'qrscejrow {i + 1}:{pxuid}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
        context.bot.send_message(chat_id=user_id, text=f'分类: {fl_pro}', reply_markup=InlineKeyboardMarkup(keyboard))


def qrscejrow(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    del_message(query.message)

    nowuid = query.data.replace('qrscejrow ', '').split(':')[1]
    delete_secondary_category_by_nowuid(context, user_id, nowuid)


def delcurconfirm(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    nowuid = query.data.replace('delcurconfirm ', '')
    ej_item = ejfl.find_one({'nowuid': nowuid})
    if ej_item is None:
        query.edit_message_text('该分类不存在或已删除')
        return
    uid = ej_item['uid']
    fl_pro = fenlei.find_one({'uid': uid})['projectname']
    ej_projectname = ej_item['projectname']
    keyboard = [
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}确认删除', callback_data=f'delcurejfl {nowuid}')],
        [InlineKeyboardButton('⬅️返回商品详情', callback_data=f'fejxxi {nowuid}')]
    ]
    fstext = f'''
确认删除该分类？

主分类: {fl_pro}
二级分类: {ej_projectname}
    '''
    query.edit_message_text(text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def delcurejfl(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    del_message(query.message)
    nowuid = query.data.replace('delcurejfl ', '')
    delete_secondary_category_by_nowuid(context, user_id, nowuid)


def delfl(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    keylist = list(fenlei.find({}, sort=[('row', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    count = []
    for i in keylist:
        uid = i['uid']
        projectname = i['projectname']
        row = i['row']
        keyboard[i["row"] - 1].append(InlineKeyboardButton(projectname, callback_data=f'flxxi {uid}'))
        count.append(row)
    if count == []:
        context.bot.send_message(chat_id=user_id, text='没有按钮存在')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除第{i + 1}行', callback_data=f'qrscflrow {i + 1}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
        context.bot.send_message(chat_id=user_id, text='商品管理', reply_markup=InlineKeyboardMarkup(keyboard))


def qrscflrow(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    del_message(query.message)
    row = int(query.data.replace('qrscflrow ', ''))
    bot_id = context.bot.id
    fenlei.delete_many({"row": row})
    max_list = list(fenlei.find({'row': {"$gt": row}}))
    for i in max_list:
        max_row = i['row']
        fenlei.update_many({'row': max_row}, {"$set": {"row": max_row - 1}})
    keylist = list(fenlei.find({}, sort=[('row', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    for i in keylist:
        uid = i['uid']
        projectname = i['projectname']
        row = i['row']
        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}', callback_data=f'flxxi {uid}'))
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}新建一行', callback_data='newfl'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}调整行排序', callback_data='paixufl'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}删除一行', callback_data='delfl')])
    context.bot.send_message(chat_id=user_id, text='商品管理', reply_markup=InlineKeyboardMarkup(keyboard))


def backzcd(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    bot_id = context.bot.id
    user_id = query.from_user.id
    keyboard = build_category_catalog_keyboard(user_id)
    fstext = get_ui_text('category_list_text', viewer_user_id=user_id)
    keyboard.append([InlineKeyboardButton(get_ui_text('close_with_icon', viewer_user_id=user_id), callback_data=f'close {user_id}')])
    query.edit_message_text(fstext, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


def is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        pass

    try:
        import unicodedata
        unicodedata.numeric(s)
        return True
    except (TypeError, ValueError):
        pass

    return False


def get_trc20_address():
    row = shangtext.find_one({'projectname': '充值地址'}) or {}
    return str(row.get('text', '') or '').strip()


def get_text_config(projectname, default=''):
    row = shangtext.find_one({'projectname': projectname}) or {}
    value = row.get('text', default)
    if value is None:
        return default
    return value


def set_text_config(projectname, value):
    shangtext.update_one({'projectname': projectname}, {'$set': {'text': value}}, upsert=True)


def get_restock_push_target():
    target = str(get_text_config('补货通知群组', '') or '').strip()
    if not target:
        return ''
    lowered = target.lower()
    for prefix in ('https://t.me/', 'http://t.me/', 'https://telegram.me/', 'http://telegram.me/', 't.me/', 'telegram.me/'):
        if lowered.startswith(prefix):
            target = target[len(prefix):].strip()
            target = target.split('?', 1)[0].split('/', 1)[0].strip()
            break
    if target and not target.startswith('@') and not re.fullmatch(r'-?\d+', target):
        target = f'@{target}'
    return target


def build_restock_push_config_text():
    target = get_restock_push_target()
    target_text = target or '未配置'
    return (
        f'{ADMIN_EMOJI_RESTOCK}补货通知推送\n\n'
        f'{ADMIN_EMOJI_GOODS} 当前目标：<code>{target_text}</code>\n\n'
        '支持填写群组/频道 @username、chat_id、或 t.me 链接\n'
        '例如：<code>@yourchannel</code>、<code>-1001234567890</code>、<code>https://t.me/yourchannel</code>'
    )


def build_restock_push_config_keyboard(user_id):
    return [
        [InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}设置群组/频道', callback_data='setrestocktarget')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}'),
         InlineKeyboardButton('⬅️返回主界面', callback_data='backstart')]
    ]


def build_restock_push_broadcast_text(category_name, projectname, money, added_count, stock_count):
    category_name = str(category_name or '未分类')
    projectname = str(projectname or '商品')
    category_and_product = f'{category_name}/{projectname}'
    return (
        '[emoji:5318840353510408444:🔴][emoji:5318840353510408444:🔴]库存更新[emoji:5318840353510408444:🔴][emoji:5318840353510408444:🔴]\n\n'
        f'{category_and_product}\n\n'
        f'[emoji:5397916757333654639:➕]添加库存 {added_count} 个\n\n'
        f'[emoji:4965219701572503640:💰]商品单价 {money} U\n\n'
        f'[emoji:5282843764451195532:🖥]剩余库存 {stock_count} 个'
    )


def build_restock_push_broadcast_message(category_name, projectname, money, added_count, stock_count):
    text = build_restock_push_broadcast_text(category_name, projectname, money, added_count, stock_count)
    text, entities = build_custom_emoji_text_entities(text)
    if text:
        entities = [MessageEntity(type='bold', offset=0, length=utf16_len(text))] + list(entities or [])
    return text, entities


def notify_restock_broadcast(context, nowuid, added_count=0):
    if int(added_count or 0) <= 0:
        return
    target = get_restock_push_target()
    if not target:
        return
    payload = get_product_purchase_payload(nowuid)
    if not payload:
        return
    projectname = payload['projectname']
    category_name = payload['category_name']
    money = payload['money']
    stock_count = payload['stock_count']
    text, entities = build_restock_push_broadcast_message(category_name, projectname, money, added_count, stock_count)
    keyboard = None
    bot_username = str(getattr(context.bot, 'username', '') or '').strip()
    buy_url = build_product_purchase_deep_link(bot_username, nowuid)
    if buy_url:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('[emoji:5451937962629544243:🛍]购买商品', url=buy_url)]])
    try:
        context.bot.send_message(chat_id=target, text=text, entities=entities, reply_markup=keyboard)
    except Exception as exc:
        logging.warning('restock broadcast failed for %s: %s', target, exc)


def get_welcome_content(default_text=DEFAULT_CLONE_WELCOME_TEXT):
    text = str(get_text_config('欢迎语', default_text) or '').strip()
    if not text or text == '欢迎使用机器人':
        return default_text, []
    entities_raw = get_text_config('欢迎语样式', b'\x80\x03]q\x00.')
    entities = safe_pickle_loads(entities_raw)
    return text, entities


def get_localized_welcome_content(user_id):
    lang = get_user_lang(user_id)
    text, entities = get_welcome_content()
    if lang == 'zh':
        return text, entities

    custom_en = str(
        get_text_config('欢迎语英文', '')
        or get_text_config('英文欢迎语', '')
        or get_text_config('欢迎语:en', '')
        or get_text_config('欢迎语:en-US', '')
        or ''
    ).strip()
    if custom_en:
        return custom_en, []

    auto_en = translate_text(text, 'en').strip() if text else ''
    if auto_en and auto_en != text:
        return auto_en, []
    return DEFAULT_CLONE_WELCOME_TEXT_EN, []


def build_user_home_reply_keyboard(user_id):
    lang = get_user_lang(user_id)
    keylist = get_key.find({}, sort=[('Row', 1), ('first', 1)])
    keyboard = [[] for _ in range(100)]
    for item in keylist:
        row = max(1, int(item.get('Row', 1))) - 1
        label = localize_button_label(item.get('projectname', ''), user_id=user_id, lang=lang)
        keyboard[row].append(KeyboardButton(label))
    keyboard = [row for row in keyboard if row]
    keyboard.append([KeyboardButton(get_ui_text('language_toggle', lang=lang))])
    if BOT_CLONE_ENABLED and ALLOW_PUBLIC_BOT_CLONE:
        keyboard.append([KeyboardButton(localize_button_label('#g [emoji:5287684458881756303:🤖]一键克隆同款', user_id=user_id, lang=lang))])
    return keyboard


def send_user_home(context, user_id):
    warm_storefront_translation_cache(user_id=user_id)
    welcome_text, entities = get_localized_welcome_content(user_id)
    reply_markup = ReplyKeyboardMarkup(build_user_home_reply_keyboard(user_id), resize_keyboard=True, one_time_keyboard=False)
    welcome_kwargs = {'chat_id': user_id, 'text': welcome_text, 'reply_markup': reply_markup}
    if entities:
        welcome_kwargs['entities'] = entities
    elif welcome_uses_html_parse(welcome_text, entities):
        welcome_kwargs['parse_mode'] = 'HTML'
    context.bot.send_message(**welcome_kwargs)


def build_user_profile_text(user_id, username, creation_time, zgsl, zgje, balance):
    lang = get_user_lang(user_id)
    username = str(username or '').strip().lstrip('@')
    if username:
        safe_username = html.escape(username, quote=False)
        username_html = f'@{safe_username}'
    else:
        username_html = '未设置' if lang == 'zh' else 'Not set'
    return get_ui_text(
        'profile_text',
        lang=lang,
        username=username,
        username_html=username_html,
        user_id=user_id,
        creation_time=creation_time,
        zgsl=zgsl,
        zgje=standard_num(zgje),
        USDT=format_usdt_2(balance),
    )


def build_profile_keyboard(user_id):
    return [
        [InlineKeyboardButton(get_ui_text('purchase_history_button', viewer_user_id=user_id), callback_data=f'gmaijilu {user_id}')],
        [InlineKeyboardButton('🔗推广链接', callback_data=f'tglink {user_id}')],
        [InlineKeyboardButton(get_ui_text('close', viewer_user_id=user_id), callback_data=f'close {user_id}')],
    ]


def localize_catalog_name(value, user_id, lang=None):
    text = str(value or '').strip() or '商品'
    lang = normalize_lang_code(lang or get_user_lang(user_id))
    if lang != 'en':
        return text

    localized = localize_button_label(text, user_id=user_id, lang=lang)
    if contains_cjk(localized):
        localized = localize_dynamic_text(text, user_id=user_id, lang=lang)
    return localized


def shorten_catalog_button_label(text, stock_count=None, lang=None):
    text = re.sub(r'\s+', ' ', str(text or '').strip())
    lang = normalize_lang_code(lang)
    max_length = 46 if lang == 'en' else 40
    suffix = f' [{int(stock_count)}]' if stock_count is not None else ''
    prefix = ''
    visible_text = text

    emoji_id, alt, emoji_style, rest = parse_dynamic_emoji_prefix(text)
    if emoji_id:
        prefix = f'[emoji:{emoji_id}:{alt}'
        if emoji_style:
            prefix += f':{emoji_style}'
        prefix += ']'
        visible_text = re.sub(r'\s+', ' ', str(rest or '').strip())
    else:
        _, emoji_text, clean_text = extract_known_button_icon(text)
        if emoji_text and text.strip().startswith(emoji_text):
            prefix = emoji_text
            visible_text = re.sub(r'\s+', ' ', str(clean_text or '').strip())

    visible_text = visible_text.strip()
    if len(visible_text) + len(suffix) <= max_length:
        shortened = f'{visible_text}{suffix}'
    else:
        keep = max(6, max_length - len(suffix) - 1)
        bracket_match = re.match(r'^(.*?)(\s*[（(][^）)]*[）)])$', visible_text)
        if bracket_match:
            primary_name = bracket_match.group(1).rstrip()
            bracket_text = bracket_match.group(2).strip()
            if len(primary_name) + len(suffix) <= max_length:
                remaining = max_length - len(primary_name) - len(suffix)
                if remaining > 0:
                    if len(bracket_text) <= remaining:
                        shortened = f'{primary_name}{bracket_text}{suffix}'
                    elif remaining >= 4:
                        left_bracket = bracket_text[0]
                        right_bracket = bracket_text[-1]
                        inner_keep = max(1, remaining - 3)
                        shortened = f'{primary_name}{left_bracket}{bracket_text[1:1 + inner_keep]}…{right_bracket}{suffix}'
                    else:
                        shortened = f'{primary_name}{suffix}'
                else:
                    shortened = f'{primary_name}{suffix}'
            else:
                shortened = f'{visible_text[:keep].rstrip()}…{suffix}'
        else:
            shortened = f'{visible_text[:keep].rstrip()}…{suffix}'

    return f'{prefix}{shortened}' if prefix else shortened


def build_category_catalog_keyboard(user_id):
    lang = get_user_lang(user_id)
    keylist = list(fenlei.find({}, sort=[('row', 1)]))
    keyboard = [[] for _ in range(100)]
    for item in keylist:
        uid = item['uid']
        row = max(1, int(item.get('row', 1))) - 1
        hsl = 0
        for child in list(ejfl.find({'uid': uid})):
            hsl += len(list(hb.find({'nowuid': child['nowuid'], 'state': 0})))
        projectname = localize_catalog_name(item.get('projectname'), user_id, lang=lang)
        button_text = shorten_catalog_button_label(projectname, stock_count=hsl, lang=lang)
        keyboard[row].append(InlineKeyboardButton(button_text, callback_data=f'catejflsp {uid}:{hsl}'))
    return [row for row in keyboard if row]


def get_clone_price_decimal():
    raw = str(get_text_config('一键克隆价格', '0') or '0').strip()
    try:
        price = Decimal(raw)
    except Exception:
        price = Decimal('0')
    if price < 0:
        price = Decimal('0')
    return price.quantize(Decimal('0.01'))


def format_clone_price(value=None):
    price = get_clone_price_decimal() if value is None else Decimal(str(value))
    price = price.quantize(Decimal('0.01'))
    text = format(price, 'f').rstrip('0').rstrip('.')
    return text or '0'


def get_stock_count(nowuid):
    return hb.count_documents({'nowuid': str(nowuid), 'state': 0})


def is_restock_notice_subscribed(nowuid, user_id):
    return restock_notices.find_one({'nowuid': str(nowuid), 'user_id': int(user_id)}) is not None


def build_product_purchase_keyboard(nowuid, uid, user_id, stock_count=None):
    stock_count = get_stock_count(nowuid) if stock_count is None else int(stock_count)
    buy_button = InlineKeyboardButton(get_ui_text('buy_now', viewer_user_id=user_id), callback_data=f'gmqq {nowuid}') if stock_count > 0 else InlineKeyboardButton(get_ui_text('out_of_stock_button', viewer_user_id=user_id), callback_data=f'restocknotice {nowuid}')
    return [
        [buy_button],
        [InlineKeyboardButton(get_ui_text('main_menu', viewer_user_id=user_id), callback_data='backzcd'),
         InlineKeyboardButton(get_ui_text('back', viewer_user_id=user_id), callback_data=f'catejflsp {uid}:1000')]
    ]


def nostock(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer(get_ui_text('current_no_stock', viewer_user_id=query.from_user.id), show_alert=bool("true"))


def notify_restock_subscribers(context, nowuid):
    nowuid = str(nowuid)
    stock_count = get_stock_count(nowuid)
    if stock_count <= 0:
        return
    ejfl_list = ejfl.find_one({'nowuid': nowuid}) or {}
    if not ejfl_list:
        return
    projectname = str(ejfl_list.get('projectname') or '商品')
    money = ejfl_list.get('money', 0)
    rows = list(restock_notices.find({'nowuid': nowuid}))
    if not rows:
        return
    sent_user_ids = set()
    for row in rows:
        target_user_id = row.get('user_id')
        if not target_user_id or target_user_id in sent_user_ids:
            continue
        sent_user_ids.add(target_user_id)
        try:
            text = (
                f"[emoji:5312028599803460968:🆗] {translate_text('你关注的商品已补货', get_user_lang(target_user_id))}\n\n"
                f"[emoji:5312361253610475399:🛒] {translate_text('商品', get_user_lang(target_user_id))}：{localize_catalog_name(projectname, target_user_id)}\n"
                f"[emoji:4965219701572503640:💰] {translate_text('价格', get_user_lang(target_user_id))}：{standard_num(money)} USDT\n"
                f"[emoji:5028746137645876535:📈] {translate_text('当前库存', get_user_lang(target_user_id))}：{stock_count}"
            )
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(localize_dynamic_text('🛒立即购买', user_id=target_user_id), callback_data=f'gmsp {nowuid}:{stock_count}')]])
            context.bot.send_message(chat_id=target_user_id, text=text, reply_markup=keyboard)
        except Exception:
            pass
    restock_notices.delete_many({'nowuid': nowuid})


def notify_restock_if_needed(context, nowuid, previous_stock, added_count=0):
    notify_restock_broadcast(context, nowuid, added_count)


def get_source_admin_user_ids():
    admin_ids = set(int(i) for i in ADMIN_USER_IDS)
    try:
        for row in user.find({'state': '4'}, {'user_id': 1}):
            uid = row.get('user_id')
            if uid:
                admin_ids.add(int(uid))
    except Exception:
        pass
    return sorted(admin_ids)


def is_clone_fee_exempt(user_id, state=None):
    if state is not None and str(state) == '4':
        return True
    return int(user_id) in set(get_source_admin_user_ids())


def get_user_clone_credit(user_id):
    row = user.find_one({'user_id': user_id}, {'clone_credit': 1}) or {}
    try:
        return max(int(row.get('clone_credit', 0) or 0), 0)
    except Exception:
        return 0


def notify_source_admins(context, text, reply_markup=None, exclude_user_ids=None):
    excluded = {str(i) for i in (exclude_user_ids or [])}
    for admin_id in get_source_admin_user_ids():
        if str(admin_id) in excluded:
            continue
        try:
            context.bot.send_message(chat_id=admin_id, text=text, parse_mode='HTML', reply_markup=reply_markup,
                                     disable_web_page_preview=True)
        except Exception:
            continue


def build_clone_list_keyboard(user_id, page=0, page_size=8):
    query = {'state': {'$ne': 'deleted'}, 'clone_kind': {'$ne': 'agent'}}
    rows = list(clone_instances.find(query, sort=[('created_at', -1)], skip=page * page_size, limit=page_size))
    keyboard = []
    for row in rows:
        bot_id = row.get('bot_id')
        bot_username = str(row.get('bot_username') or f'bot{bot_id}')
        requester_user_id = row.get('requester_user_id')
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}@{bot_username}', callback_data=f'cloneinfo {bot_id}')])
        if requester_user_id:
            keyboard[-1].append(InlineKeyboardButton(f'{ADMIN_EMOJI_USERLIST}{requester_user_id}', callback_data=f'cloneinfo {bot_id}'))

    total = clone_instances.count_documents(query)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}上一页', callback_data=f'clonelist {page - 1}'))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(f'{MOOD_EMOJI_FAST}下一页', callback_data=f'clonelist {page + 1}'))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_OKPAY}设置克隆价格', callback_data='setcloneprice')])
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data='backstart'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
    return keyboard, total


def build_agent_runtime_stats(agent_bot_id):
    tenant_id = normalize_tenant_id(agent_bot_id)
    base_stats = get_agent_stats(tenant_id)
    order_query = {'tenant_id': tenant_id}
    return {
        'user_count': int(base_stats.get('user_count', 0) or 0),
        'total_balance': float(base_stats.get('total_balance', 0) or 0),
        'total_spent': float(base_stats.get('total_spent', 0) or 0),
        'total_items': int(base_stats.get('total_orders', 0) or 0),
        'purchase_records': int(base_stats.get('purchase_records', 0) or 0),
        'pending_topups': int(topup_orders.count_documents({'tenant_id': tenant_id, 'state': 'pending'})),
        'paid_topups': int(topup_orders.count_documents({'tenant_id': tenant_id, 'state': 'paid'})),
        'order_count': int(tenant_orders.count_documents(order_query)),
        'delivered_count': int(tenant_orders.count_documents(dict(order_query, state='delivered'))),
        'partial_refund_count': int(tenant_orders.count_documents(dict(order_query, state='partial_refunded'))),
        'refunded_count': int(tenant_orders.count_documents(dict(order_query, state='refunded'))),
        'pending_withdrawals': int(agent_withdrawals.count_documents({'agent_bot_id': tenant_id, 'state': 'pending'})),
        'paid_withdrawals': int(agent_withdrawals.count_documents({'agent_bot_id': tenant_id, 'state': 'paid'})),
    }


def build_agent_list_keyboard(user_id, page=0, page_size=8):
    query = {'state': {'$ne': 'deleted'}, 'clone_kind': 'agent'}
    rows = list(clone_instances.find(query, sort=[('created_at', -1)], skip=page * page_size, limit=page_size))
    keyboard = []
    for row in rows:
        bot_id = str(row.get('bot_id') or '')
        bot_username = str(row.get('bot_username') or f'bot{bot_id}')
        stats = build_agent_runtime_stats(bot_id)
        keyboard.append([
            InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}@{bot_username}', callback_data=f'agentinfo {bot_id}'),
            InlineKeyboardButton(f'{ADMIN_EMOJI_GOODS}{standard_num(stats.get("total_spent", 0))}', callback_data=f'agentinfo {bot_id}'),
        ])

    total = clone_instances.count_documents(query)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}上一页', callback_data=f'agentlist {page - 1}'))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(f'{MOOD_EMOJI_FAST}下一页', callback_data=f'agentlist {page + 1}'))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data='backstart'), InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
    return keyboard, total


def build_agent_users_text(agent_bot_id, page=0, page_size=12):
    coll = get_agent_bot_user_collection(agent_bot_id)
    rows = list(coll.find({}, sort=[('USDT', -1), ('count_id', 1), ('user_id', 1)], skip=page * page_size, limit=page_size))
    total = coll.count_documents({})
    lines = [f'<b>{ADMIN_EMOJI_USERLIST}代理用户列表</b>', '', f'代理ID：<code>{agent_bot_id}</code>', f'总用户数：<code>{total}</code>']
    if not rows:
        lines.extend(['', '暂无用户'])
    for row in rows:
        username = str(row.get('username') or '').strip()
        username_text = f'@{html.escape(username, quote=False)}' if username else '未设置'
        lines.extend([
            '',
            f'ID：<code>{row.get("user_id")}</code>',
            f'用户名：{username_text}',
            f'余额：<code>{standard_num(row.get("USDT", 0))} USDT</code>',
            f'累计件数：<code>{int(row.get("zgsl", 0) or 0)}</code>',
            f'累计消费：<code>{standard_num(row.get("zgje", 0))} USDT</code>',
        ])
    return '\n'.join(lines), total


def build_agent_users_keyboard(user_id, bot_id, page, total, page_size=12):
    keyboard = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}上一页', callback_data=f'agentusers {bot_id}:{page - 1}'))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(f'{MOOD_EMOJI_FAST}下一页', callback_data=f'agentusers {bot_id}:{page + 1}'))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE_LIST}返回代理详情', callback_data=f'agentinfo {bot_id}')])
    keyboard.append([InlineKeyboardButton('⬅️返回主界面', callback_data='backstart'), InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}关闭', callback_data=f'close {user_id}')])
    return keyboard


def send_clone_success_notice(context, requester_user_id, result, fee_paid='0'):
    requester = user.find_one({'user_id': requester_user_id}) or {}
    requester_name = str(requester.get('fullname', '') or '').replace('<', '').replace('>', '') or str(requester_user_id)
    requester_username = str(requester.get('username', '') or '').strip()
    fee_text = format_clone_price(fee_paid)
    text = f'''
<b>[emoji:4988174149991007503:🥳] 有人克隆了你的机器人</b>

克隆用户：<a href="tg://user?id={requester_user_id}">{requester_name}</a> @{requester_username}
机器人：@{result['bot_username']}
管理员：<code>{requester_user_id}</code>
支付金额：<code>{fee_text} USDT</code>

目录：<code>{result['clone_dir']}</code>
数据库：<code>{result['db_name']}</code>
Bot服务：<code>{result['service_name']}.service</code>
监听服务：<code>{result['listener_service_name']}.service</code>
    '''
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}克隆列表', callback_data='clonelist 0')]])
    notify_source_admins(context, text, reply_markup=keyboard)


def remove_clone_instance(bot_id, deleted_by=None, source_bot_id=None, source_db_name=None):
    record = clone_instances.find_one({'bot_id': str(bot_id), 'state': {'$ne': 'deleted'}})
    if record is None:
        raise RuntimeError('未找到这个克隆实例')

    if source_bot_id is not None and str(record.get('bot_id') or '') == str(source_bot_id):
        raise RuntimeError('不能删除当前源机器人实例')

    db_name = str(record.get('db_name') or '').strip()
    if source_db_name and db_name == str(source_db_name):
        raise RuntimeError('检测到当前源机器人的数据库名，已阻止删除。大概率是误用了源机器人的 Bot Token 去克隆。')

    for service_name in [record.get('service_name'), record.get('listener_service_name')]:
        if not service_name:
            continue
        service_unit = f'{service_name}.service'
        try:
            run_system_command(['systemctl', 'disable', '--now', service_unit], timeout=25)
        except Exception:
            try:
                run_system_command(['systemctl', 'stop', service_unit], timeout=20)
            except Exception:
                pass
        service_path = Path('/etc/systemd/system') / service_unit
        try:
            if service_path.exists():
                service_path.unlink()
        except Exception:
            pass
        try:
            run_system_command(['systemctl', 'reset-failed', service_unit], timeout=15)
        except Exception:
            pass

    try:
        run_system_command(['systemctl', 'daemon-reload'], timeout=20)
    except Exception:
        pass

    clone_dir = str(record.get('clone_dir') or '').strip()
    if clone_dir:
        shutil.rmtree(clone_dir, ignore_errors=True)

    if db_name:
        try:
            teleclient.drop_database(db_name)
        except Exception:
            pass

    clone_instances.delete_one({'_id': record['_id']})
    return record


def finish_clone_delete_in_background(context, user_id, bot_id, source_bot_id=None, source_db_name=None):
    try:
        record = remove_clone_instance(bot_id, deleted_by=user_id, source_bot_id=source_bot_id, source_db_name=source_db_name)
    except Exception as exc:
        clone_instances.update_one(
            {'bot_id': str(bot_id), 'state': 'deleting'},
            {'$set': {'state': 'active'}, '$unset': {'deleting_at': ''}}
        )
        try:
            context.bot.send_message(chat_id=user_id, text=f'删除克隆失败：{exc}')
        except Exception:
            pass
        return

    requester_user_id = record.get('requester_user_id')
    bot_username = str(record.get('bot_username') or '').strip()
    display_bot = f'@{bot_username}' if bot_username else str(record.get("bot_id"))
    text = f'[emoji:5312028599803460968:🆗] 已删除克隆实例\n\n[emoji:5287684458881756303:🤖] 机器人：{display_bot}\n[emoji:6321041414067068140:👤] 管理员：{requester_user_id}'
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}返回克隆列表', callback_data='clonelist 0')]])
    try:
        context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
    except Exception:
        pass

    notify_source_admins(
        context,
        f'<b>{ADMIN_EMOJI_CLOSE}克隆实例已删除</b>\n\n[emoji:5287684458881756303:🤖] 机器人：{display_bot}\n[emoji:6321041414067068140:👤] 管理员：<code>{requester_user_id}</code>\n[emoji:6321041414067068140:👤] 删除人：<code>{user_id}</code>',
        exclude_user_ids=[user_id]
    )


def get_okpay_shop_id():
    return str(get_text_config('OKPay商户ID', OKPAY_SHOP_ID) or '').strip()


def get_okpay_shop_token():
    return str(get_text_config('OKPayToken', OKPAY_SHOP_TOKEN) or '').strip()


def get_okpay_name():
    return str(get_text_config('OKPay名称', OKPAY_NAME) or '').strip()


def get_okpay_bot_username(bot=None):
    if bot is not None:
        username = getattr(bot, 'username', '') or ''
        if username:
            return str(username).strip().lstrip('@')
    username = getattr(OKPAY_BOT, 'username', '') if OKPAY_BOT is not None else ''
    if username:
        return str(username).strip().lstrip('@')
    return str(get_text_config('OKPay机器人用户名', OKPAY_BOT_USERNAME) or '').strip().lstrip('@')


def refresh_okpay_entry_status():
    enabled = bool(get_okpay_shop_id() and get_okpay_shop_token() and get_okpay_name())
    set_text_config('OKPay入口开启', 1 if enabled else 0)
    return enabled


def okpay_entry_enabled():
    row = shangtext.find_one({'projectname': 'OKPay入口开启'})
    if row is not None:
        value = row.get('text')
        return str(value).strip() in ('1', 'true', 'True', 'yes', 'on')
    if okpay_enabled():
        return True
    return SHOW_OKPAY_RECHARGE_ENTRY


def is_valid_trc20_address(address):
    if not isinstance(address, str):
        return False
    return re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}', address.strip()) is not None


def format_usdt_amount(value, places='0.0001'):
    amount = Decimal(str(value)).quantize(Decimal(places))
    text = format(amount, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


TOPUP_STATE_PENDING = 0
TOPUP_STATE_PAID = 1
TOPUP_STATE_EXPIRED = 2
TOPUP_STATE_CANCELED = 3
TOPUP_STATE_PROCESSING = 4


def current_ts_ms():
    return int(time.time() * 1000)


def build_topup_expire_ts_ms(created_ts_ms, minutes=10):
    return int(created_ts_ms) + int(minutes * 60 * 1000)


def build_topup_order_id(prefix, user_id):
    return f"{prefix}{current_ts_ms()}{user_id}{uuid.uuid4().hex[:6].upper()}"


def parse_decimal_amount(value, places='0.01'):
    return Decimal(str(value)).quantize(Decimal(places))


def allocate_trc20_pay_amount(base_amount, user_id):
    base = Decimal(str(base_amount)).quantize(Decimal('0.0001'))
    rng = random.SystemRandom()
    pending_amounts = set()
    for row in topup.find({'type': 'trc20', 'state': TOPUP_STATE_PENDING}, {'pay_amount_text': 1}):
        pay_amount_text = row.get('pay_amount_text')
        if pay_amount_text:
            pending_amounts.add(str(pay_amount_text))

    recent_suffixes = set()
    recent_rows = topup.find(
        {'type': 'trc20', 'user_id': user_id},
        {'pay_amount_text': 1, 'requested_amount': 1, 'money': 1},
        sort=[('timer', -1)],
        limit=50
    )
    for row in recent_rows:
        requested_amount = row.get('requested_amount', row.get('money', 0))
        try:
            requested_base = Decimal(str(requested_amount)).quantize(Decimal('0.0001'))
        except Exception:
            continue
        if requested_base != base:
            continue
        pay_amount_text = str(row.get('pay_amount_text') or '').strip()
        if not pay_amount_text:
            continue
        try:
            suffix = int((Decimal(pay_amount_text) - base) * Decimal('10000'))
        except Exception:
            continue
        if 1 <= suffix <= 9000:
            recent_suffixes.add(suffix)

    preferred_suffixes = [suffix for suffix in range(1, 9001) if suffix not in recent_suffixes]
    fallback_suffixes = [suffix for suffix in range(1, 9001) if suffix in recent_suffixes]
    rng.shuffle(preferred_suffixes)
    rng.shuffle(fallback_suffixes)

    for suffix in preferred_suffixes + fallback_suffixes:
        pay_amount = (base + (Decimal(suffix) / Decimal('10000'))).quantize(Decimal('0.0001'))
        pay_amount_text = format_usdt_amount(pay_amount)
        if pay_amount_text not in pending_amounts:
            return pay_amount, pay_amount_text
    raise RuntimeError('当前待支付TRC20订单过多，请稍后重试')


def okpay_enabled():
    return bool(get_okpay_shop_id() and get_okpay_shop_token())


def okpay_sign(data):
    shop_id = get_okpay_shop_id()
    shop_token = get_okpay_shop_token()
    if not shop_id or not shop_token:
        raise RuntimeError('OKPay未配置，请先在后台设置商户ID和Token')
    data = dict(data)
    data['id'] = shop_id
    data = {k: v for k, v in data.items() if v is not None and v != ''}
    data = OrderedDict(sorted(data.items()))
    query = urllib.parse.urlencode(data, quote_via=urllib.parse.quote)
    query = urllib.parse.unquote(query)
    data['sign'] = hashlib.md5((query + '&token=' + shop_token).encode()).hexdigest().upper()
    return dict(data)


def okpay_post(api_name, data):
    url = OKPAY_API_URL.rstrip('/') + '/' + api_name.lstrip('/')
    response = requests.post(url, data=okpay_sign(data), timeout=20)
    response.raise_for_status()
    return response.json()


def okpay_pay_link(unique_id, amount, coin='USDT', include_callback=True, bot=None):
    okpay_name = get_okpay_name() or 'OKPay'
    okpay_bot_username = get_okpay_bot_username(bot)
    data = {
        'unique_id': unique_id,
        'name': f'{okpay_name}充值',
        'amount': amount,
        'return_url': f'https://t.me/{okpay_bot_username}' if okpay_bot_username else 'https://t.me/',
        'coin': coin
    }
    if include_callback and OKPAY_CALLBACK_URL:
        data['callback_url'] = OKPAY_CALLBACK_URL
    return okpay_post('payLink', data)


def okpay_check_deposit(unique_id):
    return okpay_post('checkDeposit', {
        'unique_id': unique_id,
    })


def okpay_build_query(data):
    pairs = []
    for key in sorted(data.keys()):
        value = data[key]
        if value is None or value == '':
            continue
        encoded_key = urllib.parse.quote(str(key), safe='[]')
        encoded_value = urllib.parse.quote(str(value), safe='+-')
        pairs.append(f'{encoded_key}={encoded_value}')
    return '&'.join(pairs)


def okpay_build_nested_callback_query(data):
    normal = {}
    nested_data = {}
    for key, value in data.items():
        m = re.fullmatch(r'data\[([^\]]+)\]', str(key))
        if m:
            nested_data[m.group(1)] = value
        else:
            normal[key] = value

    parts = []
    for key in sorted(normal.keys()):
        if key == 'data':
            continue
        encoded_key = urllib.parse.quote(str(key), safe='[]')
        encoded_value = urllib.parse.quote(str(normal[key]), safe='+-')
        parts.append(f'{encoded_key}={encoded_value}')
        if key == 'code' and nested_data:
            for nk in ['order_id', 'unique_id', 'pay_user_id', 'amount', 'coin', 'status', 'type']:
                if nk in nested_data and nested_data[nk] not in (None, ''):
                    parts.append(f'data[{nk}]={urllib.parse.quote(str(nested_data[nk]), safe="+-")}')
            for nk in sorted(k for k in nested_data.keys() if k not in ['order_id', 'unique_id', 'pay_user_id', 'amount', 'coin', 'status', 'type']):
                if nested_data[nk] not in (None, ''):
                    parts.append(f'data[{nk}]={urllib.parse.quote(str(nested_data[nk]), safe="+-")}')
    if nested_data and 'code' not in normal:
        for nk in ['order_id', 'unique_id', 'pay_user_id', 'amount', 'coin', 'status', 'type']:
            if nk in nested_data and nested_data[nk] not in (None, ''):
                parts.append(f'data[{nk}]={urllib.parse.quote(str(nested_data[nk]), safe="+-")}')
    return '&'.join(parts)


def okpay_verify_callback(data):
    data = dict(data)
    shop_token = get_okpay_shop_token()
    in_sign = data.pop('sign', '')
    if not in_sign or not shop_token:
        return False
    data = {k: v for k, v in data.items() if v is not None and v != ''}
    queries = [okpay_build_query(data), okpay_build_nested_callback_query(data)]
    for query in queries:
        sign = hashlib.md5((query + '&token=' + shop_token).encode()).hexdigest().upper()
        if sign == in_sign:
            return True
    return False


def send_admin_topup_notice(bot, text):
    if bot is None or not text:
        return
    for admin_row in list(user.find({'state': '4'}, {'user_id': 1})):
        admin_user_id = admin_row.get('user_id')
        if not admin_user_id:
            continue
        try:
            bot.send_message(
                chat_id=admin_user_id,
                text=text,
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
        except Exception:
            continue


def okpay_mark_deposit_paid(payload):
    unique_id = payload.get('data[unique_id]') or payload.get('unique_id')
    amount = payload.get('data[amount]') or payload.get('amount')
    coin = payload.get('data[coin]') or payload.get('coin') or 'USDT'
    pay_user_id = payload.get('data[pay_user_id]') or payload.get('pay_user_id')
    order_id = payload.get('data[order_id]') or payload.get('order_id')
    pay_status = str(payload.get('data[status]') or payload.get('status') or '')
    pay_type = payload.get('data[type]') or payload.get('type') or 'deposit'

    if not unique_id or pay_type != 'deposit' or pay_status != '1':
        return False, 'not_paid'

    order = topup.find_one({'bianhao': unique_id})
    if order is None:
        return False, 'order_not_found'
    if order.get('state') == TOPUP_STATE_PAID:
        return True, 'already_paid'
    if order.get('state') == TOPUP_STATE_PROCESSING:
        return False, 'order_processing'
    if order.get('state') != TOPUP_STATE_PENDING:
        return False, 'order_expired'

    expire_ts_ms = int(order.get('expire_ts_ms') or 0)
    if expire_ts_ms and current_ts_ms() > expire_ts_ms:
        topup.update_one({'bianhao': unique_id, 'state': TOPUP_STATE_PENDING}, {'$set': {'state': TOPUP_STATE_EXPIRED, 'expired_timer': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), 'status': -1}})
        return False, 'order_expired'

    expected_coin = str(order.get('coin') or 'USDT').strip().upper()
    paid_coin = str(coin or expected_coin).strip().upper()
    if paid_coin != expected_coin:
        return False, 'coin_mismatch'

    try:
        expected_amount = parse_decimal_amount(order.get('money', 0))
        paid_amount = parse_decimal_amount(amount)
    except Exception:
        return False, 'invalid_amount'

    if expected_amount <= 0 or paid_amount <= 0:
        return False, 'invalid_amount'
    if paid_amount != expected_amount:
        return False, 'amount_mismatch'

    user_id = order['user_id']
    if user.find_one({'user_id': user_id}, {'_id': 1}) is None:
        return False, 'user_not_found'

    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    paid_amount_float = float(paid_amount)
    claimed_order = topup.find_one_and_update(
        {'_id': order['_id'], 'state': TOPUP_STATE_PENDING},
        {'$set': {
            'state': TOPUP_STATE_PROCESSING,
            'processing_timer': timer,
            'processing_amount': paid_amount_float,
            'processing_coin': paid_coin,
            'processing_order_id': order_id,
            'processing_pay_user_id': pay_user_id
        }},
        return_document=ReturnDocument.BEFORE
    )
    if claimed_order is None:
        latest_order = topup.find_one({'_id': order['_id']}, {'state': 1}) or {}
        latest_state = latest_order.get('state')
        if latest_state == TOPUP_STATE_PAID:
            return True, 'already_paid'
        if latest_state == TOPUP_STATE_PROCESSING:
            return False, 'order_processing'
        return False, 'order_expired'

    updated_user = user.find_one_and_update(
        {'user_id': user_id},
        {'$inc': {'USDT': paid_amount_float}},
        return_document=ReturnDocument.AFTER
    )
    if updated_user is None:
        topup.update_one(
            {'_id': order['_id'], 'state': TOPUP_STATE_PROCESSING},
            {'$set': {'state': TOPUP_STATE_PENDING}, '$unset': {
                'processing_timer': '',
                'processing_amount': '',
                'processing_coin': '',
                'processing_order_id': '',
                'processing_pay_user_id': ''
            }}
        )
        return False, 'user_not_found'

    now_money = standard_num(updated_user.get('USDT', 0))
    now_money = float(now_money) if str(now_money).count('.') > 0 else int(now_money)
    finalize_result = topup.update_one({'_id': order['_id'], 'state': TOPUP_STATE_PROCESSING}, {'$set': {
        'state': TOPUP_STATE_PAID,
        'status': 1,
        'paid_timer': timer,
        'paid_ts_ms': current_ts_ms(),
        'okpay_order_id': order_id,
        'pay_user_id': pay_user_id,
        'coin': paid_coin,
        'paid_amount': paid_amount_float
    }, '$unset': {
        'processing_timer': '',
        'processing_amount': '',
        'processing_coin': '',
        'processing_order_id': '',
        'processing_pay_user_id': ''
    }})
    if finalize_result.modified_count != 1:
        logging.error('OKPay订单状态落库失败，订单进入processing保护态: %s', unique_id)
        return False, 'order_finalize_failed'

    apply_referral_commission(OKPAY_BOT, user_id, paid_amount_float, unique_id, 'okpay', timer)
    user_logging(unique_id, 'OKPay充值', user_id, paid_amount_float, timer)

    user_row = user.find_one({'user_id': user_id}, {'fullname': 1, 'username': 1}) or {}
    display_name = html.escape(str(user_row.get('fullname') or user_id).replace('<', '').replace('>', ''), quote=False)
    username = str(user_row.get('username') or '').strip().lstrip('@')
    username_text = f' @{html.escape(username, quote=False)}' if username else ''
    admin_notify_text = (
        f'用户: <a href="tg://user?id={user_id}">{display_name}</a>{username_text} OKPay充值成功\n'
        f'订单号: <code>{html.escape(str(unique_id), quote=False)}</code>\n'
        f'充值: {paid_amount_float} {html.escape(paid_coin, quote=False)}\n'
        f'OKPay订单: <code>{html.escape(str(order_id or "-"), quote=False)}</code>'
    )

    if OKPAY_BOT is not None:
        try:
            notify_text = f'<b>✅ OKPay充值到账：{paid_amount_float} {paid_coin}\n\n💳 当前余额：{now_money} USDT</b>'
            if get_user_lang(user_id) == 'en':
                notify_text = translate_text(notify_text, 'en')
            OKPAY_BOT.send_message(
                chat_id=user_id,
                text=notify_text,
                parse_mode='HTML'
            )
        except Exception as exc:
            print(f'OKPay到账通知失败: {exc}')
        send_admin_topup_notice(OKPAY_BOT, admin_notify_text)
    return True, 'paid'


def okpay_normalize_check_deposit_result(result):
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, dict):
        data = result if isinstance(result, dict) else {}
    unique_id = data.get('unique_id') or result.get('unique_id') if isinstance(result, dict) else None
    order_id = data.get('order_id') or result.get('order_id') if isinstance(result, dict) else None
    amount = data.get('amount') or result.get('amount') if isinstance(result, dict) else None
    status = str(data.get('status') or result.get('status') or '') if isinstance(result, dict) else ''
    coin = data.get('coin') or result.get('coin') if isinstance(result, dict) else None
    pay_type = data.get('type') or result.get('type') if isinstance(result, dict) else None
    return {
        'unique_id': unique_id,
        'order_id': order_id,
        'amount': amount,
        'status': status,
        'coin': coin or 'USDT',
        'type': pay_type or 'deposit',
    }


def okpay_check_and_credit(unique_id):
    result = okpay_check_deposit(unique_id)
    payload = okpay_normalize_check_deposit_result(result)
    if payload.get('status') != '1':
        return False, 'not_paid', result
    ok, msg = okpay_mark_deposit_paid(payload)
    return ok, msg, result


class OkpayCallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OKPay callback server is running')

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length).decode('utf-8', errors='ignore')
        if 'application/json' in self.headers.get('Content-Type', ''):
            body = json.loads(raw or '{}')
            payload = {}
            for k, v in body.items():
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        payload[f'{k}[{kk}]'] = vv
                else:
                    payload[k] = v
        else:
            parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
            payload = {k: v[-1] for k, v in parsed.items()}

        if not okpay_verify_callback(payload):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'bad sign')
            return

        ok, msg = okpay_mark_deposit_paid(payload)
        self.send_response(200 if ok else 400)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'status': 'success' if ok else 'error', 'message': msg}).encode())


def start_okpay_callback_server(bot):
    global OKPAY_BOT, OKPAY_HTTPD
    OKPAY_BOT = bot
    if not okpay_enabled():
        print('OKPay未配置，跳过回调服务')
        return
    if OKPAY_HTTPD is not None:
        return
    try:
        OKPAY_HTTPD = ThreadingHTTPServer((OKPAY_CALLBACK_HOST, OKPAY_CALLBACK_PORT), OkpayCallbackHandler)
        t = threading.Thread(target=OKPAY_HTTPD.serve_forever, daemon=True)
        t.start()
        print(f'OKPay回调服务已启动: {OKPAY_CALLBACK_HOST}:{OKPAY_CALLBACK_PORT}')
    except Exception as exc:
        print(f'OKPay回调服务启动失败: {exc}')


async def on_post_init(application):
    global APP_EVENT_LOOP
    APP_EVENT_LOOP = asyncio.get_running_loop()
    start_okpay_callback_server(SyncTelegramProxy(application.bot, lambda: APP_EVENT_LOOP))
    warm_storefront_translation_cache(lang='en')


def create_trc20_deposit_order(context, user_id, amount):
    lang = get_user_lang(user_id)
    trc20 = get_trc20_address()
    if not is_valid_trc20_address(trc20):
        context.bot.send_message(chat_id=user_id, text=translate_text('TRC20充值地址未正确配置，请先联系管理员设置有效地址', lang))
        return

    amount = Decimal(str(amount)).quantize(Decimal('0.0001'))
    if amount <= 0:
        context.bot.send_message(chat_id=user_id, text=translate_text('充值金额必须大于0', lang))
        return

    created_ts_ms = current_ts_ms()
    expire_ts_ms = build_topup_expire_ts_ms(created_ts_ms, minutes=10)
    created_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created_ts_ms / 1000))
    deadline_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expire_ts_ms / 1000))
    bianhao = build_topup_order_id('TRC20', user_id)
    topup.update_many({'user_id': user_id, 'type': 'trc20', 'state': TOPUP_STATE_PENDING}, {'$set': {'state': TOPUP_STATE_CANCELED, 'canceled_timer': created_time, 'cancel_reason': 'recreated'}})

    reserved_id = None
    pay_amount = None
    pay_amount_text = None
    for _ in range(30):
        try:
            pay_amount, pay_amount_text = allocate_trc20_pay_amount(amount, user_id)
        except Exception as exc:
            context.bot.send_message(chat_id=user_id, text=translate_text(f'创建TRC20充值订单失败：{exc}', lang))
            return

        try:
            result = topup.insert_one({
                'bianhao': bianhao,
                'user_id': user_id,
                'money': float(pay_amount),
                'requested_amount': float(amount),
                'pay_amount': float(pay_amount),
                'pay_amount_text': pay_amount_text,
                'timer': created_time,
                'created_ts_ms': created_ts_ms,
                'expire_ts_ms': expire_ts_ms,
                'message_id': 0,
                'message_kind': 'photo',
                'type': 'trc20',
                'state': TOPUP_STATE_PENDING,
                'status': 0,
                'to_address': trc20,
                'coin': 'USDT'
            })
            reserved_id = result.inserted_id
            break
        except DuplicateKeyError:
            continue

    if reserved_id is None:
        context.bot.send_message(chat_id=user_id, text=translate_text('当前TRC20订单创建人数较多，请稍后重试', lang))
        return

    caption = f'''
<b>[emoji:6323075330189826977:😃] 充值详情</b>

[emoji:5350486389806868244:✅] 唯一收款地址：<code>{trc20}</code>
（推荐使用扫码转账更加安全 点击上方地址即可快速复制粘贴）

[emoji:4965219701572503640:💰] 实际支付金额：<code>{pay_amount_text} USDT</code>
（[emoji:5416117059207572332:➡️] 点击上方金额可快速复制粘贴）

[emoji:5370715226209525171:🔋]充值订单创建时间：{created_time}
[emoji:5370688996844249600:🪫]转账最后截止时间：{deadline_time}

❗️请一定按照金额后面小数点转账，否则无法自动到账
❗️付款前请再次核对地址与金额，避免转错
    '''
    keyboard = [
        [InlineKeyboardButton(translate_text('❌取消订单', lang), callback_data=f'qxdingdan {bianhao}')]
    ]
    if lang == 'en':
        caption = translate_text(caption, 'en')

    qr_image = qrcode.make(data=trc20)
    qr_buffer = io.BytesIO()
    qr_image.save(qr_buffer, format='PNG')
    qr_buffer.seek(0)
    qr_buffer.name = f'{bianhao}.png'

    try:
        message_id = context.bot.send_photo(
            chat_id=user_id,
            photo=qr_buffer,
            caption=caption,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as exc:
        topup.delete_one({'_id': reserved_id})
        context.bot.send_message(chat_id=user_id, text=translate_text(f'创建TRC20充值订单失败：{exc}', lang))
        return

    topup.update_one({'_id': reserved_id}, {'$set': {'message_id': message_id.message_id}})


def create_okpay_deposit_order(context, user_id, amount):
    lang = get_user_lang(user_id)
    if not refresh_okpay_entry_status():
        context.bot.send_message(chat_id=user_id, text=translate_text('OKPay未配置，请先联系管理员在后台配置商户ID、Token 和 名称', lang))
        return

    amount = standard_num(amount)
    amount = float(amount) if str(amount).count('.') > 0 else int(amount)
    if float(amount) <= 0:
        context.bot.send_message(chat_id=user_id, text=translate_text('充值金额必须大于0', lang))
        return

    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    created_ts_ms = current_ts_ms()
    expire_ts_ms = build_topup_expire_ts_ms(created_ts_ms, minutes=10)
    topup.update_many({'user_id': user_id, 'type': 'okpay', 'state': TOPUP_STATE_PENDING}, {'$set': {'state': TOPUP_STATE_CANCELED, 'canceled_timer': timer, 'cancel_reason': 'recreated'}})
    bianhao = build_topup_order_id('OKPAY', user_id)
    try:
        result = okpay_pay_link(bianhao, amount, 'USDT', bot=context.bot)
    except Exception as exc:
        context.bot.send_message(chat_id=user_id, text=translate_text(f'创建OKPay充值订单失败：{exc}', lang))
        return

    if isinstance(result, dict) and result.get('status') == 'error':
        msg = str(result.get('msg') or '')
        if 'callback_url' in msg and ('验证失败' in msg or '安全风险' in msg):
            try:
                result = okpay_pay_link(bianhao, amount, 'USDT', include_callback=False, bot=context.bot)
            except Exception as exc:
                context.bot.send_message(chat_id=user_id, text=translate_text(f'创建OKPay充值订单失败：{exc}', lang))
                return

    data = result.get('data') or {}
    pay_url = data.get('pay_url') or result.get('pay_url')
    okpay_order_id = data.get('order_id') or result.get('order_id')
    if not pay_url:
        context.bot.send_message(chat_id=user_id, text=translate_text(f'创建OKPay充值订单失败：{result}', lang))
        return

    text = f'''
<b>OKPay充值订单已创建</b>

订单号：<code>{bianhao}</code>
充值金额：<code>{amount} USDT</code>

请点击下面按钮完成支付，支付成功后系统会自动加余额。
    '''
    keyboard = [
        [InlineKeyboardButton(translate_text('💳 打开OKPay支付', lang), url=pay_url)],
        [InlineKeyboardButton(translate_text('✅ 我已支付', lang), callback_data=f'okpay_paid {bianhao}')],
        [InlineKeyboardButton(translate_text('❌取消订单', lang), callback_data=f'qxdingdan {bianhao}')]
    ]
    if lang == 'en':
        text = translate_text(text, 'en')
    message_id = context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    topup.insert_one({
        'bianhao': bianhao,
        'user_id': user_id,
        'money': float(amount),
        'timer': timer,
        'created_ts_ms': created_ts_ms,
        'expire_ts_ms': expire_ts_ms,
        'message_id': message_id.message_id,
        'type': 'okpay',
        'state': TOPUP_STATE_PENDING,
        'status': 0,
        'okpay_order_id': okpay_order_id,
        'pay_url': pay_url,
        'coin': 'USDT'
    })


def dabaohao(context, user_id, folder_names, leixing, nowuid, erjiprojectname, notice_text, yssj):
    zip_filename, added_files, missing_entries = build_delivery_zip(leixing, user_id, nowuid, folder_names)
    current_time = datetime.datetime.now()

    formatted_time = current_time.strftime("%Y%m%d%H%M%S")
    timestamp = str(current_time.timestamp()).replace(".", "")
    bianhao = formatted_time + timestamp
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    if missing_entries:
        logging.warning(
            'delivery package missing inventory files: leixing=%s nowuid=%s user_id=%s entries=%s',
            leixing,
            nowuid,
            user_id,
            ', '.join(missing_entries),
        )

    if added_files <= 0:
        failure_text = '这批库存文件没找到，暂时没法发货，请联系客服处理。'
        logging.error(
            'delivery package is empty: leixing=%s nowuid=%s user_id=%s requested=%s',
            leixing,
            nowuid,
            user_id,
            ', '.join(map(str, folder_names)),
        )
        goumaijilua(leixing, bianhao, user_id, erjiprojectname, '', failure_text, timer)
        context.bot.send_message(chat_id=user_id, text=failure_text)
        return

    goumaijilua(leixing, bianhao, user_id, erjiprojectname, str(zip_filename), notice_text, timer)
    with open(zip_filename, "rb") as document_fp:
        context.bot.send_document(chat_id=user_id, document=document_fp)
    if notice_text:
        context.bot.send_message(chat_id=user_id, text=notice_text, parse_mode='HTML', disable_web_page_preview=True)


def qrgaimai(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    bot_id = context.bot.id
    user_id = query.from_user.id
    lang = get_user_lang(user_id)
    fullname = query.from_user.full_name.replace('<', '').replace('>', '')
    username = query.from_user.username
    data = query.data.replace('qrgaimai ', '')
    data_parts = data.split(':')
    nowuid = data_parts[0]
    gmsl = int(data_parts[1]) if len(data_parts) > 1 and str(data_parts[1]).isdigit() else 0
    user_list = user.find_one({'user_id': user_id})
    USDT = user_list['USDT']
    if gmsl <= 0:
        context.bot.send_message(chat_id=user_id, text=translate_text('购买数量只能输入大于0的整数', lang))
        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
        return

    ejfl_list = ejfl.find_one({'nowuid': nowuid})
    if not ejfl_list:
        context.bot.send_message(chat_id=user_id, text=translate_text('未找到这个商品', lang))
        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
        return

    unit_price_raw = ejfl_list.get('money', 0)
    unit_price_decimal = Decimal(str(unit_price_raw or 0))
    zxymoney = standard_num(unit_price_decimal * Decimal(str(gmsl)))
    zxymoney = float(zxymoney) if str(zxymoney).count('.') > 0 else int(zxymoney)
    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
    if kc < gmsl:
        context.bot.send_message(chat_id=user_id, text=translate_text('当前库存不足', lang))
        return
    if zxymoney == 0:
        return
    if USDT >= zxymoney:
        now_price = standard_num(float(USDT) - float(zxymoney))
        now_price = float(now_price) if str((now_price)).count('.') > 0 else int(standard_num(now_price))

        fhtype = hb.find_one({'nowuid': nowuid})['leixing']
        projectname = ejfl_list['projectname']
        erjiprojectname = ejfl_list['projectname']
        yijiid = ejfl_list['uid']
        yiji_list = fenlei.find_one({'uid': yijiid})
        yijiprojectname = yiji_list['projectname']
        lang = get_user_lang(user_id)
        success_text = build_purchase_success_header(zxymoney, now_price, user_id=user_id)
        fstext = get_buy_notice_text(ejfl_list.get('text', ''))
        notice_text = str(fstext or '').strip()
        if lang == 'en' and notice_text:
            notice_text = translate_text(notice_text, 'en')
        account_check_runtime = get_account_check_runtime_status(fhtype) if fhtype in ACCOUNT_CHECK_SUPPORTED_TYPES else {'ready': False, 'reason': 'unsupported_entry_type'}
        use_account_check = ACCOUNT_CHECK_ENABLED and fhtype in ACCOUNT_CHECK_SUPPORTED_TYPES and bool(account_check_runtime.get('ready'))
        runtime_reason = ''
        if ACCOUNT_CHECK_ENABLED and fhtype in ACCOUNT_CHECK_SUPPORTED_TYPES and not account_check_runtime.get('ready'):
            runtime_reason = str(account_check_runtime.get('reason', 'account_check_runtime_unavailable'))
        if fhtype == '协议号':
            progress_message_id = None
            reused_progress_message = False
            if use_account_check:
                progress_message_id, reused_progress_message = begin_account_check_progress_message(context.bot, query, user_id, gmsl)
            order_id = create_delivery_order_id()
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            selected_docs, charged_user, reserve_state = reserve_inventory_and_charge({'nowuid': nowuid}, gmsl, user_id, order_id, timer, zxymoney)
            if reserve_state == 'stock':
                failure_text = translate_text('当前库存不足', lang)
                if use_account_check:
                    update_account_check_status_message(context.bot, user_id, progress_message_id, failure_text)
                else:
                    context.bot.send_message(chat_id=user_id, text=failure_text)
                user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                return
            if reserve_state == 'balance' or not charged_user:
                failure_text = translate_text('❌ 余额不足，请及时充值！', lang)
                if use_account_check:
                    update_account_check_status_message(context.bot, user_id, progress_message_id, failure_text)
                else:
                    context.bot.send_message(chat_id=user_id, text=failure_text)
                user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                return
            actual_balance = standard_num(charged_user.get('USDT', now_price))
            actual_balance = float(actual_balance) if str(actual_balance).count('.') > 0 else int(actual_balance)
            success_text = build_purchase_success_header(zxymoney, actual_balance, user_id=user_id)
            if not use_account_check:
                context.bot.send_message(chat_id=user_id, text=success_text, parse_mode='HTML', disable_web_page_preview=True)
                if runtime_reason:
                    warning_text = (
                        f'<b>{ACCOUNT_CHECK_EMOJI_TIMEOUT} 检测环境未就绪，本次未执行账号检测，已按原始库存直发。</b>\n\n'
                        f'<b>原因：</b> <code>{runtime_reason}</code>'
                    )
                    if lang == 'en':
                        warning_text = translate_text(warning_text, 'en')
                    send_html_message(context.bot, user_id, warning_text)
                    for admin_user in list(user.find({'state': '4'})):
                        try:
                            send_html_message(
                                context.bot,
                                admin_user['user_id'],
                                f'<b>{ACCOUNT_CHECK_EMOJI_TIMEOUT} 账号检测环境未就绪</b>\n\n商品类型: {fhtype}\n用户ID: <code>{user_id}</code>\n原因: <code>{runtime_reason}</code>'
                            )
                        except Exception:
                            pass
            if not use_account_check or not reused_progress_message:
                del_message(query.message)
            folder_names = [doc['projectname'] for doc in selected_docs]

            if use_account_check:
                selected_items = [{'hbid': doc['hbid'], 'projectname': doc['projectname']} for doc in selected_docs]
                threading.Thread(
                    target=deliver_accounts_with_check,
                    args=[
                        context, user_id, fullname, username, nowuid, erjiprojectname, yijiprojectname, '协议号',
                        selected_items, notice_text, order_id, float(zxymoney) / max(gmsl, 1), zxymoney,
                        progress_message_id
                    ],
                    daemon=True,
                ).start()
            else:
                fstext = f'''
用户: <a href="tg://user?id={user_id}">{fullname}</a> @{username}
用户ID: <code>{user_id}</code>
购买商品: {yijiprojectname}/{erjiprojectname}
购买数量: {gmsl}
购买金额: {zxymoney}
                '''
                for i in list(user.find({"state": '4'})):
                    try:
                        context.bot.send_message(chat_id=i['user_id'], text=fstext, parse_mode='HTML')
                    except:
                        pass

                Timer(1, dabaohao,
                      args=[context, user_id, folder_names, '协议号', nowuid, erjiprojectname, notice_text, timer]).start()
            # shijiancuo = int(time.time())
            # zip_filename = f"./协议号发货/{user_id}_{shijiancuo}.zip"
            # with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            #     # 将每个文件及其内容添加到 zip 文件中
            #     for file_name in folder_names:
            #         # 检查是否存在以 .json 或 .session 结尾的文件
            #         json_file_path = os.path.join(f"./协议号/{nowuid}", file_name + ".json")
            #         session_file_path = os.path.join(f"./协议号/{nowuid}", file_name + ".session")
            #         if os.path.exists(json_file_path):
            #             zipf.write(json_file_path, os.path.basename(json_file_path))
            #         if os.path.exists(session_file_path):
            #             zipf.write(session_file_path, os.path.basename(session_file_path))
            # current_time = datetime.datetime.now()

            # # 将当前时间格式化为字符串
            # formatted_time = current_time.strftime("%Y%m%d%H%M%S")

            # # 添加时间戳
            # timestamp = str(current_time.timestamp()).replace(".", "")

            # # 组合编号
            # bianhao = formatted_time + timestamp
            # timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            # goumaijilua('协议号', bianhao, user_id, erjiprojectname,zip_filename,fstext, timer)
            # # 发送 zip 文件给用户
            # query.message.reply_document(open(zip_filename, "rb"))



        elif fhtype == '谷歌':
            order_id = create_delivery_order_id()
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            selected_docs, charged_user, reserve_state = reserve_inventory_and_charge({'nowuid': nowuid, 'leixing': '谷歌'}, gmsl, user_id, order_id, timer, zxymoney)
            if reserve_state == 'stock':
                context.bot.send_message(chat_id=user_id, text=translate_text('当前库存不足', lang))
                user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                return
            if reserve_state == 'balance' or not charged_user:
                context.bot.send_message(chat_id=user_id, text=translate_text('❌ 余额不足，请及时充值！', lang))
                user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                return
            actual_balance = standard_num(charged_user.get('USDT', now_price))
            actual_balance = float(actual_balance) if str(actual_balance).count('.') > 0 else int(actual_balance)
            success_text = build_purchase_success_header(zxymoney, actual_balance, user_id=user_id)
            context.bot.send_message(chat_id=user_id, text=success_text, parse_mode='HTML', disable_web_page_preview=True)
            del_message(query.message)

            folder_names = []
            for j in selected_docs:
                data = j['data']
                us1 = data['账户']
                us2 = data['密码']
                us3 = data['子邮件']
                fste23xt = f'账户: {us1}\n密码: {us2}\n子邮件: {us3}\n'
                folder_names.append(fste23xt)

            folder_names = '\n'.join(folder_names)

            shijiancuo = int(time.time())
            zip_filename = f"./谷歌发货/{user_id}_{shijiancuo}.txt"
            with open(zip_filename, "w") as f:
                f.write(folder_names)
            current_time = datetime.datetime.now()

            # 将当前时间格式化为字符串
            formatted_time = current_time.strftime("%Y%m%d%H%M%S")

            # 添加时间戳
            timestamp = str(current_time.timestamp()).replace(".", "")

            # 组合编号
            bianhao = formatted_time + timestamp
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            goumaijilua('谷歌', bianhao, user_id, erjiprojectname, zip_filename, fstext, timer)

            query.message.reply_document(open(zip_filename, "rb"))
            if notice_text:
                context.bot.send_message(chat_id=user_id, text=notice_text, parse_mode='HTML', disable_web_page_preview=True)

            fstext = f'''
用户: <a href="tg://user?id={user_id}">{fullname}</a> @{username}
用户ID: <code>{user_id}</code>
购买商品: {yijiprojectname}/{erjiprojectname}
购买数量: {gmsl}
购买金额: {zxymoney}
            '''
            for i in list(user.find({"state": '4'})):
                try:
                    context.bot.send_message(chat_id=i['user_id'], text=fstext, parse_mode='HTML')
                except:
                    pass


        elif fhtype == 'API':
            order_id = create_delivery_order_id()
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            selected_docs, charged_user, reserve_state = reserve_inventory_and_charge({'nowuid': nowuid}, gmsl, user_id, order_id, timer, zxymoney)
            if reserve_state == 'stock':
                context.bot.send_message(chat_id=user_id, text=translate_text('当前库存不足', lang))
                user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                return
            if reserve_state == 'balance' or not charged_user:
                context.bot.send_message(chat_id=user_id, text=translate_text('❌ 余额不足，请及时充值！', lang))
                user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                return
            actual_balance = standard_num(charged_user.get('USDT', now_price))
            actual_balance = float(actual_balance) if str(actual_balance).count('.') > 0 else int(actual_balance)
            success_text = build_purchase_success_header(zxymoney, actual_balance, user_id=user_id)
            context.bot.send_message(chat_id=user_id, text=success_text, parse_mode='HTML', disable_web_page_preview=True)
            del_message(query.message)

            folder_names = [j['projectname'] for j in selected_docs]

            shijiancuo = int(time.time())

            zip_filename = f"./手机接码发货/{user_id}_{shijiancuo}.txt"
            with open(zip_filename, "w") as f:
                for folder_name in folder_names:
                    f.write(folder_name + "\n")

            current_time = datetime.datetime.now()

            # 将当前时间格式化为字符串
            formatted_time = current_time.strftime("%Y%m%d%H%M%S")

            # 添加时间戳
            timestamp = str(current_time.timestamp()).replace(".", "")

            # 组合编号
            bianhao = formatted_time + timestamp
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            goumaijilua('API链接', bianhao, user_id, erjiprojectname, zip_filename, fstext, timer)

            query.message.reply_document(open(zip_filename, "rb"))
            if notice_text:
                context.bot.send_message(chat_id=user_id, text=notice_text, parse_mode='HTML', disable_web_page_preview=True)

            fstext = f'''
用户: <a href="tg://user?id={user_id}">{fullname}</a> @{username}
用户ID: <code>{user_id}</code>
购买商品: {yijiprojectname}/{erjiprojectname}
购买数量: {gmsl}
购买金额: {zxymoney}
            '''
            for i in list(user.find({"state": '4'})):
                try:
                    context.bot.send_message(chat_id=i['user_id'], text=fstext, parse_mode='HTML')
                except:
                    pass
        elif fhtype == '会员链接':
            order_id = create_delivery_order_id()
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            selected_docs, charged_user, reserve_state = reserve_inventory_and_charge({'nowuid': nowuid}, gmsl, user_id, order_id, timer, zxymoney)
            if reserve_state == 'stock':
                context.bot.send_message(chat_id=user_id, text=translate_text('当前库存不足', lang))
                user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                return
            if reserve_state == 'balance' or not charged_user:
                context.bot.send_message(chat_id=user_id, text=translate_text('❌ 余额不足，请及时充值！', lang))
                user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                return
            actual_balance = standard_num(charged_user.get('USDT', now_price))
            actual_balance = float(actual_balance) if str(actual_balance).count('.') > 0 else int(actual_balance)
            success_text = build_purchase_success_header(zxymoney, actual_balance, user_id=user_id)
            context.bot.send_message(chat_id=user_id, text=success_text, parse_mode='HTML', disable_web_page_preview=True)
            del_message(query.message)
            folder_names = [j['projectname'] for j in selected_docs]

            folder_names = '\n'.join(folder_names)

            current_time = datetime.datetime.now()

            # 将当前时间格式化为字符串
            formatted_time = current_time.strftime("%Y%m%d%H%M%S")

            # 添加时间戳
            timestamp = str(current_time.timestamp()).replace(".", "")

            # 组合编号
            bianhao = formatted_time + timestamp
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            goumaijilua('会员链接', bianhao, user_id, erjiprojectname, folder_names, fstext, timer)

            if notice_text:
                context.bot.send_message(chat_id=user_id, text=notice_text, parse_mode='HTML', disable_web_page_preview=True)
            context.bot.send_message(chat_id=user_id, text=folder_names, disable_web_page_preview=True)

            fstext = f'''
用户: <a href="tg://user?id={user_id}">{fullname}</a> @{username}
用户ID: <code>{user_id}</code>
购买商品: {yijiprojectname}/{erjiprojectname}
购买数量: {gmsl}
购买金额: {zxymoney}
            '''
            for i in list(user.find({"state": '4'})):
                try:
                    context.bot.send_message(chat_id=i['user_id'], text=fstext, parse_mode='HTML')
                except:
                    pass
        else:
            progress_message_id = None
            reused_progress_message = False
            if use_account_check:
                progress_message_id, reused_progress_message = begin_account_check_progress_message(context.bot, query, user_id, gmsl)
            order_id = create_delivery_order_id()
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            selected_docs, charged_user, reserve_state = reserve_inventory_and_charge({'nowuid': nowuid}, gmsl, user_id, order_id, timer, zxymoney)
            if reserve_state == 'stock':
                failure_text = translate_text('当前库存不足', lang)
                if use_account_check:
                    update_account_check_status_message(context.bot, user_id, progress_message_id, failure_text)
                else:
                    context.bot.send_message(chat_id=user_id, text=failure_text)
                user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                return
            if reserve_state == 'balance' or not charged_user:
                failure_text = translate_text('❌ 余额不足，请及时充值！', lang)
                if use_account_check:
                    update_account_check_status_message(context.bot, user_id, progress_message_id, failure_text)
                else:
                    context.bot.send_message(chat_id=user_id, text=failure_text)
                user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                return
            actual_balance = standard_num(charged_user.get('USDT', now_price))
            actual_balance = float(actual_balance) if str(actual_balance).count('.') > 0 else int(actual_balance)
            success_text = build_purchase_success_header(zxymoney, actual_balance, user_id=user_id)
            if not use_account_check:
                context.bot.send_message(chat_id=user_id, text=success_text, parse_mode='HTML', disable_web_page_preview=True)
                if runtime_reason:
                    warning_text = (
                        f'<b>{ACCOUNT_CHECK_EMOJI_TIMEOUT} 检测环境未就绪，本次未执行账号检测，已按原始库存直发。</b>\n\n'
                        f'<b>原因：</b> <code>{runtime_reason}</code>'
                    )
                    if lang == 'en':
                        warning_text = translate_text(warning_text, 'en')
                    send_html_message(context.bot, user_id, warning_text)
                    for admin_user in list(user.find({'state': '4'})):
                        try:
                            send_html_message(
                                context.bot,
                                admin_user['user_id'],
                                f'<b>{ACCOUNT_CHECK_EMOJI_TIMEOUT} 账号检测环境未就绪</b>\n\n商品类型: {fhtype}\n用户ID: <code>{user_id}</code>\n原因: <code>{runtime_reason}</code>'
                            )
                        except Exception:
                            pass
            if not use_account_check or not reused_progress_message:
                del_message(query.message)

            folder_names = [doc['projectname'] for doc in selected_docs]

            if use_account_check:
                selected_items = [{'hbid': doc['hbid'], 'projectname': doc['projectname']} for doc in selected_docs]
                threading.Thread(
                    target=deliver_accounts_with_check,
                    args=[
                        context, user_id, fullname, username, nowuid, erjiprojectname, yijiprojectname, '直登号',
                        selected_items, notice_text, order_id, float(zxymoney) / max(gmsl, 1), zxymoney,
                        progress_message_id
                    ],
                    daemon=True,
                ).start()
            else:
                fstext = f'''
用户: <a href="tg://user?id={user_id}">{fullname}</a> @{username}
用户ID: <code>{user_id}</code>
购买商品: {yijiprojectname}/{erjiprojectname}
购买数量: {gmsl}
购买金额: {zxymoney}
                '''
                for i in list(user.find({"state": '4'})):
                    try:
                        context.bot.send_message(chat_id=i['user_id'], text=fstext, parse_mode='HTML')
                    except:
                        pass

                Timer(1, dabaohao,
                      args=[context, user_id, folder_names, '直登号', nowuid, erjiprojectname, notice_text, timer]).start()
            # shijiancuo = int(time.time())
            # zip_filename = f"./发货/{user_id}_{shijiancuo}.zip"
            # with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            #     # 将每个文件夹及其内容添加到 zip 文件中
            #     for folder_name in folder_names:
            #         full_folder_path = os.path.join(f"./号包/{nowuid}", folder_name)
            #         if os.path.exists(full_folder_path):
            #             # 添加文件夹及其内容
            #             for root, dirs, files in os.walk(full_folder_path):
            #                 for file in files:
            #                     file_path = os.path.join(root, file)
            #                     # 使用相对路径在压缩包中添加文件，并设置压缩包内部的路径
            #                     zipf.write(file_path, os.path.join(folder_name, os.path.relpath(file_path, full_folder_path)))
            #         else:
            #             # update.message.reply_text(f"文件夹 '{folder_name}' 不存在！")
            #             pass

            # # 发送 zip 文件给用户

            # folder_names = '\n'.join(folder_names)

            # current_time = datetime.datetime.now()

            # # 将当前时间格式化为字符串
            # formatted_time = current_time.strftime("%Y%m%d%H%M%S")

            # # 添加时间戳
            # timestamp = str(current_time.timestamp()).replace(".", "")

            # # 组合编号
            # bianhao = formatted_time + timestamp
            # timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            # goumaijilua('直登号', bianhao, user_id, erjiprojectname, zip_filename,fstext, timer)

            # query.message.reply_document(open(zip_filename, "rb"))




    else:
        context.bot.send_message(chat_id=user_id, text=translate_text('❌ 余额不足，请及时充值！', lang))
        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
        return


def qchuall(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    bot_id = context.bot.id
    user_id = query.from_user.id

    nowuid = query.data.replace('qchuall ', '')

    ejfl_list = ejfl.find_one({'nowuid': nowuid})
    fhtype = hb.find_one({'nowuid': nowuid})['leixing']
    projectname = ejfl_list['projectname']
    yijiid = ejfl_list['uid']
    yiji_list = fenlei.find_one({'uid': yijiid})
    yijiprojectname = yiji_list['projectname']

    folder_names = []
    if fhtype == '协议号':
        for j in list(hb.find({"nowuid": nowuid, 'state': 0})):
            projectname = j['projectname']
            hbid = j['hbid']
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            hb.delete_one({'hbid': hbid})
            folder_names.append(projectname)
        zip_filename, added_files, _ = build_delivery_zip(fhtype, user_id, nowuid, folder_names)
        if added_files > 0:
            query.message.reply_document(open(zip_filename, "rb"))
        else:
            query.message.reply_text("这批库存文件没找到，暂时没法发货。")

    elif fhtype == 'API':
        for j in list(hb.find({"nowuid": nowuid, 'state': 0})):
            projectname = j['projectname']
            hbid = j['hbid']
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            hb.delete_one({'hbid': hbid})
            folder_names.append(projectname)

        shijiancuo = int(time.time())

        zip_filename = f"./手机接码发货/{user_id}_{shijiancuo}.txt"
        with open(zip_filename, "w") as f:
            for folder_name in folder_names:
                f.write(folder_name + "\n")

        query.message.reply_document(open(zip_filename, "rb"))

    elif fhtype == '谷歌':
        for j in list(hb.find({"nowuid": nowuid, 'state': 0, 'leixing': '谷歌'})):
            projectname = j['projectname']
            hbid = j['hbid']
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            hb.update_one({'hbid': hbid}, {"$set": {'state': 1, 'yssj': timer, 'gmid': user_id}})
            data = j['data']
            us1 = data['账户']
            us2 = data['密码']
            us3 = data['子邮件']
            fste23xt = f'login: {us1}\npassword: {us2}\nsubmail: {us3}\n'
            hb.delete_one({'hbid': hbid})
            folder_names.append(fste23xt)
        folder_names = '\n'.join(folder_names)
        shijiancuo = int(time.time())

        zip_filename = f"./谷歌发货/{user_id}_{shijiancuo}.txt"
        with open(zip_filename, "w") as f:

            f.write(folder_names)

        query.message.reply_document(open(zip_filename, "rb"))


    elif fhtype == '会员链接':
        for j in list(hb.find({"nowuid": nowuid, 'state': 0})):
            projectname = j['projectname']
            hbid = j['hbid']
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            hb.delete_one({'hbid': hbid})
            folder_names.append(projectname)
        folder_names = '\n'.join(folder_names)

        context.bot.send_message(chat_id=user_id, text=folder_names, disable_web_page_preview=True)
    else:
        for j in list(hb.find({"nowuid": nowuid, 'state': 0})):
            projectname = j['projectname']
            hbid = j['hbid']
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            hb.delete_one({'hbid': hbid})
            folder_names.append(projectname)

        zip_filename, added_files, _ = build_delivery_zip(fhtype, user_id, nowuid, folder_names)
        if added_files > 0:
            query.message.reply_document(open(zip_filename, "rb"))
        else:
            query.message.reply_text("这批库存文件没找到，暂时没法发货。")

    ej_list = ejfl.find_one({'nowuid': nowuid})
    uid = ej_list['uid']
    ej_projectname = ej_list['projectname']
    money = ej_list['money']
    fl_pro = fenlei.find_one({'uid': uid})['projectname']
    keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
    ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
    fstext = f'''
主分类: {fl_pro}
二级分类: {ej_projectname}

价格: {money}U
库存: {kc}
已售: {ys}
    '''
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def qxdingdan(update: Update, context: CallbackContext):
    query = update.callback_query
    chat = query.message.chat
    query.answer()
    bot_id = context.bot.id
    chat_id = chat.id
    user_id = query.from_user.id
    order_id = query.data.replace('qxdingdan ', '', 1).strip()

    topup.update_one(
        {'bianhao': order_id, 'user_id': user_id, 'state': TOPUP_STATE_PENDING},
        {'$set': {'state': TOPUP_STATE_CANCELED, 'canceled_timer': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), 'cancel_reason': 'user_canceled'}}
    )
    safe_delete_message(context.bot, query.from_user.id, query.message.message_id, 'delete_cancel_topup_message')


def okpay_paid(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    lang = get_user_lang(user_id)
    query.answer(translate_text('正在检查支付状态，请稍候...', lang))
    unique_id = query.data.replace('okpay_paid ', '', 1).strip()
    order = topup.find_one({'bianhao': unique_id})
    if order is None or order.get('type') != 'okpay':
        context.bot.send_message(chat_id=user_id, text=translate_text('未找到对应的OKPay充值订单，请重新创建订单', lang))
        return
    if order.get('user_id') != user_id:
        context.bot.send_message(chat_id=user_id, text=translate_text('这笔订单不属于你，无法主动查单', lang))
        return
    if order.get('state') == TOPUP_STATE_PAID:
        context.bot.send_message(chat_id=user_id, text=translate_text('这笔OKPay订单已经到账，无需重复检查', lang))
        return
    if order.get('state') != TOPUP_STATE_PENDING:
        context.bot.send_message(chat_id=user_id, text=translate_text('这笔OKPay订单已失效，请重新创建订单', lang))
        return

    try:
        ok, msg, result = okpay_check_and_credit(unique_id)
    except Exception as exc:
        context.bot.send_message(chat_id=user_id, text=translate_text(f'查询OKPay订单失败：{exc}', lang))
        return

    if ok:
        keyboard = [[InlineKeyboardButton(translate_text('✅已到账（点击关闭）', lang), callback_data=f'close {user_id}')]]
        try:
            context.bot.edit_message_text(
                chat_id=user_id,
                message_id=query.message.message_id,
                text=translate_text('✅ OKPay订单已确认支付，余额已自动到账。', lang),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            pass
        return

    if msg == 'already_paid':
        context.bot.send_message(chat_id=user_id, text=translate_text('这笔OKPay订单已经到账，无需重复检查', lang))
        return
    if msg == 'order_expired':
        context.bot.send_message(chat_id=user_id, text=translate_text('这笔OKPay订单已超时失效，请重新创建订单', lang))
        return
    if msg == 'amount_mismatch':
        context.bot.send_message(chat_id=user_id, text=translate_text('检测到OKPay实际到账金额与订单金额不一致，系统已拒绝入账，请联系管理员核对。', lang))
        return
    if msg == 'coin_mismatch':
        context.bot.send_message(chat_id=user_id, text=translate_text('检测到OKPay到账币种与订单不一致，系统已拒绝入账，请联系管理员核对。', lang))
        return
    if msg in ('order_processing', 'order_finalize_failed'):
        context.bot.send_message(chat_id=user_id, text=translate_text('这笔OKPay订单正在处理中，请稍后再查看余额；如长时间未到账请联系管理员。', lang))
        return

    context.bot.send_message(
        chat_id=user_id,
        text=translate_text('暂未查询到这笔OKPay订单已付款，请确认支付成功后稍等几秒再点一次“我已支付”。', lang)
    )


def textkeyboard(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if chat.type == 'private':
        user_id = chat.id
        username = chat.username
        firstname = chat.first_name
        lastname = chat.last_name
        bot_id = context.bot.id
        fullname = chat.full_name.replace('<', '').replace('>', '')
        reply_to_message_id = update.effective_message.message_id
        timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        user_list = ensure_user_exists(user_id, username, fullname, lastname, getattr(update.effective_user, 'language_code', None))
        creation_time = user_list['creation_time']
        state = user_list['state']
        sign = user_list['sign']
        USDT = user_list['USDT']
        zgje = user_list['zgje']
        zgsl = user_list['zgsl']
        lang = get_user_lang(user_id, getattr(update.effective_user, 'language_code', None))
        raw_text = update.message.text or ''
        stored_text = get_message_storage_text(update.message) or raw_text
        text = get_message_match_text(update.message) or raw_text
        normalized_text = normalize_menu_text(text)
        zxh = update.message.text_html
        yyzt = shangtext.find_one({'projectname': '营业状态'})['text']
        if yyzt == 0:
            if state != '4':
                return

        get_key_list = list(get_key.find({}))
        get_prolist = []
        normalized_key_map = {}
        for i in get_key_list:
            projectname = i["projectname"]
            button_match_text = get_button_match_text(projectname)
            localized_projectname = localize_dynamic_text(projectname, user_id=user_id, lang=lang)
            localized_button_match_text = get_button_match_text(localized_projectname)
            get_prolist.extend([projectname, localized_projectname])
            if button_match_text != projectname:
                get_prolist.append(button_match_text)
            if localized_button_match_text != localized_projectname:
                get_prolist.append(localized_button_match_text)
            normalized_key_map.setdefault(normalize_menu_text(projectname), i)
            normalized_key_map.setdefault(normalize_menu_text(button_match_text), i)
            normalized_key_map.setdefault(normalize_menu_text(localized_projectname), i)
            normalized_key_map.setdefault(normalize_menu_text(localized_button_match_text), i)
        if update.message.text:
            if (raw_text in get_prolist or text in get_prolist or normalized_text in normalized_key_map) and not should_preserve_sign_on_menu_match(sign):
                sign = 0

        if matches_ui_text(text, 'language_toggle'):
            new_lang = toggle_user_lang(user_id)
            context.bot.send_message(chat_id=user_id, text=get_ui_text('language_switch_done', lang=new_lang))
            send_user_home(context, user_id)
            return

        if matches_ui_text(text, 'language_switch_zh') or matches_ui_text(text, 'language_switch_en'):
            new_lang = 'zh' if matches_ui_text(text, 'language_switch_zh') else 'en'
            if new_lang == 'en':
                warm_storefront_translation_cache(user_id=user_id, lang=new_lang, wait=True)
            set_user_lang(user_id, new_lang)
            if new_lang == 'en':
                warm_storefront_translation_cache(user_id=user_id, lang=new_lang)
            context.bot.send_message(chat_id=user_id, text=get_ui_text('language_switch_done', lang=new_lang))
            send_user_home(context, user_id)
            return
        if matches_ui_text(text, 'main_menu'):
            send_user_home(context, user_id)
            return
        if sign != 0:
            if update.message.text:

                if sign == 'addhb':
                    lang = get_user_lang(user_id)
                    if is_number(text):

                        money = float(text) if text.count('.') > 0 else int(text)
                        if money < 1:
                            context.bot.send_message(chat_id=user_id, text=translate_text('⚠️ 输入错误，最少金额不能小于1U', lang))
                            return
                        if USDT >= money:
                            keyboard = [[InlineKeyboardButton(translate_text('🚫取消', lang), callback_data=f'close {user_id}')]]
                            user.update_one({'user_id': user_id}, {"$set": {'sign': f'sethbsl {money}'}})
                            context.bot.send_message(chat_id=user_id, text=translate_text('<b>💡 请回复你要发送的红包数量</b>', lang),
                                                     parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

                        else:
                            user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                            context.bot.send_message(chat_id=user_id, text=translate_text('⚠️ 操作失败，余额不足', lang))
                    else:
                        context.bot.send_message(chat_id=user_id, text=translate_text('⚠️ 输入错误，请输入数字！', lang))
                elif 'sethbsl' in sign:
                    lang = get_user_lang(user_id)
                    money = sign.replace('sethbsl ', '')
                    money = float(money) if money.count('.') > 0 else int(money)

                    if is_number(text) and text.count('.') == 0:
                        hbsl = int(text)
                        if hbsl == 0:
                            context.bot.send_message(chat_id=user_id, text=translate_text('红包数量不能为0', lang))
                            return
                        if hbsl > 100:
                            context.bot.send_message(chat_id=user_id, text=translate_text('红包数量最大为100', lang))
                            return
                        user_list = user.find_one({"user_id": user_id})
                        USDT = user_list['USDT']
                        if USDT < money:
                            user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                            context.bot.send_message(chat_id=user_id, text=translate_text('⚠️ 操作失败，余额不足', lang))
                            return
                        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                        uid = generate_24bit_uid()
                        timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                        hongbao.insert_one({
                            'uid': uid,
                            'user_id': user_id,
                            'fullname': fullname,
                            'hbmoney': money,
                            'hbsl': hbsl,
                            'timer': timer,
                            'state': 0
                        })
                        now_money = standard_num(USDT - money)
                        now_money = float(now_money) if str((now_money)).count('.') > 0 else int(
                            standard_num(now_money))
                        user.update_one({'user_id': user_id}, {"$set": {'USDT': now_money}})
                        safe_fullname = html.escape(fullname, quote=False)
                        fstext = f'''
🧧 {safe_fullname} 发送了一个红包
💵总金额:{money} USDT💰 剩余:{hbsl}/{hbsl}

✅ 红包添加成功，请点击按钮发送
                        '''
                        if lang == 'en':
                            fstext = translate_text(fstext, 'en')
                        keyboard = [
                            [InlineKeyboardButton(translate_text('发送红包', lang), switch_inline_query=f'redpacket {uid}')]
                        ]

                        context.bot.send_message(chat_id=user_id, text=fstext,
                                                 reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

                    else:
                        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                        context.bot.send_message(chat_id=user_id, text=translate_text('⚠️ 输入错误，请输入数字！', lang))


                elif sign == 'startupdate_zh':
                    welcome_text = stored_text or text
                    shangtext.update_one({"projectname": '欢迎语'}, {"$set": {"text": welcome_text}})
                    shangtext.update_one({"projectname": '欢迎语样式'}, {"$set": {"text": pickle.dumps([])}})
                    translated_welcome_en = ''
                    try:
                        translated_welcome_en = str(translate_text(welcome_text, 'en') or '').strip()
                    except Exception:
                        logging.warning('Warm welcome translation cache failed for text=%r', welcome_text, exc_info=True)
                    if translated_welcome_en and translated_welcome_en != welcome_text:
                        shangtext.update_one(
                            {"projectname": '欢迎语英文'},
                            {"$set": {"projectname": '欢迎语英文', "text": translated_welcome_en}},
                            upsert=True
                        )
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'当前中文欢迎语为:\n\n{welcome_text}')
                elif sign == 'startupdate_en':
                    welcome_text_en = stored_text or text
                    shangtext.update_one(
                        {"projectname": '欢迎语英文'},
                        {"$set": {"projectname": '欢迎语英文', "text": welcome_text_en}},
                        upsert=True
                    )
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'当前英文欢迎语为:\n\n{welcome_text_en}')
                elif 'okzdycz' in sign:
                    if is_number(text):
                        del_message(update.message)
                        del_message_id = sign.replace('okzdycz ', '')
                        safe_delete_message(context.bot, user_id, del_message_id, 'delete_okpay_custom_amount_prompt')
                        money = float(text)
                        user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                        create_okpay_deposit_order(context, user_id, money)

                    else:
                        keyboard = [[InlineKeyboardButton(get_ui_text('cancel_input', viewer_user_id=user_id), callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text=get_ui_text('please_enter_number', viewer_user_id=user_id),
                                                 reply_markup=InlineKeyboardMarkup(keyboard))
                elif 'zdycz' in sign:
                    if is_number(text):
                        del_message(update.message)
                        del_message_id = sign.replace('zdycz ', '')
                        safe_delete_message(context.bot, user_id, del_message_id, 'delete_trc20_custom_amount_prompt')
                        money = float(text)
                        user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                        create_trc20_deposit_order(context, user_id, money)

                    else:
                        keyboard = [[InlineKeyboardButton(get_ui_text('cancel_input', viewer_user_id=user_id), callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text=get_ui_text('please_enter_number', viewer_user_id=user_id),
                                                 reply_markup=InlineKeyboardMarkup(keyboard))


                elif 'gmqq' in sign:
                    del_message(update.message)
                    data = sign.replace('gmqq ', '')
                    nowuid = data.split(':')[0]
                    del_message_id = data.split(':')[1]
                    safe_delete_message(context.bot, user_id, del_message_id, 'delete_buy_prompt_message')

                    ejfl_list = ejfl.find_one({'nowuid': nowuid})
                    projectname = ejfl_list['projectname']
                    money = ejfl_list['money']
                    uid = ejfl_list['uid']
                    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                    clean_text = text.strip()
                    if clean_text.isdigit():
                        gmsl = int(clean_text)
                        if gmsl <= 0:
                            keyboard = [[InlineKeyboardButton(get_ui_text('cancel_purchase', viewer_user_id=user_id), callback_data=f'close {user_id}')]]
                            context.bot.send_message(chat_id=user_id, text=get_ui_text('quantity_positive_integer', viewer_user_id=user_id),
                                                     reply_markup=InlineKeyboardMarkup(keyboard))
                            return

                        zxymoney = standard_num(gmsl * money)
                        zxymoney = float(zxymoney) if str((zxymoney)).count('.') > 0 else int(standard_num(zxymoney))
                        if kc < gmsl:
                            keyboard = [[InlineKeyboardButton(get_ui_text('cancel_purchase', viewer_user_id=user_id), callback_data=f'close {user_id}')]]
                            context.bot.send_message(chat_id=user_id, text=get_ui_text('stock_insufficient_retry', viewer_user_id=user_id),
                                                     reply_markup=InlineKeyboardMarkup(keyboard))

                            return

                        fstext = get_ui_text('purchase_confirm_text', viewer_user_id=user_id, projectname=localize_catalog_name(projectname, user_id), gmsl=gmsl, zxymoney=zxymoney, USDT=format_usdt_2(USDT))
                        keyboard = [
                            [InlineKeyboardButton(get_ui_text('cancel_trade', viewer_user_id=user_id), callback_data=f'close {user_id}'),
                             InlineKeyboardButton(get_ui_text('confirm_purchase', viewer_user_id=user_id), callback_data=f'qrgaimai {nowuid}:{gmsl}')],
                            [InlineKeyboardButton(get_ui_text('main_menu', viewer_user_id=user_id), callback_data='backzcd')]

                        ]
                        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                        context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                                 reply_markup=InlineKeyboardMarkup(keyboard))

                    else:
                        keyboard = [[InlineKeyboardButton(get_ui_text('cancel_purchase', viewer_user_id=user_id), callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text=get_ui_text('quantity_positive_integer_retry', viewer_user_id=user_id),
                                                 reply_markup=InlineKeyboardMarkup(keyboard))
                        # user.update_one({'user_id': user_id},{"$set":{'sign': 0}})

                        return
                elif 'upmoney' in sign:
                    if is_number(text):
                        nowuid = sign.replace('upmoney ', '')
                        money = float(text) if text.count('.') > 0 else int(text)
                        ejfl.update_one({"nowuid": nowuid}, {"$set": {"money": money}})
                        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})

                        ej_list = ejfl.find_one({'nowuid': nowuid})
                        uid = ej_list['uid']
                        ej_projectname = ej_list['projectname']
                        money = ej_list['money']
                        fl_pro = fenlei.find_one({'uid': uid})['projectname']
                        keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
                        kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                        ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
                        fstext = f'''
主分类: {fl_pro}
二级分类: {ej_projectname}

价格: {money}U
库存: {kc}
已售: {ys}
                        '''
                        context.bot.send_message(chat_id=user_id, text=fstext,
                                                 reply_markup=InlineKeyboardMarkup(keyboard))

                    else:
                        context.bot.send_message(chat_id=user_id, text=f'请输入数字', parse_mode='HTML')

                elif 'upejflname' in sign:
                    nowuid = sign.replace('upejflname ', '')
                    ejfl.update_one({"nowuid": nowuid}, {"$set": {"projectname": stored_text}})
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']
                    fl_pro = fenlei.find_one({'uid': uid})['projectname']
                    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], []]
                    ej_list = ejfl.find({'uid': uid})
                    for i in ej_list:
                        nowuid = i['nowuid']
                        projectname = i['projectname']
                        row = i['row']
                        keyboard[row - 1].append(
                            InlineKeyboardButton(f'{projectname}', callback_data=f'fejxxi {nowuid}'))

                    keyboard.append([InlineKeyboardButton('修改分类名', callback_data=f'upspname {uid}'),
                                     InlineKeyboardButton('新增二级分类', callback_data=f'newejfl {uid}')])
                    keyboard.append([InlineKeyboardButton('调整二级分类排序', callback_data=f'paixuejfl {uid}'),
                                     InlineKeyboardButton('删除二级分类', callback_data=f'delejfl {uid}')])
                    keyboard.append([InlineKeyboardButton('❌关闭', callback_data=f'close {user_id}')])
                    fstext = f'''
分类: {fl_pro}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))

                elif 'upspname' in sign:
                    uid = sign.replace('upspname ', '')
                    fenlei.update_one({"uid": uid}, {"$set": {"projectname": stored_text}})
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})

                    keylist = list(fenlei.find({}, sort=[('row', 1)]))
                    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], []]
                    for i in keylist:
                        uid = i['uid']
                        projectname = i['projectname']
                        row = i['row']
                        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}', callback_data=f'flxxi {uid}'))
                    keyboard.append([InlineKeyboardButton("新建一行", callback_data='newfl'),
                                     InlineKeyboardButton('调整行排序', callback_data='paixufl'),
                                     InlineKeyboardButton('删除一行', callback_data='delfl')])
                    context.bot.send_message(chat_id=user_id, text='商品管理',
                                             reply_markup=InlineKeyboardMarkup(keyboard))
                elif sign == 'settrc20':
                    if not is_valid_trc20_address(text):
                        keyboard = [[InlineKeyboardButton('❌取消输入', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text='地址格式错误，请输入以 T 开头、长度 34 位的 TRC20 地址',
                                                 reply_markup=InlineKeyboardMarkup(keyboard))
                        return
                    shangtext.update_one({"projectname": '充值地址'}, {"$set": {"text": text}})
                    img = qrcode.make(data=text)
                    with open(f'{text}.png', 'wb') as f:
                        img.save(f)
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'当前充值地址为: {text}', parse_mode='HTML')
                elif sign == 'setbuynotice':
                    set_text_config('购买提醒', ensure_buy_notice_bold(stored_text))
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    title_text, notice_text = build_buy_notice_config_text()
                    context.bot.send_message(chat_id=user_id, text='购买提醒文案已保存', parse_mode='HTML')
                    context.bot.send_message(chat_id=user_id, text=title_text, parse_mode='HTML')
                    context.bot.send_message(
                        chat_id=user_id,
                        text=notice_text,
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup(build_buy_notice_config_keyboard(user_id))
                    )
                elif sign == 'setrestocktarget':
                    target = text.strip()
                    if not target:
                        keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text='请输入群组/频道 @username 或 chat_id',
                                                 reply_markup=InlineKeyboardMarkup(keyboard))
                        return
                    set_text_config('补货通知群组', target)
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=build_restock_push_config_text(), parse_mode='HTML',
                                             reply_markup=InlineKeyboardMarkup(build_restock_push_config_keyboard(user_id)))
                elif sign == 'setokpayid':
                    set_text_config('OKPay商户ID', text.strip())
                    if refresh_okpay_entry_status():
                        start_okpay_callback_server(SyncTelegramProxy(context.bot, lambda: APP_EVENT_LOOP))
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'OKPay 商户ID 已保存\n\n{build_okpay_config_text()}', parse_mode='HTML', reply_markup=InlineKeyboardMarkup(build_okpay_config_keyboard(user_id)))
                elif sign == 'setokpaytoken':
                    set_text_config('OKPayToken', text.strip())
                    if refresh_okpay_entry_status():
                        start_okpay_callback_server(SyncTelegramProxy(context.bot, lambda: APP_EVENT_LOOP))
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'OKPay Token 已保存\n\n{build_okpay_config_text()}', parse_mode='HTML', reply_markup=InlineKeyboardMarkup(build_okpay_config_keyboard(user_id)))
                elif sign == 'setokpayname':
                    set_text_config('OKPay名称', text.strip())
                    if refresh_okpay_entry_status():
                        start_okpay_callback_server(SyncTelegramProxy(context.bot, lambda: APP_EVENT_LOOP))
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'OKPay 名称已保存\n\n{build_okpay_config_text()}', parse_mode='HTML', reply_markup=InlineKeyboardMarkup(build_okpay_config_keyboard(user_id)))
                elif sign == 'setcloneprice':
                    if not is_number(text):
                        keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消输入', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text='请输入数字，输入 0 表示免费', reply_markup=InlineKeyboardMarkup(keyboard))
                        return
                    price = Decimal(str(text)).quantize(Decimal('0.01'))
                    if price < 0:
                        price = Decimal('0')
                    set_text_config('一键克隆价格', format(price, 'f'))
                    user.update_one({'user_id': user_id}, {'$set': {'sign': 0}})
                    keyboard, total = build_clone_list_keyboard(user_id, 0)
                    text = f'<b>[emoji:5287684458881756303:🤖] 克隆列表</b>\n\n当前付费价格：<code>{format_clone_price(price)} USDT</code>\n活跃克隆数：<code>{total}</code>'
                    context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
                elif sign == 'clonebottoken':
                    lang = get_user_lang(user_id)
                    if not can_use_clonebot(state):
                        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                        context.bot.send_message(chat_id=user_id, text=get_clone_unavailable_text(user_id))
                        return
                    fee = get_clone_price_decimal()
                    fee_exempt = is_clone_fee_exempt(user_id, state)
                    clone_credit = get_user_clone_credit(user_id)
                    if fee > 0 and not fee_exempt and clone_credit <= 0:
                        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                        send_clonebot_prompt(context, user_id)
                        return
                    context.bot.send_message(
                        chat_id=user_id,
                        text='[emoji:5220195537520711716:⚡️] Cloning in progress, please wait…\n\n[emoji:5287684458881756303:🤖] New Bot Token received. Creating and starting your new Bot now.' if lang == 'en' else '[emoji:5220195537520711716:⚡️] 正在克隆中，请稍等…\n\n[emoji:5287684458881756303:🤖] 已收到新的 Bot Token，正在为你创建并启动新 Bot。',
                        parse_mode='HTML'
                    )
                    try:
                        result = clone_bot_instance(text.strip(), user_id, source_bot_id=context.bot.id)
                    except Exception as exc:
                        keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}Cancel Input' if lang == 'en' else f'{ADMIN_EMOJI_CLOSE}取消输入', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text=f'Clone This Bot failed: {exc}' if lang == 'en' else f'一键克隆失败：{exc}',
                                                 reply_markup=InlineKeyboardMarkup(keyboard))
                        return
                    update_doc = {'sign': 0}
                    if fee > 0 and not fee_exempt and clone_credit > 0:
                        update_doc['clone_credit'] = max(clone_credit - 1, 0)
                    user.update_one({'user_id': user_id}, {"$set": update_doc})
                    clone_instances.update_one(
                        {'bot_id': str(result['bot_id'])},
                        {'$set': {
                            'source_bot_id': str(context.bot.id),
                            'source_bot_username': str(getattr(context.bot, 'username', '') or ''),
                            'bot_id': str(result['bot_id']),
                            'bot_username': str(result['bot_username']),
                            'requester_user_id': user_id,
                            'requester_username': username or '',
                            'requester_name': fullname,
                            'clone_dir': result['clone_dir'],
                            'db_name': result['db_name'],
                            'service_name': result['service_name'],
                            'listener_service_name': result['listener_service_name'],
                            'created_at': timer,
                            'state': 'active',
                            'fee_paid': float(fee) if fee > 0 and not fee_exempt else 0,
                        }},
                        upsert=True
                    )
                    clone_text = f'''
[emoji:5312028599803460968:🆗] 一键克隆成功

[emoji:5287684458881756303:🤖] 机器人：@{result['bot_username']}
[emoji:6321041414067068140:👤] 管理员：{user_id}
                    '''
                    if lang == 'en':
                        clone_text = f'''
[emoji:5312028599803460968:🆗] Clone Successful

[emoji:5287684458881756303:🤖] Bot: @{result['bot_username']}
[emoji:6321041414067068140:👤] Admin: {user_id}
                    '''
                    context.bot.send_message(chat_id=user_id, text=clone_text, parse_mode='HTML')
                    send_clone_success_notice(context, user_id, result, fee_paid=(float(fee) if fee > 0 and not fee_exempt else 0))
                elif sign == 'agentbottoken':
                    if str(state) != '4' and user_id not in get_source_admin_user_ids():
                        user.update_one({'user_id': user_id}, {'$set': {'sign': 0}})
                        context.bot.send_message(chat_id=user_id, text='只有后台管理员可以创建代理 Bot')
                        return
                    context.bot.send_message(
                        chat_id=user_id,
                        text='[emoji:5220195537520711716:⚡️] 正在创建代理 Bot，请稍等…\n\n[emoji:5287684458881756303:🤖] 已收到新的代理 Token，正在自动克隆并注册 systemd。',
                        parse_mode='HTML'
                    )
                    try:
                        result = clone_agent_instance(text.strip(), user_id, source_bot_id=context.bot.id)
                    except Exception as exc:
                        keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}取消输入', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text=f'创建代理 Bot 失败：{exc}', reply_markup=InlineKeyboardMarkup(keyboard))
                        return
                    user.update_one({'user_id': user_id}, {'$set': {'sign': 0}})
                    clone_instances.update_one(
                        {'bot_id': str(result['bot_id'])},
                        {'$set': {
                            'source_bot_id': str(context.bot.id),
                            'source_bot_username': str(getattr(context.bot, 'username', '') or ''),
                            'bot_id': str(result['bot_id']),
                            'bot_username': str(result['bot_username']),
                            'requester_user_id': user_id,
                            'requester_username': username or '',
                            'requester_name': fullname,
                            'clone_dir': result['clone_dir'],
                            'db_name': result['db_name'],
                            'service_name': result['service_name'],
                            'listener_service_name': result.get('listener_service_name', ''),
                            'clone_kind': 'agent',
                            'created_at': timer,
                            'state': 'active',
                            'fee_paid': 0,
                        }},
                        upsert=True
                    )
                    context.bot.send_message(
                        chat_id=user_id,
                        text=f'[emoji:5312028599803460968:🆗] 代理 Bot 创建成功\n\n[emoji:5287684458881756303:🤖] 机器人：@{result["bot_username"]}\n[emoji:6321041414067068140:👤] 管理员：{user_id}\n[emoji:5132131004097496494:🧩] 服务：<code>{result["service_name"]}.service</code>',
                        parse_mode='HTML'
                    )
                elif 'setkeyname' in sign:
                    qudata = sign.replace('setkeyname ', '')
                    qudataall = qudata.split(':')
                    row = int(qudataall[0])
                    first = int(qudataall[1])
                    get_key.update_one({'Row': row, 'first': first}, {'$set': {'projectname': stored_text}})
                    keylist = list(get_key.find({}, sort=[('Row', 1), ('first', 1)]))
                    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                                [], [], [], [], [], [], [], [], []]
                    for i in keylist:
                        projectname = i['projectname']
                        row = i['Row']
                        first = i['first']
                        keyboard[i["Row"] - 1].append(
                            InlineKeyboardButton(projectname, callback_data=f'keyxq {row}:{first}'))
                    keyboard.append([InlineKeyboardButton('新建一行', callback_data='newrow'),
                                     InlineKeyboardButton('删除一行', callback_data='delrow'),
                                     InlineKeyboardButton('调整行排序', callback_data='paixurow')])
                    keyboard.append([InlineKeyboardButton('修改按钮', callback_data='newkey')])
                    user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                    context.bot.send_message(chat_id=user_id, text='自定义按钮',
                                             reply_markup=InlineKeyboardMarkup(keyboard))
                elif 'settuwenset' in sign:
                    qudata = sign.replace('settuwenset ', '')
                    qudataall = qudata.split(':')
                    row = int(qudataall[0])
                    first = int(qudataall[1])
                    entities = update.message.entities or []
                    save_text = stored_text or raw_text
                    save_entities = [] if has_custom_emoji_entities(entities) or needs_dynamic_emoji_parse(save_text) else entities
                    get_key.update_one({'Row': row, 'first': first}, {'$set': {'text': save_text}})
                    get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_id': ''}})
                    get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_type': 'text'}})
                    get_key.update_one({'Row': row, 'first': first}, {'$set': {'entities': pickle.dumps(save_entities)}})
                    user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                    send_key_save_success_notice(context, user_id)
                    send_key_content_preview(context, user_id, text=save_text, file_type='text',
                                             entities=save_entities)
                elif 'setkeyboard' in sign:
                    qudata = sign.replace('setkeyboard ', '')
                    qudataall = qudata.split(':')
                    row = int(qudataall[0])
                    first = int(qudataall[1])
                    text = text.replace('｜', '|').replace(' ', '')
                    keyboard = parse_urls(text)
                    dumped = pickle.dumps(keyboard)
                    try:
                        message_id = context.bot.send_message(chat_id=user_id, text=f'尾随按钮设置',
                                                              reply_markup=InlineKeyboardMarkup(keyboard))
                        get_key.update_one({'Row': row, 'first': first}, {"$set": {'keyboard': dumped}})
                        get_key.update_one({'Row': row, 'first': first}, {"$set": {'key_text': text}})
                        timer11 = Timer(3, del_message, args=[message_id])
                        timer11.start()
                    except:
                        keyboard = [[InlineKeyboardButton('格式配置错误,请检查', callback_data='ddd')]]
                        message_id = context.bot.send_message(chat_id=user_id, text='格式配置错误,请检查',
                                                              reply_markup=InlineKeyboardMarkup(keyboard))
                        timer11 = Timer(3, del_message, args=[message_id])
                        timer11.start()
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                elif 'update_sysm' in sign:
                    nowuid = sign.replace('update_sysm ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']
                    ejfl.update_one({"nowuid": nowuid}, {"$set": {'sysm': zxh}})
                    fstext = f'''
新的使用说明为:
{zxh}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML')
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})

                    ej_list = ejfl.find_one({'nowuid': nowuid})
                    uid = ej_list['uid']
                    money = ej_list['money']
                    ej_projectname = ej_list['projectname']
                    fl_pro = fenlei.find_one({'uid': uid})['projectname']
                    keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
                    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                    ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
                    fstext = f'''
主分类: {fl_pro}
二级分类: {ej_projectname}

价格: {money}U
库存: {kc}
已售: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))
                elif 'update_wbts' in sign:
                    nowuid = sign.replace('update_wbts ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']
                    ejfl.update_one({"nowuid": nowuid}, {"$set": {'text': stored_text}})
                    fstext = f'''
新的提示为:
{stored_text}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML')
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})

                    ej_list = ejfl.find_one({'nowuid': nowuid})
                    uid = ej_list['uid']
                    money = ej_list['money']
                    ej_projectname = ej_list['projectname']
                    fl_pro = fenlei.find_one({'uid': uid})['projectname']
                    keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
                    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                    ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
                    fstext = f'''
主分类: {fl_pro}
二级分类: {ej_projectname}

价格: {money}U
库存: {kc}
已售: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


                elif 'update_hy' in sign:
                    nowuid = sign.replace('update_hy ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']
                    previous_stock = get_stock_count(nowuid)

                    text = text.split('\n')
                    count = 0
                    for i in text:
                        if 'https:' in i:
                            if hb.find_one({'nowuid': nowuid, 'projectname': i}) is None:
                                hbid = generate_24bit_uid()
                                shangchuanhaobao('会员链接',uid, nowuid, hbid, i, timer)
                                count += 1

                    update.message.reply_text(f'本次上传了{count}个链接')
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    notify_restock_if_needed(context, nowuid, previous_stock, count)

                    ej_list = ejfl.find_one({'nowuid': nowuid})
                    uid = ej_list['uid']
                    money = ej_list['money']
                    ej_projectname = ej_list['projectname']
                    fl_pro = fenlei.find_one({'uid': uid})['projectname']
                    keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
                    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                    ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
                    fstext = f'''
主分类: {fl_pro}
二级分类: {ej_projectname}

价格: {money}U
库存: {kc}
已售: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))

            elif update.message.document:
                if 'update_hb' in sign:
                    nowuid = sign.replace('update_hb ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']
                    previous_stock = get_stock_count(nowuid)

                    file = update.message.document
                    # 获取文件名
                    filename = file.file_name

                    # 获取文件ID
                    file_id = file.file_id
                    # 下载文件
                    new_file = context.bot.get_file(file_id)
                    # 将文件保存到本地
                    new_file_path = f'./临时文件夹/{filename}'
                    new_file.download(new_file_path)

                    context.bot.send_message(chat_id=user_id, text='上传中，请勿重复操作')
                    # 解压缩文件
                    count = 0
                    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    extracted_folder_names = set()
                    with zipfile.ZipFile(new_file_path, 'r') as zip_ref:
                        for file_info in zip_ref.infolist():
                            match = re.match(r'^([^/]+)/.*$', file_info.filename)
                            if match:
                                extracted_folder_names.add(match.group(1))
                            zip_ref.extract(file_info, f'号包/{nowuid}')

                    for extracted_folder_name in sorted(extracted_folder_names):
                        source_paths = collect_delivery_source_paths('直登号', nowuid, extracted_folder_name)
                        if not source_paths:
                            logging.warning(
                                'skip empty direct inventory upload: nowuid=%s projectname=%s',
                                nowuid,
                                extracted_folder_name,
                            )
                            empty_folder_path = find_existing_storage_path('号包', nowuid, extracted_folder_name)
                            if empty_folder_path.exists() and empty_folder_path.is_dir():
                                shutil.rmtree(empty_folder_path, ignore_errors=True)
                            continue

                        if hb.find_one({'nowuid': nowuid, 'projectname': extracted_folder_name}) is None:
                            count += 1
                            hbid = generate_24bit_uid()
                            shangchuanhaobao('直登号',uid, nowuid, hbid, extracted_folder_name, timer)

                    safe_send_message(context, user_id, f'解压并处理完成！本次上传了{count}个号')
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    notify_restock_if_needed(context, nowuid, previous_stock, count)

                    ej_list = ejfl.find_one({'nowuid': nowuid})
                    uid = ej_list['uid']
                    money = ej_list['money']
                    ej_projectname = ej_list['projectname']
                    fl_pro = fenlei.find_one({'uid': uid})['projectname']
                    keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
                    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                    ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
                    fstext = f'''
主分类: {fl_pro}
二级分类: {ej_projectname}

价格: {money}U
库存: {kc}
已售: {ys}
                    '''
                    safe_send_message(context, user_id, fstext, reply_markup=InlineKeyboardMarkup(keyboard))

                elif 'update_gg' in sign:
                    nowuid = sign.replace('update_gg ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']
                    previous_stock = get_stock_count(nowuid)

                    file = update.message.document
                    # 获取文件名
                    filename = file.file_name

                    # 获取文件ID
                    file_id = file.file_id
                    # 下载文件
                    new_file = context.bot.get_file(file_id)
                    # 将文件保存到本地
                    new_file_path = f'./临时文件夹/{filename}'
                    new_file.download(new_file_path)

                    context.bot.send_message(chat_id=user_id, text='上传中，请勿重复操作')

                    with open(new_file_path, 'r', encoding='utf-8') as file:
                        link_list = file.read()

                    login = re.findall('login: (.*)', link_list)
                    password = re.findall('password: (.*)', link_list)
                    submail = re.findall('submail: (.*)', link_list)
                    # 将匹配结果打包成元组列表
                    matches = list(zip(login, password, submail))

                    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    count = 0
                    for i in matches:
                        login = i[0]
                        password = i[1]
                        submail = i[2]
                        jihe12 = {'账户': login, '密码': password, '子邮件': submail}
                        if hb.find_one({'nowuid': nowuid, 'projectname': login}) is None:
                            hbid = generate_24bit_uid()
                            shangchuanhaobao('谷歌',uid, nowuid, hbid, login, timer)
                            hb.update_one({'hbid': hbid}, {"$set": {"leixing": '谷歌', 'data': jihe12}})
                            count += 1

                    update.message.reply_text(f'处理完成！本次上传了{count}个谷歌号')
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    notify_restock_if_needed(context, nowuid, previous_stock, count)

                    ej_list = ejfl.find_one({'nowuid': nowuid})
                    uid = ej_list['uid']
                    money = ej_list['money']
                    ej_projectname = ej_list['projectname']
                    fl_pro = fenlei.find_one({'uid': uid})['projectname']
                    keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
                    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                    ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
                    fstext = f'''
主分类: {fl_pro}
二级分类: {ej_projectname}

价格: {money}U
库存: {kc}
已售: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))

                elif 'update_txt' in sign:
                    nowuid = sign.replace('update_txt ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']
                    previous_stock = get_stock_count(nowuid)

                    file = update.message.document
                    # 获取文件名
                    filename = file.file_name

                    # 获取文件ID
                    file_id = file.file_id
                    # 下载文件
                    new_file = context.bot.get_file(file_id)
                    # 将文件保存到本地
                    new_file_path = f'./临时文件夹/{filename}'
                    new_file.download(new_file_path)

                    context.bot.send_message(chat_id=user_id, text='上传中，请勿重复操作')

                    link_list = []
                    with open(new_file_path, 'r', encoding='utf-8') as file:
                        # 逐行读取文件内容
                        for line in file:
                            # 去除每行末尾的换行符并添加到列表中
                            link_list.append(line.strip())
                    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    count = 0
                    for i in link_list:
                        if hb.find_one({'nowuid': nowuid, 'projectname': i}) is None:
                            hbid = generate_24bit_uid()
                            shangchuanhaobao('API',uid, nowuid, hbid, i, timer)
                            count += 1

                    update.message.reply_text(f'处理完成！本次上传了{count}个api链接')
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    notify_restock_if_needed(context, nowuid, previous_stock, count)

                    ej_list = ejfl.find_one({'nowuid': nowuid})
                    uid = ej_list['uid']
                    money = ej_list['money']
                    ej_projectname = ej_list['projectname']
                    fl_pro = fenlei.find_one({'uid': uid})['projectname']
                    keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
                    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                    ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
                    fstext = f'''
主分类: {fl_pro}
二级分类: {ej_projectname}

价格: {money}U
库存: {kc}
已售: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))
                elif 'update_xyh' in sign:
                    nowuid = sign.replace('update_xyh ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']
                    previous_stock = get_stock_count(nowuid)

                    file = update.message.document
                    # 获取文件名
                    filename = file.file_name

                    # 获取文件ID
                    file_id = file.file_id
                    # 下载文件
                    new_file = context.bot.get_file(file_id)
                    # 将文件保存到本地
                    new_file_path = f'./临时文件夹/{filename}'
                    new_file.download(new_file_path)

                    context.bot.send_message(chat_id=user_id, text='上传中，请勿重复操作')
                    # 解压缩文件
                    count = 0
                    tj_dict = {}
                    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    with zipfile.ZipFile(new_file_path, 'r') as zip_ref:
                        for file_info in zip_ref.infolist():
                            filename = file_info.filename
                            if filename.endswith('.json') or filename.endswith('.session'):
                                # 仅解压 session 或者 json 格式的文件
                                fli1 = filename.replace('.json', '').replace('.session', '')
                                if fli1 not in tj_dict.keys():

                                    hbid = generate_24bit_uid()
                                    if hb.find_one({'nowuid': nowuid, 'projectname': fli1}) is None:
                                        tj_dict[fli1] = 1
                                        shangchuanhaobao('协议号',uid, nowuid, hbid, fli1, timer)

                                zip_ref.extract(member=file_info, path=f'协议号/{nowuid}')
                                pass
                            else:
                                pass
                    for i in tj_dict:
                        count += 1

                    safe_send_message(context, user_id, f'解压并处理完成！本次上传了{count}个协议号')

                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    notify_restock_if_needed(context, nowuid, previous_stock, count)

                    ej_list = ejfl.find_one({'nowuid': nowuid})
                    uid = ej_list['uid']
                    money = ej_list['money']
                    ej_projectname = ej_list['projectname']
                    fl_pro = fenlei.find_one({'uid': uid})['projectname']
                    keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
                    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                    ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
                    fstext = f'''
主分类: {fl_pro}
二级分类: {ej_projectname}

价格: {money}U
库存: {kc}
已售: {ys}
                    '''
                    safe_send_message(context, user_id, fstext, reply_markup=InlineKeyboardMarkup(keyboard))

            else:
                caption = update.message.caption or ''
                entities = update.message.caption_entities or []
                stored_caption = build_storage_text_from_entities(caption, entities)
                save_entities = [] if has_custom_emoji_entities(entities) or needs_dynamic_emoji_parse(stored_caption) else entities

                if 'settuwenset' in sign:
                    qudata = sign.replace('settuwenset ', '')
                    qudataall = qudata.split(':')
                    row = int(qudataall[0])
                    first = int(qudataall[1])
                    if update.message.photo:
                        file = update.message.photo[-1].file_id
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'text': stored_caption}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_id': file}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_type': 'photo'}})
                        user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'entities': pickle.dumps(save_entities)}})
                        send_key_save_success_notice(context, user_id)
                        send_key_content_preview(context, user_id, text=stored_caption,
                                                 file_type='photo', file_id=file, entities=save_entities)
                    elif update.message.animation:
                        file = update.message.animation.file_id
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'text': stored_caption}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_id': file}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_type': 'animation'}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'state': 1}})
                        user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'entities': pickle.dumps(save_entities)}})
                        send_key_save_success_notice(context, user_id)
                        send_key_content_preview(context, user_id, text=stored_caption,
                                                 file_type='animation', file_id=file, entities=save_entities)
                    else:
                        file = update.message.video.file_id
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'text': stored_caption}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_id': file}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_type': 'video'}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'state': 1}})
                        user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'entities': pickle.dumps(save_entities)}})
                        send_key_save_success_notice(context, user_id)
                        send_key_content_preview(context, user_id, text=stored_caption,
                                                 file_type='video', file_id=file, entities=save_entities)
        else:
            if text == '开始营业':
                if state == '4':
                    shangtext.update_one({'projectname': '营业状态'}, {"$set": {"text": 1}})
                    context.bot.send_message(chat_id=user_id, text='开始营业')
            elif text == '停止营业':
                if state == '4':
                    shangtext.update_one({'projectname': '营业状态'}, {"$set": {"text": 0}})
                    context.bot.send_message(chat_id=user_id, text='停止营业')
            elif is_area_code_search_text(raw_text):
                handle_area_code_search(context, user_id, fullname, username, raw_text.strip())
                return

            key_list = get_key.find_one({"projectname": raw_text})
            if key_list is None and text != raw_text:
                key_list = get_key.find_one({"projectname": text})
            if key_list is None and normalized_text:
                key_list = normalized_key_map.get(normalized_text)
            if matches_ui_text(text, 'menu_clone_same'):
                del_message(update.message)
                send_clonebot_prompt(context, user_id)
            elif matches_ui_text(text, 'menu_profile'):
                del_message(update.message)
                profile_username = username or fullname
                fstext = build_user_profile_text(user_id, profile_username, creation_time, zgsl, zgje, USDT)
                context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                         reply_markup=InlineKeyboardMarkup(build_profile_keyboard(user_id)), disable_web_page_preview=True)
            elif matches_ui_text(text, 'menu_recharge'):
                del_message(update.message)
                send_recharge_method_menu(context, user_id)

            elif '红包' in text or matches_ui_text(text, 'menu_redpacket'):
                del_message(update.message)
                fstext = get_ui_text('redpacket_menu_title', viewer_user_id=user_id)
                keyboard = [
                    [InlineKeyboardButton(get_ui_text('redpacket_ongoing_active', viewer_user_id=user_id), callback_data='jxzhb'),
                     InlineKeyboardButton(get_ui_text('redpacket_ended_tab', viewer_user_id=user_id), callback_data='yjshb')],
                    [InlineKeyboardButton(get_ui_text('redpacket_add', viewer_user_id=user_id), callback_data='addhb')],
                    [InlineKeyboardButton(get_ui_text('close', viewer_user_id=user_id), callback_data=f'close {user_id}')]
                ]
                context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))

            elif matches_ui_text(text, 'menu_goods_list'):
                del_message(update.message)
                keyboard = build_category_catalog_keyboard(user_id)
                fstext = get_ui_text('category_list_text', viewer_user_id=user_id)
                keyboard.append([InlineKeyboardButton(get_ui_text('close_with_icon', viewer_user_id=user_id), callback_data=f'close {user_id}')])
                context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                         reply_markup=InlineKeyboardMarkup(keyboard))

            else:
                if key_list != None:
                    del_message(update.message)
                    key_text = key_list['key_text']
                    print_text = key_list['text']
                    file_type = key_list['file_type']
                    file_id = key_list['file_id']
                    entities = safe_pickle_loads(key_list['entities'])
                    keyboard = load_saved_inline_keyboard(key_list.get('keyboard'), key_list.get('key_text'))
                    if context.bot.username in ['TelergamKFbot', 'Tclelgnam_bot']:
                        pass
                    else:
                        if print_text == '' and file_id == '':
                            fallback_text = localize_dynamic_text(key_list.get('projectname') or text, user_id=user_id, lang=lang)
                            send_key_content_preview(context, user_id, text=fallback_text, file_type='text',
                                                     entities=[], keyboard=keyboard)
                        else:
                            localized_preview_text = localize_dynamic_text(print_text, user_id=user_id) if print_text else print_text
                            localized_entities = entities if localized_preview_text == print_text else []
                            send_key_content_preview(context, user_id, text=localized_preview_text, file_type=file_type,
                                                     file_id=file_id, entities=localized_entities, keyboard=keyboard)


def del_message(message):
    if should_skip_optional_telegram_action('message.delete'):
        return
    try:
        message.delete()
    except (TimedOut, NetworkError) as exc:
        note_telegram_transient_error('message.delete', exc)
    except BadRequest as exc:
        exc_text = str(exc).lower()
        if 'message to delete not found' in exc_text or "message can't be deleted" in exc_text or 'message can\'t be deleted' in exc_text:
            return
        raise
    except Forbidden:
        return


def standard_num(num):
    value = Decimal(str(num)).quantize(Decimal("0.01"))
    return value.to_integral() if value == value.to_integral() else value.normalize()


def format_usdt_2(value):
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal('0.01'))
    except Exception:
        return '0.00'
    return f'{amount:.2f}'


def jiexi(context: CallbackContext):
    trc20 = get_trc20_address()
    if not is_valid_trc20_address(trc20):
        return

    qukuai_query = {'state': 0, 'to_address': trc20}
    if TRC20_USDT_CONTRACT:
        qukuai_query['contract_address'] = TRC20_USDT_CONTRACT
    qukuai_list = qukuai.find(qukuai_query)
    for i in qukuai_list:
        txid = i['txid']
        quant = i['quant']
        from_address = i['from_address']
        block_timestamp = int(i.get('block_timestamp') or 0)
        quant123 = Decimal(str(quant)) / Decimal('1000000')
        today_money = abs(quant123.quantize(Decimal('0.0001')))
        pay_amount_text = format_usdt_amount(today_money)
        dj_list = topup.find_one(
            {
                'type': 'trc20',
                'state': TOPUP_STATE_PENDING,
                'to_address': trc20,
                'pay_amount_text': pay_amount_text,
                'created_ts_ms': {'$lte': block_timestamp},
                'expire_ts_ms': {'$gte': block_timestamp}
            },
            sort=[('created_ts_ms', 1)]
        )
        if dj_list is not None:
            message_id = dj_list['message_id']
            user_id = dj_list['user_id']
            user_list = user.find_one({'user_id': user_id})
            if user_list is None:
                qukuai.update_one({'txid': txid}, {"$set": {"state": 2, 'reason': 'user_not_found'}})
                continue
            user_id = user_list['user_id']
            USDT = user_list['USDT']

            now_price = standard_num(float(USDT) + float(today_money))
            now_price = float(now_price) if str((now_price)).count('.') > 0 else int(standard_num(now_price))
            keyboard = [[InlineKeyboardButton("✅已读（点击销毁此消息）", callback_data=f'close {user_id}')]]
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            order_id = dj_list['bianhao']

            user_logging(order_id, 'TRC20充值', user_id, float(today_money), timer)
            us_list = user.find_one({"user_id": user_id})
            user.update_one({'user_id': user_id}, {"$set": {'USDT': now_price}})
            apply_referral_commission(context.bot, user_id, float(today_money), order_id, 'trc20', timer)
            topup.update_one({'_id': dj_list['_id']}, {'$set': {
                'state': TOPUP_STATE_PAID,
                'status': 1,
                'paid_timer': timer,
                'paid_ts_ms': current_ts_ms(),
                'paid_amount': float(today_money),
                'txid': txid,
                'from_address': from_address,
                'quant_raw': str(quant)
            }})
            text = f'''
<b>[emoji:5193209274452425995:🎉] 恭喜您的充值到账啦！！</b>

[emoji:5954227490179255253:🔵] 订单号：<code>{dj_list['bianhao']}</code>
[emoji:5954227490179255253:🔵] 到账金额：<code>{pay_amount_text} USDT</code>
[emoji:5954227490179255253:🔵] 交易哈希：<code>{txid}</code>

[emoji:5445353829304387411:💳] 当前余额：<code>{now_price} USDT</code>
            '''
            safe_delete_message(context.bot, user_id, message_id, 'delete_recharge_success_message')
            try:
                context.bot.send_message(chat_id=user_id, text=text,
                                         reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            except:
                pass
            us_firstname = us_list['fullname'].replace('<', '').replace('>', '')
            us_username = us_list['username']
            text = f'''
用户: <a href="tg://user?id={user_id}">{us_firstname}</a> @{us_username} TRC20充值成功
地址: <code>{from_address}</code>
充值: {pay_amount_text} USDT
<a href="https://tronscan.org/#/transaction/{txid}">充值详细</a>
            '''
            for us in list(user.find({'state': '4'})):
                try:
                    context.bot.send_message(chat_id=us['user_id'], text=text, parse_mode='HTML',
                                             disable_web_page_preview=True)
                except:
                    continue
            qukuai.update_one({'txid': txid}, {"$set": {"state": 1, 'match_order': dj_list['bianhao'], 'match_user_id': user_id}})
        else:
            qukuai.update_one({'txid': txid}, {"$set": {"state": 2, 'reason': 'order_not_found_or_expired'}})


def topup_realtime_loop(context: CallbackContext):
    while 1:
        try:
            jiexi(context)
        except Exception as exc:
            logging.exception('TRC20到账监听循环异常: %s', exc)
        time.sleep(3)


def jianceguoqi(context: CallbackContext):
    while 1:
        for i in topup.find({'state': TOPUP_STATE_PENDING}):
            timer = i['timer']
            user_id = i['user_id']
            message_id = i['message_id']
            expire_ts_ms = int(i.get('expire_ts_ms') or 0)
            if expire_ts_ms <= 0:
                dt = datetime.datetime.strptime(timer, '%Y-%m-%d %H:%M:%S')
                expire_ts_ms = int((dt + timedelta(minutes=10)).timestamp() * 1000)

            keyboard = [[InlineKeyboardButton("✅已读（点击销毁此消息）", callback_data=f'close {user_id}')]]

            if current_ts_ms() >= expire_ts_ms:
                try:
                    if i.get('type') == 'okpay':
                        context.bot.edit_message_text(chat_id=user_id, message_id=message_id,
                                                      text='❌ OKPay充值订单已超时失效，请重新创建订单。\n\n超过 10 分钟后再支付，将不会自动到账。',
                                                      reply_markup=InlineKeyboardMarkup(keyboard))
                    elif i.get('type') == 'trc20':
                        if message_id:
                            safe_delete_message(context.bot, user_id, message_id, 'delete_expired_trc20_order_message')
                        context.bot.send_message(
                            chat_id=user_id,
                            text='❌ TRC20充值订单已超时失效，请重新创建订单。\n\n超过 10 分钟后再转账，将不会自动到账。',
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                    else:
                        context.bot.edit_message_media(chat_id=user_id, message_id=message_id, media=InputMediaPhoto(media='AgACAgQAAxkBAAI4Nmagu-8nD4AQrv6ftlzrLjLSxlOnAAJavzEbAZYIUch6ykGfk6CaAQADAgADeQADNQQ', caption='❌ 订单支付超时(或金额错误)'),reply_markup=InlineKeyboardMarkup(keyboard))

                except:
                    pass
                topup.update_one({'_id': i['_id'], 'state': TOPUP_STATE_PENDING}, {'$set': {'state': TOPUP_STATE_EXPIRED, 'expired_timer': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), 'status': -1}})
        time.sleep(3)

def suoyouchengxu(context: CallbackContext):
    global TOPUP_LOOP_STARTED
    if not TOPUP_LOOP_STARTED:
        Timer(1, topup_realtime_loop, args=[context]).start()
        TOPUP_LOOP_STARTED = True
    Timer(1, jianceguoqi, args=[context]).start()
    
    
    job = context.job_queue.get_jobs_by_name('suoyouchengxu')
    if job != ():
        job[0].schedule_removal()


def fbgg(update: Update, context: CallbackContext):
    chat = update.effective_chat
    # print(chat)
    if chat.type == 'private':
        user_id = chat['id']
        chat_id = user_id
        username = chat['username']
        firstname = chat['first_name']
        fullname = chat['full_name']
        timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        lastname = chat['last_name']
        text = update.message.text
        user_list = user.find_one({'user_id': user_id})
        USDT = user_list['USDT']
        state = user_list['state']
        if state == '4':

            context.bot.send_message(chat_id=user_id, text='开始发送广告')
            fstext = text.replace('/gg ', '')
            for i in user.find({}):
                yh_id = i['user_id']
                keyboard = [[InlineKeyboardButton("✅已读（点击销毁此消息）", callback_data=f'close {yh_id}')]]
                try:
                    context.bot.send_message(chat_id=i['user_id'], text=fstext,
                                             reply_markup=InlineKeyboardMarkup(keyboard))
                except:
                    pass
                time.sleep(3)
            context.bot.send_message(chat_id=user_id, text='广告发送完成')


def is_source_admin_user(user_id):
    try:
        return int(user_id) in set(get_source_admin_user_ids())
    except Exception:
        return False


def get_agent_runtime_record(agent_bot_id):
    tenant_id = normalize_tenant_id(agent_bot_id)
    runtime = agent_bots.find_one({'agent_bot_id': tenant_id}) or {}
    clone_row = clone_instances.find_one({'bot_id': tenant_id, 'clone_kind': 'agent', 'state': {'$ne': 'deleted'}}) or {}
    return tenant_id, runtime, clone_row


def load_agent_bot_token(agent_bot_id):
    tenant_id, runtime, clone_row = get_agent_runtime_record(agent_bot_id)
    clone_dir = Path(str((clone_row or {}).get('clone_dir') or (runtime or {}).get('clone_dir') or '').strip())
    if not clone_dir:
        return ''
    env_path = clone_dir / 'agent_service' / '.env'
    if not env_path.exists():
        return ''
    env_map = dotenv_values(env_path) or {}
    return str(env_map.get('AGENT_BOT_TOKEN') or '').strip()


def send_agent_bot_text(agent_bot_id, chat_id, text, parse_mode='HTML'):
    token = load_agent_bot_token(agent_bot_id)
    if not token:
        return False, 'missing_agent_bot_token'
    try:
        response = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data={
                'chat_id': int(chat_id),
                'text': str(text or ''),
                'parse_mode': parse_mode,
                'disable_web_page_preview': 'true',
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json() or {}
        if not payload.get('ok'):
            return False, str(payload.get('description') or 'send_failed')
        return True, ''
    except Exception as exc:
        return False, str(exc)


def build_agent_admin_balance_notice(delta_amount, balance_after):
    delta_amount = float(delta_amount or 0)
    action_text = '通过管理员充值' if delta_amount >= 0 else '通过管理员扣款'
    amount_text = standard_num(abs(delta_amount))
    balance_text = format_usdt_2(balance_after)
    return f'''
<b>✅ {action_text}：{amount_text} USDT

💳 当前余额：{balance_text} USDT</b>
    '''


def format_agent_order_state(state):
    mapping = {
        'reserved': '已预占',
        'delivered': '已发货',
        'delivery_failed': '发货失败',
        'refunded': '已退款',
        'partial_refunded': '部分退款',
        'refund_pending': '退款中',
        'paid': '已支付',
        'pending': '待处理',
        'canceled': '已取消',
    }
    state = str(state or '').strip()
    return mapping.get(state, state or '未知')


def build_agent_purchase_history_text(agent_bot_id, user_id, limit=10):
    tenant_id = normalize_tenant_id(agent_bot_id)
    rows = list(tenant_orders.find({'tenant_id': tenant_id, 'user_id': int(user_id)}, sort=[('created_ts_ms', -1), ('created_at', -1)], limit=max(1, int(limit))))
    if not rows:
        rows = []
        for row in list(get_agent_bot_gmjlu_collection(tenant_id).find({'user_id': int(user_id)}, sort=[('timer', -1)], limit=max(1, int(limit)))):
            rows.append({
                'created_at': row.get('timer') or '',
                'product_name': row.get('projectname') or '',
                'quantity': 1,
                'total_amount': '',
                'state': row.get('leixing') or '',
                'order_id': row.get('bianhao') or '',
            })
    lines = ['🛒 最近购买记录']
    if not rows:
        lines.append('暂无购买记录')
        return '\n'.join(lines)
    for idx, row in enumerate(rows, 1):
        total_amount = row.get('total_amount')
        amount_text = f'{standard_num(total_amount)} USDT' if total_amount not in (None, '') else '—'
        lines.extend([
            '',
            f'{idx}. 时间：{row.get("created_at") or ""}',
            f'商品：{row.get("product_name") or row.get("source_product_name") or ""}',
            f'数量：{int(row.get("quantity", 1) or 1)}',
            f'金额：{amount_text}',
            f'状态：{format_agent_order_state(row.get("state"))}',
            f'订单号：{row.get("order_id") or row.get("bianhao") or ""}',
        ])
    return '\n'.join(lines)


def adm(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if chat.type != 'private':
        return
    user_id = chat['id']
    chat_id = user_id
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    text = update.message.text
    text1 = text.split(' ')
    user_list = user.find_one({'user_id': user_id})
    if not user_list or not is_source_admin_user(user_id):
        return

    if len(text1) == 4:
        agent_bot_id = str(text1[1]).strip()
        try:
            df_id = int(text1[2])
        except Exception:
            context.bot.send_message(chat_id=chat_id, text='用户ID格式错误')
            return
        money_raw = str(text1[3]).strip()
        if not money_raw or money_raw[0] not in {'+', '-'}:
            context.bot.send_message(chat_id=chat_id, text='格式为: /add 代理id 用户id +-数值')
            return
        amount_text = money_raw[1:].strip()
        if not is_number(amount_text):
            context.bot.send_message(chat_id=chat_id, text='非数字，操作失败')
            return
        tenant_id, runtime, clone_row = get_agent_runtime_record(agent_bot_id)
        if not runtime and not clone_row:
            context.bot.send_message(chat_id=chat_id, text='代理不存在')
            return
        agent_user = get_agent_bot_user(tenant_id, df_id)
        if agent_user is None:
            context.bot.send_message(chat_id=chat_id, text='代理用户不存在')
            return
        amount = float(amount_text)
        order_id = build_tenant_order_id('ADMIN', tenant_id, df_id)
        if money_raw.startswith('+'):
            result = credit_tenant_wallet(
                tenant_id,
                df_id,
                amount,
                currency='USDT',
                biz_type='admin_credit',
                ref_id=order_id,
                description=f'主号铺管理员手动加余额 {user_id}',
                meta={'operator_user_id': user_id, 'operator_scope': 'main_admin'},
            )
            action_text = '加余额'
            delta_amount = amount
        else:
            result = debit_tenant_wallet(
                tenant_id,
                df_id,
                amount,
                currency='USDT',
                biz_type='admin_debit',
                ref_id=order_id,
                description=f'主号铺管理员手动减余额 {user_id}',
                meta={'operator_user_id': user_id, 'operator_scope': 'main_admin'},
            )
            if result is None:
                context.bot.send_message(chat_id=chat_id, text='扣款失败：余额不足或用户不存在')
                return
            action_text = '减余额'
            delta_amount = -amount
        agent_user = get_agent_bot_user(tenant_id, df_id) or agent_user
        bot_name = str((runtime or {}).get('bot_name') or (runtime or {}).get('bot_username') or tenant_id)
        username_text = str(agent_user.get('username') or agent_user.get('fullname') or '').strip() or '未设置'
        fstext = f'''
代理: {bot_name} ({tenant_id})
用户ID: {df_id}
用户名: {username_text}
操作: {action_text}
变动: {standard_num(delta_amount)} USDT
余额: {standard_num(result['balance_after'])} USDT
        '''
        context.bot.send_message(chat_id=chat_id, text=fstext)
        notice_ok, notice_error = send_agent_bot_text(tenant_id, df_id, build_agent_admin_balance_notice(delta_amount, result['balance_after']))
        if not notice_ok:
            context.bot.send_message(chat_id=chat_id, text=f'已完成余额调整，但代理用户通知发送失败：{notice_error}')
        return

    if len(text1) == 3:
        df_id = int(text1[1])
        money = text1[2]
        if user.find_one({'user_id': df_id}) is None:
            context.bot.send_message(chat_id=chat_id, text='用户不存在')
            return
        if '+' in money:
            money = money.replace('+', '')
            if not is_number(money):
                context.bot.send_message(chat_id=chat_id, text='非数字，操作失败')
                return
            hyh_list = user.find_one({'user_id': df_id})
            hyh_money = hyh_list['USDT']
            now_money = standard_num(hyh_money + float(money))
            now_money = float(now_money) if str((now_money)).count('.') > 0 else int(standard_num(now_money))

            order_id = generate_24bit_uid()
            user_logging(order_id, '充值', df_id, money, timer)
            user.update_one({'user_id': df_id}, {'$set': {'USDT': now_money}})
            hyh_list = user.find_one({"user_id": df_id})
            fullname = hyh_list['fullname']
            USDT = hyh_list['USDT']
            fstext = f'''
ID: {df_id}
昵称: {fullname}
余额: {format_usdt_2(USDT)}
            '''
            context.bot.send_message(chat_id=chat_id, text=fstext)

            fstext = f'''
<b>✅    通过管理员充值：{money} USDT

💳    您的余额：{format_usdt_2(USDT)}  USDT</b>
            '''
            context.bot.send_message(chat_id=df_id, text=fstext, parse_mode='HTML')
        else:
            money = money.replace('-', '')
            if not is_number(money):
                context.bot.send_message(chat_id=chat_id, text='非数字，操作失败')
                return
            hyh_list = user.find_one({'user_id': df_id})
            hyh_money = hyh_list['USDT']
            now_money = standard_num(hyh_money - float(money))
            now_money = float(now_money) if str((now_money)).count('.') > 0 else int(standard_num(now_money))

            order_id = generate_24bit_uid()
            user_logging(order_id, '扣款', df_id, money, timer)
            user.update_one({'user_id': df_id}, {'$set': {'USDT': now_money}})
            hyh_list = user.find_one({"user_id": df_id})
            fullname = hyh_list['fullname']
            USDT = hyh_list['USDT']
            fstext = f'''
ID: {df_id}
昵称: {fullname}
余额: {format_usdt_2(USDT)}
            '''
            context.bot.send_message(chat_id=chat_id, text=fstext)

            fstext = f'''
<b>✅    通过管理员扣款：{money} USDT

💳    您的余额：{format_usdt_2(USDT)}  USDT</b>
            '''
            context.bot.send_message(chat_id=df_id, text=fstext, parse_mode='HTML')
        return

    context.bot.send_message(chat_id=chat_id, text='格式为: /add id +-数值 或 /add 代理id 用户id +-数值')


def cha(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if chat.type != 'private':
        return
    user_id = chat['id']
    chat_id = user_id
    text = update.message.text
    text1 = text.split(' ')
    user_list = user.find_one({'user_id': user_id})
    if not user_list or not is_source_admin_user(user_id):
        return

    if len(text1) == 3:
        agent_bot_id = str(text1[1]).strip()
        try:
            df_id = int(text1[2])
        except Exception:
            context.bot.send_message(chat_id=chat_id, text='格式为: /cha 代理id 用户id')
            return
        tenant_id, runtime, clone_row = get_agent_runtime_record(agent_bot_id)
        if not runtime and not clone_row:
            context.bot.send_message(chat_id=chat_id, text='代理不存在')
            return
        df_list = get_agent_bot_user(tenant_id, df_id)
        if df_list is None:
            context.bot.send_message(chat_id=chat_id, text='代理用户不存在')
            return
        df_fullname = df_list.get('fullname')
        df_username = df_list.get('username')
        if not df_username:
            df_username = df_fullname
        creation_time = df_list.get('creation_time') or df_list.get('created_at') or ''
        zgsl = df_list.get('zgsl', 0)
        zgje = df_list.get('zgje', 0)
        agent_balance = df_list.get('USDT', 0)
        fstext = build_user_profile_text(df_id, df_username, creation_time, zgsl, zgje, agent_balance)
        agent_name = str((runtime or {}).get('bot_name') or (runtime or {}).get('bot_username') or tenant_id)
        fstext = f'<b>代理：</b><code>{html.escape(str(agent_name), quote=False)}</code>\n<b>代理ID：</b><code>{html.escape(str(tenant_id), quote=False)}</code>\n\n' + fstext
        history_text = build_agent_purchase_history_text(tenant_id, df_id)
        context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML', disable_web_page_preview=True)
        context.bot.send_message(chat_id=user_id, text=history_text, disable_web_page_preview=True)
        return

    if len(text1) == 2:
        jieguo = text1[1]
        if is_number(jieguo):
            df_id = int(jieguo)
            df_list = user.find_one({'user_id': df_id})
            if df_list is None:
                context.bot.send_message(chat_id=chat_id, text='用户不存在')
                return
        else:
            df_list = user.find_one({'username': jieguo.replace('@', '')})
            if df_list is None:
                context.bot.send_message(chat_id=chat_id, text='用户不存在')
                return
            df_id = df_list['user_id']
        df_fullname = df_list['fullname']
        df_username = df_list['username']
        if df_username is None:
            df_username = df_fullname
        creation_time = df_list['creation_time']
        zgsl = df_list['zgsl']
        zgje = df_list['zgje']
        USDT = df_list['USDT']
        fstext = build_user_profile_text(df_id, df_username, creation_time, zgsl, zgje, USDT)
        fstext = fstext + '\n\n' + build_admin_referral_text(df_list)
        keyboard = [
            [InlineKeyboardButton('🛒购买记录', callback_data=f'gmaijilu {df_id}')],
            [InlineKeyboardButton('🔗推广链接', callback_data=f'tglink {df_id}')],
            [InlineKeyboardButton('关闭', callback_data=f'close {user_id}')],
        ]
        context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                 reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        return

    context.bot.send_message(chat_id=chat_id, text='格式为: /cha id或用户名 或 /cha 代理id 用户id')


def create_folder_if_not_exists(folder_path):
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
        # print(f"Folder '{folder_path}' created successfully.")
    else:
        pass
        # print(f"Folder '{folder_path}' already exists.")


def parse_url(content):
    args = content.split('&')
    if len(args) < 2:
        (title, url) = ("格式错误，点击联系管理员", "www.baidu.com")
    else:
        (title, url) = (args[0].strip(), (None if len(args) < 1 else args[1].strip()))
    return create_keyboard(title, url)


def create_keyboard(title, url=None, callback_data=None, inline_query=None):
    return [InlineKeyboardButton(title, url=url, callback_data=callback_data,
                                 switch_inline_query_current_chat=inline_query)]


def load_saved_inline_keyboard(raw_keyboard=None, key_text=''):
    key_text = str(key_text or '').strip()
    if key_text:
        try:
            rebuilt_keyboard = parse_urls(key_text)
            if rebuilt_keyboard:
                return rebuilt_keyboard
        except Exception:
            logging.warning('Failed to rebuild inline keyboard from key_text=%r', key_text, exc_info=True)
    return safe_pickle_loads(raw_keyboard) or []


def parse_urls(content, maxurl=99):
    cnt_url = 0
    keyboard = []
    rows = content.split('\n')
    for row in rows:
        krow = []
        els = row.split('|')
        for el in els:
            kel = parse_url(el)
            if not kel:
                continue
            krow = krow + kel
            cnt_url = cnt_url + 1
            if cnt_url == maxurl:
                break
        keyboard.append(krow)
        if cnt_url == maxurl:
            break
    return keyboard


def main():
    bot_token = os.getenv('BOT_TOKEN')
    if not bot_token:
        raise RuntimeError('缺少 BOT_TOKEN，请先在 .env 里配置 Telegram Bot Token')

    application = ApplicationBuilder().token(bot_token).post_init(on_post_init).build()

    for command_name, callback in [
        ('start', start),
        ('emojiid', emojiid),
        ('add', adm),
        ('cha', cha),
        ('gg', fbgg),
    ]:
        application.add_handler(CommandHandler(command_name, sync_handler(callback)))

    callback_handlers = [
        ('startupdate', startupdate), ('clonebot', clonebot), ('cloneagent', cloneagent), ('clonepay', clonepay), ('clonelist', clonelist), ('agentlist', agentlist), ('cloneinfo ', cloneinfo), ('agentinfo ', agentinfo), ('agentusers ', agentusers), ('clonerestart ', clonerestart), ('agentrestart ', agentrestart), ('clonedelete ', clonedelete), ('agentdelete ', agentdelete), ('setcloneprice', setcloneprice), ('restockpushcfg', restockpushcfg), ('buynoticecfg', buynoticecfg), ('setbuynotice', setbuynotice), ('setrestocktarget', setrestocktarget), ('restockrequestarea ', restockrequestarea), ('nostock ', nostock), ('okpaycfg', okpaycfg), ('setokpayid', setokpayid), ('setokpaytoken', setokpaytoken), ('setokpayname', setokpayname), ('delrow', delrow), ('newrow', newrow), ('newkey', newkey),
        ('backstart', backstart), ('paixurow', paixurow), ('addzdykey', addzdykey),
        ('qrscdelrow ', qrscdelrow), ('addhangkey ', addhangkey), ('delhangkey ', delhangkey),
        ('qrdelliekey ', qrdelliekey), ('keyxq ', keyxq), ('setkeyname ', setkeyname),
        ('settuwenset ', settuwenset), ('setkeyboard ', setkeyboard), ('cattuwenset ', cattuwenset),
        ('paixuyidong ', paixuyidong), ('close ', close), ('yuecz ', yuecz), ('okyuecz ', okyuecz), ('settrc20', settrc20),
        ('spgli', spgli), ('newfl', newfl), ('flxxi ', flxxi), ('upspname ', upspname),
        ('newejfl ', newejfl), ('fejxxi ', fejxxi), ('upejflname ', upejflname),
        ('catejflsp ', catejflsp), ('backzcd', backzcd), ('paixufl', paixufl), ('flpxyd ', flpxyd),
        ('delfl', delfl), ('qrscflrow ', qrscflrow), ('paixuejfl ', paixuejfl), ('ejfpaixu ', ejfpaixu),
        ('delejfl ', delejfl), ('qrscejrow ', qrscejrow), ('delcurconfirm ', delcurconfirm), ('delcurejfl ', delcurejfl), ('update_hb ', update_hb), ('gmsp ', gmsp),
        ('upmoney ', upmoney), ('gmqq', gmqq), ('qrgaimai ', qrgaimai),
        ('update_xyh ', update_xyh), ('update_hy ', update_hy), ('yhnext ', yhnext), ('yhlist', yhlist),
        ('gmaijilu', gmaijilu), ('tglink ', tglink), ('zcfshuo', zcfshuo), ('gmainext ', gmainext), ('update_txt ', update_txt),
        ('backgmjl ', backgmjl), ('qchuall ', qchuall), ('update_wbts ', update_wbts),
        ('update_gg ', update_gg), ('zdycz', zdycz), ('okzdycz', okzdycz), ('recharge_menu', recharge_menu), ('recharge_trc20', recharge_trc20), ('recharge_okpay', recharge_okpay), ('addhb', addhb), ('lqhb ', lqhb),
        ('xzhb ', xzhb), ('yjshb', yjshb), ('jxzhb', jxzhb), ('shokuan ', shokuan),
        ('update_sysm ', update_sysm), ('qxdingdan ', qxdingdan), ('okpay_paid ', okpay_paid), ('sifa', sifa),
        ('kaiqisifa', kaiqisifa), ('tuwen', tuwen), ('anniu', anniu), ('cattu', cattu),
    ]
    for pattern, callback in callback_handlers:
        application.add_handler(CallbackQueryHandler(sync_handler(callback), pattern=pattern))

    application.add_error_handler(global_error_handler)

    application.add_handler(InlineQueryHandler(sync_handler(inline_query)))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.REPLY, sync_handler(huifu)))
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.ANIMATION | filters.VIDEO | filters.Document.ALL) & (~filters.COMMAND),
        sync_handler(textkeyboard)
    ))

    application.job_queue.run_repeating(sync_job(suoyouchengxu), interval=1, first=1, name='suoyouchengxu')
    application.run_polling(timeout=600)


if __name__ == '__main__':

    for i in ['发货', '协议号发货', '手机接码发货', '临时文件夹', '谷歌发货', '协议号', '号包', 'ban']:
        create_folder_if_not_exists(i)
    main()
