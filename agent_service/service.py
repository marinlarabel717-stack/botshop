from __future__ import annotations

import asyncio
import html
import logging
import re
import sys
import tempfile
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
from account_health_check import check_account_inventory_item, get_account_check_runtime_status
from haopubot import (
    ACCOUNT_CHECK_ENABLED,
    ACCOUNT_CHECK_SUPPORTED_TYPES,
    ACCOUNT_CHECK_TIMEOUT_SECONDS,
    InlineKeyboardButton,
    KeyboardButton,
    archive_invalid_inventory_item,
    build_custom_emoji_text_entities,
    build_delivery_zip,
    create_delivery_order_id,
    find_existing_storage_path,
    get_buy_notice_text,
    get_ui_text,
    resolve_inventory_check_target,
    write_invalid_archive_meta,
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


def render_text(text: str):
    return build_custom_emoji_text_entities(str(text or ''))


def strip_basic_html(text: str) -> str:
    text = str(text or '')
    for old, new in (
        ('<b>', ''), ('</b>', ''),
        ('<code>', ''), ('</code>', ''),
    ):
        text = text.replace(old, new)
    return text


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


def build_home_keyboard(config: AgentRuntimeConfig, lang: str = 'zh') -> ReplyKeyboardMarkup:
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
    if config.customer_service:
        keyboard.append([KeyboardButton(PREMIUM_SUPPORT_EN if lang == 'en' else PREMIUM_SUPPORT)])
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
    agent_bots.update_one(
        {'agent_bot_id': config.agent_bot_id},
        {'$set': {
            'agent_bot_id': config.agent_bot_id,
            'agent_name': config.agent_name,
            'agent_username': config.agent_username,
            'customer_service': config.customer_service,
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
    base_price = product_row.get('money', 0)
    price = override.get('price', base_price)
    display_name = str(override.get('display_name') or product_row.get('projectname') or '商品')
    return {
        'nowuid': nowuid,
        'uid': product_row.get('uid'),
        'projectname': display_name,
        'source_projectname': str(product_row.get('projectname') or '商品'),
        'price': price,
        'source_price': base_price,
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


def build_purchase_result_text(order: dict) -> str:
    return (
        f'[emoji:5220195537520711716:⚡️]订单已创建，正在发货\n\n'
        f'订单号：{order.get("order_id") or ""}\n'
        f'商品：{order.get("product_name") or "商品"}\n'
        f'数量：{order.get("quantity", 1)}\n'
        f'扣款：{standard_num(order.get("total_amount", 0))} USDT\n'
        f'余额：{standard_num(order.get("balance_after", 0))} USDT\n'
        f'状态：{order.get("state") or "reserved"}\n\n'
        '接下来会自动执行真实发货；如商品支持检测，会先检测再按结果退款。'
    )


def build_purchase_status_text(config: AgentRuntimeConfig, status: str, user_id: int) -> str:
    mapping = {
        'product_not_found': get_agent_ui_text(config, 'product_not_found', user_id=user_id),
        'stock': get_agent_ui_text(config, 'current_no_stock', user_id=user_id),
        'balance': get_agent_ui_text(config, 'insufficient_balance', user_id=user_id),
        'invalid_price': '商品价格异常，请联系管理员。',
        'product_disabled': '当前代理未开放这个商品。',
    }
    return mapping.get(status, f'下单失败：{status}')


def build_agent_account_check_progress_text(total_count: int, checked_count: int, alive_count: int, invalid_count: int, frozen_count: int, timeout_count: int) -> str:
    return (
        '[emoji:6237934454019461140:🧠]账号检测中\n\n'
        f'总数：{total_count}\n'
        f'已检测：{checked_count}\n'
        f'可用：{alive_count}\n'
        f'失效：{invalid_count}\n'
        f'冻结：{frozen_count}\n'
        f'超时：{timeout_count}'
    )


def build_agent_delivery_result_text(order: dict, total_count: int, alive_count: int, invalid_count: int, frozen_count: int, timeout_count: int, refund_amount: float, balance_after: float) -> str:
    charged_amount = float(standard_num(float(order.get('total_amount', 0) or 0) - float(refund_amount or 0)))
    lines = [
        '[emoji:5193209274452425995:🎉]购买成功',
        '',
        f'订单号：{order.get("order_id") or ""}',
        f'商品：{order.get("product_name") or "商品"}',
        f'总数：{total_count}',
        f'有效：{alive_count}',
        f'失效：{invalid_count}',
        f'冻结：{frozen_count}',
    ]
    if timeout_count:
        lines.append(f'超时：{timeout_count}')
    lines.extend([
        f'实际扣款：{standard_num(charged_amount)} USDT',
        f'退款：{standard_num(refund_amount)} USDT',
        f'余额：{standard_num(balance_after)} USDT',
    ])
    if timeout_count:
        lines.extend(['', '[emoji:5382194935057372936:⏱️]超时账号已随文件一起发出，如需售后请联系客服。'])
    return '\n'.join(lines)


def build_agent_delivery_admin_notice(order: dict, user_row: dict, total_count: int, alive_count: int, invalid_count: int, frozen_count: int, timeout_count: int, refund_amount: float) -> str:
    username = str(user_row.get('username') or '').strip()
    username_text = f'@{username}' if username else '未设置'
    return '\n'.join([
        '[emoji:5312361253610475399:🛒]代理订单通知',
        '',
        f'代理：{order.get("tenant_id") or ""}',
        f'用户：{username_text}',
        f'用户ID：{order.get("user_id") or ""}',
        f'订单号：{order.get("order_id") or ""}',
        f'商品：{order.get("category_name") or ""}/{order.get("product_name") or "商品"}',
        f'总数：{total_count}',
        f'有效：{alive_count}',
        f'失效：{invalid_count}',
        f'冻结：{frozen_count}',
        f'超时：{timeout_count}',
        f'退款：{standard_num(refund_amount)} USDT',
    ])


def write_text_temp_file(prefix: str, suffix: str, content: str) -> str:
    temp_dir = Path(tempfile.gettempdir()) / 'botshop-agent-delivery'
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_path = temp_dir / f'{prefix}_{datetime.now().strftime("%Y%m%d%H%M%S%f")}{suffix}'
    file_path.write_text(str(content or ''), encoding='utf-8')
    return str(file_path)


def build_agent_delivery_file(leixing: str, user_id: int, nowuid: str, selected_docs: list[dict]) -> str | None:
    selected_docs = list(selected_docs or [])
    if not selected_docs:
        return None
    project_names = [str(item.get('projectname') or '') for item in selected_docs if item.get('projectname')]
    if leixing == '协议号':
        return str(build_delivery_zip(leixing, user_id, nowuid, project_names))
    if any(find_existing_storage_path('直登号', nowuid, projectname).exists() for projectname in project_names):
        return str(build_delivery_zip(leixing, user_id, nowuid, project_names))
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


async def send_agent_admin_notice(config: AgentRuntimeConfig, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    for admin_user_id in list(config.admin_ids or []):
        try:
            await send_rendered(context.bot, int(admin_user_id), text)
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
    notice_text = str(get_buy_notice_text(product.get('text', '')) or '').strip()
    total_count = len(selected_docs)
    update_tenant_order(config.agent_bot_id, order_id, {'state': 'delivering', 'delivery_started_at': beijing_now_str(), 'delivery_type': leixing})

    use_account_check = ACCOUNT_CHECK_ENABLED and leixing in ACCOUNT_CHECK_SUPPORTED_TYPES and bool(get_account_check_runtime_status(leixing).get('ready'))
    alive_items: list[dict] = []
    invalid_items: list[dict] = []
    frozen_items: list[dict] = []
    timeout_items: list[dict] = []
    progress_message = None

    if use_account_check:
        progress_message = await send_rendered(context.bot, user_id, build_agent_account_check_progress_text(total_count, 0, 0, 0, 0, 0))
        for index, item in enumerate(selected_docs, start=1):
            projectname = str(item.get('projectname') or '')
            entry_type, target_path = resolve_inventory_check_target(leixing, nowuid, projectname)
            check_result = await asyncio.to_thread(check_account_inventory_item, entry_type, str(target_path), ACCOUNT_CHECK_TIMEOUT_SECONDS)
            hb.update_one(
                {'_id': item.get('_id')},
                {'$set': {
                    'delivery_check_state': check_result.get('status', 'timeout'),
                    'delivery_check_reason': str(check_result.get('reason', ''))[:500],
                    'delivery_check_timer': beijing_now_str(),
                }}
            )
            meta = {'hbid': item.get('hbid'), 'projectname': projectname, 'status': check_result.get('status', 'timeout'), 'reason': check_result.get('reason', '')}
            status = check_result.get('status')
            if status == 'alive':
                alive_items.append(item)
            elif status == 'invalid':
                invalid_items.append(archive_invalid_inventory_item(leixing, nowuid, projectname, order_id, meta))
            elif status == 'frozen':
                frozen_items.append(archive_invalid_inventory_item(leixing, nowuid, projectname, order_id, meta))
            else:
                timeout_items.append(item)
            if progress_message is not None:
                try:
                    rendered_text, entities = render_text(build_agent_account_check_progress_text(total_count, index, len(alive_items), len(invalid_items), len(frozen_items), len(timeout_items)))
                    await context.bot.edit_message_text(chat_id=user_id, message_id=progress_message.message_id, text=rendered_text, entities=entities)
                except Exception:
                    logger.warning('update agent account-check progress failed: tenant=%s order=%s', config.agent_bot_id, order_id, exc_info=True)
    else:
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
        await context.bot.send_message(chat_id=user_id, text=notice_text, parse_mode='HTML', disable_web_page_preview=True)

    final_state = 'delivered'
    if refund_amount > 0 and len(alive_items) + len(timeout_items) == 0:
        final_state = 'refunded'
    elif refund_amount > 0:
        final_state = 'partial_refunded'
    final_text = build_agent_delivery_result_text(order, total_count, len(alive_items), len(invalid_items), len(frozen_items), len(timeout_items), refund_amount, balance_after)
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
    await send_agent_admin_notice(config, context, build_agent_delivery_admin_notice(order, user_row, total_count, len(alive_items), len(invalid_items), len(frozen_items), len(timeout_items), refund_amount))


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
        reply_markup=build_home_keyboard(config, lang=(user_row or {}).get('lang', config.default_lang)),
    )


async def send_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None or update.effective_chat is None:
        return
    user_row = get_agent_bot_user(config.agent_bot_id, tg_user.id) or {'user_id': tg_user.id, 'USDT': 0, 'username': tg_user.username}
    await reply_rendered(update, build_home_text(config, user_row), reply_markup=build_home_keyboard(config, lang=user_row.get('lang', config.default_lang)))


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
    if not is_agent_admin(config, tg_user.id):
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
    if not is_agent_admin(config, tg_user.id):
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
    if len(context.args) < 2:
        await update.effective_chat.send_message('用法：/agent_withdraw <amount> <wallet_address> [note]')
        return
    try:
        amount = float(context.args[0])
    except Exception:
        await update.effective_chat.send_message('提现金额必须是数字。')
        return
    wallet_address = str(context.args[1]).strip()
    note = ' '.join(context.args[2:]).strip()
    withdrawal, status = create_agent_withdrawal_request(config.agent_bot_id, tg_user.id, amount, address=wallet_address, note=note)
    if status != 'pending':
        await update.effective_chat.send_message('申请失败：余额不足或金额非法。')
        return
    await update.effective_chat.send_message(
        f'提现申请已提交\n单号：<code>{html.escape(str(withdrawal.get("withdrawal_id") or ""), quote=False)}</code>\n金额：<code>{standard_num(withdrawal.get("amount", 0))} USDT</code>\n地址：<code>{html.escape(wallet_address, quote=False)}</code>\n状态：<code>pending</code>',
        parse_mode='HTML'
    )
    await send_agent_admin_notice(
        config,
        context,
        f'[emoji:5220214598585568818:🚨]新的代理提现申请\n\n用户ID：{tg_user.id}\n单号：{withdrawal.get("withdrawal_id") or ""}\n金额：{standard_num(withdrawal.get("amount", 0))} USDT\n地址：{wallet_address}'
    )


async def agent_withdraw_review(update: Update, context: ContextTypes.DEFAULT_TYPE, target_status: str) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None:
        return
    if not is_agent_admin(config, tg_user.id):
        await update.effective_chat.send_message('只有代理管理员可以审核提现。')
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
    if not is_agent_admin(config, tg_user.id):
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
    if not is_agent_admin(config, tg_user.id):
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
    if data == 'agent_noop':
        await query.answer('这部分下一步继续接', show_alert=False)
        return
    await query.answer()
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
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
        prompt = get_agent_ui_text(config, 'enter_quantity_prompt', user_id=query.from_user.id)
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
        order, status = create_tenant_purchase_order(config.agent_bot_id, query.from_user.id, nowuid, quantity=quantity)
        if order is None:
            await query.answer(build_purchase_status_text(config, status, query.from_user.id), show_alert=True)
            return
        set_agent_sign(config.agent_bot_id, query.from_user.id, 0)
        context.application.create_task(deliver_agent_order(context, config, order.get('order_id')))
        await edit_rendered(query, build_purchase_result_text(order), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(get_agent_ui_text(config, 'main_menu', user_id=query.from_user.id), callback_data='agent_home')]]))
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
    if sign.startswith('gmqq '):
        nowuid = sign.replace('gmqq ', '', 1).strip()
        if text.isdigit():
            quantity = int(text)
            if quantity <= 0:
                await update.effective_chat.send_message(get_agent_ui_text(config, 'quantity_positive_integer_retry', user_id=tg_user.id))
                return
            product = ejfl.find_one({'nowuid': nowuid})
            if product is None or not is_product_enabled_for_agent(config.agent_bot_id, nowuid):
                set_agent_sign(config.agent_bot_id, tg_user.id, 0)
                await reply_rendered(update, build_purchase_status_text(config, 'product_not_found', tg_user.id))
                return
            payload = resolve_agent_product_payload(config.agent_bot_id, product)
            stock_count = int(payload.get('stock', 0) or 0)
            if stock_count < quantity:
                await update.effective_chat.send_message(get_agent_ui_text(config, 'stock_insufficient_retry', user_id=tg_user.id))
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
            get_agent_ui_text(config, 'quantity_positive_integer_retry', user_id=tg_user.id),
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
        target = config.customer_service or '暂未配置'
        await reply_rendered(update, f'[emoji:5954078884310814346:☎️]当前代理客服：{target}')
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
    application.add_handler(CommandHandler('agent_price', agent_price))
    application.add_handler(CommandHandler('agent_price_clear', agent_price_clear))
    application.add_handler(CommandHandler('agent_credit', agent_credit))
    application.add_handler(CommandHandler('agent_mark_paid', agent_mark_paid))
    application.add_handler(CommandHandler('agent_withdraw', agent_withdraw))
    application.add_handler(CommandHandler('agent_withdraw_paid', agent_withdraw_paid))
    application.add_handler(CommandHandler('agent_withdraw_reject', agent_withdraw_reject))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern=r'^agent_'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    if application.job_queue is not None:
        application.job_queue.run_repeating(process_agent_topups, interval=3, first=3, name='agent_topup_matcher')
    logger.info('agent_service started: agent_bot_id=%s name=%s at=%s', config.agent_bot_id, config.agent_name, datetime.utcnow().isoformat())
    application.run_polling(timeout=600)


if __name__ == '__main__':
    main()
