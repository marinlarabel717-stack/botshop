from __future__ import annotations

import html
import logging
import sys
from datetime import datetime
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PARENT_DIR = CURRENT_DIR.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import AgentRuntimeConfig
from mongo import (
    agent_bots,
    agent_product_prices,
    beijing_now_str,
    create_tenant_topup_order,
    credit_tenant_wallet,
    ejfl,
    expire_tenant_topup_orders,
    ensure_agent_mongo_indexes,
    ensure_agent_user_exists,
    fenlei,
    get_agent_stats,
    get_batch_stock,
    get_real_time_stock,
    get_agent_bot_user,
    get_latest_pending_topup_order,
    mark_tenant_topup_paid,
    standard_num,
    topup_orders,
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


def is_agent_admin(config: AgentRuntimeConfig, user_id: int) -> bool:
    return int(user_id) in set(config.admin_ids or ())


def build_home_keyboard(config: AgentRuntimeConfig, lang: str = 'zh') -> ReplyKeyboardMarkup:
    if lang == 'en':
        keyboard = [[
            KeyboardButton(MENU_GOODS_EN),
            KeyboardButton(MENU_PROFILE_EN),
            KeyboardButton(MENU_RECHARGE_EN),
        ]]
    else:
        keyboard = [[
            KeyboardButton(MENU_GOODS_ZH),
            KeyboardButton(MENU_PROFILE_ZH),
            KeyboardButton(MENU_RECHARGE_ZH),
        ]]
    if config.customer_service:
        keyboard.append([KeyboardButton(MENU_SUPPORT_EN if lang == 'en' else MENU_SUPPORT_ZH)])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def is_valid_trc20_address(address: str) -> bool:
    import re
    return re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}', str(address or '').strip()) is not None


def build_recharge_menu_text(config: AgentRuntimeConfig) -> str:
    address = html.escape(config.trc20_address or '未配置', quote=False)
    return (
        f'<b>{html.escape(config.agent_name, quote=False)} 充值中心</b>\n\n'
        '当前阶段先接入 TRC20 充值订单与账本。\n'
        f'收款地址：<code>{address}</code>\n\n'
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
    keyboard.append([InlineKeyboardButton('📦查看待支付订单', callback_data='agent_topup_pending')])
    keyboard.append([InlineKeyboardButton('🏠返回首页', callback_data='agent_home')])
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
        f'<b>代理充值订单</b>\n\n'
        f'订单号：<code>{html.escape(str(order.get("order_id") or ""), quote=False)}</code>\n'
        f'状态：{state_map.get(str(order.get("state") or "pending"), str(order.get("state") or "pending"))}\n'
        f'充值方式：<code>{html.escape(str(order.get("type") or "trc20").upper(), quote=False)}</code>\n'
        f'应付金额：<code>{html.escape(str(order.get("pay_amount_text") or order.get("pay_amount") or 0), quote=False)} {html.escape(str(order.get("currency") or "USDT"), quote=False)}</code>\n'
        f'收款地址：<code>{html.escape(str(order.get("to_address") or config.trc20_address or "未配置"), quote=False)}</code>\n'
        f'创建时间：<code>{html.escape(str(order.get("created_at") or ""), quote=False)}</code>\n'
        f'过期时间：<code>{html.escape(str(order.get("expire_at") or ""), quote=False)}</code>\n\n'
        '说明：下一步会把真实链上监听和代理充值确认接到这条订单链路上。'
    )


def build_topup_order_keyboard(order: dict) -> InlineKeyboardMarkup:
    order_id = str(order.get('order_id') or '')
    keyboard = []
    if order.get('state') == 'pending':
        keyboard.append([InlineKeyboardButton('♻️刷新订单状态', callback_data=f'agent_topup_view:{order_id}')])
    keyboard.append([InlineKeyboardButton('💸继续充值', callback_data='agent_recharge_menu')])
    keyboard.append([InlineKeyboardButton('🏠返回首页', callback_data='agent_home')])
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
        f'<b>欢迎来到 {html.escape(config.agent_name, quote=False)}</b>\n\n'
        f'代理标识：<code>{html.escape(config.agent_bot_id, quote=False)}</code>\n'
        f'你的账号：<code>{user_row.get("user_id")}</code>\n'
        f'用户名：{username_text}\n'
        f'当前余额：<code>{user_row.get("USDT", 0)} USDT</code>\n\n'
        f'当前代理用户数：<code>{stats.get("user_count", 0)}</code>\n'
        f'订单记录数：<code>{stats.get("purchase_records", 0)}</code>\n\n'
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
        f'<b>{html.escape(config.agent_name, quote=False)} 商品目录</b>\n\n'
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
        return f'<b>分类：{html.escape(category_name, quote=False)}</b>\n\n当前代理在这个分类下暂时没有可售商品。'
    return (
        f'<b>分类：{html.escape(category_name, quote=False)}</b>\n\n'
        f'共 {len(products)} 个可售商品，下面价格优先按代理覆盖显示。'
    )


def build_product_keyboard(uid: str, products: list[dict]) -> InlineKeyboardMarkup:
    keyboard = []
    for item in products:
        price_text = standard_num(item.get('price', 0))
        button_text = f"{item.get('projectname')}    ${price_text}    [ {item.get('stock', 0)} ]"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"agent_goods:{item['nowuid']}")])
    keyboard.append([InlineKeyboardButton('⬅️返回分类', callback_data='agent_catalog')])
    return InlineKeyboardMarkup(keyboard)


def build_product_detail_text(payload: dict) -> str:
    override = payload.get('override') or {}
    override_note = '代理自定义价' if 'price' in override else '跟随主号铺价格'
    source_name = payload.get('source_projectname') or payload.get('projectname')
    text = (
        f'<b>{html.escape(str(payload.get("projectname") or "商品"), quote=False)}</b>\n\n'
        f'商品ID：<code>{html.escape(str(payload.get("nowuid") or ""), quote=False)}</code>\n'
        f'当前价格：<code>{standard_num(payload.get("price", 0))} USDT</code>\n'
        f'当前库存：<code>{payload.get("stock", 0)}</code>\n'
        f'价格来源：{override_note}\n'
        f'主号铺名称：{html.escape(str(source_name), quote=False)}\n'
        f'主号铺基准价：<code>{standard_num(payload.get("source_price", 0))} USDT</code>\n\n'
        '下单链路下一步继续接；这一步先把代理商品展示和价格覆盖跑通。'
    )
    if override.get('display_name'):
        text += f'\n代理显示名：{html.escape(str(override.get("display_name")), quote=False)}'
    return text


def build_product_detail_keyboard(uid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🛒暂未开放购买', callback_data='agent_noop')],
        [InlineKeyboardButton('⬅️返回商品列表', callback_data=f'agent_cate:{uid}')],
        [InlineKeyboardButton('🏠返回分类目录', callback_data='agent_catalog')],
    ])


async def send_catalog(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    await context.bot.send_message(
        chat_id=chat_id,
        text=build_goods_catalog_text(config),
        parse_mode='HTML',
        reply_markup=build_category_keyboard(config),
    )


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
    await update.effective_chat.send_message(
        build_home_text(config, user_row or {'user_id': tg_user.id, 'USDT': 0, 'username': tg_user.username}),
        parse_mode='HTML',
        reply_markup=build_home_keyboard(config, lang=(user_row or {}).get('lang', config.default_lang)),
    )


async def send_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None or update.effective_chat is None:
        return
    user_row = get_agent_bot_user(config.agent_bot_id, tg_user.id) or {'user_id': tg_user.id, 'USDT': 0, 'username': tg_user.username}
    await update.effective_chat.send_message(
        build_home_text(config, user_row),
        parse_mode='HTML',
        reply_markup=build_home_keyboard(config, lang=user_row.get('lang', config.default_lang)),
    )


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    tg_user = update.effective_user
    if tg_user is None or update.effective_chat is None:
        return
    user_row = get_agent_bot_user(config.agent_bot_id, tg_user.id) or {}
    text = (
        f'<b>代理个人中心</b>\n\n'
        f'代理标识：<code>{html.escape(config.agent_bot_id, quote=False)}</code>\n'
        f'用户ID：<code>{tg_user.id}</code>\n'
        f'余额：<code>{standard_num(user_row.get("USDT", 0))} USDT</code>\n'
        f'总购数量：<code>{user_row.get("zgsl", 0)}</code>\n'
        f'总购金额：<code>{standard_num(user_row.get("zgje", 0))} USDT</code>'
    )
    await update.effective_chat.send_message(text, parse_mode='HTML')


async def send_recharge_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    expire_tenant_topup_orders(config.agent_bot_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=build_recharge_menu_text(config),
        parse_mode='HTML',
        reply_markup=build_recharge_menu_keyboard(config),
    )


async def send_pending_topup(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    expire_tenant_topup_orders(config.agent_bot_id)
    order = get_latest_pending_topup_order(config.agent_bot_id, user_id, payment_type='trc20')
    if order is None:
        await context.bot.send_message(chat_id=chat_id, text='当前没有待支付的代理充值订单。')
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=build_topup_order_text(order, config),
        parse_mode='HTML',
        reply_markup=build_topup_order_keyboard(order),
    )


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
        await query.edit_message_text(
            text=build_goods_catalog_text(config),
            parse_mode='HTML',
            reply_markup=build_category_keyboard(config),
        )
        return
    if data == 'agent_home':
        user_row = get_agent_bot_user(config.agent_bot_id, query.from_user.id) or {'user_id': query.from_user.id, 'USDT': 0, 'username': query.from_user.username}
        await query.edit_message_text(
            text=build_home_text(config, user_row),
            parse_mode='HTML',
        )
        return
    if data == 'agent_recharge_menu':
        await query.edit_message_text(
            text=build_recharge_menu_text(config),
            parse_mode='HTML',
            reply_markup=build_recharge_menu_keyboard(config),
        )
        return
    if data == 'agent_topup_pending':
        order = get_latest_pending_topup_order(config.agent_bot_id, query.from_user.id, payment_type='trc20')
        if order is None:
            await query.edit_message_text(
                text='当前没有待支付的代理充值订单。',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('💸去创建充值订单', callback_data='agent_recharge_menu')]]),
            )
            return
        await query.edit_message_text(
            text=build_topup_order_text(order, config),
            parse_mode='HTML',
            reply_markup=build_topup_order_keyboard(order),
        )
        return
    if data.startswith('agent_topup_view:'):
        order_id = data.split(':', 1)[1]
        order = get_latest_pending_topup_order(config.agent_bot_id, query.from_user.id, payment_type='trc20')
        if order is None or str(order.get('order_id')) != order_id:
            order = topup_orders.find_one({'order_id': order_id, 'tenant_id': config.agent_bot_id, 'user_id': query.from_user.id})
        if order is None:
            await query.edit_message_text('充值订单不存在或已被清理。')
            return
        await query.edit_message_text(
            text=build_topup_order_text(order, config),
            parse_mode='HTML',
            reply_markup=build_topup_order_keyboard(order),
        )
        return
    if data.startswith('agent_topup_amount:'):
        amount = float(data.split(':', 1)[1])
        if not is_valid_trc20_address(config.trc20_address):
            await query.edit_message_text('代理 TRC20 收款地址未配置，先在 agent_service/.env 里补 AGENT_TRC20_ADDRESS。')
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
        await query.edit_message_text(
            text=build_topup_order_text(order, config),
            parse_mode='HTML',
            reply_markup=build_topup_order_keyboard(order),
        )
        return
    if data.startswith('agent_cate:'):
        uid = data.split(':', 1)[1]
        category, products = build_product_list_payload(config, uid)
        if category is None:
            await query.edit_message_text('分类不存在或已删除。')
            return
        await query.edit_message_text(
            text=build_product_list_text(str(category.get('projectname') or '商品分类'), products),
            parse_mode='HTML',
            reply_markup=build_product_keyboard(uid, products),
        )
        return
    if data.startswith('agent_goods:'):
        nowuid = data.split(':', 1)[1]
        product = ejfl.find_one({'nowuid': nowuid})
        if product is None or not is_product_enabled_for_agent(config.agent_bot_id, nowuid):
            await query.edit_message_text('商品不存在、已删除或当前代理未开放。')
            return
        payload = resolve_agent_product_payload(config.agent_bot_id, product)
        await query.edit_message_text(
            text=build_product_detail_text(payload),
            parse_mode='HTML',
            reply_markup=build_product_detail_keyboard(str(product.get('uid') or '')),
        )
        return


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    text = str(update.effective_message.text or '').strip() if update.effective_message else ''
    if not text:
        return
    if text in (MENU_GOODS_ZH, MENU_GOODS_EN):
        await send_catalog(update.effective_chat.id, context)
        return
    if text in (MENU_PROFILE_ZH, MENU_PROFILE_EN):
        await show_profile(update, context)
        return
    if text in (MENU_SUPPORT_ZH, MENU_SUPPORT_EN):
        target = config.customer_service or '暂未配置'
        await update.effective_chat.send_message(f'当前代理客服：{target}')
        return
    if text in (MENU_RECHARGE_ZH, MENU_RECHARGE_EN):
        await send_recharge_menu(update.effective_chat.id, context)
        return
    await update.effective_chat.send_message('代理服务已启动。现在可以先看商品目录；充值、下单、结算下一步继续接。')


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
    application.add_handler(CallbackQueryHandler(handle_callback, pattern=r'^agent_'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    logger.info('agent_service started: agent_bot_id=%s name=%s at=%s', config.agent_bot_id, config.agent_name, datetime.utcnow().isoformat())
    application.run_polling(timeout=600)


if __name__ == '__main__':
    main()
