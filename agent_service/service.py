from __future__ import annotations

import asyncio
import html
import logging
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PARENT_DIR = CURRENT_DIR.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import AgentRuntimeConfig, load_agent_env

load_agent_env()

from mongo import (
    agent_bots,
    agent_product_prices,
    beijing_now_str,
    create_agent_withdrawal_request,
    create_tenant_purchase_order,
    create_tenant_topup_order,
    credit_tenant_wallet,
    ejfl,
    expire_tenant_topup_orders,
    ensure_agent_mongo_indexes,
    ensure_agent_user_exists,
    fenlei,
    get_agent_stats,
    get_agent_bot_gmjlu_collection,
    get_agent_bot_user_collection,
    get_batch_stock,
    get_real_time_stock,
    get_agent_bot_user,
    get_latest_pending_topup_order,
    get_tenant_order,
    hb,
    mark_tenant_topup_paid,
    qukuai,
    refund_tenant_order,
    standard_num,
    tenant_orders,
    topup_orders,
    update_agent_withdrawal_status,
    update_tenant_order,
)
from account_health_check import check_account_inventory_item_with_ttl_update, get_account_check_runtime_status
from haopubot import (
    ACCOUNT_CHECK_ENABLED,
    ACCOUNT_CHECK_SUPPORTED_TYPES,
    ACCOUNT_CHECK_TIMEOUT_SECONDS,
    ACCOUNT_CHECK_PROGRESS_INTERVAL_SECONDS,
    ACCOUNT_CHECK_PROGRESS_STEP,
    InlineKeyboardButton,
    KeyboardButton,
    archive_invalid_inventory_item,
    build_custom_emoji_text_entities,
    build_delivery_zip,
    create_delivery_order_id,
    find_existing_storage_path,
    get_buy_notice_text,
    get_message_storage_text,
    get_source_admin_user_ids,
    get_ui_text,
    resolve_inventory_check_target,
    write_invalid_archive_meta,
    APP_VERSION,
)


logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger('agent_service')


MENU_GOODS_ZH = '🛒商品列表'
MENU_GOODS_EN = '🛒 Product Catalog'
MENU_PROFILE_ZH = '👤个人中心'
MENU_PROFILE_EN = '👤 Profile'
MENU_RECHARGE_ZH = '💸我要充值'
MENU_RECHARGE_EN = '💸 Recharge'
MENU_SUPPORT_ZH = '📞联系客服'
MENU_SUPPORT_EN = '📞 Contact Support'

PREMIUM_GOODS = '[emoji:5312361253610475399:🛒]商品列表'
PREMIUM_GOODS_EN = '[emoji:5312361253610475399:🛒]Product Catalog'
PREMIUM_PROFILE = '[emoji:5929391996408959380:🏞]个人中心'
PREMIUM_PROFILE_EN = '[emoji:5929391996408959380:🏞]Profile'
PREMIUM_RECHARGE = '[emoji:5197474438970363734:💳]我要充值'
PREMIUM_RECHARGE_EN = '[emoji:5197474438970363734:💳]Recharge'
PREMIUM_SUPPORT = '[emoji:5954078884310814346:☎️]联系客服'
PREMIUM_SUPPORT_EN = '[emoji:5954078884310814346:☎️]Contact Support'
PREMIUM_ADMIN = '[emoji:5341715473882955310:⚙️]代理后台'

ADMIN_SIGN_PRICE_DELTA = 'admin_set_price_delta'
ADMIN_SIGN_CUSTOMER_SERVICE = 'admin_set_customer_service'
ADMIN_SIGN_RESTOCK_TARGET = 'admin_set_restock_target'
ADMIN_SIGN_PURCHASE_NOTICE = 'admin_set_purchase_notice'
USER_SIGN_BIND_WITHDRAW = 'user_bind_withdraw_address'
USER_SIGN_APPLY_WITHDRAW = 'user_apply_withdraw'
AGENT_WITHDRAW_MIN_AMOUNT = 10.0


def render_text(text: str):
    return build_custom_emoji_text_entities(str(text or ''))


def strip_basic_html(text: str) -> str:
    text = html.unescape(str(text or ''))
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()


def get_agent_lang(config: AgentRuntimeConfig, user_id: int | None = None, user_row: dict | None = None) -> str:
    if user_row and user_row.get('lang'):
        return str(user_row.get('lang') or config.default_lang)
    if user_id is not None:
        row = get_agent_bot_user(config.agent_bot_id, user_id) or {}
        if row.get('lang'):
            return str(row.get('lang') or config.default_lang)
    return str(config.default_lang or 'zh')


def get_agent_ui_text(config: AgentRuntimeConfig, key: str, user_id: int | None = None, user_row: dict | None = None, **kwargs) -> str:
    lang = get_agent_lang(config, user_id=user_id, user_row=user_row)
    return get_ui_text(key, lang=lang, **kwargs)


def set_agent_sign(agent_bot_id: str, user_id: int, sign: str | int) -> None:
    get_agent_bot_user_collection(agent_bot_id).update_one({'user_id': int(user_id)}, {'$set': {'sign': sign}})


def get_agent_runtime_doc(config: AgentRuntimeConfig) -> dict:
    row = agent_bots.find_one({'agent_bot_id': config.agent_bot_id}) or {}
    return row


def get_agent_customer_service(config: AgentRuntimeConfig) -> str:
    row = get_agent_runtime_doc(config)
    return str(row.get('customer_service') or config.customer_service or '').strip()


def get_agent_restock_target(config: AgentRuntimeConfig) -> str:
    row = get_agent_runtime_doc(config)
    return str(row.get('restock_target') or '').strip()


def get_agent_price_delta(config: AgentRuntimeConfig) -> float:
    row = get_agent_runtime_doc(config)
    try:
        return float(row.get('price_delta', 0) or 0)
    except Exception:
        return 0.0


def get_agent_purchase_notice(config: AgentRuntimeConfig) -> str:
    row = get_agent_runtime_doc(config)
    return str(row.get('purchase_notice') or '').strip()


def build_agent_notice_preview(notice_text: str, empty_text: str = '未配置') -> str:
    notice_text = str(notice_text or '').strip()
    if not notice_text:
        return empty_text
    preview = notice_text.replace('\r', '\n').replace('\n', ' / ')
    return preview[:48] + ('...' if len(preview) > 48 else '')


def update_agent_runtime_settings(config: AgentRuntimeConfig, **fields) -> dict:
    payload = {k: v for k, v in fields.items() if v is not None}
    payload['updated_at'] = beijing_now_str()
    agent_bots.update_one({'agent_bot_id': config.agent_bot_id}, {'$set': payload}, upsert=True)
    return get_agent_runtime_doc(config)


def get_user_withdraw_address(config: AgentRuntimeConfig, user_id: int) -> str:
    user_row = get_agent_bot_user(config.agent_bot_id, user_id) or {}
    return str(user_row.get('withdraw_address') or '').strip()


def normalize_menu_text(text: str) -> str:
    text = str(text or '').strip()
    text = re.sub(r'\[emoji:\d+:(.*?)\]', lambda m: m.group(1) or '', text)
    text = re.sub(r'^[\W_]+', '', text).strip()
    return text.casefold()


async def send_rendered(bot, chat_id: int, text: str, reply_markup=None):
    rendered_text, entities = render_text(text)
    return await bot.send_message(chat_id=chat_id, text=rendered_text, entities=entities, reply_markup=reply_markup)


async def reply_rendered(update: Update, text: str, reply_markup=None):
    message = update.effective_message
    if message is None:
        return None
    rendered_text, entities = render_text(text)
    return await message.reply_text(text=rendered_text, entities=entities, reply_markup=reply_markup)


async def edit_rendered(query, text: str, reply_markup=None):
    rendered_text, entities = render_text(text)
    return await query.edit_message_text(text=rendered_text, entities=entities, reply_markup=reply_markup)


def is_agent_admin(config: AgentRuntimeConfig, user_id: int) -> bool:
    return int(user_id) in set(config.admin_ids or ())


def is_agent_admin_or_source_admin(config: AgentRuntimeConfig, user_id: int) -> bool:
    try:
        source_admin_ids = {int(item) for item in list(get_source_admin_user_ids() or [])}
    except Exception:
        source_admin_ids = set()
    return is_agent_admin(config, user_id) or int(user_id) in source_admin_ids


def build_home_keyboard(config: AgentRuntimeConfig, lang: str = 'zh', user_id: int | None = None) -> ReplyKeyboardMarkup:
    if lang == 'en':
        keyboard = [[
            KeyboardButton(PREMIUM_GOODS_EN),
            KeyboardButton(PREMIUM_PROFILE_EN),
            KeyboardButton(PREMIUM_RECHARGE_EN),
        ]]
    else:
        keyboard = [[
            KeyboardButton(PREMIUM_GOODS),
            KeyboardButton(PREMIUM_PROFILE),
            KeyboardButton(PREMIUM_RECHARGE),
        ]]
    if get_agent_customer_service(config):
        keyboard.append([KeyboardButton(PREMIUM_SUPPORT_EN if lang == 'en' else PREMIUM_SUPPORT)])
    if user_id is not None and is_agent_admin_or_source_admin(config, int(user_id)):
        keyboard.append([KeyboardButton(PREMIUM_ADMIN)])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def is_valid_trc20_address(address: str) -> bool:
    import re
    return re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}', str(address or '').strip()) is not None


def build_recharge_menu_text(config: AgentRuntimeConfig) -> str:
    return (
        f'[emoji:5197474438970363734:💳]{config.agent_name} 充值中心\n\n'
        '当前阶段先接入 TRC20 充值订单与账本。\n'
        f'[emoji:5080312910866024090:💵]收款地址：{config.trc20_address or "未配置"}\n\n'
        '选择下方金额后会生成代理充值订单。'
    )


def build_admin_panel_text(config: AgentRuntimeConfig) -> str:
    stats = get_agent_stats(config.agent_bot_id)
    customer_service = get_agent_customer_service(config) or '未配置'
    restock_target = get_agent_restock_target(config) or '未配置'
    purchase_notice = build_agent_notice_preview(get_agent_purchase_notice(config))
    price_delta = get_agent_price_delta(config)
    pending_withdraws = int(agent_bots.database['agent_withdrawals'].count_documents({'agent_bot_id': config.agent_bot_id, 'state': 'pending'}))
    return (
        f'[emoji:5341715473882955310:⚙️]{config.agent_name} 代理管理后台\n\n'
        f'[emoji:5954227490179255253:🔵]代理ID：{config.agent_bot_id}\n'
        f'[emoji:6321041414067068140:👤]用户数：{stats.get("user_count", 0)}\n'
        f'[emoji:5028746137645876535:📈]累计销售：{standard_num(stats.get("total_spent", 0))} USDT\n'
        f'[emoji:5397916757333654639:➕]全局差价：{standard_num(price_delta)} USDT\n'
        f'[emoji:5954078884310814346:☎️]客服：{customer_service}\n'
        f'[emoji:5220214598585568818:🚨]补货通知：{restock_target}\n'
        f'[emoji:5235511932064129087:🎁]购买后通知：{purchase_notice}\n'
        f'[emoji:5445353829304387411:💳]待审提款：{pending_withdraws}\n\n'
        '点下面按钮进入对应管理。'
    )


def build_admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('[emoji:6321041414067068140:👤]用户列表', callback_data='admin_users:0'),
            InlineKeyboardButton('[emoji:5397916757333654639:➕]配置差价', callback_data='admin_price_delta'),
        ],
        [
            InlineKeyboardButton('[emoji:5445353829304387411:💳]提款申请', callback_data='admin_withdraws:0'),
            InlineKeyboardButton('[emoji:5954078884310814346:☎️]客服配置', callback_data='admin_customer_service'),
        ],
        [
            InlineKeyboardButton('[emoji:5220214598585568818:🚨]补货通知配置', callback_data='admin_restock_target'),
            InlineKeyboardButton('[emoji:5235511932064129087:🎁]购买后通知', callback_data='admin_purchase_notice'),
        ],
        [
            InlineKeyboardButton('[emoji:5954227490179255253:🔵]返回首页', callback_data='agent_home'),
        ],
    ])


def build_admin_users_text(config: AgentRuntimeConfig, page: int = 0, page_size: int = 10) -> tuple[str, int]:
    rows = list(get_agent_bot_user_collection(config.agent_bot_id).find({}, sort=[('USDT', -1), ('zgje', -1), ('user_id', 1)], skip=page * page_size, limit=page_size))
    total = get_agent_bot_user_collection(config.agent_bot_id).count_documents({})
    lines = [f'[emoji:6321041414067068140:👤]代理用户列表', '', f'总用户数：{total}']
    if not rows:
        lines.extend(['', '暂无用户'])
    for row in rows:
        username = str(row.get('username') or '').strip()
        username_text = f'@{username}' if username else '未设置'
        lines.extend([
            '',
            f'ID：{row.get("user_id")}',
            f'用户名：{username_text}',
            f'余额：{standard_num(row.get("USDT", 0))} USDT',
            f'购买件数：{int(row.get("zgsl", 0) or 0)}',
            f'消费金额：{standard_num(row.get("zgje", 0))} USDT',
        ])
    return '\n'.join(lines), total


def build_admin_users_keyboard(page: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    keyboard = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('[emoji:5222044641200720562:🌸]上一页', callback_data=f'admin_users:{page - 1}'))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton('[emoji:5220195537520711716:⚡️]下一页', callback_data=f'admin_users:{page + 1}'))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton('[emoji:5954227490179255253:🔵]返回代理后台', callback_data='admin_home')])
    return InlineKeyboardMarkup(keyboard)


def build_admin_price_delta_text(config: AgentRuntimeConfig) -> str:
    return (
        '[emoji:5397916757333654639:➕]全局差价配置\n\n'
        f'当前差价：{standard_num(get_agent_price_delta(config))} USDT\n\n'
        '说明：设置 +0.2，表示主号铺所有商品原价统一 +0.2 作为代理售价。'
    )


def build_admin_config_keyboard(back='admin_home') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton('[emoji:5954227490179255253:🔵]返回代理后台', callback_data=back)]])


def build_admin_purchase_notice_text(config: AgentRuntimeConfig) -> str:
    current = get_agent_purchase_notice(config)
    current_text = current or '未配置'
    return (
        '[emoji:5235511932064129087:🎁]购买后通知配置\n\n'
        f'当前内容：\n{current_text}\n\n'
        '请直接把最终想发给用户的内容发给机器人即可。\n'
        '会员表情可以直接发，机器人会自动识别保存，不需要手打代码。\n\n'
        '例如：\n'
        '✔️您的账号已打包完成\n\n'
        '⚠️二级密码请在文件里查看 2fa\n\n'
        '如果要清空，直接发送：关闭'
    )


def build_withdraw_bind_text(config: AgentRuntimeConfig, user_id: int) -> str:
    address = get_user_withdraw_address(config, user_id) or '未绑定'
    return (
        '[emoji:5445353829304387411:💳]提款申请\n\n'
        f'最低提款：{standard_num(AGENT_WITHDRAW_MIN_AMOUNT)} USDT\n'
        f'当前绑定地址：{address}\n\n'
        '请先绑定 TRC20 地址，再提交提款金额。'
    )


def build_withdraw_bind_keyboard(config: AgentRuntimeConfig, user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton('[emoji:5443127283898405358:📥]绑定TRC20地址', callback_data='user_withdraw_bind')],
    ]
    if get_user_withdraw_address(config, user_id):
        keyboard.append([InlineKeyboardButton('[emoji:5445353829304387411:💳]申请提款', callback_data='user_withdraw_apply')])
    keyboard.append([InlineKeyboardButton('[emoji:5954227490179255253:🔵]返回首页', callback_data='agent_home')])
    return InlineKeyboardMarkup(keyboard)


def build_admin_withdraw_list_text(config: AgentRuntimeConfig, page: int = 0, page_size: int = 8) -> tuple[str, list[dict], int]:
    rows = list(agent_bots.database['agent_withdrawals'].find({'agent_bot_id': config.agent_bot_id}, sort=[('created_at', -1)], skip=page * page_size, limit=page_size))
    total = agent_bots.database['agent_withdrawals'].count_documents({'agent_bot_id': config.agent_bot_id})
    lines = [f'[emoji:5445353829304387411:💳]提款申请列表', '', f'总申请数：{total}']
    if not rows:
        lines.extend(['', '暂无提款申请'])
    for row in rows:
        lines.extend([
            '',
            f'单号：{row.get("withdrawal_id") or ""}',
            f'用户：{row.get("user_id") or ""}',
            f'金额：{standard_num(row.get("amount", 0))} USDT',
            f'状态：{row.get("state") or "pending"}',
        ])
    return '\n'.join(lines), rows, total


def build_admin_withdraw_list_keyboard(rows: list[dict], page: int, total: int, page_size: int = 8) -> InlineKeyboardMarkup:
    keyboard = []
    for row in rows:
        withdrawal_id = str(row.get('withdrawal_id') or '')
        keyboard.append([InlineKeyboardButton(f'[emoji:5445353829304387411:💳]{withdrawal_id}', callback_data=f'admin_withdraw_detail:{withdrawal_id}')])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('[emoji:5222044641200720562:🌸]上一页', callback_data=f'admin_withdraws:{page - 1}'))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton('[emoji:5220195537520711716:⚡️]下一页', callback_data=f'admin_withdraws:{page + 1}'))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton('[emoji:5954227490179255253:🔵]返回代理后台', callback_data='admin_home')])
    return InlineKeyboardMarkup(keyboard)


def build_admin_withdraw_detail_text(row: dict) -> str:
    return (
        '[emoji:5445353829304387411:💳]提款申请详情\n\n'
        f'单号：{row.get("withdrawal_id") or ""}\n'
        f'用户ID：{row.get("user_id") or ""}\n'
        f'金额：{standard_num(row.get("amount", 0))} USDT\n'
        f'地址：{row.get("address") or "未绑定"}\n'
        f'状态：{row.get("state") or "pending"}\n'
        f'备注：{row.get("note") or "无"}\n'
        f'申请时间：{row.get("created_at") or ""}'
    )


def build_admin_withdraw_detail_keyboard(row: dict) -> InlineKeyboardMarkup:
    withdrawal_id = str(row.get('withdrawal_id') or '')
    keyboard = []
    if str(row.get('state') or 'pending') == 'pending':
        keyboard.append([
            InlineKeyboardButton('[emoji:5312028599803460968:🆗]确认已打款', callback_data=f'admin_withdraw_paid:{withdrawal_id}'),
            InlineKeyboardButton('[emoji:5210952531676504517:❌]驳回并退回', callback_data=f'admin_withdraw_reject:{withdrawal_id}'),
        ])
    keyboard.append([InlineKeyboardButton('[emoji:5954227490179255253:🔵]返回提款列表', callback_data='admin_withdraws:0')])
    return InlineKeyboardMarkup(keyboard)


def build_admin_withdraw_notice_keyboard(withdrawal_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('[emoji:5312028599803460968:🆗]确认已打款', callback_data=f'admin_withdraw_paid:{withdrawal_id}'),
            InlineKeyboardButton('[emoji:5210952531676504517:❌]驳回并退回', callback_data=f'admin_withdraw_reject:{withdrawal_id}'),
        ],
        [InlineKeyboardButton('[emoji:5445353829304387411:💳]打开提款列表', callback_data='admin_withdraws:0')],
    ])


async def submit_agent_withdraw_request(config: AgentRuntimeConfig, context: ContextTypes.DEFAULT_TYPE, user_id: int, amount: float, note: str = '') -> tuple[dict | None, str]:
    amount = float(amount)
    if amount < AGENT_WITHDRAW_MIN_AMOUNT:
        return None, 'min_amount'
    wallet_address = get_user_withdraw_address(config, user_id)
    if not is_valid_trc20_address(wallet_address):
        return None, 'address_missing'
    withdrawal, status = create_agent_withdrawal_request(config.agent_bot_id, user_id, amount, address=wallet_address, note=note)
    if status != 'pending':
        return withdrawal, status
    notice_text = (
        '[emoji:5220214598585568818:🚨]新的提款申请\n\n'
        f'用户ID：{user_id}\n'
        f'单号：{withdrawal.get("withdrawal_id") or ""}\n'
        f'金额：{standard_num(withdrawal.get("amount", 0))} USDT\n'
        f'地址：{wallet_address}\n'
        f'备注：{note or "无"}'
    )
    await send_agent_admin_notice(config, context, notice_text, reply_markup=build_admin_withdraw_notice_keyboard(str(withdrawal.get('withdrawal_id') or '')))
    return withdrawal, status


def build_recharge_menu_keyboard(config: AgentRuntimeConfig) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for idx, amount in enumerate(config.recharge_amounts, start=1):
        row.append(InlineKeyboardButton(f'{standard_num(amount)} USDT', callback_data=f'agent_topup_amount:{amount}'))
        if idx % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
        keyboard.append([InlineKeyboardButton('[emoji:5282843764451195532:🖥]查看待支付订单', callback_data='agent_topup_pending')])
    keyboard.append([InlineKeyboardButton('[emoji:5954227490179255253:🔵]返回首页', callback_data='agent_home')])
    return InlineKeyboardMarkup(keyboard)


def build_topup_order_text(order: dict, config: AgentRuntimeConfig) -> str:
    state_map = {
        'pending': '待支付',
        'paid': '已到账',
        'expired': '已过期',
        'canceled': '已取消',
        'processing': '处理中',
    }
    return (
        f'[emoji:5197474438970363734:💳]代理充值订单\n\n'
        f'[emoji:5954227490179255253:🔵]订单号：{order.get("order_id") or ""}\n'
        f'[emoji:5301246586918024418:⚠️]状态：{state_map.get(str(order.get("state") or "pending"), str(order.get("state") or "pending"))}\n'
        f'[emoji:5080312910866024090:💵]充值方式：{str(order.get("type") or "trc20").upper()}\n'
        f'[emoji:4965219701572503640:💰]应付金额：{order.get("pay_amount_text") or order.get("pay_amount") or 0} {order.get("currency") or "USDT"}\n'
        f'[emoji:6314528083277778985:🏦]收款地址：{order.get("to_address") or config.trc20_address or "未配置"}\n'
        f'[emoji:5028418466000930064:📆]创建时间：{order.get("created_at") or ""}\n'
        f'[emoji:5382194935057372936:⏱️]过期时间：{order.get("expire_at") or ""}\n\n'
        '说明：下一步会把真实链上监听和代理充值确认接到这条订单链路上。'
    )


def build_topup_order_keyboard(order: dict) -> InlineKeyboardMarkup:
    order_id = str(order.get('order_id') or '')
    keyboard = []
    if order.get('state') == 'pending':
        keyboard.append([InlineKeyboardButton('[emoji:5217818964612108191:✨]刷新订单状态', callback_data=f'agent_topup_view:{order_id}')])
    keyboard.append([InlineKeyboardButton('[emoji:5197474438970363734:💳]继续充值', callback_data='agent_recharge_menu')])
    keyboard.append([InlineKeyboardButton('[emoji:5954227490179255253:🔵]返回首页', callback_data='agent_home')])
    return InlineKeyboardMarkup(keyboard)


def upsert_agent_bot_runtime(config: AgentRuntimeConfig) -> None:
    now = beijing_now_str()
    current = agent_bots.find_one({'agent_bot_id': config.agent_bot_id}) or {}
    agent_bots.update_one(
        {'agent_bot_id': config.agent_bot_id},
        {'$set': {
            'agent_bot_id': config.agent_bot_id,
            'agent_name': config.agent_name,
            'agent_username': config.agent_username,
            'customer_service': str(current.get('customer_service') or config.customer_service or '').strip(),
            'restock_target': str(current.get('restock_target') or '').strip(),
            'purchase_notice': str(current.get('purchase_notice') or '').strip(),
            'price_delta': float(current.get('price_delta', 0) or 0),
            'default_lang': config.default_lang,
            'admin_ids': list(config.admin_ids or ()),
            'updated_at': now,
            'state': 'active',
        }, '$setOnInsert': {
            'created_at': now,
        }},
        upsert=True,
    )
    ensure_agent_mongo_indexes(config.agent_bot_id)


def build_welcome_text(config: AgentRuntimeConfig, user_row: dict) -> str:
    username = str(user_row.get('username') or '').strip().lstrip('@')
    username_text = f'@{html.escape(username, quote=False)}' if username else '未设置'
    stats = get_agent_stats(config.agent_bot_id)
    return (
        f'[emoji:5222044641200720562:🌸]欢迎来到 {config.agent_name}\n\n'
        f'[emoji:5954227490179255253:🔵]代理标识：{config.agent_bot_id}\n'
        f'[emoji:5929391996408959380:🏞]你的账号：{user_row.get("user_id")}\n'
        f'用户名：{username_text}\n'
        f'[emoji:4972482444025398275:👛]当前余额：{user_row.get("USDT", 0)} USDT\n\n'
        f'[emoji:6321041414067068140:👤]当前代理用户数：{stats.get("user_count", 0)}\n'
        f'[emoji:5312361253610475399:🛒]订单记录数：{stats.get("purchase_records", 0)}\n\n'
        f'代理分销服务已接入商品列表骨架，下一步继续接充值、下单与结算。'
    )


def build_home_text(config: AgentRuntimeConfig, user_row: dict) -> str:
    return build_welcome_text(config, user_row)


def get_override_doc(agent_bot_id: str, nowuid: str) -> dict:
    return agent_product_prices.find_one({'agent_bot_id': agent_bot_id, 'nowuid': str(nowuid)}) or {}


def is_product_enabled_for_agent(agent_bot_id: str, nowuid: str) -> bool:
    override = get_override_doc(agent_bot_id, nowuid)
    if 'enabled' in override:
        return bool(override.get('enabled'))
    return True


def resolve_agent_product_payload(agent_bot_id: str, product_row: dict) -> dict:
    nowuid = str(product_row.get('nowuid') or '')
    override = get_override_doc(agent_bot_id, nowuid)
    runtime = agent_bots.find_one({'agent_bot_id': agent_bot_id}) or {}
    base_price = float(product_row.get('money', 0) or 0)
    price_delta = float(runtime.get('price_delta', 0) or 0)
    price = override.get('price', base_price + price_delta)
    display_name = str(override.get('display_name') or product_row.get('projectname') or '商品')
    return {
        'nowuid': nowuid,
        'uid': product_row.get('uid'),
        'projectname': display_name,
        'source_projectname': str(product_row.get('projectname') or '商品'),
        'price': price,
        'source_price': base_price,
        'price_delta': price_delta,
        'override': override,
        'stock': get_real_time_stock(nowuid),
    }


def build_category_rows(agent_bot_id: str) -> list[dict]:
    rows: list[dict] = []
    for category in list(fenlei.find({}, sort=[('row', 1)])):
        uid = category.get('uid')
        if not uid:
            continue
        products = list(ejfl.find({'uid': uid}, sort=[('row', 1)]))
        nowuids = [str(item.get('nowuid')) for item in products if item.get('nowuid')]
        stock_map = get_batch_stock(nowuids)
        total_stock = 0
        enabled_products = 0
        for item in products:
            nowuid = str(item.get('nowuid') or '')
            if not nowuid or not is_product_enabled_for_agent(agent_bot_id, nowuid):
                continue
            enabled_products += 1
            total_stock += int(stock_map.get(nowuid, 0) or 0)
        if enabled_products <= 0:
            continue
        rows.append({
            'uid': uid,
            'projectname': str(category.get('projectname') or '商品分类'),
            'row': int(category.get('row', 1) or 1),
            'stock': total_stock,
            'product_count': enabled_products,
        })
    return rows


def build_goods_catalog_text(config: AgentRuntimeConfig) -> str:
    return (
        f'[emoji:5312361253610475399:🛒]{config.agent_name} 商品目录\n\n'
        '下面显示的是当前代理可售分类。\n'
        '商品结构跟主号铺同步，价格优先读取代理覆盖。'
    )


def build_category_keyboard(config: AgentRuntimeConfig) -> InlineKeyboardMarkup:
    keyboard = []
    for item in build_category_rows(config.agent_bot_id):
        keyboard.append([
            InlineKeyboardButton(
                f"{item['projectname']} [ {item['stock']} ]",
                callback_data=f"agent_cate:{item['uid']}"
            )
        ])
    if not keyboard:
        keyboard.append([InlineKeyboardButton('暂无可售分类', callback_data='agent_noop')])
    return InlineKeyboardMarkup(keyboard)


def build_product_list_payload(config: AgentRuntimeConfig, uid: str) -> tuple[dict | None, list[dict]]:
    category = fenlei.find_one({'uid': uid})
    if category is None:
        return None, []
    rows = []
    for product in list(ejfl.find({'uid': uid}, sort=[('row', 1)])):
        nowuid = str(product.get('nowuid') or '')
        if not nowuid or not is_product_enabled_for_agent(config.agent_bot_id, nowuid):
            continue
        payload = resolve_agent_product_payload(config.agent_bot_id, product)
        rows.append(payload)
    rows.sort(key=lambda item: (-int(item.get('stock', 0) or 0), str(item.get('projectname') or '')))
    return category, rows


def build_product_list_text(category_name: str, products: list[dict]) -> str:
    if not products:
        return f'[emoji:5954227490179255253:🔵]分类：{category_name}\n\n当前代理在这个分类下暂时没有可售商品。'
    return (
        f'[emoji:5954227490179255253:🔵]分类：{category_name}\n\n'
        f'共 {len(products)} 个可售商品，下面价格优先按代理覆盖显示。'
    )


def build_agent_product_purchase_text(config: AgentRuntimeConfig, payload: dict, user_id: int) -> str:
    projectname = str(payload.get('projectname') or '商品')
    price_text = standard_num(payload.get('price', 0))
    stock_count = int(payload.get('stock', 0) or 0)
    lang = get_agent_lang(config, user_id=user_id)
    if lang == 'en':
        return (
            f'[emoji:5260463209562776385:✅] You are buying: {projectname}\n\n'
            f'[emoji:4965219701572503640:💰] Price: {price_text} USDT\n\n'
            f'[emoji:5282843764451195532:🖥] Stock: {stock_count}\n\n'
            '[emoji:5301246586918024418:⚠️] If this is your first purchase of this item, please test with a small quantity first to avoid unnecessary disputes. Thank you.'
        )
    return (
        f'[emoji:5260463209562776385:✅] 您正在购买： {projectname}\n\n'
        f'[emoji:4965219701572503640:💰] 价格： {price_text} USDT\n\n'
        f'[emoji:5282843764451195532:🖥] 库存： {stock_count}\n\n'
        '[emoji:5301246586918024418:⚠️] 未使用过该商品的，请先少量购买测试，以免造成不必要的争执！谢谢合作！'
    )


def build_agent_product_purchase_keyboard(config: AgentRuntimeConfig, nowuid: str, uid: str, user_id: int, stock_count: int | None = None) -> InlineKeyboardMarkup:
    stock_count = get_real_time_stock(nowuid) if stock_count is None else int(stock_count)
    if stock_count > 0:
        buy_button = InlineKeyboardButton(get_agent_ui_text(config, 'buy_now', user_id=user_id), callback_data=f'agent_buy:{nowuid}')
    else:
        buy_button = InlineKeyboardButton(get_agent_ui_text(config, 'out_of_stock_button', user_id=user_id), callback_data='agent_noop')
    return InlineKeyboardMarkup([
        [buy_button],
        [
            InlineKeyboardButton(get_agent_ui_text(config, 'main_menu', user_id=user_id), callback_data='agent_home'),
            InlineKeyboardButton(get_agent_ui_text(config, 'back', user_id=user_id), callback_data=f'agent_cate:{uid}'),
        ]
    ])


def build_product_keyboard(uid: str, products: list[dict]) -> InlineKeyboardMarkup:
    keyboard = []
    for item in products:
        price_text = standard_num(item.get('price', 0))
        button_text = f"{item.get('projectname')}    ${price_text}    [ {item.get('stock', 0)} ]"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"agent_goods:{item['nowuid']}")])
    keyboard.append([InlineKeyboardButton('[emoji:5954227490179255253:🔵]返回分类', callback_data='agent_catalog')])
    return InlineKeyboardMarkup(keyboard)


def build_product_detail_text(payload: dict) -> str:
    override = payload.get('override') or {}
    override_note = '代理自定义价' if 'price' in override else '跟随主号铺价格'
    source_name = payload.get('source_projectname') or payload.get('projectname')
    text = (
        f'{str(payload.get("projectname") or "商品")}\n\n'
        f'[emoji:5954227490179255253:🔵]商品ID：{payload.get("nowuid") or ""}\n'
        f'[emoji:4965219701572503640:💰]当前价格：{standard_num(payload.get("price", 0))} USDT\n'
        f'[emoji:5282843764451195532:🖥]当前库存：{payload.get("stock", 0)}\n'
        f'价格来源：{override_note}\n'
        f'主号铺名称：{source_name}\n'
        f'主号铺基准价：{standard_num(payload.get("source_price", 0))} USDT\n\n'
        '下单链路下一步继续接；这一步先把代理商品展示和价格覆盖跑通。'
    )
    if override.get('display_name'):
        text += f'\n代理显示名：{override.get("display_name")}'
    return text


def build_product_detail_keyboard(config: AgentRuntimeConfig, nowuid: str, uid: str, user_id: int, stock_count: int | None = None) -> InlineKeyboardMarkup:
    return build_agent_product_purchase_keyboard(config, nowuid, uid, user_id, stock_count=stock_count)


def build_purchase_confirm_text(config: AgentRuntimeConfig, payload: dict, user_row: dict, quantity: int) -> str:
    total_amount = standard_num(float(payload.get('price', 0) or 0) * max(1, int(quantity or 1)))
    return strip_basic_html(get_agent_ui_text(
        config,
        'purchase_confirm_text',
        user_id=int(user_row.get('user_id') or 0),
        projectname=str(payload.get('projectname') or '商品'),
        gmsl=max(1, int(quantity or 1)),
        zxymoney=total_amount,
        USDT=standard_num(user_row.get('USDT', 0)),
    ))


def build_purchase_confirm_keyboard(config: AgentRuntimeConfig, nowuid: str, uid: str, user_id: int, quantity: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(get_agent_ui_text(config, 'cancel_trade', user_id=user_id), callback_data='agent_home'),
            InlineKeyboardButton(get_agent_ui_text(config, 'confirm_purchase', user_id=user_id), callback_data=f'agent_buy_confirm:{nowuid}:{quantity}')
        ],
        [InlineKeyboardButton(get_agent_ui_text(config, 'main_menu', user_id=user_id), callback_data='agent_home')],
    ])


def build_purchase_result_text(config: AgentRuntimeConfig, order: dict, user_id: int) -> str:
    deducted_text = standard_num(order.get('total_amount', 0))
    remaining_text = standard_num(order.get('balance_after', 0))
    lang = get_agent_lang(config, user_id=user_id)
    if lang == 'en':
        return (
            '[emoji:5193209274452425995:🎉]Purchase Successful\n\n'
            f'[emoji:4965219701572503640:💰]Deducted from Balance: {deducted_text} USDT\n'
            f'[emoji:5954227490179255253:🔵]Remaining Balance: {remaining_text} USDT'
        )
    return (
        '[emoji:5193209274452425995:🎉]购买成功\n\n'
        f'[emoji:4965219701572503640:💰]已从余额中扣除：{deducted_text} USDT\n'
        f'[emoji:5954227490179255253:🔵]当前余额：{remaining_text} USDT'
    )


def build_purchase_status_text(config: AgentRuntimeConfig, status: str, user_id: int) -> str:
    mapping = {
        'product_not_found': strip_basic_html(get_agent_ui_text(config, 'product_not_found', user_id=user_id)),
        'stock': strip_basic_html(get_agent_ui_text(config, 'current_no_stock', user_id=user_id)),
        'balance': strip_basic_html(get_agent_ui_text(config, 'insufficient_balance', user_id=user_id)),
        'invalid_price': '商品价格异常，请联系管理员。',
        'product_disabled': '当前代理未开放这个商品。',
    }
    return mapping.get(status, f'下单失败：{status}')


def build_agent_account_check_progress_text(config: AgentRuntimeConfig, total_count: int, checked_count: int, alive_count: int, invalid_count: int, frozen_count: int, timeout_count: int, user_id: int) -> str:
    lang = get_agent_lang(config, user_id=user_id)
    if lang == 'en':
        return (
            '[emoji:6237934454019461140:🧠]Logging in and updating 24-month delete-after-inactive setting, please wait...\n\n'
            f'Checked: {checked_count} / {total_count}'
        )
    return (
        '[emoji:6237934454019461140:🧠]正在检测账号状态 请稍等\n\n'
        f'已检测：{checked_count} / {total_count}'
    )


def summarize_agent_check_reason(reason: str, limit: int = 120) -> str:
    text = str(reason or '').strip().replace('\n', ' ').replace('\r', ' ')
    if len(text) <= limit:
        return text
    return f'{text[:limit - 3]}...'


def build_agent_delivery_result_text(config: AgentRuntimeConfig, order: dict, total_count: int, alive_count: int, invalid_count: int, frozen_count: int, timeout_count: int, refund_amount: float, balance_after: float, user_id: int, first_invalid_reason: str = '', first_frozen_reason: str = '', first_invalid_entry_type: str = '', first_invalid_path: str = '', first_frozen_entry_type: str = '', first_frozen_path: str = '') -> str:
    charged_amount = float(standard_num(float(order.get('total_amount', 0) or 0) - float(refund_amount or 0)))
    refund_text = standard_num(refund_amount)
    remaining_text = standard_num(balance_after)
    lang = get_agent_lang(config, user_id=user_id)
    lines = []
    if alive_count == 0 and timeout_count == 0:
        lines.append('[emoji:6213214271531126888:🐛]All checked accounts were invalid. Refund issued.' if lang == 'en' else '[emoji:6213214271531126888:🐛]全部检测账号均无效，已退款')
    else:
        lines.append('[emoji:5193209274452425995:🎉]Purchase Successful' if lang == 'en' else '[emoji:5193209274452425995:🎉]购买成功')
    lines.append('')
    if lang == 'en':
        lines.extend([
            f'[emoji:5352625743081775722:🎚️]Total Accounts: {total_count}',
            f'[emoji:5260463209562776385:✅]Valid Accounts: {alive_count}',
            f'[emoji:5273914604752216432:❌]Invalid Accounts: {invalid_count}',
            f'[emoji:5449449325434266744:❄️]Frozen Accounts: {frozen_count}',
        ])
    else:
        lines.extend([
            f'[emoji:5352625743081775722:🎚️]总账号数：{total_count}',
            f'[emoji:5260463209562776385:✅]有效账号：{alive_count}',
            f'[emoji:5273914604752216432:❌]无效账号：{invalid_count}',
            f'[emoji:5449449325434266744:❄️]冻结账号：{frozen_count}',
        ])
    if timeout_count:
        lines.append(f'[emoji:5382194935057372936:⏱️]Timed-out Accounts: {timeout_count}' if lang == 'en' else f'[emoji:5382194935057372936:⏱️]超时账号：{timeout_count}')
    lines.append(f'[emoji:4965219701572503640:💰]Deducted from Balance: {charged_amount} USDT' if lang == 'en' else f'[emoji:4965219701572503640:💰]已从余额中扣除：{charged_amount} USDT')
    if refund_amount:
        lines.append(f'[emoji:5235511932064129087:🎁]Refunded to Balance: {refund_text} USDT' if lang == 'en' else f'[emoji:5235511932064129087:🎁]已退款回余额：{refund_text} USDT')
    lines.append(f'[emoji:5954227490179255253:🔵]Remaining Balance: {remaining_text} USDT' if lang == 'en' else f'[emoji:5954227490179255253:🔵]当前余额：{remaining_text} USDT')
    if timeout_count:
        lines.extend([
            '',
            '[emoji:5382194935057372936:⏱️]Timed-out accounts were retried twice and still could not be checked. They were delivered with the file. Please contact support if needed.' if lang == 'en' else '[emoji:5382194935057372936:⏱️]超时账号已自动重试 2 次，仍无法完成检测，现已随文件一起发出；如需售后请联系客服。'
        ])
    return '\n'.join(lines)


def build_agent_delivery_admin_notice(order: dict, user_row: dict, total_count: int, alive_count: int, invalid_count: int, frozen_count: int, timeout_count: int, refund_amount: float, first_invalid_reason: str = '', first_frozen_reason: str = '', first_invalid_entry_type: str = '', first_invalid_path: str = '', first_frozen_entry_type: str = '', first_frozen_path: str = '') -> str:
    username = str(user_row.get('username') or order.get('username') or '').strip().lstrip('@')
    username_text = f'@{username}' if username else '未设置'
    charged_amount = standard_num(float(order.get('total_amount', 0) or 0) - float(refund_amount or 0))
    lines = [
        '[emoji:5312361253610475399:🛒]代理订单通知',
        '',
        f'代理：{order.get("tenant_id") or ""}',
        f'用户：{username_text}',
        f'用户ID：{order.get("user_id") or ""}',
        f'订单号：{order.get("order_id") or ""}',
        f'商品：{order.get("category_name") or ""}/{order.get("product_name") or "商品"}',
        '',
        f'总数：{total_count}',
        f'有效：{alive_count}',
        f'失效：{invalid_count}',
        f'冻结：{frozen_count}',
        f'超时：{timeout_count}',
        f'扣款：{charged_amount} USDT',
        f'退款：{standard_num(refund_amount)} USDT',
    ]
    return '\n'.join(lines)


def write_text_temp_file(prefix: str, suffix: str, content: str) -> str:
    temp_dir = Path(tempfile.gettempdir()) / 'botshop-agent-delivery'
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_path = temp_dir / f'{prefix}_{datetime.now().strftime("%Y%m%d%H%M%S%f")}{suffix}'
    file_path.write_text(str(content or ''), encoding='utf-8')
    return str(file_path)


def get_agent_account_check_concurrency(total_count: int) -> int:
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


AGENT_ACCOUNT_CHECK_MAX_RETRIES = 2


def resolve_agent_inventory_check_target(leixing: str, nowuid: str, projectname: str):
    projectname = str(projectname or '').strip()
    folder_path = find_existing_storage_path('号包', nowuid, projectname)
    if folder_path.exists() and folder_path.is_dir():
        tdata_path = folder_path / 'tdata'
        if tdata_path.exists() and tdata_path.is_dir():
            return '直登号', tdata_path
        session_files = sorted(folder_path.glob('*.session'))
        if session_files:
            return '协议号', session_files[0]
    session_path = find_existing_storage_path('协议号', nowuid, f'{projectname}.session')
    if session_path.exists():
        return '协议号', session_path
    return resolve_inventory_check_target(leixing, nowuid, projectname)


def infer_agent_delivery_type(leixing: str, nowuid: str, project_names: list[str]) -> str:
    if any(find_existing_storage_path('号包', nowuid, projectname).exists() for projectname in project_names):
        return '直登号'
    if any(find_existing_storage_path('协议号', nowuid, f'{projectname}.session').exists() or find_existing_storage_path('协议号', nowuid, f'{projectname}.json').exists() for projectname in project_names):
        return '协议号'
    return leixing


def build_agent_delivery_file(leixing: str, user_id: int, nowuid: str, selected_docs: list[dict]) -> str | None:
    selected_docs = list(selected_docs or [])
    if not selected_docs:
        return None
    project_names = [str(item.get('projectname') or '') for item in selected_docs if item.get('projectname')]
    actual_delivery_type = infer_agent_delivery_type(leixing, nowuid, project_names)
    if actual_delivery_type in {'协议号', '直登号'}:
        return str(build_delivery_zip(actual_delivery_type, user_id, nowuid, project_names))
    if leixing == 'API':
        return write_text_temp_file(str(user_id), '.txt', '\n'.join(project_names))
    if selected_docs and isinstance(selected_docs[0].get('data'), dict):
        lines = []
        for item in selected_docs:
            data = item.get('data') or {}
            values = [str(v) for v in data.values() if str(v or '').strip()]
            lines.append('\n'.join(values) if values else str(item.get('projectname') or ''))
        return write_text_temp_file(str(user_id), '.txt', '\n\n'.join(lines))
    return write_text_temp_file(str(user_id), '.txt', '\n'.join(project_names))


async def send_agent_admin_notice(config: AgentRuntimeConfig, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None) -> None:
    for admin_user_id in list(config.admin_ids or []):
        try:
            await send_rendered(context.bot, int(admin_user_id), text, reply_markup=reply_markup)
        except Exception:
            logger.warning('send agent admin notice failed: tenant=%s admin=%s', config.agent_bot_id, admin_user_id, exc_info=True)


async def deliver_agent_order(context: ContextTypes.DEFAULT_TYPE, config: AgentRuntimeConfig, order_id: str) -> None:
    order = get_tenant_order(config.agent_bot_id, order_id) or {}
    if not order:
        return
    user_id = int(order.get('user_id') or 0)
    user_row = get_agent_bot_user(config.agent_bot_id, user_id) or {'user_id': user_id}
    nowuid = str(order.get('nowuid') or '')
    selected_docs = list(hb.find({'delivery_order_id': order_id}, sort=[('_id', 1)])) if order_id else []
    if not selected_docs:
        update_tenant_order(config.agent_bot_id, order_id, {'state': 'delivery_failed', 'delivery_error': 'reserved_inventory_missing', 'updated_at': beijing_now_str()})
        return
    leixing = str((selected_docs[0] or {}).get('leixing') or '')
    product = ejfl.find_one({'nowuid': nowuid}) or {}
    agent_notice_text = get_agent_purchase_notice(config)
    notice_text = agent_notice_text or str(get_buy_notice_text(product.get('text', '')) or '').strip()
    total_count = len(selected_docs)
    update_tenant_order(config.agent_bot_id, order_id, {'state': 'delivering', 'delivery_started_at': beijing_now_str(), 'delivery_type': leixing})

    use_account_check = ACCOUNT_CHECK_ENABLED and leixing in ACCOUNT_CHECK_SUPPORTED_TYPES and bool(get_account_check_runtime_status(leixing).get('ready'))
    alive_items: list[dict] = []
    invalid_items: list[dict] = []
    frozen_items: list[dict] = []
    timeout_items: list[dict] = []
    progress_message = None

    if use_account_check:
        progress_message = await send_rendered(context.bot, user_id, build_agent_account_check_progress_text(config, total_count, 0, 0, 0, 0, 0, user_id))
        checked_count = 0
        last_progress_ts = 0.0
        first_invalid_reason = ''
        first_invalid_entry_type = ''
        first_invalid_path = ''
        first_frozen_reason = ''
        first_frozen_entry_type = ''
        first_frozen_path = ''
        semaphore = asyncio.Semaphore(get_agent_account_check_concurrency(total_count))

        async def run_agent_check(item: dict):
            projectname = str(item.get('projectname') or '')
            entry_type, target_path = resolve_agent_inventory_check_target(leixing, nowuid, projectname)
            runtime_status = get_account_check_runtime_status(entry_type)
            if not runtime_status.get('ready'):
                check_result = {
                    'status': 'timeout',
                    'reason': f"runtime_not_ready:{runtime_status.get('reason', 'unknown')}",
                    'entry_type': entry_type,
                    'path': str(target_path),
                    'attempts': 1,
                    'max_retries': AGENT_ACCOUNT_CHECK_MAX_RETRIES,
                }
                return item, projectname, check_result
            attempts = 0
            check_result = {'status': 'timeout', 'reason': 'empty_check_result'}
            while True:
                attempts += 1
                async with semaphore:
                    try:
                        check_result = await asyncio.to_thread(check_account_inventory_item_with_ttl_update, entry_type, str(target_path), ACCOUNT_CHECK_TIMEOUT_SECONDS)
                    except Exception as exc:
                        check_result = {'status': 'timeout', 'reason': str(exc) or exc.__class__.__name__}
                if check_result.get('status') != 'timeout':
                    break
                if attempts > AGENT_ACCOUNT_CHECK_MAX_RETRIES:
                    timeout_reason = str(check_result.get('reason', '') or '')
                    check_result = {
                        'status': 'invalid',
                        'reason': f'agent_ttl_check_timeout_after_retries:{timeout_reason}' if timeout_reason else 'agent_ttl_check_timeout_after_retries',
                    }
                    break
                logger.warning(
                    'agent ttl-check timeout, retrying: tenant=%s order=%s hbid=%s attempt=%s/%s path=%s reason=%s',
                    config.agent_bot_id,
                    order_id,
                    item.get('hbid'),
                    attempts,
                    AGENT_ACCOUNT_CHECK_MAX_RETRIES + 1,
                    str(target_path),
                    check_result.get('reason', ''),
                )
            check_result = dict(check_result or {})
            check_result['attempts'] = attempts
            check_result['max_retries'] = AGENT_ACCOUNT_CHECK_MAX_RETRIES
            return item, projectname, check_result

        tasks = [asyncio.create_task(run_agent_check(item)) for item in selected_docs]
        try:
            for future in asyncio.as_completed(tasks):
                item, projectname, check_result = await future
                checked_count += 1
                reason_text = str(check_result.get('reason', '') or '')
                attempts = int(check_result.get('attempts', 1) or 1)
                if check_result.get('status') == 'timeout' and attempts > 1:
                    reason_text = f'{reason_text} | retries={attempts - 1}'.strip(' |')
                logger.warning(
                    'agent ttl-check result: tenant=%s order=%s hbid=%s project=%s entry_type=%s status=%s attempts=%s path=%s reason=%s',
                    config.agent_bot_id,
                    order_id,
                    item.get('hbid'),
                    projectname,
                    check_result.get('entry_type') or '',
                    check_result.get('status', 'timeout'),
                    attempts,
                    check_result.get('path') or '',
                    reason_text,
                )
                hb.update_one(
                    {'_id': item.get('_id')},
                    {'$set': {
                        'delivery_check_state': check_result.get('status', 'timeout'),
                        'delivery_check_reason': reason_text[:500],
                        'delivery_check_timer': beijing_now_str(),
                    }}
                )
                meta = {'hbid': item.get('hbid'), 'projectname': projectname, 'status': check_result.get('status', 'timeout'), 'reason': reason_text, 'attempts': attempts}
                status = check_result.get('status')
                if status == 'alive':
                    alive_items.append(item)
                elif status == 'invalid':
                    if not first_invalid_reason and reason_text:
                        first_invalid_reason = reason_text
                        first_invalid_entry_type = str(check_result.get('entry_type') or '')
                        first_invalid_path = str(check_result.get('path') or '')
                    invalid_items.append(archive_invalid_inventory_item(leixing, nowuid, projectname, order_id, meta))
                elif status == 'frozen':
                    if not first_frozen_reason and reason_text:
                        first_frozen_reason = reason_text
                        first_frozen_entry_type = str(check_result.get('entry_type') or '')
                        first_frozen_path = str(check_result.get('path') or '')
                    frozen_items.append(archive_invalid_inventory_item(leixing, nowuid, projectname, order_id, meta))
                else:
                    timeout_items.append(item)

                should_push_progress = (
                    checked_count == total_count
                    or checked_count % ACCOUNT_CHECK_PROGRESS_STEP == 0
                    or time.time() - last_progress_ts >= ACCOUNT_CHECK_PROGRESS_INTERVAL_SECONDS
                )
                if progress_message is not None and should_push_progress:
                    try:
                        rendered_text, entities = render_text(build_agent_account_check_progress_text(config, total_count, checked_count, len(alive_items), len(invalid_items), len(frozen_items), len(timeout_items), user_id))
                        await context.bot.edit_message_text(chat_id=user_id, message_id=progress_message.message_id, text=rendered_text, entities=entities)
                    except Exception:
                        logger.warning('update agent account-check progress failed: tenant=%s order=%s', config.agent_bot_id, order_id, exc_info=True)
                    last_progress_ts = time.time()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
    else:
        first_invalid_reason = ''
        first_invalid_entry_type = ''
        first_invalid_path = ''
        first_frozen_reason = ''
        first_frozen_entry_type = ''
        first_frozen_path = ''
        alive_items = list(selected_docs)

    refund_amount = float(standard_num(float(order.get('unit_price', 0) or 0) * (len(invalid_items) + len(frozen_items))))
    refund_result = refund_tenant_order(config.agent_bot_id, user_id, order_id, refund_amount, reason='agent_account_check_refund', meta={'tenant_id': config.agent_bot_id})
    balance_after = float(refund_result.get('balance_after', user_row.get('USDT', 0) or 0))

    if invalid_items or frozen_items:
        write_invalid_archive_meta(order_id, {
            'tenant_id': config.agent_bot_id,
            'order_id': order_id,
            'user_id': user_id,
            'product_nowuid': nowuid,
            'product_name': order.get('product_name'),
            'delivery_type': leixing,
            'total_count': total_count,
            'alive_count': len(alive_items),
            'invalid_count': len(invalid_items),
            'frozen_count': len(frozen_items),
            'timeout_count': len(timeout_items),
            'refund_amount': refund_amount,
            'created_at': beijing_now_str(),
            'invalid_items': invalid_items,
            'frozen_items': frozen_items,
        })

    delivery_file = build_agent_delivery_file(leixing, user_id, nowuid, alive_items + timeout_items)
    if delivery_file:
        with open(delivery_file, 'rb') as fp:
            await context.bot.send_document(chat_id=user_id, document=fp)
    if notice_text:
        if agent_notice_text:
            await send_rendered(context.bot, user_id, notice_text)
        else:
            await context.bot.send_message(chat_id=user_id, text=notice_text, parse_mode='HTML', disable_web_page_preview=True)

    final_state = 'delivered'
    if refund_amount > 0 and len(alive_items) + len(timeout_items) == 0:
        final_state = 'refunded'
    elif refund_amount > 0:
        final_state = 'partial_refunded'
    final_text = build_agent_delivery_result_text(config, order, total_count, len(alive_items), len(invalid_items), len(frozen_items), len(timeout_items), refund_amount, balance_after, user_id, first_invalid_reason=first_invalid_reason, first_frozen_reason=first_frozen_reason, first_invalid_entry_type=first_invalid_entry_type, first_invalid_path=first_invalid_path, first_frozen_entry_type=first_frozen_entry_type, first_frozen_path=first_frozen_path)
    if progress_message is not None:
        try:
            rendered_text, entities = render_text(final_text)
            await context.bot.edit_message_text(chat_id=user_id, message_id=progress_message.message_id, text=rendered_text, entities=entities)
        except Exception:
            await send_rendered(context.bot, user_id, final_text)
    else:
        await send_rendered(context.bot, user_id, final_text)

    update_tenant_order(config.agent_bot_id, order_id, {
        'state': final_state,
        'delivery_finished_at': beijing_now_str(),
        'delivery_type': leixing,
        'alive_count': len(alive_items),
        'invalid_count': len(invalid_items),
        'frozen_count': len(frozen_items),
        'timeout_count': len(timeout_items),
        'refund_amount': refund_amount,
        'balance_after': balance_after,
        'delivery_file': delivery_file or '',
    })
    get_agent_bot_gmjlu_collection(config.agent_bot_id).insert_one({
        'leixing': '代理发货',
        'bianhao': order_id,
        'user_id': user_id,
        'projectname': str(order.get('product_name') or '商品'),
        'text': delivery_file or '',
        'ts': final_text,
        'timer': beijing_now_str(),
    })
    await send_agent_admin_notice(config, context, build_agent_delivery_admin_notice(order, user_row, total_count, len(alive_items), len(invalid_items), len(frozen_items), len(timeout_items), refund_amount, first_invalid_reason=first_invalid_reason, first_frozen_reason=first_frozen_reason, first_invalid_entry_type=first_invalid_entry_type, first_invalid_path=first_invalid_path, first_frozen_entry_type=first_frozen_entry_type, first_frozen_path=first_frozen_path))


def format_trc20_amount_text(value) -> str:
    return f'{float(standard_num(value)):.4f}'.rstrip('0').rstrip('.')


async def process_agent_topups(context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    if not config.trc20_address or not is_valid_trc20_address(config.trc20_address):
        return
    for row in list(qukuai.find({'state': 0, 'to_address': config.trc20_address}, sort=[('block_timestamp', 1)], limit=20)):
        txid = str(row.get('txid') or '').strip()
        if not txid:
            continue
        amount_text = format_trc20_amount_text(float(row.get('quant', 0) or 0) / 1000000)
        block_ts = int(row.get('block_timestamp') or 0)
        order = topup_orders.find_one({
            'tenant_id': config.agent_bot_id,
            'type': 'trc20',
            'state': 'pending',
            'to_address': config.trc20_address,
            'pay_amount_text': amount_text,
            'created_ts_ms': {'$lte': block_ts},
            'expire_ts_ms': {'$gte': block_ts},
        }, sort=[('created_ts_ms', 1)])
        if order is None:
            continue
        paid_order, status = mark_tenant_topup_paid(order.get('order_id'), txid=txid, paid_amount=float(row.get('quant', 0) or 0) / 1000000, currency='USDT', channel='trc20_listener', meta={'from_address': row.get('from_address')})
        if status not in {'paid', 'already_paid'}:
            continue
        qukuai.update_one({'_id': row['_id']}, {'$set': {'state': 1, 'match_order': order.get('order_id'), 'match_user_id': order.get('user_id'), 'match_tenant_id': config.agent_bot_id}})
        user_id = int(order.get('user_id') or 0)
        text = (
            '[emoji:5193209274452425995:🎉]代理充值到账\n\n'
            f'订单号：{order.get("order_id") or ""}\n'
            f'金额：{amount_text} USDT\n'
            f'余额：{standard_num((paid_order or {}).get("balance_after", 0))} USDT'
        )
        try:
            await send_rendered(context.bot, user_id, text)
        except Exception:
            logger.warning('send agent topup success failed: tenant=%s user=%s order=%s', config.agent_bot_id, user_id, order.get('order_id'), exc_info=True)


async def agent_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None or update.effective_chat is None:
        return
    rows = list(tenant_orders.find({'tenant_id': config.agent_bot_id, 'user_id': tg_user.id}, sort=[('created_ts_ms', -1)], limit=10))
    if not rows:
        await reply_rendered(update, '[emoji:5312361253610475399:🛒]当前还没有代理订单记录。')
        return
    lines = ['[emoji:5312361253610475399:🛒]最近代理订单']
    for row in rows:
        lines.extend([
            '',
            f'订单号：{row.get("order_id") or ""}',
            f'商品：{row.get("product_name") or "商品"}',
            f'金额：{standard_num(row.get("total_amount", 0))} USDT',
            f'状态：{row.get("state") or ""}',
        ])
    await reply_rendered(update, '\n'.join(lines))


async def send_catalog(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    await send_rendered(context.bot, chat_id, build_goods_catalog_text(config), reply_markup=build_category_keyboard(config))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None or update.effective_chat is None:
        return
    fullname = (tg_user.full_name or '').replace('<', '').replace('>', '')
    user_row = ensure_agent_user_exists(
        config.agent_bot_id,
        tg_user.id,
        tg_user.username,
        fullname,
        tg_user.last_name,
        getattr(tg_user, 'language_code', None) or config.default_lang,
        state='1',
    )
    await reply_rendered(
        update,
        build_home_text(config, user_row or {'user_id': tg_user.id, 'USDT': 0, 'username': tg_user.username}),
        reply_markup=build_home_keyboard(config, lang=(user_row or {}).get('lang', config.default_lang), user_id=tg_user.id),
    )


async def send_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None or update.effective_chat is None:
        return
    user_row = get_agent_bot_user(config.agent_bot_id, tg_user.id) or {'user_id': tg_user.id, 'USDT': 0, 'username': tg_user.username}
    await reply_rendered(update, build_home_text(config, user_row), reply_markup=build_home_keyboard(config, lang=user_row.get('lang', config.default_lang), user_id=tg_user.id))


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None or update.effective_chat is None:
        return
    user_row = get_agent_bot_user(config.agent_bot_id, tg_user.id) or {}
    text = (
        f'[emoji:5929391996408959380:🏞]代理个人中心\n\n'
        f'[emoji:5954227490179255253:🔵]代理标识：{config.agent_bot_id}\n'
        f'[emoji:5929391996408959380:🏞]用户ID：{tg_user.id}\n'
        f'[emoji:4972482444025398275:👛]余额：{standard_num(user_row.get("USDT", 0))} USDT\n'
        f'[emoji:5443127283898405358:📥]提款地址：{get_user_withdraw_address(config, tg_user.id) or "未绑定"}\n'
        f'[emoji:6273995106810863535:🌑]总购数量：{user_row.get("zgsl", 0)}\n'
        f'[emoji:5028746137645876535:📈]总购金额：{standard_num(user_row.get("zgje", 0))} USDT'
    )
    await reply_rendered(update, text)


async def send_recharge_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    expire_tenant_topup_orders(config.agent_bot_id)
    await send_rendered(context.bot, chat_id, build_recharge_menu_text(config), reply_markup=build_recharge_menu_keyboard(config))


async def send_pending_topup(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    expire_tenant_topup_orders(config.agent_bot_id)
    order = get_latest_pending_topup_order(config.agent_bot_id, user_id, payment_type='trc20')
    if order is None:
        await send_rendered(context.bot, chat_id, '[emoji:5301246586918024418:⚠️]当前没有待支付的代理充值订单。')
        return
    await send_rendered(context.bot, chat_id, build_topup_order_text(order, config), reply_markup=build_topup_order_keyboard(order))


async def agent_credit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None:
        return
    if not is_agent_admin_or_source_admin(config, tg_user.id):
        await update.effective_chat.send_message('只有代理管理员可以手动上分。')
        return
    if len(context.args) < 2:
        await update.effective_chat.send_message('用法：/agent_credit <user_id> <amount> [备注]')
        return
    try:
        target_user_id = int(context.args[0])
        amount = float(context.args[1])
    except Exception:
        await update.effective_chat.send_message('user_id 或 amount 格式不对。')
        return
    note = ' '.join(context.args[2:]).strip() or '代理管理员手动上分'
    ensure_agent_user_exists(config.agent_bot_id, target_user_id)
    result = credit_tenant_wallet(
        config.agent_bot_id,
        target_user_id,
        amount,
        biz_type='manual_credit',
        ref_id=f'ADMIN{tg_user.id}',
        description=note,
        meta={'operator_user_id': tg_user.id},
    )
    await update.effective_chat.send_message(
        f'已完成手动上分\n用户: <code>{target_user_id}</code>\n金额: <code>{standard_num(amount)} USDT</code>\n余额: <code>{standard_num(result["balance_after"])} USDT</code>',
        parse_mode='HTML'
    )


async def agent_mark_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None:
        return
    if not is_agent_admin_or_source_admin(config, tg_user.id):
        await update.effective_chat.send_message('只有代理管理员可以手动确认到账。')
        return
    if not context.args:
        await update.effective_chat.send_message('用法：/agent_mark_paid <order_id> [txid]')
        return
    order_id = str(context.args[0]).strip()
    txid = str(context.args[1]).strip() if len(context.args) > 1 else ''
    order, status = mark_tenant_topup_paid(
        order_id,
        txid=txid,
        currency='USDT',
        channel='agent_admin_manual',
        meta={'operator_user_id': tg_user.id},
    )
    if status != 'paid' and status != 'already_paid':
        await update.effective_chat.send_message(f'处理失败：{status}')
        return
    await update.effective_chat.send_message(
        f'订单状态：{status}\n订单号：<code>{html.escape(order_id, quote=False)}</code>\n当前状态：<code>{html.escape(str((order or {}).get("state") or status), quote=False)}</code>',
        parse_mode='HTML'
    )


async def agent_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None:
        return
    if len(context.args) < 1:
        await update.effective_chat.send_message('用法：/agent_withdraw <amount> [note]\n先用 /bindtrc20 绑定地址，或走 /withdraw 按钮流程。')
        return
    try:
        amount = float(context.args[0])
    except Exception:
        await update.effective_chat.send_message('提现金额必须是数字。')
        return
    note = ' '.join(context.args[1:]).strip()
    withdrawal, status = await submit_agent_withdraw_request(config, context, tg_user.id, amount, note=note)
    if status == 'min_amount':
        await update.effective_chat.send_message(f'最低提款 {standard_num(AGENT_WITHDRAW_MIN_AMOUNT)} USDT。')
        return
    if status == 'address_missing':
        await update.effective_chat.send_message('请先绑定 TRC20 地址。可发送 /withdraw 走按钮流程，或先用 /bindtrc20 <地址>。')
        return
    if status != 'pending':
        await update.effective_chat.send_message('申请失败：余额不足或金额非法。')
        return
    wallet_address = get_user_withdraw_address(config, tg_user.id)
    await update.effective_chat.send_message(
        f'提现申请已提交\n单号：<code>{html.escape(str(withdrawal.get("withdrawal_id") or ""), quote=False)}</code>\n金额：<code>{standard_num(withdrawal.get("amount", 0))} USDT</code>\n地址：<code>{html.escape(wallet_address, quote=False)}</code>\n状态：<code>pending</code>',
        parse_mode='HTML'
    )


async def bind_trc20(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None:
        return
    if context.args:
        address = str(context.args[0]).strip()
        if not is_valid_trc20_address(address):
            await update.effective_chat.send_message('TRC20 地址格式不对，请检查后重试。')
            return
        get_agent_bot_user_collection(config.agent_bot_id).update_one({'user_id': tg_user.id}, {'$set': {'withdraw_address': address, 'last_contact_time': beijing_now_str()}})
        await update.effective_chat.send_message(f'已绑定提款地址：<code>{html.escape(address, quote=False)}</code>', parse_mode='HTML')
        return
    set_agent_sign(config.agent_bot_id, tg_user.id, USER_SIGN_BIND_WITHDRAW)
    await reply_rendered(update, '[emoji:5443127283898405358:📥]请发送你的 TRC20 地址', reply_markup=build_admin_config_keyboard('agent_home'))


async def withdraw_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None:
        return
    await reply_rendered(update, build_withdraw_bind_text(config, tg_user.id), reply_markup=build_withdraw_bind_keyboard(config, tg_user.id))


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None:
        return
    if not is_agent_admin_or_source_admin(config, tg_user.id):
        await update.effective_chat.send_message('只有代理管理员或主号铺管理员可以打开后台。')
        return
    await reply_rendered(update, build_admin_panel_text(config), reply_markup=build_admin_panel_keyboard())


async def agent_withdraw_review(update: Update, context: ContextTypes.DEFAULT_TYPE, target_status: str) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None:
        return
    if not is_agent_admin_or_source_admin(config, tg_user.id):
        await update.effective_chat.send_message('只有代理管理员或主号铺管理员可以审核提现。')
        return
    if not context.args:
        await update.effective_chat.send_message(f'用法：/{"agent_withdraw_paid" if target_status == "paid" else "agent_withdraw_reject"} <withdrawal_id> [note]')
        return
    withdrawal_id = str(context.args[0]).strip()
    note = ' '.join(context.args[1:]).strip()
    withdrawal, status = update_agent_withdrawal_status(config.agent_bot_id, withdrawal_id, target_status, operator_user_id=tg_user.id, note=note)
    if status not in {'paid', 'rejected', 'already_done'}:
        await update.effective_chat.send_message(f'处理失败：{status}')
        return
    await update.effective_chat.send_message(
        f'提现单：<code>{html.escape(withdrawal_id, quote=False)}</code>\n结果：<code>{html.escape(status, quote=False)}</code>\n当前状态：<code>{html.escape(str((withdrawal or {}).get("state") or status), quote=False)}</code>',
        parse_mode='HTML'
    )
    if withdrawal and withdrawal.get('user_id'):
        notice = '提现已打款，请注意查收。' if target_status == 'paid' else '提现申请已被驳回，金额已退回余额。'
        try:
            await send_rendered(context.bot, int(withdrawal.get('user_id')), f'[emoji:5312028599803460968:🆗]{notice}\n\n单号：{withdrawal_id}\n金额：{standard_num((withdrawal or {}).get("amount", 0))} USDT')
        except Exception:
            logger.warning('notify withdrawal review failed: tenant=%s withdrawal=%s', config.agent_bot_id, withdrawal_id, exc_info=True)


async def agent_withdraw_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await agent_withdraw_review(update, context, 'paid')


async def agent_withdraw_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await agent_withdraw_review(update, context, 'rejected')


async def agent_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None:
        return
    if not is_agent_admin_or_source_admin(config, tg_user.id):
        await update.effective_chat.send_message('只有代理管理员可以设置价格覆盖。')
        return
    if len(context.args) < 2:
        await update.effective_chat.send_message('用法：/agent_price <nowuid> <price> [display_name]')
        return
    nowuid = str(context.args[0]).strip()
    product = ejfl.find_one({'nowuid': nowuid})
    if product is None:
        await update.effective_chat.send_message('没找到这个商品 nowuid。')
        return
    try:
        price = float(context.args[1])
    except Exception:
        await update.effective_chat.send_message('价格必须是数字。')
        return
    display_name = ' '.join(context.args[2:]).strip()
    update_doc = {
        'agent_bot_id': config.agent_bot_id,
        'nowuid': nowuid,
        'price': price,
        'updated_at': beijing_now_str(),
        'enabled': True,
    }
    if display_name:
        update_doc['display_name'] = display_name
    agent_product_prices.update_one(
        {'agent_bot_id': config.agent_bot_id, 'nowuid': nowuid},
        {'$set': update_doc, '$setOnInsert': {'created_at': beijing_now_str()}},
        upsert=True,
    )
    await update.effective_chat.send_message(
        f'已设置代理价格覆盖\nnowuid: <code>{html.escape(nowuid, quote=False)}</code>\n价格: <code>{standard_num(price)} USDT</code>',
        parse_mode='HTML'
    )


async def agent_price_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None:
        return
    if not is_agent_admin_or_source_admin(config, tg_user.id):
        await update.effective_chat.send_message('只有代理管理员可以清除价格覆盖。')
        return
    if not context.args:
        await update.effective_chat.send_message('用法：/agent_price_clear <nowuid>')
        return
    nowuid = str(context.args[0]).strip()
    agent_product_prices.delete_one({'agent_bot_id': config.agent_bot_id, 'nowuid': nowuid})
    await update.effective_chat.send_message(f'已清除代理价格覆盖：<code>{html.escape(nowuid, quote=False)}</code>', parse_mode='HTML')


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    data = str(query.data or '')
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    if data.startswith('admin_'):
        if not is_agent_admin_or_source_admin(config, query.from_user.id):
            await query.answer('只有代理管理员或主号铺管理员可以操作后台。', show_alert=True)
            return
        await query.answer()
        if data == 'admin_home':
            await edit_rendered(query, build_admin_panel_text(config), reply_markup=build_admin_panel_keyboard())
            return
        if data.startswith('admin_users:'):
            page = int(data.split(':', 1)[1]) if data.split(':', 1)[1].isdigit() else 0
            text, total = build_admin_users_text(config, page)
            await edit_rendered(query, text, reply_markup=build_admin_users_keyboard(page, total))
            return
        if data == 'admin_price_delta':
            set_agent_sign(config.agent_bot_id, query.from_user.id, ADMIN_SIGN_PRICE_DELTA)
            await edit_rendered(query, build_admin_price_delta_text(config), reply_markup=build_admin_config_keyboard('admin_home'))
            await send_rendered(context.bot, query.from_user.id, '[emoji:5397916757333654639:➕]请发送新的全局差价，例如：+0.2 或 0.2')
            return
        if data == 'admin_customer_service':
            set_agent_sign(config.agent_bot_id, query.from_user.id, ADMIN_SIGN_CUSTOMER_SERVICE)
            await edit_rendered(query, f'[emoji:5954078884310814346:☎️]当前客服：{get_agent_customer_service(config) or "未配置"}\n\n请发送新的客服用户名，例如：@support', reply_markup=build_admin_config_keyboard('admin_home'))
            return
        if data == 'admin_restock_target':
            set_agent_sign(config.agent_bot_id, query.from_user.id, ADMIN_SIGN_RESTOCK_TARGET)
            await edit_rendered(query, f'[emoji:5220214598585568818:🚨]当前补货通知：{get_agent_restock_target(config) or "未配置"}\n\n请发送新的补货通知目标，例如：@channel 或 -100xxxx', reply_markup=build_admin_config_keyboard('admin_home'))
            return
        if data == 'admin_purchase_notice':
            set_agent_sign(config.agent_bot_id, query.from_user.id, ADMIN_SIGN_PURCHASE_NOTICE)
            await edit_rendered(query, build_admin_purchase_notice_text(config), reply_markup=build_admin_config_keyboard('admin_home'))
            return
        if data.startswith('admin_withdraws:'):
            page = int(data.split(':', 1)[1]) if data.split(':', 1)[1].isdigit() else 0
            text, rows, total = build_admin_withdraw_list_text(config, page)
            await edit_rendered(query, text, reply_markup=build_admin_withdraw_list_keyboard(rows, page, total))
            return
        if data.startswith('admin_withdraw_detail:'):
            withdrawal_id = data.split(':', 1)[1]
            row = agent_bots.database['agent_withdrawals'].find_one({'agent_bot_id': config.agent_bot_id, 'withdrawal_id': withdrawal_id})
            if row is None:
                await query.answer('提款申请不存在。', show_alert=True)
                return
            await edit_rendered(query, build_admin_withdraw_detail_text(row), reply_markup=build_admin_withdraw_detail_keyboard(row))
            return
        if data.startswith('admin_withdraw_paid:') or data.startswith('admin_withdraw_reject:'):
            target_status = 'paid' if data.startswith('admin_withdraw_paid:') else 'rejected'
            withdrawal_id = data.split(':', 1)[1]
            withdrawal, status = update_agent_withdrawal_status(config.agent_bot_id, withdrawal_id, target_status, operator_user_id=query.from_user.id, note='button_review')
            if status not in {'paid', 'rejected', 'already_done'}:
                await query.answer(f'处理失败：{status}', show_alert=True)
                return
            if withdrawal and withdrawal.get('user_id'):
                notice = '提现已打款，请注意查收。' if target_status == 'paid' else '提现申请已被驳回，金额已退回余额。'
                await send_rendered(context.bot, int(withdrawal.get('user_id')), f'[emoji:5312028599803460968:🆗]{notice}\n\n单号：{withdrawal_id}\n金额：{standard_num((withdrawal or {}).get("amount", 0))} USDT')
            await edit_rendered(query, build_admin_withdraw_detail_text(withdrawal or {'withdrawal_id': withdrawal_id, 'state': status}), reply_markup=build_admin_withdraw_detail_keyboard(withdrawal or {'withdrawal_id': withdrawal_id, 'state': status}))
            return
    if data.startswith('user_'):
        await query.answer()
        if data == 'user_withdraw_bind':
            set_agent_sign(config.agent_bot_id, query.from_user.id, USER_SIGN_BIND_WITHDRAW)
            await edit_rendered(query, '[emoji:5443127283898405358:📥]请发送你的 TRC20 地址', reply_markup=build_admin_config_keyboard('agent_home'))
            return
        if data == 'user_withdraw_apply':
            if not is_valid_trc20_address(get_user_withdraw_address(config, query.from_user.id)):
                await query.answer('请先绑定 TRC20 地址。', show_alert=True)
                return
            set_agent_sign(config.agent_bot_id, query.from_user.id, USER_SIGN_APPLY_WITHDRAW)
            await edit_rendered(query, f'[emoji:5445353829304387411:💳]请输入提款金额\n\n最低提款：{standard_num(AGENT_WITHDRAW_MIN_AMOUNT)} USDT\n当前地址：{get_user_withdraw_address(config, query.from_user.id)}', reply_markup=build_admin_config_keyboard('agent_home'))
            return
    if data == 'agent_noop':
        await query.answer('这部分下一步继续接', show_alert=False)
        return
    await query.answer()
    if data == 'agent_catalog':
        await edit_rendered(query, build_goods_catalog_text(config), reply_markup=build_category_keyboard(config))
        return
    if data == 'agent_home':
        user_row = get_agent_bot_user(config.agent_bot_id, query.from_user.id) or {'user_id': query.from_user.id, 'USDT': 0, 'username': query.from_user.username}
        await edit_rendered(query, build_home_text(config, user_row))
        return
    if data == 'agent_recharge_menu':
        await edit_rendered(query, build_recharge_menu_text(config), reply_markup=build_recharge_menu_keyboard(config))
        return
    if data == 'agent_topup_pending':
        order = get_latest_pending_topup_order(config.agent_bot_id, query.from_user.id, payment_type='trc20')
        if order is None:
            await edit_rendered(query, '[emoji:5301246586918024418:⚠️]当前没有待支付的代理充值订单。', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('[emoji:5197474438970363734:💳]去创建充值订单', callback_data='agent_recharge_menu')]]))
            return
        await edit_rendered(query, build_topup_order_text(order, config), reply_markup=build_topup_order_keyboard(order))
        return
    if data.startswith('agent_topup_view:'):
        order_id = data.split(':', 1)[1]
        order = get_latest_pending_topup_order(config.agent_bot_id, query.from_user.id, payment_type='trc20')
        if order is None or str(order.get('order_id')) != order_id:
            order = topup_orders.find_one({'order_id': order_id, 'tenant_id': config.agent_bot_id, 'user_id': query.from_user.id})
        if order is None:
            await edit_rendered(query, '[emoji:5301246586918024418:⚠️]充值订单不存在或已被清理。')
            return
        await edit_rendered(query, build_topup_order_text(order, config), reply_markup=build_topup_order_keyboard(order))
        return
    if data.startswith('agent_topup_amount:'):
        amount = float(data.split(':', 1)[1])
        if not is_valid_trc20_address(config.trc20_address):
            await edit_rendered(query, '[emoji:5220214598585568818:🚨]代理 TRC20 收款地址未配置，先在 agent_service/.env 里补 AGENT_TRC20_ADDRESS。')
            return
        order = create_tenant_topup_order(
            config.agent_bot_id,
            query.from_user.id,
            'trc20',
            requested_amount=amount,
            pay_amount=amount,
            pay_amount_text=str(standard_num(amount)),
            currency='USDT',
            to_address=config.trc20_address,
            expire_minutes=10,
            extra={'source': 'agent_service'},
        )
        await edit_rendered(query, build_topup_order_text(order, config), reply_markup=build_topup_order_keyboard(order))
        return
    if data.startswith('agent_cate:'):
        uid = data.split(':', 1)[1]
        category, products = build_product_list_payload(config, uid)
        if category is None:
            await edit_rendered(query, '[emoji:5301246586918024418:⚠️]分类不存在或已删除。')
            return
        await edit_rendered(query, build_product_list_text(str(category.get('projectname') or '商品分类'), products), reply_markup=build_product_keyboard(uid, products))
        return
    if data.startswith('agent_goods:'):
        nowuid = data.split(':', 1)[1]
        product = ejfl.find_one({'nowuid': nowuid})
        if product is None or not is_product_enabled_for_agent(config.agent_bot_id, nowuid):
            await edit_rendered(query, '[emoji:5301246586918024418:⚠️]商品不存在、已删除或当前代理未开放。')
            return
        payload = resolve_agent_product_payload(config.agent_bot_id, product)
        await edit_rendered(query, build_agent_product_purchase_text(config, payload, query.from_user.id), reply_markup=build_product_detail_keyboard(config, nowuid, str(product.get('uid') or ''), query.from_user.id, stock_count=int(payload.get('stock', 0) or 0)))
        return
    if data.startswith('agent_buy:'):
        nowuid = data.split(':', 1)[1]
        product = ejfl.find_one({'nowuid': nowuid})
        if product is None or not is_product_enabled_for_agent(config.agent_bot_id, nowuid):
            await query.answer(build_purchase_status_text(config, 'product_not_found', query.from_user.id), show_alert=True)
            return
        payload = resolve_agent_product_payload(config.agent_bot_id, product)
        if int(payload.get('stock', 0) or 0) <= 0:
            await query.answer(build_purchase_status_text(config, 'stock', query.from_user.id), show_alert=True)
            return
        set_agent_sign(config.agent_bot_id, query.from_user.id, f'gmqq {nowuid}')
        prompt = strip_basic_html(get_agent_ui_text(config, 'enter_quantity_prompt', user_id=query.from_user.id))
        await query.answer()
        await reply_rendered(update, prompt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_agent_ui_text(config, 'cancel_purchase', user_id=query.from_user.id), callback_data='agent_home')]]))
        return
    if data.startswith('agent_buy_confirm:'):
        data_parts = data.split(':')
        nowuid = data_parts[1] if len(data_parts) > 1 else ''
        quantity = int(data_parts[2]) if len(data_parts) > 2 and str(data_parts[2]).isdigit() else 0
        if quantity <= 0:
            await query.answer(get_agent_ui_text(config, 'quantity_positive_integer', user_id=query.from_user.id), show_alert=True)
            return
        try:
            await query.answer('正在下单，请稍候...')
        except Exception:
            pass
        try:
            ensure_agent_user_exists(
                config.agent_bot_id,
                query.from_user.id,
                query.from_user.username,
                (query.from_user.full_name or '').replace('<', '').replace('>', ''),
                query.from_user.last_name,
                getattr(query.from_user, 'language_code', None) or config.default_lang,
                state='1',
            )
            order, status = create_tenant_purchase_order(config.agent_bot_id, query.from_user.id, nowuid, quantity=quantity)
            if order is None:
                await reply_rendered(update, build_purchase_status_text(config, status, query.from_user.id), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_agent_ui_text(config, 'main_menu', user_id=query.from_user.id), callback_data='agent_home')]]))
                return
            set_agent_sign(config.agent_bot_id, query.from_user.id, 0)
            context.application.create_task(deliver_agent_order(context, config, order.get('order_id')))
            await edit_rendered(query, build_purchase_result_text(config, order, query.from_user.id), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_agent_ui_text(config, 'main_menu', user_id=query.from_user.id), callback_data='agent_home')]]))
        except Exception as exc:
            logger.exception('agent buy confirm failed: tenant=%s user=%s data=%s', config.agent_bot_id, query.from_user.id, data)
            await reply_rendered(update, f'[emoji:5301246586918024418:⚠️]下单失败：{strip_basic_html(str(exc))}', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_agent_ui_text(config, 'main_menu', user_id=query.from_user.id), callback_data='agent_home')]]))
        return


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    text = str(update.effective_message.text or '').strip() if update.effective_message else ''
    if not text:
        return
    tg_user = update.effective_user
    if tg_user is None:
        return
    user_row = get_agent_bot_user(config.agent_bot_id, tg_user.id) or {}
    sign = str(user_row.get('sign') or '').strip()
    if sign == ADMIN_SIGN_PRICE_DELTA:
        raw = text.strip().replace('＋', '+')
        raw = raw[1:] if raw.startswith('+') else raw
        try:
            delta = float(raw)
        except Exception:
            await update.effective_chat.send_message('请输入正确差价，例如：+0.2 或 0.2')
            return
        if delta < 0:
            await update.effective_chat.send_message('差价不能小于 0。')
            return
        set_agent_sign(config.agent_bot_id, tg_user.id, 0)
        update_agent_runtime_settings(config, price_delta=float(standard_num(delta)))
        await reply_rendered(update, f'[emoji:5312028599803460968:🆗]已更新全局差价：{standard_num(delta)} USDT', reply_markup=build_admin_panel_keyboard())
        return
    if sign == ADMIN_SIGN_CUSTOMER_SERVICE:
        target = text.strip()
        set_agent_sign(config.agent_bot_id, tg_user.id, 0)
        update_agent_runtime_settings(config, customer_service=target)
        await reply_rendered(update, f'[emoji:5312028599803460968:🆗]已更新客服用户名：{target}', reply_markup=build_admin_panel_keyboard())
        return
    if sign == ADMIN_SIGN_RESTOCK_TARGET:
        target = text.strip()
        set_agent_sign(config.agent_bot_id, tg_user.id, 0)
        update_agent_runtime_settings(config, restock_target=target)
        await reply_rendered(update, f'[emoji:5312028599803460968:🆗]已更新补货通知目标：{target}', reply_markup=build_admin_panel_keyboard())
        return
    if sign == ADMIN_SIGN_PURCHASE_NOTICE:
        raw_notice_text = get_message_storage_text(update.effective_message) or text
        notice_text = str(raw_notice_text or '').strip()
        if notice_text in {'关闭', '清空', 'none', 'NONE'}:
            notice_text = ''
        set_agent_sign(config.agent_bot_id, tg_user.id, 0)
        update_agent_runtime_settings(config, purchase_notice=notice_text)
        preview = build_agent_notice_preview(notice_text)
        await reply_rendered(update, f'[emoji:5312028599803460968:🆗]已更新购买后通知：{preview}', reply_markup=build_admin_panel_keyboard())
        return
    if sign == USER_SIGN_BIND_WITHDRAW:
        address = text.strip()
        if not is_valid_trc20_address(address):
            await update.effective_chat.send_message('TRC20 地址格式不对，请重新发送。')
            return
        set_agent_sign(config.agent_bot_id, tg_user.id, 0)
        get_agent_bot_user_collection(config.agent_bot_id).update_one({'user_id': tg_user.id}, {'$set': {'withdraw_address': address, 'last_contact_time': beijing_now_str()}})
        await reply_rendered(update, f'[emoji:5312028599803460968:🆗]已绑定提款地址\n\n{address}', reply_markup=build_withdraw_bind_keyboard(config, tg_user.id))
        return
    if sign == USER_SIGN_APPLY_WITHDRAW:
        try:
            amount = float(text.strip())
        except Exception:
            await update.effective_chat.send_message(f'请输入提款金额，最低 {standard_num(AGENT_WITHDRAW_MIN_AMOUNT)} USDT。')
            return
        withdrawal, status = await submit_agent_withdraw_request(config, context, tg_user.id, amount)
        if status == 'min_amount':
            await update.effective_chat.send_message(f'最低提款 {standard_num(AGENT_WITHDRAW_MIN_AMOUNT)} USDT。')
            return
        if status == 'address_missing':
            await update.effective_chat.send_message('请先绑定 TRC20 地址。')
            return
        if status != 'pending':
            await update.effective_chat.send_message('申请失败：余额不足或金额非法。')
            return
        set_agent_sign(config.agent_bot_id, tg_user.id, 0)
        await reply_rendered(update, f'[emoji:5312028599803460968:🆗]提款申请已提交\n\n单号：{withdrawal.get("withdrawal_id") or ""}\n金额：{standard_num(withdrawal.get("amount", 0))} USDT\n地址：{get_user_withdraw_address(config, tg_user.id)}', reply_markup=build_withdraw_bind_keyboard(config, tg_user.id))
        return
    if sign.startswith('gmqq '):
        nowuid = sign.replace('gmqq ', '', 1).strip()
        if text.isdigit():
            quantity = int(text)
            if quantity <= 0:
                await update.effective_chat.send_message(strip_basic_html(get_agent_ui_text(config, 'quantity_positive_integer_retry', user_id=tg_user.id)))
                return
            product = ejfl.find_one({'nowuid': nowuid})
            if product is None or not is_product_enabled_for_agent(config.agent_bot_id, nowuid):
                set_agent_sign(config.agent_bot_id, tg_user.id, 0)
                await reply_rendered(update, build_purchase_status_text(config, 'product_not_found', tg_user.id))
                return
            payload = resolve_agent_product_payload(config.agent_bot_id, product)
            stock_count = int(payload.get('stock', 0) or 0)
            if stock_count < quantity:
                await update.effective_chat.send_message(strip_basic_html(get_agent_ui_text(config, 'stock_insufficient_retry', user_id=tg_user.id)))
                return
            total_amount = float(payload.get('price', 0) or 0) * quantity
            if float(user_row.get('USDT', 0) or 0) < total_amount:
                set_agent_sign(config.agent_bot_id, tg_user.id, 0)
                await reply_rendered(update, build_purchase_status_text(config, 'balance', tg_user.id))
                return
            set_agent_sign(config.agent_bot_id, tg_user.id, 0)
            confirm_text = build_purchase_confirm_text(config, payload, user_row or {'user_id': tg_user.id, 'USDT': 0}, quantity)
            await reply_rendered(update, confirm_text, reply_markup=build_purchase_confirm_keyboard(config, nowuid, str(product.get('uid') or ''), tg_user.id, quantity))
            return
        await update.effective_chat.send_message(
            strip_basic_html(get_agent_ui_text(config, 'quantity_positive_integer_retry', user_id=tg_user.id)),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_agent_ui_text(config, 'cancel_purchase', user_id=tg_user.id), callback_data='agent_home')]])
        )
        return
    normalized = normalize_menu_text(text)
    if normalized in {normalize_menu_text(MENU_GOODS_ZH), normalize_menu_text(MENU_GOODS_EN), normalize_menu_text('商品列表'), normalize_menu_text('Product Catalog')}:
        await send_catalog(update.effective_chat.id, context)
        return
    if normalized in {normalize_menu_text(MENU_PROFILE_ZH), normalize_menu_text(MENU_PROFILE_EN), normalize_menu_text('个人中心'), normalize_menu_text('Profile')}:
        await show_profile(update, context)
        return
    if normalized in {normalize_menu_text(MENU_SUPPORT_ZH), normalize_menu_text(MENU_SUPPORT_EN), normalize_menu_text('联系客服'), normalize_menu_text('Contact Support')}:
        target = get_agent_customer_service(config) or '暂未配置'
        await reply_rendered(update, f'[emoji:5954078884310814346:☎️]当前代理客服：{target}')
        return
    if normalized in {normalize_menu_text(PREMIUM_ADMIN), normalize_menu_text('代理后台')}:
        if not is_agent_admin_or_source_admin(config, tg_user.id):
            await reply_rendered(update, '[emoji:5301246586918024418:⚠️]只有代理管理员或主号铺管理员可以打开后台。')
            return
        await reply_rendered(update, build_admin_panel_text(config), reply_markup=build_admin_panel_keyboard())
        return
    if normalized in {normalize_menu_text(MENU_RECHARGE_ZH), normalize_menu_text(MENU_RECHARGE_EN), normalize_menu_text('我要充值'), normalize_menu_text('Recharge')}:
        await send_recharge_menu(update.effective_chat.id, context)
        return
    await reply_rendered(update, '[emoji:5222044641200720562:🌸]代理服务已启动。现在可以先看商品目录；充值、下单、结算下一步继续接。')


def main() -> None:
    config = AgentRuntimeConfig.from_env()
    config.validate()
    upsert_agent_bot_runtime(config)
    application = ApplicationBuilder().token(config.bot_token).build()
    application.bot_data['agent_config'] = config
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('admin', admin_panel))
    application.add_handler(CommandHandler('withdraw', withdraw_entry))
    application.add_handler(CommandHandler('bindtrc20', bind_trc20))
    application.add_handler(CommandHandler('agent_price', agent_price))
    application.add_handler(CommandHandler('agent_price_clear', agent_price_clear))
    application.add_handler(CommandHandler('agent_credit', agent_credit))
    application.add_handler(CommandHandler('agent_mark_paid', agent_mark_paid))
    application.add_handler(CommandHandler('agent_withdraw', agent_withdraw))
    application.add_handler(CommandHandler('agent_withdraw_paid', agent_withdraw_paid))
    application.add_handler(CommandHandler('agent_withdraw_reject', agent_withdraw_reject))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern=r'^(agent_|admin_|user_)'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    if application.job_queue is not None:
        application.job_queue.run_repeating(process_agent_topups, interval=3, first=3, name='agent_topup_matcher')
    logger.info('agent_service started: agent_bot_id=%s name=%s at=%s', config.agent_bot_id, config.agent_name, datetime.utcnow().isoformat())
    application.run_polling(timeout=600)


if __name__ == '__main__':
    main()
