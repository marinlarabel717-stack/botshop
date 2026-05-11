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

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from config import AgentRuntimeConfig
from mongo import agent_bots, beijing_now_str, ensure_agent_mongo_indexes, ensure_agent_user_exists, get_agent_stats


logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger('agent_service')


def build_home_keyboard(config: AgentRuntimeConfig, lang: str = 'zh') -> ReplyKeyboardMarkup:
    if lang == 'en':
        keyboard = [[
            KeyboardButton('🛒 Product Catalog'),
            KeyboardButton('👤 Profile'),
            KeyboardButton('💸 Recharge'),
        ]]
    else:
        keyboard = [[
            KeyboardButton('🛒商品列表'),
            KeyboardButton('👤个人中心'),
            KeyboardButton('💸我要充值'),
        ]]
    if config.customer_service:
        keyboard.append([KeyboardButton('📞联系客服' if lang != 'en' else '📞 Contact Support')])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


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
        f'这是代理分销服务骨架，下一步会往这里继续接商品、充值、订单与售后。'
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
        build_welcome_text(config, user_row or {'user_id': tg_user.id, 'USDT': 0, 'username': tg_user.username}),
        parse_mode='HTML',
        reply_markup=build_home_keyboard(config, lang=(user_row or {}).get('lang', config.default_lang)),
    )


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: AgentRuntimeConfig = context.application.bot_data['agent_config']
    text = str(update.effective_message.text or '').strip() if update.effective_message else ''
    if not text:
        return
    if '客服' in text.lower() or 'support' in text.lower():
        target = config.customer_service or '暂未配置'
        await update.effective_chat.send_message(f'当前代理客服：{target}')
        return
    await update.effective_chat.send_message('代理服务骨架已启动。下一步将继续接入商品列表、充值与订单链路。')


def main() -> None:
    config = AgentRuntimeConfig.from_env()
    config.validate()
    upsert_agent_bot_runtime(config)
    application = ApplicationBuilder().token(config.bot_token).build()
    application.bot_data['agent_config'] = config
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    logger.info('agent_service started: agent_bot_id=%s name=%s at=%s', config.agent_bot_id, config.agent_name, datetime.utcnow().isoformat())
    application.run_polling(timeout=600)


if __name__ == '__main__':
    main()

