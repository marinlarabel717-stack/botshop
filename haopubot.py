import asyncio
import io
import datetime, qrcode, socket, struct, threading, hashlib, uuid
import inspect
import telegram
import os
import sys
import subprocess
import logging, os, shutil
from dotenv import load_dotenv, dotenv_values
import requests
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Process
from telegram import helpers

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'), override=True)

from mongo import *
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackContext, MessageHandler, CallbackQueryHandler, \
    InlineQueryHandler, filters
from telegram import InlineKeyboardMarkup,ForceReply, InlineKeyboardButton as TGInlineKeyboardButton, Update, ChatMemberRestricted, ChatPermissions, \
    ChatMemberRestricted, ChatMember, ChatMemberAdministrator, KeyboardButton as TGKeyboardButton, ReplyKeyboardMarkup, \
    InlineQueryResultArticle, InputTextMessageContent,InputMediaPhoto
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut
import time, json, pickle, re
from threading import Timer
from decimal import Decimal
from datetime import timedelta
import zipfile
from pathlib import Path
from pymongo.errors import DuplicateKeyError

BASE_DIR = Path(__file__).resolve().parent
VERSION_FILE = BASE_DIR / 'VERSION'
try:
    APP_VERSION = VERSION_FILE.read_text(encoding='utf-8').strip()
except Exception:
    APP_VERSION = '0.1.0'


ADMIN_EMOJI_USERLIST = '[emoji:6321041414067068140:ðŸ‘¤]'
ADMIN_EMOJI_DM = '[emoji:5456535802429330837:ðŸ’¬]'
ADMIN_EMOJI_TRC20 = '[emoji:5443127283898405358:ðŸ“¥]'
ADMIN_EMOJI_OKPAY = '[emoji:5445353829304387411:ðŸ’³]'
ADMIN_EMOJI_GOODS = '[emoji:5312361253610475399:ðŸ›’]'
ADMIN_EMOJI_WELCOME = '[emoji:5458382591121964689:âœï¸]'
ADMIN_EMOJI_MENU = '[emoji:5341715473882955310:âš™ï¸]'
ADMIN_EMOJI_CLONE = '#g [emoji:5287684458881756303:ðŸ¤–]'
ADMIN_EMOJI_CLOSE = '[emoji:5210952531676504517:âŒ]'

MOOD_EMOJI_SOFT = '[emoji:5222044641200720562:ðŸŒ¸]'
MOOD_EMOJI_SPARKLE = '[emoji:5217818964612108191:âœ¨]'
MOOD_EMOJI_STAR = '[emoji:5220064167356025824:â­ï¸]'
MOOD_EMOJI_FAST = '[emoji:5220195537520711716:âš¡ï¸]'
MOOD_EMOJI_FIRE = '[emoji:5220166546491459639:ðŸ”¥]'


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


def sanitize_service_name(value):
    value = re.sub(r'[^a-zA-Z0-9_.-]+', '-', str(value or '').strip()).strip('-').lower()
    return value or 'bot'


def sanitize_db_name(value):
    value = re.sub(r'[^a-zA-Z0-9_]+', '_', str(value or '').strip()).strip('_').lower()
    return value or 'botshop_clone'


def run_system_command(args, cwd=None, timeout=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'å‘½ä»¤æ‰§è¡Œå¤±è´¥')
    return result.stdout.strip()


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
        raise RuntimeError('Bot Token æ ¼å¼ä¸æ­£ç¡®')
    response = requests.get(f'https://api.telegram.org/bot{token}/getMe', timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not payload.get('ok'):
        raise RuntimeError(payload.get('description') or 'Bot Token æ— æ•ˆ')
    result = payload.get('result') or {}
    if not result.get('is_bot'):
        raise RuntimeError('æä¾›çš„ Token ä¸æ˜¯æœºå™¨äºº Token')
    return result


def render_env_lines(env_map):
    preferred_keys = [
        'BOT_TOKEN', 'ADMIN_USER_IDS',
        'MONGO_URI', 'MONGO_USER', 'MONGO_PASSWORD', 'MONGO_AUTH_DB', 'MONGO_DB_NAME', 'MONGO_CHAIN_DB_NAME',
        'OKPAY_API_URL', 'OKPAY_SHOP_ID', 'OKPAY_SHOP_TOKEN', 'OKPAY_NAME', 'OKPAY_BOT_USERNAME', 'OKPAY_CALLBACK_URL',
        'OKPAY_CALLBACK_HOST', 'OKPAY_CALLBACK_PORT',
        'SHOW_TRC20_RECHARGE_ENTRY', 'SHOW_OKPAY_RECHARGE_ENTRY',
        'TRONGRID_API_BASE', 'TRONGRID_API_KEY', 'TRC20_USDT_CONTRACT', 'TRONGRID_POLL_SECONDS',
        'TRONGRID_LOOKBACK_MINUTES', 'TRONGRID_MONITOR_ADDRESSES',
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

    (clone_dir / '.env').write_text(render_env_lines(env_map), encoding='utf-8')
    return db_name


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
User=root
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
'''


def clone_bot_instance(bot_token, admin_user_id):
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        raise RuntimeError('å½“å‰è¿›ç¨‹ä¸æ˜¯ rootï¼Œæ— æ³•è‡ªåŠ¨å®‰è£… systemd æœåŠ¡')

    bot_info = get_bot_profile(bot_token)
    bot_id = str(bot_info.get('id'))
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

    return {
        'bot_id': bot_id,
        'bot_username': bot_username,
        'clone_dir': str(clone_dir),
        'db_name': db_name,
        'service_name': service_name,
        'listener_service_name': listener_service_name,
    }


OKPAY_API_URL = os.getenv('OKPAY_API_URL', 'https://api.okaypay.me/shop/')
OKPAY_SHOP_ID = os.getenv('OKPAY_SHOP_ID', '')
OKPAY_SHOP_TOKEN = os.getenv('OKPAY_SHOP_TOKEN', '')
OKPAY_NAME = os.getenv('OKPAY_NAME', 'å·é“º')
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
TRC20_USDT_CONTRACT = os.getenv('TRC20_USDT_CONTRACT', 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t').strip()
OKPAY_BOT = None
OKPAY_HTTPD = None
APP_EVENT_LOOP = None


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


ensure_topup_indexes()

clone_instances = mydb['clone_instances']


def ensure_clone_indexes():
    try:
        clone_instances.create_index([('bot_id', 1)], name='uniq_clone_bot_id', unique=True)
    except Exception:
        pass
    try:
        clone_instances.create_index([('requester_user_id', 1), ('created_at', -1)], name='clone_requester_created')
    except Exception:
        pass


ensure_clone_indexes()


DYNAMIC_EMOJI_RE = re.compile(r'\[(?:emoji|ce|custom_emoji):([0-9]+)(?::([^:\]]+))?(?::(danger|success|primary))?\]')
DYNAMIC_EMOJI_PREFIX_RE = re.compile(r'^\s*\[(?:emoji|ce|custom_emoji):([0-9]+)(?::([^:\]]+))?(?::(danger|success|primary))?\]\s*(.*)$', re.S)
KNOWN_DYNAMIC_EMOJI_IDS = OrderedDict([
    ('ðŸ””', '5458603043203327669'),
    ('ðŸ“±', '5330237710655306682'),
    ('ðŸ‘‘', '5217822164362739968'),
    ('ðŸ“Š', '5231200819986047254'),
    ('ðŸ‘›', '4972482444025398275'),
    ('âŒ', '5210952531676504517'),
    ('ðŸ˜ƒ', '6323075330189826977'),
    ('ðŸ˜€', '5080312910866024090'),
    ('ðŸ˜„', '6321339712430676611'),
    ('ðŸ’°', '4965219701572503640'),
    ('âœ…', '5350486389806868244'),
    ('ðŸ›’', '5312361253610475399'),
    ('âš ï¸', '5447644880824181073'),
    ('âš ', '5447644880824181073'),
    ('â—ï¸', '5274099962655816924'),
    ('â—', '5274099962655816924'),
    ('â‰ï¸', '5219866512961062330'),
    ('â‰', '5219866512961062330'),
    ('âž•', '6320823470246600333'),
    ('ðŸ’¸', '5424925715009118244'),
    ('ðŸ’³', '5445353829304387411'),
    ('ðŸ”‹', '5370715226209525171'),
    ('ðŸª«', '5370688996844249600'),
    ('ðŸš«', '5240241223632954241'),
    ('ðŸ ', '5416041192905265756'),
    ('ðŸ’¡', '5190691070702279446'),
    ('ðŸ“¥', '5443127283898405358'),
    ('ðŸ”´', '5411225014148014586'),
    ('ðŸŸ¢', '5416081784641168838'),
    ('ðŸ‘¤', '6321041414067068140'),
    ('ðŸ’¬', '5456535802429330837'),
    ('â™¥ï¸', '6273982526851652490'),
    ('â™¥', '6273982526851652490'),
    ('ðŸ›«', '5201691993775818138'),
    ('ðŸŽ‰', '5193209274452425995'),
    ('ðŸ¥³', '5458824569026532353'),
    ('ðŸ’«', '5469744063815102906'),
    ('âœˆï¸', '5300866598276450274'),
    ('âœˆ', '5300866598276450274'),
    ('âž¡ï¸', '5416117059207572332'),
    ('âž¡', '5416117059207572332'),
    ('âœï¸', '5458382591121964689'),
    ('âœ', '5458382591121964689'),
    ('ðŸŒŽ', '5224450179368767019'),
    ('ðŸ¥‡', '5440539497383087970'),
    ('ðŸ¥ˆ', '5447203607294265305'),
    ('ðŸ¥‰', '5453902265922376865'),
    ('ðŸ‡¨ðŸ‡³', '5224435456220868088'),
    ('ðŸŒ¹', '5363938656874673963'),
    ('ðŸ’Ž', '5427168083074628963'),
    ('ðŸ¦', '5332455502917949981'),
    ('âš™ï¸', '5341715473882955310'),
    ('âš™', '5341715473882955310'),
    ('â¬…ï¸', '5253955286137338977'),
    ('â¬…', '5253955286137338977'),
    ('ðŸ“', '6321175945327680619'),
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
    """Parse button prefix: [emoji:custom_emoji_id:ðŸ“±]æŒ‰é’®æ–‡å­— or [emoji:id:ðŸ“±:primary]æŒ‰é’®æ–‡å­—"""
    if not isinstance(text, str):
        return None, None, None, text
    m = DYNAMIC_EMOJI_PREFIX_RE.match(text)
    if not m:
        return None, None, None, text
    emoji_id = m.group(1)
    alt = m.group(2) or 'âœ¨'
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
            alt = m.group(2) or 'âœ¨'
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
                return custom_emoji_id, (alt or 'âœ¨')
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
                alt = source_text[py_start:py_end] or 'âœ¨'
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


def should_preserve_sign_on_menu_match(sign):
    if not sign:
        return False
    sign = str(sign)
    editable_prefixes = (
        'startupdate',
        'upejflname ',
        'upspname ',
        'setkeyname ',
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
        alt = args[0] if args else (reply_alt or 'âœ¨')
        label = ' '.join(args[1:]).strip() if len(args) > 1 else ''
    else:
        alt = args[0] if args else (own_alt or 'âœ¨')
        label = ' '.join(args[1:]).strip() if len(args) > 1 else ''

    if not custom_emoji_id:
        fstext = (
            'ç”¨æ³•ï¼š\n'
            '1. ç›´æŽ¥å‘é€ï¼š/emojiid è‡ªå®šä¹‰emoji å•†å“åˆ—è¡¨\n'
            '2. æˆ–å…ˆå‘ä¸€ä¸ªè‡ªå®šä¹‰ emojiï¼Œå†å›žå¤é‚£æ¡æ¶ˆæ¯å‘é€ï¼š/emojiid ðŸ’¬ å•†å“åˆ—è¡¨\n'
            'æ³¨æ„ï¼šè¿™é‡Œå¿…é¡»æ˜¯ Telegram è‡ªå®šä¹‰ emojiï¼Œä¸æ˜¯æ™®é€šç³»ç»Ÿ emojiã€‚'
        )
        context.bot.send_message(chat_id=chat.id, text=fstext)
        return

    result = f'[emoji:{custom_emoji_id}:{alt}]'
    if label:
        result += label
    context.bot.send_message(chat_id=chat.id, text=result)


def InlineKeyboardButton(text, *args, **kwargs):
    """Backward-compatible inline button with optional dynamic emoji icon.

    Usage: InlineKeyboardButton('[emoji:5368324170671202286:ðŸ“±]å•†å“åˆ—è¡¨', callback_data='...')
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
            raise RuntimeError('Telegram event loop å°šæœªåˆå§‹åŒ–')
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
                last_exc = None
                max_attempts = 2 if target_name in transient_methods else 1
                for attempt in range(max_attempts):
                    try:
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
                            time.sleep(1)
                            continue
                        logging.warning('Telegram transient error on %s: %s', target_name, exc)
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
        logging.warning('Telegram network error: %s', err)
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


def inline_query(update: Update, context: CallbackContext):
    """Handle the inline query. This is run when you type: @botusername <query>"""
    query = update.inline_query.query
    if not query:  # empty query should not be handled
        update.inline_query.answer(results=[], cache_time=0)
        return

    yh_list = update['inline_query']['from_user']
    user_id = yh_list['id']
    fullname = yh_list['full_name']

    if is_number(query):
        money = query
        money = float(money) if str(money).count('.') > 0 else int(money)
        user_list = user.find_one({'user_id': user_id})
        USDT = user_list['USDT']
        if USDT >= money:
            if money <= 0:
                url = helpers.create_deep_linked_url(context.bot.username, str(user_id))
                keyboard = [
                    [InlineKeyboardButton(context.bot.first_name, url=url)]
                ]
                fstext = f'''
âš ï¸æ“ä½œå¤±è´¥ï¼Œè½¬è´¦é‡‘é¢å¿…é¡»å¤§äºŽ0
                '''

                hyy = shangtext.find_one({'projectname': 'æ¬¢è¿Žè¯­'})['text']
                hyyys = shangtext.find_one({'projectname': 'æ¬¢è¿Žè¯­æ ·å¼'})['text']
            
                entities = safe_pickle_loads(hyyys)

                results = [
                    InlineQueryResultArticle(
                        id=str(uuid.uuid4()),
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        title=fstext,
                        input_message_content=InputTextMessageContent(
                            hyy,entities=entities
                        )
                    ),
                ]

                update.inline_query.answer(results=results, cache_time=0)
                return
            uid = generate_24bit_uid()
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            zhuanz.insert_one({
                'uid': uid,
                'user_id': user_id,
                'fullname': fullname,
                'money': money,
                'timer': timer,
                'state': 0
            })
            # keyboard = [[InlineKeyboardButton("ðŸ“¥æ”¶æ¬¾", callback_data=f'shokuan {user_id}:{money}')]]
            keyboard = [[InlineKeyboardButton("ðŸ“¥æ”¶æ¬¾", callback_data=f'shokuan {uid}')]]
            fstext = f'''
è½¬è´¦ {query} U
            '''

            zztext = f'''
<b>è½¬è´¦ç»™ä½  {query} U</b>

è¯·åœ¨24å°æ—¶å†…é¢†å–
            '''
            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    title=fstext,
                    description='âš ï¸æ‚¨æ­£åœ¨å‘å¯¹æ–¹è½¬è´¦Uå¹¶ç«‹å³ç”Ÿæ•ˆ',
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
âš ï¸æ“ä½œå¤±è´¥ï¼Œä½™é¢ä¸è¶³ï¼ŒðŸ’°å½“å‰ä½™é¢ï¼š{USDT}U
            '''

            hyy = shangtext.find_one({'projectname': 'æ¬¢è¿Žè¯­'})['text']
            hyyys = shangtext.find_one({'projectname': 'æ¬¢è¿Žè¯­æ ·å¼'})['text']
        
            entities = safe_pickle_loads(hyyys)

            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    title=fstext,
                    input_message_content=InputTextMessageContent(
                        hyy, entities=entities
                    )
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
                title="å‚æ•°é”™è¯¯",
                input_message_content=InputTextMessageContent(
                    f"<b>é”™è¯¯</b>", parse_mode='HTML'
                )),
        ]

        update.inline_query.answer(results=results, cache_time=0)
        return
    yh_id = hongbao_list['user_id']
    if yh_id != user_id:

        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="ðŸ§§è¿™ä¸æ˜¯ä½ çš„çº¢åŒ…",
                input_message_content=InputTextMessageContent(
                    f"<b>ðŸ§§è¿™ä¸æ˜¯ä½ çš„çº¢åŒ…</b>", parse_mode='HTML'
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
                    title="ðŸ§§çº¢åŒ…å·²é¢†å–å®Œ",
                    input_message_content=InputTextMessageContent(
                        f"<b>ðŸ§§çº¢åŒ…å·²é¢†å–å®Œ</b>", parse_mode='HTML'
                    )),
            ]

            update.inline_query.answer(results=results, cache_time=0)
        else:
            qbrtext = []
            jiangpai = {'0': 'ðŸ¥‡', '1': 'ðŸ¥ˆ', '2': 'ðŸ¥‰'}
            count = 0
            qb_list = list(qb.find({'uid': uid}, sort=[('money', -1)]))
            for i in qb_list:
                qbid = i['user_id']
                qbname = i['fullname'].replace('<', '').replace('>', '')
                qbtimer = i['timer'][-8:]
                qbmoney = i['money']
                if str(count) in jiangpai.keys():

                    qbrtext.append(
                        f'{jiangpai[str(count)]} <code>{qbmoney}</code>({qbtimer}) USDTðŸ’° - <a href="tg://user?id={qbid}">{qbname}</a>')
                else:
                    qbrtext.append(
                        f'<code>{qbmoney}</code>({qbtimer}) USDTðŸ’° - <a href="tg://user?id={qbid}">{qbname}</a>')
                count += 1
            qbrtext = '\n'.join(qbrtext)

            syhb = hbsl - len(qb_list)

            fstext = f'''
ðŸ§§ <a href="tg://user?id={user_id}">{fullname}</a> å‘é€äº†ä¸€ä¸ªçº¢åŒ…
ðŸ’µæ€»é‡‘é¢:{hbmoney} USDTðŸ’° å‰©ä½™:{syhb}/{hbsl}

{qbrtext}
            '''

            url = helpers.create_deep_linked_url(context.bot.username, str(user_id))
            keyboard = [
                [InlineKeyboardButton('é¢†å–çº¢åŒ…', callback_data=f'lqhb {uid}')],
                [InlineKeyboardButton(context.bot.first_name, url=url)]
            ]

            results = [
                InlineQueryResultArticle(
                    id=str(uuid.uuid4()),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    title=f"ðŸ’µæ€»é‡‘é¢:{hbmoney} USDTðŸ’° å‰©ä½™:{syhb}/{hbsl}",
                    input_message_content=InputTextMessageContent(
                        fstext, parse_mode='HTML'
                    )
                ),
            ]

            update.inline_query.answer(results=results, cache_time=0)


def shokuan(update: Update, context: CallbackContext):
    query = update.callback_query
    # data = query.data.replace('shokuan ','')
    uid = query.data.replace('shokuan ', '')

    # fb_id = int(data.split(':')[0])
    # fb_money = data.split(':')[1]
    # fb_money = float(fb_money) if str((fb_money)).count('.') > 0 else int(standard_num(fb_money))
    fb_list = zhuanz.find_one({'uid': uid})
    fb_state = fb_list['state']
    if fb_state == 1:
        fstext = f'''
âŒ é¢†å–å¤±è´¥
        '''
        query.answer(fstext, show_alert=bool("true"))
        return
    fb_id = fb_list['user_id']
    fb_money = fb_list['money']
    yh_list = user.find_one({'user_id': fb_id})
    yh_usdt = yh_list['USDT']
    if yh_usdt < fb_money:
        fstext = f'''
âŒ é¢†å–å¤±è´¥.USDT æ“ä½œå¤±è´¥ï¼Œä½™é¢ä¸è¶³
        '''
        zhuanz.update_one({'uid': uid}, {"$set": {"state": 1}})
        query.answer(fstext, show_alert=bool("true"))
        return

    now_money = standard_num(yh_usdt - fb_money)
    now_money = float(now_money) if str((now_money)).count('.') > 0 else int(standard_num(now_money))
    user.update_one({'user_id': fb_id}, {"$set": {'USDT': now_money}})

    zhuanz.update_one({'uid': uid}, {"$set": {"state": 1}})
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
    fstext = f'''
<a href="tg://user?id={user_id}">{fullname}</a> å·²é¢†å– <b>{fb_money}</b> USDT
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
        query.answer('çº¢åŒ…å·²æŠ¢å®Œ', show_alert=bool("true"))
        return

    qhb_list = qb.find_one({"uid": uid, 'user_id': user_id})
    if qhb_list is not None:
        query.answer('ä½ å·²é¢†å–è¯¥çº¢åŒ…', show_alert=bool("true"))
        return
    qb_list = list(qb.find({'uid': uid}, sort=[('money', -1)]))

    syhb = hbsl - len(qb_list)
    # ä»¥ä¸‹æ˜¯éšæœºåˆ†é…é‡‘é¢çš„ä»£ç 
    remaining_money = hbmoney - sum(q['money'] for q in qb_list)  # è®¡ç®—å‰©ä½™çº¢åŒ…æ€»é¢
    if syhb > 1:
        # å¤šäºŽä¸€ä¸ªçº¢åŒ…å‰©ä½™æ—¶ï¼Œä½¿ç”¨æ­£æ€åˆ†å¸ƒéšæœºç”Ÿæˆé‡‘é¢
        mean_money = remaining_money / syhb  # è®¡ç®—æ¯ä¸ªçº¢åŒ…çš„å¹³å‡é‡‘é¢
        std_dev = mean_money / 3  # æ ‡å‡†å·®è®¾å®šä¸ºå¹³å‡é‡‘é¢çš„1/3
        money = standard_num(max(0.01, round(random.normalvariate(mean_money, std_dev), 2)))  # ä½¿ç”¨æ­£æ€åˆ†å¸ƒç”Ÿæˆé‡‘é¢ï¼Œå¹¶ä¿ç•™ä¸¤ä½å°æ•°
        money = float(money) if str(money).count('.') > 0 else int(money)
    else:
        # å¦‚æžœåªæœ‰ä¸€ä¸ªçº¢åŒ…å‰©ä½™ï¼Œç›´æŽ¥å°†å‰©ä½™é‡‘é¢åˆ†é…ç»™è¯¥çº¢åŒ…
        money = round(remaining_money, 2)  # å°†å‰©ä½™é‡‘é¢ä¿ç•™ä¸¤ä½å°æ•°
        money = float(money) if str(money).count('.') > 0 else int(money)

    # å°†é‡‘é¢ä¿å­˜åˆ°æ•°æ®åº“
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

    query.answer(f'é¢†å–çº¢åŒ…æˆåŠŸï¼Œé‡‘é¢:{money}', show_alert=bool("true"))

    jiangpai = {'0': 'ðŸ¥‡', '1': 'ðŸ¥ˆ', '2': 'ðŸ¥‰'}

    qb_list = list(qb.find({'uid': uid}, sort=[('money', -1)]))

    syhb = hbsl - len(qb_list)
    qbrtext = []
    count = 0
    for i in qb_list:
        qbid = i['user_id']
        qbname = i['fullname'].replace('<', '').replace('>', '')
        qbtimer = i['timer'][-8:]
        qbmoney = i['money']
        if str(count) in jiangpai.keys():

            qbrtext.append(
                f'{jiangpai[str(count)]} <code>{qbmoney}</code>({qbtimer}) USDTðŸ’° - <a href="tg://user?id={qbid}">{qbname}</a>')
        else:
            qbrtext.append(f'<code>{qbmoney}</code>({qbtimer}) USDTðŸ’° - <a href="tg://user?id={qbid}">{qbname}</a>')
        count += 1
    qbrtext = '\n'.join(qbrtext)

    fstext = f'''
ðŸ§§ <a href="tg://user?id={fb_id}">{fb_fullname}</a> å‘é€äº†ä¸€ä¸ªçº¢åŒ…
ðŸ’µæ€»é‡‘é¢:{hbmoney} USDTðŸ’° å‰©ä½™:{syhb}/{hbsl}

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
            [InlineKeyboardButton('é¢†å–çº¢åŒ…', callback_data=f'lqhb {uid}')],
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
    jiangpai = {'0': 'ðŸ¥‡', '1': 'ðŸ¥ˆ', '2': 'ðŸ¥‰'}
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
            if str(count) in jiangpai.keys():

                qbrtext.append(
                    f'{jiangpai[str(count)]} <code>{qbmoney}</code>({qbtimer}) USDTðŸ’° - <a href="tg://user?id={qbid}">{qbname}</a>')
            else:
                qbrtext.append(f'<code>{qbmoney}</code>({qbtimer}) USDTðŸ’° - <a href="tg://user?id={qbid}">{qbname}</a>')
            count += 1
        qbrtext = '\n'.join(qbrtext)

        fstext = f'''
ðŸ§§ <a href="tg://user?id={fb_id}">{fb_fullname}</a> å‘é€äº†ä¸€ä¸ªçº¢åŒ…
ðŸ•¦ æ—¶é—´:{timer}
ðŸ’µ æ€»é‡‘é¢:{hbmoney} USDT
çŠ¶æ€:è¿›è¡Œä¸­
å‰©ä½™:{syhb}/{hbsl}

{qbrtext}
        '''
        keyboard = [[InlineKeyboardButton('å‘é€çº¢åŒ…', switch_inline_query=f'redpacket {uid}')],
                    [InlineKeyboardButton('â­•ï¸å…³é—­', callback_data=f'close {user_id}')]]
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
            if str(count) in jiangpai.keys():

                qbrtext.append(
                    f'{jiangpai[str(count)]} <code>{qbmoney}</code>({qbtimer}) USDTðŸ’° - <a href="tg://user?id={qbid}">{qbname}</a>')
            else:
                qbrtext.append(f'<code>{qbmoney}</code>({qbtimer}) USDTðŸ’° - <a href="tg://user?id={qbid}">{qbname}</a>')
            count += 1
        qbrtext = '\n'.join(qbrtext)

        fstext = f'''
ðŸ§§ <a href="tg://user?id={fb_id}">{fb_fullname}</a> å‘é€äº†ä¸€ä¸ªçº¢åŒ…
ðŸ•¦ æ—¶é—´:{timer}
ðŸ’µ æ€»é‡‘é¢:{hbmoney} USDT
çŠ¶æ€:å·²ç»“æŸ
å‰©ä½™:0/{hbsl}

{qbrtext}
        '''

        keyboard = [[InlineKeyboardButton('â­•ï¸å…³é—­', callback_data=f'close {user_id}')]]
        context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                 reply_markup=InlineKeyboardMarkup(keyboard))


def jxzhb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id

    keyboard = [
        [InlineKeyboardButton('â—¾ï¸è¿›è¡Œä¸­', callback_data='jxzhb'),
         InlineKeyboardButton('å·²ç»“æŸ', callback_data='yjshb')],

    ]

    for i in list(hongbao.find({'user_id': user_id, 'state': 0})):
        timer = i['timer'][-14:-3]
        hbsl = i['hbsl']
        uid = i['uid']
        qb_list = list(qb.find({'uid': uid}, sort=[('money', -1)]))
        syhb = hbsl - len(qb_list)
        hbmoney = i['hbmoney']
        keyboard.append(
            [InlineKeyboardButton(f'ðŸ§§[{timer}] {syhb}/{hbsl} - {hbmoney} USDT', callback_data=f'xzhb {uid}')])

    keyboard.append([InlineKeyboardButton('âž•æ·»åŠ ', callback_data='addhb')])
    keyboard.append([InlineKeyboardButton('å…³é—­', callback_data=f'close {user_id}')])

    query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))


def yjshb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id

    keyboard = [
        [InlineKeyboardButton('ï¸è¿›è¡Œä¸­', callback_data='jxzhb'),
         InlineKeyboardButton('â—¾å·²ç»“æŸ', callback_data='yjshb')],

    ]

    for i in list(hongbao.find({'user_id': user_id, 'state': 1})):
        timer = i['timer'][-14:-3]
        hbsl = i['hbsl']
        uid = i['uid']
        hbmoney = i['hbmoney']
        keyboard.append(
            [InlineKeyboardButton(f'ðŸ§§[{timer}] 0/{hbsl} - {hbmoney} USDT (over)', callback_data=f'xzhb {uid}')])

    keyboard.append([InlineKeyboardButton('âž•æ·»åŠ ', callback_data='addhb')])
    keyboard.append([InlineKeyboardButton('å…³é—­', callback_data=f'close {user_id}')])

    query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))


def addhb(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    fstext = f'''
ðŸ’¡ è¯·å›žå¤ä½ è¦å‘é€çš„æ€»é‡‘é¢()? ä¾‹å¦‚: <code>8.88</code>
    '''
    keyboard = [[InlineKeyboardButton('ðŸš«å–æ¶ˆ', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {'sign': 'addhb'}})
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard),
                             parse_mode='HTML')


def ensure_user_exists(user_id, username, fullname, lastname):
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    user_list = user.find_one({'user_id': user_id})
    if user_list is None:
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
        user_list = user.find_one({'user_id': user_id})
    else:
        updates = {'last_contact_time': timer}
        if user_list.get('username') != username:
            updates['username'] = username
        if user_list.get('fullname') != fullname:
            updates['fullname'] = fullname
        if user_list.get('lastname') != lastname:
            updates['lastname'] = lastname
        if updates:
            user.update_one({'user_id': user_id}, {'$set': updates})
            user_list = user.find_one({'user_id': user_id})
    if user_id in ADMIN_USER_IDS:
        user.update_one({'user_id': user_id}, {'$set': {'state': '4'}})
        user_list = user.find_one({'user_id': user_id})
    return user_list


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
[emoji:5287684458881756303:ðŸ¤–] æœºå™¨äººä½¿ç”¨äººæ•°ï¼š{user_count}
[emoji:4972482444025398275:ðŸ‘›] æœºå™¨äººæ€»ä½™é¢ï¼š{standard_num(total_balance)} USDT
[emoji:5220195537520711716:âš¡ï¸] ä»Šæ—¥æ”¶å…¥ï¼š{today_income} USDT
[emoji:5222097061276566531:ðŸƒ] æ˜¨æ—¥æ”¶å…¥ï¼š{yesterday_income} USDT
        '''


def start(update: Update, context: CallbackContext):
    us = update.effective_user
    chat_id = update.effective_chat.id
    user_id = us.id
    username = us.username
    fullname = us.full_name.replace('<', '').replace('>', '')
    lastname = us.last_name
    botusername = context.bot.username
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    user_list = ensure_user_exists(user_id, username, fullname, lastname)
    state = user_list['state']
    sign = user_list['sign']
    USDT = user_list['USDT']
    zgje = user_list['zgje']
    zgsl = user_list['zgsl']
    creation_time = user_list['creation_time']
    args = update.message.text.split(maxsplit=2)
    content = args[2] if len(args) == 3 else ""
    if len(args) == 2:
        if username is None:
            username = fullname
        else:
            username = f'<a href="https://t.me/{username}">{username}</a>'
        fstext = f'''
<b>[emoji:6321041414067068140:ðŸ‘¤] æ‚¨çš„ID:</b>  <code>{user_id}</code>
<b>[emoji:5287684458881756303:ðŸ¤–] æ‚¨çš„ç”¨æˆ·å:</b>  {username}
<b>[emoji:5217818964612108191:âœ¨] æ³¨å†Œæ—¥æœŸ:</b>  {creation_time}

<b>[emoji:5220064167356025824:â­ï¸] æ€»è´­æ•°é‡:</b>  {zgsl}

<b>[emoji:5028746137645876535:ðŸ“ˆ] æ€»è´­é‡‘é¢:</b>  {standard_num(zgje)} USDT

<b>[emoji:4972482444025398275:ðŸ‘›] æ‚¨çš„ä½™é¢:</b>  {USDT} USDT
        '''

        keyboard = [[InlineKeyboardButton('ðŸ›’è´­ä¹°è®°å½•', callback_data=f'gmaijilu {user_id}')],
                    [InlineKeyboardButton('å…³é—­', callback_data=f'close {user_id}')]]
        context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                 reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        return

    hyy = shangtext.find_one({'projectname': 'æ¬¢è¿Žè¯­'})['text']
    hyyys = shangtext.find_one({'projectname': 'æ¬¢è¿Žè¯­æ ·å¼'})['text']
    keylist = get_key.find({}, sort=[('Row', 1), ('first', 1)])
    yyzt = shangtext.find_one({'projectname': 'è¥ä¸šçŠ¶æ€'})['text']
    if yyzt == 0:
        if state != '4':
            return
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    for i in keylist:
        projectname = i['projectname']
        row = i['Row']
        first = i['first']
        keyboard[i["Row"] - 1].append(KeyboardButton(projectname))
    keyboard = [row for row in keyboard if row]
    if BOT_CLONE_ENABLED and ALLOW_PUBLIC_BOT_CLONE:
        keyboard.append([KeyboardButton('#g [emoji:5287684458881756303:ðŸ¤–]ä¸€é”®å…‹éš†åŒæ¬¾')])
    entities = safe_pickle_loads(hyyys)
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False) if keyboard else None
    context.bot.send_message(chat_id=user_id, text=hyy, reply_markup=reply_markup,
                             entities=entities)
    if state == '4':
        keyboard = [
            [InlineKeyboardButton(f'{ADMIN_EMOJI_USERLIST}ç”¨æˆ·åˆ—è¡¨', callback_data='yhlist'), InlineKeyboardButton(f'{ADMIN_EMOJI_DM}å¯¹è¯ç”¨æˆ·ç§å‘', callback_data='sifa')],
            [InlineKeyboardButton(f'{ADMIN_EMOJI_TRC20}å……å€¼åœ°å€è®¾ç½®', callback_data='settrc20'),
             InlineKeyboardButton(f'{ADMIN_EMOJI_OKPAY}OKPayé…ç½®', callback_data='okpaycfg')],
            [InlineKeyboardButton(f'{ADMIN_EMOJI_GOODS}å•†å“ç®¡ç†', callback_data='spgli'),
             InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}æ¬¢è¿Žè¯­ä¿®æ”¹', callback_data='startupdate')],
            [InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}èœå•æŒ‰é’®', callback_data='addzdykey')],
        ]
        if BOT_CLONE_ENABLED:
            keyboard[-1].append(InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}ä¸€é”®å…‹éš†åŒæ¬¾', callback_data='clonebot'))
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}å…‹éš†åˆ—è¡¨', callback_data='clonelist 0')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
        jqrsyrs = len(list(user.find({})))
        numu = 0
        for i in list(user.find({"USDT": {"$gt": 0}})):
            USDT = i['USDT']

            numu += USDT

        fstext = build_admin_dashboard_text(jqrsyrs, numu)
        context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))
        # message_id = context.bot.send_photo(chat_id=user_id,  photo=open('è¾›è¿ªå……å€¼å›¾ç‰‡.png', 'rb'))
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
            if 'å›žå¤å›¾æ–‡æˆ–å›¾ç‰‡è§†é¢‘æ–‡å­—' == text:
                stored_message_text = get_message_storage_text(update.message)
                if not update.message.photo and update.message.animation is None:
                    r_text = stored_message_text or messagetext or ''
                    sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'text': r_text}})
                    sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'file_id': ''}})
                    sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'send_type': 'text'}})
                    sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'state': 1}})
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
                        sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'text': r_text}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'file_id': file}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'send_type': 'photo'}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'state': 1}})
                        message_id = context.bot.send_photo(chat_id=user_id, caption=r_text, photo=file)
                        time.sleep(3)
                        del_message(message_id)
                    elif update.message.animation is not None:
                        file = update.message.animation.file_id
                        sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'text': r_text}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'file_id': file}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'send_type': 'animation'}})
                        sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'state': 1}})
                        message_id = context.bot.sendAnimation(chat_id=user_id, caption=r_text, animation=file)
                        time.sleep(3)
                        del_message(message_id)
                    else:
                        context.bot.send_message(chat_id=user_id, text='âš ï¸ å½“å‰åªæ”¯æŒæ–‡å­—ã€å›¾ç‰‡æˆ–åŠ¨ç”»')
            elif 'å›žå¤æŒ‰é’®è®¾ç½®' == text:
                text = messagetext
                message_id = context.user_data[f'wanfapeizhi{user_id}']
                del_message(message_id)
                keyboard = parse_urls(text)
                dumped = pickle.dumps(keyboard)
                sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'keyboard': dumped}})
                sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {'key_text': text}})
                try:
                    message_id = context.bot.send_message(chat_id=user_id, text='æŒ‰é’®è®¾ç½®æˆåŠŸ',
                                                          reply_markup=InlineKeyboardMarkup(keyboard))
                    time.sleep(10)
                    del_message(message_id)

                except:
                    context.bot.send_message(chat_id=user_id, text=text)
                    message_id = context.bot.send_message(chat_id=user_id, text='æŒ‰é’®è®¾ç½®å¤±è´¥,è¯·é‡æ–°è¾“å…¥')
                    asyncio.sleep(10)
                    del_message(message_id)

def sifa(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    bot_id = context.bot.id
    fqdtw_list = sftw.find_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'})
    if fqdtw_list is None:
        sifatuwen(bot_id, 'å›¾æ–‡1ðŸ”½','','','',b'\x80\x03]q\x00]q\x01a.','')
        fqdtw_list = sftw.find_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'})
    state = fqdtw_list['state']
    keyboard = [
        [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}å›¾æ–‡è®¾ç½®', callback_data='tuwen'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}æŒ‰é’®è®¾ç½®', callback_data='anniu')],
        [InlineKeyboardButton(f'{MOOD_EMOJI_STAR}æŸ¥çœ‹å›¾æ–‡', callback_data='cattu'),
         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}å¼€å¯ç§å‘', callback_data='kaiqisifa')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')]
    ]
    if state == 1:
        context.bot.send_message(chat_id=user_id, text='ç§å‘çŠ¶æ€:å·²å…³é—­ðŸ”´', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        context.bot.send_message(chat_id=user_id, text='ç§å‘çŠ¶æ€:å·²å¼€å¯ðŸŸ¢', reply_markup=InlineKeyboardMarkup(keyboard))


def tuwen(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    context.user_data[f'key{user_id}'] = query.message
    message_id = context.bot.send_message(chat_id=user_id, text=f'å›žå¤å›¾æ–‡æˆ–å›¾ç‰‡è§†é¢‘æ–‡å­—',
                                          reply_markup=ForceReply())
    context.user_data[f'wanfapeizhi{user_id}'] = message_id

def cattu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    bot_id = context.bot.id
    fqdtw_list = sftw.find_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'})
    file_id = fqdtw_list['file_id']
    file_text = fqdtw_list['text']
    file_type = fqdtw_list['send_type']
    key_text = fqdtw_list['key_text']
    keyboard = safe_pickle_loads(fqdtw_list['keyboard'])
    keyboard.append([InlineKeyboardButton('âœ…å·²è¯»ï¼ˆç‚¹å‡»é”€æ¯æ­¤æ¶ˆæ¯ï¼‰', callback_data=f'close {user_id}')])
    if fqdtw_list['text'] == '' and fqdtw_list['file_id'] == '':
        message_id = context.bot.send_message(chat_id=user_id, text='è¯·è®¾ç½®å›¾æ–‡åŽç‚¹å‡»')
        time.sleep(3)
        del_message(message_id)
    else:
        try:
            context.bot.send_message(chat_id=user_id, text=key_text)
        except:
            pass
        if file_type == 'text':
            try:
                message_id = context.bot.send_message(chat_id=user_id, text=file_text,
                                                      reply_markup=InlineKeyboardMarkup(keyboard))
            except:
                message_id = context.bot.send_message(chat_id=user_id, text=file_text)
        else:
            if file_type == 'photo':
                try:
                    message_id = context.bot.send_photo(chat_id=user_id, caption=file_text, photo=file_id,
                                                        reply_markup=InlineKeyboardMarkup(keyboard))
                except:
                    message_id = context.bot.send_photo(chat_id=user_id, caption=file_text, photo=file_id)
            else:
                try:
                    message_id = context.bot.sendAnimation(chat_id=user_id, caption=file_text, animation=file_id,
                                                           reply_markup=InlineKeyboardMarkup(keyboard))
                except:
                    message_id = context.bot.sendAnimation(chat_id=user_id, caption=file_text, animation=file_id)
        time.sleep(3)
        del_message(message_id)

def anniu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    context.user_data[f'key{user_id}'] = query.message
    message_id = context.bot.send_message(chat_id=user_id, text=f'å›žå¤æŒ‰é’®è®¾ç½®', reply_markup=ForceReply())
    context.user_data[f'wanfapeizhi{user_id}'] = message_id

def kaiqisifa(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    bot_id = context.bot.id
    job = context.job_queue.get_jobs_by_name(f'sifa')
    if job == ():
        sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {"state": 2}})
        keyboard = [
            [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}å›¾æ–‡è®¾ç½®', callback_data='tuwen'),
             InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}æŒ‰é’®è®¾ç½®', callback_data='anniu')],
            [InlineKeyboardButton(f'{MOOD_EMOJI_STAR}æŸ¥çœ‹å›¾æ–‡', callback_data='cattu'),
             InlineKeyboardButton(f'{MOOD_EMOJI_FAST}å¼€å¯ç§å‘', callback_data='kaiqisifa')],
            [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')]]
        try:
            query.edit_message_text(text='ç§å‘çŠ¶æ€:å·²å¼€å¯ðŸŸ¢', reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as exc:
            if 'Message is not modified' not in str(exc):
                raise
        context.job_queue.run_once(sync_job(usersifa), 1, data={"user_id": user_id}, name=f'sifa')
        message_id = context.bot.send_message(chat_id=user_id, text='å¼€å¯ç§å‘')
        context.user_data['sifa'] = message_id
    else:
        message_id = context.bot.send_message(chat_id=user_id, text='ç§å‘è¿›è¡Œä¸­')
        time.sleep(3)
        del_message(message_id)

def usersifa(context: CallbackContext):
    job = context.job
    bot_id = context.bot.id
    guanli_id = job.data['user_id']
    count = 0
    shibai = 0
    fqdtw_list = sftw.find_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'})
    file_id = fqdtw_list['file_id']
    file_text = fqdtw_list['text']
    file_type = fqdtw_list['send_type']
    key_text = fqdtw_list['key_text']
    keyboard = safe_pickle_loads(fqdtw_list['keyboard'])
    
    
    keyboard.append([InlineKeyboardButton('âœ…å·²è¯»ï¼ˆç‚¹å‡»é”€æ¯æ­¤æ¶ˆæ¯ï¼‰', callback_data=f'close 12321')])
    for i in list(user.find({})):
        if file_type == 'text':
            try:
                
                message_id = context.bot.send_message(chat_id=i['user_id'], text=file_text,
                                                      reply_markup=InlineKeyboardMarkup(keyboard))
                count += 1
            except:
                shibai += 1
        else:
            if file_type == 'photo':
                try:
                    
                    message_id = context.bot.send_photo(chat_id=i['user_id'], caption=file_text, photo=file_id,
                                                        reply_markup=InlineKeyboardMarkup(keyboard))
                    count += 1
                except:
                    shibai += 1
            else:
                try:
                    
                    message_id = context.bot.sendAnimation(chat_id=i['user_id'], caption=file_text, animation=file_id,
                                                           reply_markup=InlineKeyboardMarkup(keyboard))
                    count += 1
                except:
                    shibai += 1
        time.sleep(3)
    sftw.update_one({'bot_id': bot_id,'projectname': f'å›¾æ–‡1ðŸ”½'}, {'$set': {"state": 1}})
    context.bot.send_message(chat_id=guanli_id, text=f'ç§å‘å®Œæ¯•\næˆåŠŸ:{count}\nå¤±è´¥:{shibai}')
    keyboard = [
        [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}å›¾æ–‡è®¾ç½®', callback_data='tuwen'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}æŒ‰é’®è®¾ç½®', callback_data='anniu')],
        [InlineKeyboardButton(f'{MOOD_EMOJI_STAR}æŸ¥çœ‹å›¾æ–‡', callback_data='cattu'),
         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}å¼€å¯ç§å‘', callback_data='kaiqisifa')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {guanli_id}')]]
    context.bot.send_message(chat_id=guanli_id, text='ç§å‘çŠ¶æ€:å·²å…³é—­ðŸ”´', reply_markup=InlineKeyboardMarkup(keyboard))

def backstart(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    keyboard = [
        [InlineKeyboardButton(f'{ADMIN_EMOJI_USERLIST}ç”¨æˆ·åˆ—è¡¨', callback_data='yhlist'), InlineKeyboardButton(f'{ADMIN_EMOJI_DM}å¯¹è¯ç”¨æˆ·ç§å‘', callback_data='sifa')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_TRC20}å……å€¼åœ°å€è®¾ç½®', callback_data='settrc20'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_OKPAY}OKPayé…ç½®', callback_data='okpaycfg')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_GOODS}å•†å“ç®¡ç†', callback_data='spgli'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}æ¬¢è¿Žè¯­ä¿®æ”¹', callback_data='startupdate')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}èœå•æŒ‰é’®', callback_data='addzdykey')],
    ]
    if BOT_CLONE_ENABLED:
        keyboard[-1].append(InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}ä¸€é”®å…‹éš†åŒæ¬¾', callback_data='clonebot'))
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}å…‹éš†åˆ—è¡¨', callback_data='clonelist 0')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
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
    df_id = int(query.data.replace('gmaijilu ', ''))
    jilu_list = list(gmjlu.find({'user_id': df_id}, sort=[('timer', -1)], limit=10))
    keyboard = []
    text_list = []
    count = 1
    for i in jilu_list:
        bianhao = i['bianhao']
        projectname = i['projectname']
        fhtext = i['text']

        keyboard.append([InlineKeyboardButton(f'{projectname}', callback_data=f'zcfshuo {bianhao}')])
        count += 1
    if len(list(gmjlu.find({'user_id': df_id}))) > 10:
        keyboard.append([InlineKeyboardButton('ä¸‹ä¸€é¡µ', callback_data=f'gmainext {df_id}:10')])
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›ž', callback_data=f'backgmjl {df_id}')])
    try:
        query.edit_message_text(text='ðŸ›’æ‚¨çš„è´­ç‰©è®°å½•', parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except:
        pass


def gmainext(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data.replace('gmainext ', '')
    page = data.split(":")[1]
    df_id = int(data.split(':')[0])
    user_id = query.from_user.id
    keyboard = []
    text_list = []
    jilu_list = list(gmjlu.find({"user_id": df_id}, sort=[("timer", -1)], skip=int(page), limit=10))
    count = 1
    for i in jilu_list:
        bianhao = i['bianhao']
        projectname = i['projectname']
        fhtext = i['text']

        keyboard.append([InlineKeyboardButton(f'{projectname}', callback_data=f'zcfshuo {bianhao}')])
        count += 1
    if len(list(gmjlu.find({"user_id": df_id}, sort=[("timer", -1)], skip=int(page)))) > 10:
        if int(page) == 0:
            keyboard.append([InlineKeyboardButton('ä¸‹ä¸€é¡µ', callback_data=f'gmainext {df_id}:{int(page) + 10}')])
        else:
            keyboard.append([InlineKeyboardButton('ä¸Šä¸€é¡µ', callback_data=f'gmainext {df_id}:{int(page) - 10}'),
                             InlineKeyboardButton('ä¸‹ä¸€é¡µ', callback_data=f'gmainext {df_id}:{int(page) + 10}')])
    else:
        keyboard.append([InlineKeyboardButton('ä¸Šä¸€é¡µ', callback_data=f'gmainext {df_id}:{int(page) - 10}')])

    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›ž', callback_data=f'backgmjl {df_id}')])
    try:
        query.edit_message_text(text='ðŸ›’æ‚¨çš„è´­ç‰©è®°å½•', parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
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
    else:
        df_username = f'<a href="https://t.me/{df_username}">{df_username}</a>'
    creation_time = df_list['creation_time']
    zgsl = df_list['zgsl']
    zgje = df_list['zgje']
    USDT = df_list['USDT']
    fstext = f'''
<b>ç”¨æˆ·ID:</b>  <code>{df_id}</code>
<b>ç”¨æˆ·å:</b>  {df_username} 
<b>æ³¨å†Œæ—¥æœŸ:</b>  {creation_time}

<b>æ€»è´­æ•°é‡:</b>  {zgsl}

<b>æ€»è´­é‡‘é¢:</b>  {standard_num(zgje)} USDT

<b>æ‚¨çš„ä½™é¢:</b>  {USDT} USDT
    '''

    keyboard = [[InlineKeyboardButton('ðŸ›’è´­ä¹°è®°å½•', callback_data=f'gmaijilu {df_id}')],
                [InlineKeyboardButton('å…³é—­', callback_data=f'close {df_id}')]]
    query.edit_message_text(text=fstext, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML',
                            disable_web_page_preview=True)


def zcfshuo(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    bianhao = query.data.replace('zcfshuo ', '')
    gmjlu_list = gmjlu.find_one({'bianhao': bianhao})
    leixing = gmjlu_list['leixing']
    if leixing == 'ä¼šå‘˜é“¾æŽ¥':
        text = gmjlu_list['text']

        context.bot.send_message(chat_id=user_id, text=text, disable_web_page_preview=True)

    else:
        zip_filename = gmjlu_list['text']
        fstext = gmjlu_list['ts']
        keyboard = [[InlineKeyboardButton('âœ…å·²è¯»ï¼ˆç‚¹å‡»é”€æ¯æ­¤æ¶ˆæ¯ï¼‰', callback_data=f'close {user_id}')]]
        context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML', disable_web_page_preview=True,
                                 reply_markup=InlineKeyboardMarkup(keyboard))

        query.message.reply_document(open(zip_filename, "rb"))


def yhlist(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    jilu_list = list(user.find({}, limit=10))
    keyboard = []
    text_list = []
    count = 1
    for i in jilu_list:
        df_id = i['user_id']
        df_username = i['username']
        df_fullname = i['fullname'].replace('<', '').replace('>', '')
        USDT = i['USDT']
        text_list.append(
            f'{count}. <a href="tg://user?id={df_id}">{df_fullname}</a> ID:<code>{df_id}</code>-@{df_username}-ä½™é¢:{USDT}')
        count += 1
    if len(list(user.find({}))) > 10:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}ä¸‹ä¸€é¡µ', callback_data=f'yhnext 10:{count}')])

    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data='backstart')])

    text_list = '\n'.join(text_list)
    try:
        query.edit_message_text(text=text_list, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except:
        pass


def yhnext(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data.replace('yhnext ', '')
    page = data.split(":")[0]
    count = int(data.split(":")[1])
    keyboard = []
    text_list = []
    jilu_list = list(user.find({}, skip=int(page), limit=10))
    for i in jilu_list:
        df_id = i['user_id']
        df_username = i['username']
        df_fullname = i['fullname'].replace('<', '').replace('>', '')
        USDT = i['USDT']
        text_list.append(
            f'{count}. <a href="tg://user?id={df_id}">{df_fullname}</a> ID:<code>{df_id}</code>-@{df_username}-ä½™é¢:{USDT}')
        count += 1
    if len(list(user.find({}, skip=int(page)))) > 10:
        if int(page) == 0:
            keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}ä¸‹ä¸€é¡µ', callback_data=f'yhnext {int(page) + 10}:{count}')])
        else:
            keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}ä¸Šä¸€é¡µ', callback_data=f'yhnext {int(page) - 10}:{count - 20}'),
                             InlineKeyboardButton(f'{MOOD_EMOJI_FAST}ä¸‹ä¸€é¡µ', callback_data=f'yhnext {int(page) + 10}:{count}')])
    else:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}ä¸Šä¸€é¡µ', callback_data=f'yhnext {int(page) - 10}:{count - 20}')])

    text_list = '\n'.join(text_list)
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
    query.bot.edit_message_text(text=text_list, chat_id=query.message.chat.id,
                                message_id=query.message.message_id, reply_markup=InlineKeyboardMarkup(keyboard),
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
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å»ºä¸€è¡Œ', callback_data='newfl')])
    else:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å»ºä¸€è¡Œ', callback_data='newfl'),
                         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´è¡ŒæŽ’åº', callback_data='paixufl'),
                         InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ä¸€è¡Œ', callback_data='delfl')])
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data='backstart'), InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
    text = f'''
å•†å“ç®¡ç†
    '''
    query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')



def generate_24bit_uid():
    # ç”Ÿæˆä¸€ä¸ªUUID
    uid = uuid.uuid4()

    # å°†UUIDè½¬æ¢ä¸ºå­—ç¬¦ä¸²
    uid_str = str(uid)

    # ä½¿ç”¨MD5å“ˆå¸Œç®—æ³•å°†å­—ç¬¦ä¸²å“ˆå¸Œä¸ºä¸€ä¸ª128ä½çš„å€¼
    hashed_uid = hashlib.md5(uid_str.encode()).hexdigest()

    # å–å“ˆå¸Œå€¼çš„å‰24ä½ä½œä¸ºæˆ‘ä»¬çš„24ä½UID
    return hashed_uid[:24]


def build_product_detail_keyboard(nowuid, uid, user_id):
    return [
        [InlineKeyboardButton(f'{MOOD_EMOJI_FIRE}å–å‡ºæ‰€æœ‰åº“å­˜', callback_data=f'qchuall {nowuid}'),
         InlineKeyboardButton(f'{MOOD_EMOJI_STAR}å•†å“ä½¿ç”¨è¯´æ˜Ž', callback_data=f'update_sysm {nowuid}')],
        [InlineKeyboardButton('ðŸ“„ä¸Šä¼ è°·æ­Œè´¦æˆ·', callback_data=f'update_gg {nowuid}'),
         InlineKeyboardButton('ðŸ’¡è´­ä¹°æç¤º', callback_data=f'update_wbts {nowuid}')],
        [InlineKeyboardButton('ðŸ”—ä¸Šä¼ é“¾æŽ¥', callback_data=f'update_hy {nowuid}'),
         InlineKeyboardButton('ðŸ“ä¸Šä¼ txtæ–‡ä»¶', callback_data=f'update_txt {nowuid}')],
        [InlineKeyboardButton('ðŸ“¦ä¸Šä¼ å·åŒ…', callback_data=f'update_hb {nowuid}'),
         InlineKeyboardButton('ðŸ§©ä¸Šä¼ åè®®å·', callback_data=f'update_xyh {nowuid}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}ä¿®æ”¹äºŒçº§åˆ†ç±»å', callback_data=f'upejflname {nowuid}'),
         InlineKeyboardButton('ðŸ’°ä¿®æ”¹ä»·æ ¼', callback_data=f'upmoney {nowuid}')],
        [InlineKeyboardButton('â¬…ï¸è¿”å›žåˆ†ç±»è¯¦æƒ…', callback_data=f'flxxi {uid}'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')]
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
    fenleibiao(uid, 'ç‚¹å‡»æŒ‰é’®ä¿®æ”¹', maxrow)
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
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å»ºä¸€è¡Œ', callback_data='newfl'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´è¡ŒæŽ’åº', callback_data='paixufl'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ä¸€è¡Œ', callback_data='delfl')])
    context.bot.send_message(chat_id=user_id, text='å•†å“ç®¡ç†', reply_markup=InlineKeyboardMarkup(keyboard))


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

    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}ä¿®æ”¹åˆ†ç±»å', callback_data=f'upspname {uid}'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å¢žäºŒçº§åˆ†ç±»', callback_data=f'newejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´äºŒçº§åˆ†ç±»æŽ’åº', callback_data=f'paixuejfl {uid}'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤äºŒçº§åˆ†ç±»', callback_data=f'delejfl {uid}')])
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žå•†å“ç®¡ç†', callback_data='spgli')])
    fstext = f'''
åˆ†ç±»: {fl_pro}
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
ä¸»åˆ†ç±»: {fl_pro}
äºŒçº§åˆ†ç±»: {ej_projectname}

ä»·æ ¼: {money}U
åº“å­˜: {kc}
å·²å”®: {ys}
    '''
    query.edit_message_text(text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def update_xyh(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_xyh ', '')
    fstext = f'''
å‘é€åè®®å·åŽ‹ç¼©åŒ…ï¼Œè‡ªåŠ¨è¯†åˆ«é‡Œé¢çš„jsonæˆ–sessionæ ¼å¼
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_xyh {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def update_gg(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_gg ', '')
    fstext = f'''
å‘é€txtæ–‡ä»¶
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_gg {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def update_txt(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_txt ', '')
    fstext = f'''
apiå·ç é“¾æŽ¥ä¸“ç”¨ï¼Œè¯·æ­£ç¡®ä¸Šä¼ ï¼Œå‘é€txtæ–‡ä»¶ï¼Œä¸€è¡Œä¸€ä¸ª
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_txt {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
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
å½“å‰ä½¿ç”¨è¯´æ˜Žä¸ºä¸Šé¢
è¾“å…¥æ–°çš„æ–‡å­—æ›´æ”¹
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_sysm {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
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
å½“å‰åˆ†ç±»æç¤ºä¸ºä¸Šé¢
è¾“å…¥æ–°çš„æ–‡å­—æ›´æ”¹
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_wbts {nowuid}'}})
    keyboard = [[InlineKeyboardButton('å–æ¶ˆ', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def update_hy(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_hy ', '')
    fstext = f'''
å‘é€é“¾æŽ¥ï¼Œæ¢è¡Œä»£è¡¨å¤šä¸ª
å•ä¸ª
https://t.me/giftcode/IApV5cqF2FCzAQAA5aDXkeEqQrQ
å¤šä¸ª
https://t.me/giftcode/IApV5cqF2FCzAQAA5aDXkeEqQrQ
https://t.me/giftcode/wI_oG9K2oFBSAQAA-Z2W0Fb3ng8
https://t.me/giftcode/_xSoPUXMgVBmAQAAiKBPNxWWIpY
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_hy {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard),
                             disable_web_page_preview=True)


def update_hb(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    nowuid = query.data.replace('update_hb ', '')
    fstext = f'''
å‘é€å·åŒ…
    '''
    user.update_one({"user_id": user_id}, {"$set": {"sign": f'update_hb {nowuid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def upmoney(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    uid = query.data.replace('upmoney ', '')
    fstext = f'''
è¾“å…¥æ–°çš„ä»·æ ¼
    '''

    user.update_one({"user_id": user_id}, {"$set": {"sign": f'upmoney {uid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def upejflname(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    uid = query.data.replace('upejflname ', '')
    fstext = f'''
è¾“å…¥æ–°çš„åå­—
ä¾‹å¦‚ ðŸ‡¨ðŸ‡³+86ä¸­å›½~ç›´ç™»å·(tadta)
    '''

    user.update_one({"user_id": user_id}, {"$set": {"sign": f'upejflname {uid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def upspname(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    uid = query.data.replace('upspname ', '')
    fstext = f'''
è¾“å…¥æ–°çš„åå­—
ä¾‹å¦‚ ðŸŒŽäºšæ´²å›½å®¶~âœˆç›´ç™»å·(tadta)
    '''

    user.update_one({"user_id": user_id}, {"$set": {"sign": f'upspname {uid}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
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
    erjifenleibiao(uid, nowuid, 'ç‚¹å‡»æŒ‰é’®ä¿®æ”¹', maxrow)
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

    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}ä¿®æ”¹åˆ†ç±»å', callback_data=f'upspname {uid}'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å¢žäºŒçº§åˆ†ç±»', callback_data=f'newejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´äºŒçº§åˆ†ç±»æŽ’åº', callback_data=f'paixuejfl {uid}'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤äºŒçº§åˆ†ç±»', callback_data=f'delejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
    fstext = f'''
åˆ†ç±»: {fl_pro}
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
        keyboard = [[InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å»ºä¸€è¡Œ', callback_data='newrow')]]
    else:
        keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å»ºä¸€è¡Œ', callback_data='newrow'),
                         InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ä¸€è¡Œ', callback_data='delrow'),
                         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´è¡ŒæŽ’åº', callback_data='paixurow')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}ä¿®æ”¹æŒ‰é’®', callback_data='newkey')])
        
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
    text = f'''
è‡ªå®šä¹‰æŒ‰é’®
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
        context.bot.send_message(chat_id=user_id, text='è¯·å…ˆæ–°å»ºä¸€è¡Œ')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_STAR}ç¬¬{i + 1}è¡Œ', callback_data=f'dddd'),
                             InlineKeyboardButton('âž•', callback_data=f'addhangkey {i + 1}'),
                             InlineKeyboardButton('âž–', callback_data=f'delhangkey {i + 1}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
        keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
        query.edit_message_text(text='è‡ªå®šä¹‰æŒ‰é’®', reply_markup=InlineKeyboardMarkup(keyboard))


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
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å»ºä¸€è¡Œ', callback_data='newrow'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ä¸€è¡Œ', callback_data='delrow'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´è¡ŒæŽ’åº', callback_data='paixurow')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}ä¿®æ”¹æŒ‰é’®', callback_data='newkey')])
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
    context.bot.send_message(chat_id=user_id, text='è‡ªå®šä¹‰æŒ‰é’®', reply_markup=InlineKeyboardMarkup(keyboard))


def close(update: Update, context: CallbackContext):
    query = update.callback_query
    chat = query.message.chat
    query.answer()
    yh_id = query.data.replace("close ", '')
    bot_id = context.bot.id
    chat_id = chat.id
    user_id = query.from_user.id

    user.update_one({'user_id': user_id}, {'$set': {'sign': 0}})
    context.bot.delete_message(chat_id=query.from_user.id, message_id=query.message.message_id)


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
        context.bot.send_message(chat_id=user_id, text='æ²¡æœ‰æŒ‰é’®å­˜åœ¨')
    else:
        maxrow = max(count)
        if maxrow == 1:
            context.bot.send_message(chat_id=user_id, text='åªæœ‰ä¸€è¡ŒæŒ‰é’®æ— æ³•è°ƒæ•´')
        else:
            for i in range(0, maxrow):
                if i == 0:
                    keyboard.append(
                        [InlineKeyboardButton(f'{MOOD_EMOJI_FAST}ç¬¬{i + 1}è¡Œä¸‹ç§»', callback_data=f'paixuyidong xiayi:{i + 1}')])
                elif i == maxrow - 1:
                    keyboard.append(
                        [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}ç¬¬{i + 1}è¡Œä¸Šç§»', callback_data=f'paixuyidong shangyi:{i + 1}')])
                else:
                    keyboard.append(
                        [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}ç¬¬{i + 1}è¡Œä¸Šç§»', callback_data=f'paixuyidong shangyi:{i + 1}'),
                         InlineKeyboardButton(f'{MOOD_EMOJI_FAST}ç¬¬{i + 1}è¡Œä¸‹ç§»', callback_data=f'paixuyidong xiayi:{i + 1}')])
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
            keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
            query.edit_message_text(text='è‡ªå®šä¹‰æŒ‰é’®', reply_markup=InlineKeyboardMarkup(keyboard))


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
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å»ºä¸€è¡Œ', callback_data='newrow'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ä¸€è¡Œ', callback_data='delrow'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´è¡ŒæŽ’åº', callback_data='paixurow')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}ä¿®æ”¹æŒ‰é’®', callback_data='newkey')])
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
    query.edit_message_text(text='è‡ªå®šä¹‰æŒ‰é’®', reply_markup=InlineKeyboardMarkup(keyboard))


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
        context.bot.send_message(chat_id=user_id, text='æ²¡æœ‰æŒ‰é’®å­˜åœ¨')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ç¬¬{i + 1}è¡Œ', callback_data=f'qrscdelrow {i + 1}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
        keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
        query.edit_message_text(text='è‡ªå®šä¹‰æŒ‰é’®', reply_markup=InlineKeyboardMarkup(keyboard))


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
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å»ºä¸€è¡Œ', callback_data='newrow'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ä¸€è¡Œ', callback_data='delrow'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´è¡ŒæŽ’åº', callback_data='paixurow')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}ä¿®æ”¹æŒ‰é’®', callback_data='newkey')])
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
    context.bot.send_message(chat_id=user_id,text='è‡ªå®šä¹‰æŒ‰é’®', reply_markup=InlineKeyboardMarkup(keyboard))


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
        context.bot.send_message(chat_id=user_id, text='æ²¡æœ‰æŒ‰é’®å­˜åœ¨')
    else:

        # maxrow = max(count)
        for i in range(0, len(count)):
            keyboard[count[i]].append(InlineKeyboardButton('âž–', callback_data=f'qrdelliekey {row}:{i + 1}'))
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
        keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
        query.edit_message_text(text='è‡ªå®šä¹‰æŒ‰é’®', reply_markup=InlineKeyboardMarkup(keyboard))


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
è¿™æ˜¯ç¬¬{row}è¡Œç¬¬{first}ä¸ªæŒ‰é’®

æŒ‰é’®åç§°: {projectname}
    '''

    keyboard = [
        [InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}å›¾æ–‡è®¾ç½®', callback_data=f'settuwenset {row}:{first}'),
         InlineKeyboardButton(f'{MOOD_EMOJI_STAR}æŸ¥çœ‹å›¾æ–‡è®¾ç½®', callback_data=f'cattuwenset {row}:{first}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_MENU}ä¿®æ”¹å°¾éšæŒ‰é’®', callback_data=f'setkeyboard {row}:{first}'),
         InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}ä¿®æ”¹æŒ‰é’®åå­—', callback_data=f'setkeyname {row}:{first}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')]
    ]

    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
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
è¾“å…¥è¦ä¿®æ”¹çš„åå­—

é¢œè‰²å‰ç¼€ï¼š
#r = çº¢è‰²
#g = ç»¿è‰²
#b = è“è‰²

ä¾‹å¦‚ï¼š
#r ðŸ˜ƒå•†å“åˆ—è¡¨
#g ðŸ‘¤ä¸ªäººä¸­å¿ƒ
#b ðŸ’¸æˆ‘è¦å……å€¼
    '''
    user.update_one({'user_id': user_id}, {"$set": {"sign": f'setkeyname {row}:{first}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')]]
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
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
æŒ‰ä»¥ä¸‹æ ¼å¼è®¾ç½®æŒ‰é’®ï¼Œå¡«å…¥â—ˆä¹‹é—´ï¼ŒåŒä¸€è¡Œç”¨ | éš”å¼€
æŒ‰é’®åç§°&https://t.me/... | æŒ‰é’®åç§°&https://t.me/...
æŒ‰é’®åç§°&https://t.me/... | æŒ‰é’®åç§°&https://t.me/... | æŒ‰é’®åç§°&https://t.me/....
    '''
    key_list = get_key.find_one({'Row': row, 'first': first})
    key_text = key_list['key_text']
    if key_text != '':
        context.bot.send_message(chat_id=user_id, text=key_text)
    user.update_one({'user_id': user_id}, {"$set": {"sign": f'setkeyboard {row}:{first}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')]]
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data=f'backstart')])
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
    keyboard = safe_pickle_loads(key_list['keyboard'])
    if text == '' and file_id == '':
        pass
    else:
        if file_type == 'text':
            message_id = context.bot.send_message(chat_id=user_id, text=text,
                                                  reply_markup=InlineKeyboardMarkup(keyboard), entities=entities)
        else:
            if file_type == 'photo':
                message_id = context.bot.send_photo(chat_id=user_id, caption=text, photo=file_id,
                                                    reply_markup=InlineKeyboardMarkup(keyboard),
                                                    caption_entities=entities)
            else:
                message_id = context.bot.sendAnimation(chat_id=user_id, caption=text, animation=file_id,
                                                       reply_markup=InlineKeyboardMarkup(keyboard),
                                                       caption_entities=entities)
    text = f'''
âœï¸ å‘é€ä½ çš„å›¾æ–‡è®¾ç½®

æ–‡å­—ã€è§†é¢‘ã€å›¾ç‰‡ã€gifã€å›¾æ–‡
    '''
    user.update_one({'user_id': user_id}, {"$set": {"sign": f'settuwenset {row}:{first}'}})
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')]]
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
    keyboard = safe_pickle_loads(key_list['keyboard'])
    if text == '' and file_id == '':
        message_id = context.bot.send_message(chat_id=user_id, text='è¯·è®¾ç½®å›¾æ–‡åŽç‚¹å‡»')
        timer11 = Timer(3, del_message, args=[message_id])
        timer11.start()
    else:
        if file_type == 'text':
            message_id = context.bot.send_message(chat_id=user_id, text=text,
                                                  reply_markup=InlineKeyboardMarkup(keyboard), entities=entities)
        else:
            if file_type == 'photo':
                message_id = context.bot.send_photo(chat_id=user_id, caption=text, photo=file_id,
                                                    reply_markup=InlineKeyboardMarkup(keyboard),
                                                    caption_entities=entities)
            else:
                message_id = context.bot.sendAnimation(chat_id=user_id, caption=text, animation=file_id,
                                                       reply_markup=InlineKeyboardMarkup(keyboard),
                                                       caption_entities=entities)
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
        context.bot.send_message(chat_id=user_id, text='è¯·å…ˆæ–°å»ºä¸€è¡Œ')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_STAR}ç¬¬{i + 1}è¡Œ', callback_data=f'dddd'),
                             InlineKeyboardButton('âž•', callback_data=f'addhangkey {i + 1}'),
                             InlineKeyboardButton('âž–', callback_data=f'delhangkey {i + 1}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
        context.bot.send_message(chat_id=user_id, text='è‡ªå®šä¹‰æŒ‰é’®', reply_markup=InlineKeyboardMarkup(keyboard))


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
        context.bot.send_message(chat_id=user_id, text='è¯·å…ˆæ–°å»ºä¸€è¡Œ')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_STAR}ç¬¬{i + 1}è¡Œ', callback_data=f'dddd'),
                             InlineKeyboardButton('âž•', callback_data=f'addhangkey {i + 1}'),
                             InlineKeyboardButton('âž–', callback_data=f'delhangkey {i + 1}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
        context.bot.send_message(chat_id=user_id, text='è‡ªå®šä¹‰æŒ‰é’®', reply_markup=InlineKeyboardMarkup(keyboard))


def settrc20(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    text = f'''
è¯·è¾“å…¥ä»¥ T å¼€å¤´ã€å…± 34 ä½çš„ TRC20-USDT æ”¶æ¬¾åœ°å€
'''
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'settrc20'}})
    context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def build_okpay_config_text():
    shop_id = get_okpay_shop_id()
    token = get_okpay_shop_token()
    name = get_okpay_name()
    enabled = 'å·²å¼€å¯' if refresh_okpay_entry_status() else 'æœªå¼€å¯'
    masked_token = (token[:6] + '******' + token[-4:]) if len(token) >= 12 else ('å·²è®¾ç½®' if token else 'æœªè®¾ç½®')
    return f'''
<b>OKPay å½“å‰é…ç½®</b>

å•†æˆ·IDï¼š<code>{shop_id or 'æœªè®¾ç½®'}</code>
Tokenï¼š<code>{masked_token}</code>
åç§°ï¼š<code>{name or 'æœªè®¾ç½®'}</code>
å……å€¼å…¥å£ï¼š<code>{enabled}</code>

å½“ å•†æˆ·ID / Token / åç§° ä¸‰é¡¹éƒ½é…ç½®å®ŒæˆåŽï¼Œä¼šè‡ªåŠ¨å¼€å¯ OKPay å……å€¼å…¥å£ã€‚
'''


def build_okpay_config_keyboard(user_id):
    return [
        [InlineKeyboardButton(f'{ADMIN_EMOJI_OKPAY}è®¾ç½®å•†æˆ·ID', callback_data='setokpayid'), InlineKeyboardButton(f'{MOOD_EMOJI_STAR}è®¾ç½®Token', callback_data='setokpaytoken')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}è®¾ç½®åç§°', callback_data='setokpayname')],
        [InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data='backstart')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')]
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
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'setokpayid'}})
    context.bot.send_message(chat_id=user_id, text='è¯·è¾“å…¥ OKPay å•†æˆ·ID', reply_markup=InlineKeyboardMarkup(keyboard))


def setokpaytoken(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'setokpaytoken'}})
    context.bot.send_message(chat_id=user_id, text='è¯·è¾“å…¥ OKPay Token', reply_markup=InlineKeyboardMarkup(keyboard))


def setokpayname(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'setokpayname'}})
    context.bot.send_message(chat_id=user_id, text='è¯·è¾“å…¥ OKPay åç§°ï¼ˆä¾‹å¦‚ï¼šå·é“ºï¼‰', reply_markup=InlineKeyboardMarkup(keyboard))


def can_use_clonebot(state):
    if not BOT_CLONE_ENABLED:
        return False
    return ALLOW_PUBLIC_BOT_CLONE or str(state) == '4'


def build_clone_purchase_keyboard(user_id, user_balance, fee):
    keyboard = []
    if Decimal(str(user_balance)) >= fee:
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_OKPAY}æ”¯ä»˜ {format_clone_price(fee)} USDT å¹¶ç»§ç»­', callback_data='clonepay')])
    else:
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_OKPAY}ä½™é¢ä¸è¶³ï¼Œå…ˆåŽ»å……å€¼', callback_data='recharge_menu')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')])
    return keyboard


def send_clonebot_prompt(context, user_id):
    user_list = user.find_one({'user_id': user_id}) or {}
    state = user_list.get('state')
    if not can_use_clonebot(state):
        context.bot.send_message(chat_id=user_id, text='å½“å‰æœªå¼€æ”¾ä¸€é”®å…‹éš†åŠŸèƒ½')
        return
    fee = get_clone_price_decimal()
    clone_credit = get_user_clone_credit(user_id)
    if fee > 0 and not is_clone_fee_exempt(user_id, state) and clone_credit <= 0:
        balance = Decimal(str(user_list.get('USDT', 0) or 0)).quantize(Decimal('0.01'))
        text = f'''
[emoji:5445353829304387411:ðŸ’³] å½“å‰ä¸€é”®å…‹éš†ä¸ºä»˜è´¹æ¨¡å¼

[emoji:4965219701572503640:ðŸ’°] å…‹éš†ä»·æ ¼ï¼š<code>{format_clone_price(fee)} USDT</code>
[emoji:4972482444025398275:ðŸ‘›] å½“å‰ä½™é¢ï¼š<code>{format_clone_price(balance)} USDT</code>

[emoji:5301246586918024418:âš ï¸] æ”¯ä»˜æˆåŠŸåŽï¼Œæ‰èƒ½ç»§ç»­å‘é€æ–° Bot Token è¿›è¡Œå…‹éš†ã€‚
        '''
        keyboard = build_clone_purchase_keyboard(user_id, balance, fee)
        context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        return
    text = '''
[emoji:5287684458881756303:ðŸ¤–] è¯·å‘é€ä½ è¦å…‹éš†çš„æ–° Bot Token

[emoji:5217818964612108191:âœ¨] ä¾‹å¦‚ï¼š
123456789:ABCdefGhIJKlmNoPQRsTUVwxyz123456789

[emoji:5220195537520711716:âš¡ï¸] é»˜è®¤ä¼šæŠŠå½“å‰æ“ä½œç”¨æˆ·è®¾ä¸ºæ–° Bot ç®¡ç†å‘˜ï¼Œå¹¶è‡ªåŠ¨æ‹‰èµ·æ–° Botã€‚
'''
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'clonebottoken'}})
    context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def clonepay(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    user_list = user.find_one({'user_id': user_id}) or {}
    state = user_list.get('state')
    if not can_use_clonebot(state):
        context.bot.send_message(chat_id=user_id, text='å½“å‰æœªå¼€æ”¾ä¸€é”®å…‹éš†åŠŸèƒ½')
        return
    fee = get_clone_price_decimal()
    if fee <= 0 or is_clone_fee_exempt(user_id, state):
        send_clonebot_prompt(context, user_id)
        return

    balance = Decimal(str(user_list.get('USDT', 0) or 0)).quantize(Decimal('0.01'))
    if balance < fee:
        text = f'ä½™é¢ä¸è¶³ï¼Œå½“å‰éœ€æ”¯ä»˜ {format_clone_price(fee)} USDTï¼Œæ‚¨çŽ°åœ¨ä½™é¢ä¸º {format_clone_price(balance)} USDTã€‚'
        keyboard = build_clone_purchase_keyboard(user_id, balance, fee)
        try:
            query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    order_id = 'CLONEPAY' + time.strftime('%Y%m%d%H%M%S', time.localtime()) + str(user_id)
    new_balance = (balance - fee).quantize(Decimal('0.01'))
    user.update_one({'user_id': user_id}, {'$set': {'USDT': float(new_balance)}, '$inc': {'clone_credit': 1}})
    user_logging(order_id, 'å…‹éš†åŒæ¬¾ä»˜è´¹', user_id, float(fee), time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
    send_clonebot_prompt(context, user_id)


def clonebot(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='å½“å‰æœºå™¨äººæœªå¼€æ”¾å…‹éš†åŠŸèƒ½')
        return
    send_clonebot_prompt(context, user_id)


def clonelist(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='å½“å‰æœºå™¨äººæœªå¼€æ”¾å…‹éš†ç®¡ç†')
        return
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='åªæœ‰æºæœºå™¨äººç®¡ç†å‘˜å¯ä»¥æŸ¥çœ‹å…‹éš†åˆ—è¡¨')
        return
    data = str(query.data or '').replace('clonelist', '', 1).strip()
    try:
        page = max(int(data), 0) if data else 0
    except Exception:
        page = 0
    keyboard, total = build_clone_list_keyboard(user_id, page)
    price_text = format_clone_price()
    text = f'''
<b>[emoji:5287684458881756303:ðŸ¤–] å…‹éš†åˆ—è¡¨</b>

å½“å‰ä»˜è´¹ä»·æ ¼ï¼š<code>{price_text} USDT</code>
æ´»è·ƒå…‹éš†æ•°ï¼š<code>{total}</code>

ç‚¹ä¸‹é¢æœºå™¨äººå¯æŸ¥çœ‹è¯¦æƒ…æˆ–åˆ é™¤ã€‚
    '''
    try:
        query.edit_message_text(text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def cloneinfo(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='å½“å‰æœºå™¨äººæœªå¼€æ”¾å…‹éš†ç®¡ç†')
        return
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='åªæœ‰æºæœºå™¨äººç®¡ç†å‘˜å¯ä»¥æŸ¥çœ‹å…‹éš†è¯¦æƒ…')
        return
    bot_id = str(query.data.replace('cloneinfo ', '', 1)).strip()
    row = clone_instances.find_one({'bot_id': bot_id})
    if row is None:
        context.bot.send_message(chat_id=user_id, text='æœªæ‰¾åˆ°è¿™ä¸ªå…‹éš†å®žä¾‹')
        return
    requester_user_id = row.get('requester_user_id')
    requester_name = str(row.get('requester_name') or requester_user_id or '')
    requester_username = str(row.get('requester_username') or '').strip()
    text = f'''
<b>[emoji:5287684458881756303:ðŸ¤–] å…‹éš†è¯¦æƒ…</b>

æœºå™¨äººï¼š@{row.get('bot_username')}
ç®¡ç†å‘˜ï¼š<code>{requester_user_id}</code>
ç”¨æˆ·ï¼š{requester_name} @{requester_username}
æ”¯ä»˜é‡‘é¢ï¼š<code>{format_clone_price(row.get('fee_paid', 0))} USDT</code>
åˆ›å»ºæ—¶é—´ï¼š<code>{row.get('created_at', '')}</code>

ç›®å½•ï¼š<code>{row.get('clone_dir', '')}</code>
æ•°æ®åº“ï¼š<code>{row.get('db_name', '')}</code>
BotæœåŠ¡ï¼š<code>{row.get('service_name', '')}.service</code>
ç›‘å¬æœåŠ¡ï¼š<code>{row.get('listener_service_name', '')}.service</code>
    '''
    keyboard = [
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤è¿™ä¸ªå…‹éš†', callback_data=f'clonedelete {bot_id}')],
        [InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}è¿”å›žå…‹éš†åˆ—è¡¨', callback_data='clonelist 0')]
    ]
    try:
        query.edit_message_text(text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def clonedelete(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    try:
        query.answer('æ­£åœ¨åˆ é™¤ï¼Œè¯·ç¨å€™...')
    except Exception:
        pass
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='å½“å‰æœºå™¨äººæœªå¼€æ”¾å…‹éš†ç®¡ç†')
        return
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='åªæœ‰æºæœºå™¨äººç®¡ç†å‘˜å¯ä»¥åˆ é™¤å…‹éš†å®žä¾‹')
        return
    bot_id = str(query.data.replace('clonedelete ', '', 1)).strip()
    preview_record = clone_instances.find_one({'bot_id': bot_id, 'state': {'$ne': 'deleted'}}) or {}
    bot_username = str(preview_record.get('bot_username') or '').strip()
    waiting_text = f'[emoji:5220195537520711716:âš¡ï¸] æ­£åœ¨åˆ é™¤å…‹éš†å®žä¾‹ï¼Œè¯·ç¨å€™â€¦\n\n[emoji:5287684458881756303:ðŸ¤–] æœºå™¨äººï¼š@{bot_username}' if bot_username else f'[emoji:5220195537520711716:âš¡ï¸] æ­£åœ¨åˆ é™¤å…‹éš†å®žä¾‹ï¼Œè¯·ç¨å€™â€¦\n\n[emoji:5287684458881756303:ðŸ¤–] æœºå™¨äººï¼š<code>{bot_id}</code>'
    try:
        query.edit_message_text(text=waiting_text, parse_mode='HTML')
    except Exception:
        pass
    try:
        record = remove_clone_instance(bot_id, deleted_by=user_id)
    except Exception as exc:
        try:
            context.bot.send_message(chat_id=user_id, text=f'åˆ é™¤å…‹éš†å¤±è´¥ï¼š{exc}')
        except Exception:
            pass
        return

    requester_user_id = record.get('requester_user_id')
    bot_username = str(record.get('bot_username') or bot_username or '').strip()
    display_bot = f'@{bot_username}' if bot_username else str(record.get("bot_id"))
    text = f'[emoji:5312028599803460968:ðŸ†—] å·²åˆ é™¤å…‹éš†å®žä¾‹\n\n[emoji:5287684458881756303:ðŸ¤–] æœºå™¨äººï¼š{display_bot}\n[emoji:6321041414067068140:ðŸ‘¤] ç®¡ç†å‘˜ï¼š{requester_user_id}'
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}è¿”å›žå…‹éš†åˆ—è¡¨', callback_data='clonelist 0')]])
    try:
        query.edit_message_text(text=text, reply_markup=keyboard)
    except Exception:
        try:
            context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
        except Exception:
            pass

    notify_source_admins(
        context,
        f'<b>{ADMIN_EMOJI_CLOSE}å…‹éš†å®žä¾‹å·²åˆ é™¤</b>\n\n[emoji:5287684458881756303:ðŸ¤–] æœºå™¨äººï¼š{display_bot}\n[emoji:6321041414067068140:ðŸ‘¤] ç®¡ç†å‘˜ï¼š<code>{requester_user_id}</code>\n[emoji:6321041414067068140:ðŸ‘¤] åˆ é™¤äººï¼š<code>{user_id}</code>'
    )


def setcloneprice(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    if not BOT_CLONE_ENABLED:
        context.bot.send_message(chat_id=user_id, text='å½“å‰æœºå™¨äººæœªå¼€æ”¾å…‹éš†ç®¡ç†')
        return
    user_list = user.find_one({'user_id': user_id}) or {}
    if str(user_list.get('state')) != '4' and user_id not in get_source_admin_user_ids():
        context.bot.send_message(chat_id=user_id, text='åªæœ‰æºæœºå™¨äººç®¡ç†å‘˜å¯ä»¥è®¾ç½®å…‹éš†ä»·æ ¼')
        return
    text = f'è¯·è¾“å…¥ä¸€é”®å…‹éš†ä»·æ ¼ï¼ˆUSDTï¼‰\n\nå½“å‰ä»·æ ¼ï¼š{format_clone_price()}\nè¾“å…¥ 0 è¡¨ç¤ºå…è´¹ã€‚'
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {'$set': {'sign': 'setcloneprice'}})
    context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def startupdate(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    bot_id = context.bot.id
    text = f'''
è¾“å…¥æ–°çš„æ¬¢è¿Žè¯­
'''
    keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆ', callback_data=f'close {user_id}')]]
    user.update_one({'user_id': user_id}, {"$set": {"sign": 'startupdate'}})
    context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


def build_recharge_method_keyboard(user_id):
    keyboard = []
    if SHOW_TRC20_RECHARGE_ENTRY:
        keyboard.append([InlineKeyboardButton('[emoji:5080312910866024090:ðŸ˜€] USDT ç›´å…… | é“¾ä¸Šåˆ°è´¦', callback_data='recharge_trc20')])
    if okpay_entry_enabled():
        keyboard.append([InlineKeyboardButton('[emoji:6321339712430676611:ðŸ˜„] OKPayæ”¯ä»˜ | ç§’é€Ÿåˆ°è´¦', callback_data='recharge_okpay')])
    keyboard.append([InlineKeyboardButton('å–æ¶ˆå……å€¼', callback_data=f'close {user_id}')])
    return keyboard


def send_recharge_method_menu(context, user_id):
    if not SHOW_TRC20_RECHARGE_ENTRY and not okpay_entry_enabled():
        context.bot.send_message(chat_id=user_id, text='å½“å‰æœªå¼€å¯ä»»ä½•å……å€¼æ–¹å¼ï¼Œè¯·è”ç³»ç®¡ç†å‘˜')
        return
    fstext = '[emoji:5197474438970363734:â¤µï¸] è¯·é€‰æ‹©æ”¯ä»˜æ–¹å¼'
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
    text = f'''
è¾“å…¥å……å€¼é‡‘é¢
'''
    keyboard = [[InlineKeyboardButton('å–æ¶ˆ', callback_data=f'close {user_id}')]]

    message_id = context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))

    user.update_one({'user_id': user_id}, {"$set": {"sign": f'zdycz {message_id.message_id}'}})


def okzdycz(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    text = f'''
è¾“å…¥OKPayå……å€¼é‡‘é¢
'''
    keyboard = [[InlineKeyboardButton('å–æ¶ˆ', callback_data=f'close {user_id}')]]

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
    fstext = f'''
<b>ðŸ’°è¯·é€‰æ‹©ä¸‹é¢ TRC20-USDT å……å€¼é‡‘é¢

â™¥ç³»ç»Ÿä¼šç”Ÿæˆå”¯ä¸€å°æ•°é‡‘é¢ï¼Œè¯·ä¸¥æ ¼æŒ‰è®¢å•é‡‘é¢è½¬è´¦[emoji:5219866512961062330:â‰ï¸]</b>
    '''
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
        [InlineKeyboardButton('è‡ªå®šä¹‰å……å€¼é‡‘é¢', callback_data='zdycz')],
        [InlineKeyboardButton('è¿”å›žæ”¯ä»˜æ–¹å¼', callback_data='recharge_menu')],
        [InlineKeyboardButton('å–æ¶ˆå……å€¼', callback_data=f'close {user_id}')]
    ]
    context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                             reply_markup=InlineKeyboardMarkup(keyboard))


def recharge_okpay(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    fstext = f'''
<b>è¯·é€‰æ‹©ä¸‹é¢ OKPay å……å€¼é‡‘é¢</b>
    '''
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
        [InlineKeyboardButton('è‡ªå®šä¹‰å……å€¼é‡‘é¢', callback_data='okzdycz')],
        [InlineKeyboardButton('è¿”å›žæ”¯ä»˜æ–¹å¼', callback_data='recharge_menu')],
        [InlineKeyboardButton('å–æ¶ˆå……å€¼', callback_data=f'close {user_id}')]
    ]
    context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                             reply_markup=InlineKeyboardMarkup(keyboard))

def catejflsp(update: Update, context: CallbackContext):
    query = update.callback_query

    uid = query.data.replace('catejflsp ', '').split(':')[0]
    zhsl = int(query.data.replace('catejflsp ', '').split(':')[1])
    #     if zhsl == 0:
    #         fstext =f'''
    # ðŸš«æš‚æ— å•†å“ï¼Œè”ç³»å®¢æœä¸Šæž¶
    # å®¢æœ@momoziziya
    #         '''
    #         query.answer(fstext, show_alert=bool("true"))
    #         return
    query.answer()
    bot_id = context.bot.id
    user_id = query.from_user.id

    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    ej_list = ejfl.find({'uid': uid})
    for i in ej_list:
        nowuid = i['nowuid']
        projectname = i['projectname']
        row = i['row']
        hsl = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}  ({hsl})', callback_data=f'gmsp {nowuid}:{hsl}'))

    fstext = f'''
<b>ðŸ›’è¿™æ˜¯å•†å“åˆ—è¡¨  é€‰æ‹©ä½ éœ€è¦çš„å•†å“ï¼š

â—ï¸æ²¡ä½¿ç”¨è¿‡çš„æœ¬åº—å•†å“çš„ï¼Œè¯·å…ˆå°‘é‡è´­ä¹°æµ‹è¯•ï¼Œä»¥å…é€ æˆä¸å¿…è¦çš„äº‰æ‰§ï¼è°¢è°¢åˆä½œï¼

â—ï¸è´¦æˆ·æ”¾ä¹…éš¾å…ä¼šæ­»ï¼Œæœ‰å·®å¼‚ï¼Œè¯·è”ç³»å®¢æœå”®åŽï¼æœ›ç†è§£ï¼</b>
    '''

    keyboard.append([InlineKeyboardButton('ðŸ ä¸»èœå•', callback_data='backzcd'),
                     InlineKeyboardButton('â¬…ï¸è¿”å›ž', callback_data=f'backzcd')])
    query.edit_message_text(fstext, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


def gmsp(update: Update, context: CallbackContext):
    query = update.callback_query

    data = query.data.replace('gmsp ', '')
    nowuid = data.split(':')[0]
    hsl = data.split(':')[1]

    bot_id = context.bot.id
    user_id = query.from_user.id

    hsl = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
    ejfl_list = ejfl.find_one({'nowuid': nowuid})
    projectname = ejfl_list['projectname']
    money = ejfl_list['money']
    uid = ejfl_list['uid']
    #     if hsl == 0:
    #         fstext =f'''
    # ðŸš«æš‚æ— å•†å“ï¼Œè”ç³»å®¢æœä¸Šæž¶
    # å®¢æœ@momoziziya
    #         '''
    #         query.answer(fstext, show_alert=bool("true"))
    #         return
    # else:
    query.answer()
    fstext = f'''
<b>âœ…æ‚¨æ­£åœ¨è´­ä¹°:  {projectname}

ðŸ’° ä»·æ ¼ï¼š {money} USDT

ðŸ“Š åº“å­˜ï¼š {hsl}

â—ï¸ æœªä½¿ç”¨è¿‡çš„æœ¬åº—å•†å“çš„ï¼Œè¯·å…ˆå°‘é‡è´­ä¹°æµ‹è¯•ï¼Œä»¥å…é€ æˆä¸å¿…è¦çš„äº‰æ‰§ï¼è°¢è°¢åˆä½œï¼</b>
    '''

    keyboard = [
        [InlineKeyboardButton('âœ…è´­ä¹°', callback_data=f'gmqq {nowuid}')],
        [InlineKeyboardButton('ðŸ ä¸»èœå•', callback_data='backzcd'),
         InlineKeyboardButton('â¬…ï¸è¿”å›ž', callback_data=f'catejflsp {uid}:1000')]

    ]
    query.edit_message_text(fstext, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))


def gmqq(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id

    nowuid = query.data.replace('gmqq ', '')

    ejfl_list = ejfl.find_one({'nowuid': nowuid})
    projectname = ejfl_list['projectname']
    money = ejfl_list['money']
    uid = ejfl_list['uid']

    user_list = user.find_one({'user_id': user_id})
    USDT = user_list['USDT']
    if USDT < money:
        fstext = f'''
âŒä½™é¢ä¸è¶³ï¼Œè¯·ç«‹å³å……å€¼
        '''
        query.answer(fstext, show_alert=bool("true"))
        return
    else:
        query.answer()
        # del_message(query.message)
        fstext = f'''
<b>è¯·è¾“å…¥æ•°é‡ï¼š
æ ¼å¼ï¼š</b><code>10</code>
        '''

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
        context.bot.send_message(chat_id=user_id, text='æ²¡æœ‰æŒ‰é’®å­˜åœ¨')
    else:
        maxrow = max(count)
        if maxrow == 1:
            context.bot.send_message(chat_id=user_id, text='åªæœ‰ä¸€è¡ŒæŒ‰é’®æ— æ³•è°ƒæ•´')
        else:
            for i in range(0, maxrow):
                pxuid = ejfl.find_one({'uid': uid, 'row': i + 1})['nowuid']
                if i == 0:
                    keyboard.append(
                        [InlineKeyboardButton(f'ç¬¬{i + 1}è¡Œä¸‹ç§»', callback_data=f'ejfpaixu xiayi:{i + 1}:{pxuid}')])
                elif i == maxrow - 1:
                    keyboard.append(
                        [InlineKeyboardButton(f'ç¬¬{i + 1}è¡Œä¸Šç§»', callback_data=f'ejfpaixu shangyi:{i + 1}:{pxuid}')])
                else:
                    keyboard.append(
                        [InlineKeyboardButton(f'ç¬¬{i + 1}è¡Œä¸Šç§»', callback_data=f'ejfpaixu shangyi:{i + 1}:{pxuid}'),
                         InlineKeyboardButton(f'ç¬¬{i + 1}è¡Œä¸‹ç§»', callback_data=f'ejfpaixu xiayi:{i + 1}:{pxuid}')])
            keyboard.append([InlineKeyboardButton('âŒå…³é—­', callback_data=f'close {user_id}')])
            context.bot.send_message(chat_id=user_id, text=f'åˆ†ç±»: {fl_pro}',
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

    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}ä¿®æ”¹åˆ†ç±»å', callback_data=f'upspname {uid}'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å¢žäºŒçº§åˆ†ç±»', callback_data=f'newejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´äºŒçº§åˆ†ç±»æŽ’åº', callback_data=f'paixuejfl {uid}'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤äºŒçº§åˆ†ç±»', callback_data=f'delejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
    fstext = f'''
åˆ†ç±»: {fl_pro}
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
        context.bot.send_message(chat_id=user_id, text='æ²¡æœ‰æŒ‰é’®å­˜åœ¨')
    else:
        maxrow = max(count)
        if maxrow == 1:
            context.bot.send_message(chat_id=user_id, text='åªæœ‰ä¸€è¡ŒæŒ‰é’®æ— æ³•è°ƒæ•´')
        else:
            for i in range(0, maxrow):
                if i == 0:
                    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}ç¬¬{i + 1}è¡Œä¸‹ç§»', callback_data=f'flpxyd xiayi:{i + 1}')])
                elif i == maxrow - 1:
                    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}ç¬¬{i + 1}è¡Œä¸Šç§»', callback_data=f'flpxyd shangyi:{i + 1}')])
                else:
                    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}ç¬¬{i + 1}è¡Œä¸Šç§»', callback_data=f'flpxyd shangyi:{i + 1}'),
                                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}ç¬¬{i + 1}è¡Œä¸‹ç§»', callback_data=f'flpxyd xiayi:{i + 1}')])
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
            context.bot.send_message(chat_id=user_id, text='å•†å“ç®¡ç†', reply_markup=InlineKeyboardMarkup(keyboard))


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
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å»ºä¸€è¡Œ', callback_data='newfl'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´è¡ŒæŽ’åº', callback_data='paixufl'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ä¸€è¡Œ', callback_data='delfl')])
    context.bot.send_message(chat_id=user_id, text='å•†å“ç®¡ç†', reply_markup=InlineKeyboardMarkup(keyboard))


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
        context.bot.send_message(chat_id=user_id, text='æ²¡æœ‰æŒ‰é’®å­˜åœ¨')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            pxuid = ejfl.find_one({'uid': uid, 'row': i + 1})['nowuid']
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ç¬¬{i + 1}è¡Œ', callback_data=f'qrscejrow {i + 1}:{pxuid}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
        context.bot.send_message(chat_id=user_id, text=f'åˆ†ç±»: {fl_pro}', reply_markup=InlineKeyboardMarkup(keyboard))


def qrscejrow(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    query.answer()
    del_message(query.message)

    row = int(query.data.replace('qrscejrow ', '').split(':')[0])
    nowuid = query.data.replace('qrscejrow ', '').split(':')[1]
    uid = ejfl.find_one({'nowuid': nowuid})['uid']
    bot_id = context.bot.id
    ejfl.delete_many({'uid': uid, "row": row})
    max_list = list(ejfl.find({'row': {"$gt": row}}))
    for i in max_list:
        max_row = i['row']
        ejfl.update_many({'uid': uid, 'row': max_row}, {"$set": {"row": max_row - 1}})

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

    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_WELCOME}ä¿®æ”¹åˆ†ç±»å', callback_data=f'upspname {uid}'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å¢žäºŒçº§åˆ†ç±»', callback_data=f'newejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´äºŒçº§åˆ†ç±»æŽ’åº', callback_data=f'paixuejfl {uid}'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤äºŒçº§åˆ†ç±»', callback_data=f'delejfl {uid}')])
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
    fstext = f'''
åˆ†ç±»: {fl_pro}
    '''
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


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
        context.bot.send_message(chat_id=user_id, text='æ²¡æœ‰æŒ‰é’®å­˜åœ¨')
    else:
        maxrow = max(count)
        for i in range(0, maxrow):
            keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ç¬¬{i + 1}è¡Œ', callback_data=f'qrscflrow {i + 1}')])
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
        context.bot.send_message(chat_id=user_id, text='å•†å“ç®¡ç†', reply_markup=InlineKeyboardMarkup(keyboard))


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
    keyboard.append([InlineKeyboardButton(f'{MOOD_EMOJI_SPARKLE}æ–°å»ºä¸€è¡Œ', callback_data='newfl'),
                     InlineKeyboardButton(f'{MOOD_EMOJI_FAST}è°ƒæ•´è¡ŒæŽ’åº', callback_data='paixufl'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}åˆ é™¤ä¸€è¡Œ', callback_data='delfl')])
    context.bot.send_message(chat_id=user_id, text='å•†å“ç®¡ç†', reply_markup=InlineKeyboardMarkup(keyboard))


def backzcd(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    bot_id = context.bot.id
    user_id = query.from_user.id
    keylist = list(fenlei.find({}, sort=[('row', 1)]))
    keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], []]
    for i in keylist:
        uid = i['uid']
        projectname = i['projectname']

        row = i['row']
        hsl = 0
        for j in list(ejfl.find({'uid': uid})):
            nowuid = j['nowuid']
            hsl += len(list(hb.find({'nowuid': nowuid, 'state': 0})))
        keyboard[row - 1].append(InlineKeyboardButton(f'{projectname}({hsl})', callback_data=f'catejflsp {uid}:{hsl}'))
    fstext = f'''
<b>ðŸ›’è¿™æ˜¯å•†å“åˆ—è¡¨  é€‰æ‹©ä½ éœ€è¦çš„å•†å“ï¼š

â—ï¸æ²¡ä½¿ç”¨è¿‡çš„æœ¬åº—å•†å“çš„ï¼Œè¯·å…ˆå°‘é‡è´­ä¹°æµ‹è¯•ï¼Œä»¥å…é€ æˆä¸å¿…è¦çš„äº‰æ‰§ï¼è°¢è°¢åˆä½œï¼

â—ï¸è´¦æˆ·æ”¾ä¹…éš¾å…ä¼šæ­»ï¼Œæœ‰å·®å¼‚ï¼Œè¯·è”ç³»å®¢æœå”®åŽï¼æœ›ç†è§£ï¼</b>
    '''
    keyboard.append([InlineKeyboardButton('âŒå…³é—­', callback_data=f'close {user_id}')])
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
    row = shangtext.find_one({'projectname': 'å……å€¼åœ°å€'}) or {}
    return str(row.get('text', '') or '').strip()


def get_text_config(projectname, default=''):
    row = shangtext.find_one({'projectname': projectname}) or {}
    value = row.get('text', default)
    if value is None:
        return default
    return value


def set_text_config(projectname, value):
    shangtext.update_one({'projectname': projectname}, {'$set': {'text': value}}, upsert=True)


def get_clone_price_decimal():
    raw = str(get_text_config('ä¸€é”®å…‹éš†ä»·æ ¼', '0') or '0').strip()
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


def notify_source_admins(context, text, reply_markup=None):
    for admin_id in get_source_admin_user_ids():
        try:
            context.bot.send_message(chat_id=admin_id, text=text, parse_mode='HTML', reply_markup=reply_markup,
                                     disable_web_page_preview=True)
        except Exception:
            continue


def build_clone_list_keyboard(user_id, page=0, page_size=8):
    rows = list(clone_instances.find({'state': {'$ne': 'deleted'}}, sort=[('created_at', -1)], skip=page * page_size, limit=page_size))
    keyboard = []
    for row in rows:
        bot_id = row.get('bot_id')
        bot_username = str(row.get('bot_username') or f'bot{bot_id}')
        requester_user_id = row.get('requester_user_id')
        keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}@{bot_username}', callback_data=f'cloneinfo {bot_id}')])
        if requester_user_id:
            keyboard[-1].append(InlineKeyboardButton(f'{ADMIN_EMOJI_USERLIST}{requester_user_id}', callback_data=f'cloneinfo {bot_id}'))

    total = clone_instances.count_documents({'state': {'$ne': 'deleted'}})
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f'{MOOD_EMOJI_SOFT}ä¸Šä¸€é¡µ', callback_data=f'clonelist {page - 1}'))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(f'{MOOD_EMOJI_FAST}ä¸‹ä¸€é¡µ', callback_data=f'clonelist {page + 1}'))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(f'{ADMIN_EMOJI_OKPAY}è®¾ç½®å…‹éš†ä»·æ ¼', callback_data='setcloneprice')])
    keyboard.append([InlineKeyboardButton('â¬…ï¸è¿”å›žä¸»ç•Œé¢', callback_data='backstart'),
                     InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å…³é—­', callback_data=f'close {user_id}')])
    return keyboard, total


def send_clone_success_notice(context, requester_user_id, result, fee_paid='0'):
    requester = user.find_one({'user_id': requester_user_id}) or {}
    requester_name = str(requester.get('fullname', '') or '').replace('<', '').replace('>', '') or str(requester_user_id)
    requester_username = str(requester.get('username', '') or '').strip()
    fee_text = format_clone_price(fee_paid)
    text = f'''
<b>ðŸ¤– æœ‰äººå…‹éš†äº†ä½ çš„æœºå™¨äºº</b>

å…‹éš†ç”¨æˆ·ï¼š<a href="tg://user?id={requester_user_id}">{requester_name}</a> @{requester_username}
æœºå™¨äººï¼š@{result['bot_username']}
ç®¡ç†å‘˜ï¼š<code>{requester_user_id}</code>
æ”¯ä»˜é‡‘é¢ï¼š<code>{fee_text} USDT</code>

ç›®å½•ï¼š<code>{result['clone_dir']}</code>
æ•°æ®åº“ï¼š<code>{result['db_name']}</code>
BotæœåŠ¡ï¼š<code>{result['service_name']}.service</code>
ç›‘å¬æœåŠ¡ï¼š<code>{result['listener_service_name']}.service</code>
    '''
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f'{ADMIN_EMOJI_CLONE}å…‹éš†åˆ—è¡¨', callback_data='clonelist 0')]])
    notify_source_admins(context, text, reply_markup=keyboard)


def remove_clone_instance(bot_id, deleted_by=None):
    record = clone_instances.find_one({'bot_id': str(bot_id), 'state': {'$ne': 'deleted'}})
    if record is None:
        raise RuntimeError('æœªæ‰¾åˆ°è¿™ä¸ªå…‹éš†å®žä¾‹')

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
        run_system_command(['systemctl', 'daemon-reload'], timeout=20)
    except Exception:
        pass

    clone_dir = str(record.get('clone_dir') or '').strip()
    if clone_dir:
        shutil.rmtree(clone_dir, ignore_errors=True)

    db_name = str(record.get('db_name') or '').strip()
    if db_name:
        try:
            teleclient.drop_database(db_name)
        except Exception:
            pass

    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    clone_instances.update_one({'_id': record['_id']}, {'$set': {'state': 'deleted', 'deleted_at': timer, 'deleted_by': deleted_by}})
    return record


def get_okpay_shop_id():
    return str(get_text_config('OKPayå•†æˆ·ID', OKPAY_SHOP_ID) or '').strip()


def get_okpay_shop_token():
    return str(get_text_config('OKPayToken', OKPAY_SHOP_TOKEN) or '').strip()


def get_okpay_name():
    return str(get_text_config('OKPayåç§°', OKPAY_NAME) or '').strip()


def get_okpay_bot_username(bot=None):
    if bot is not None:
        username = getattr(bot, 'username', '') or ''
        if username:
            return str(username).strip().lstrip('@')
    username = getattr(OKPAY_BOT, 'username', '') if OKPAY_BOT is not None else ''
    if username:
        return str(username).strip().lstrip('@')
    return str(get_text_config('OKPayæœºå™¨äººç”¨æˆ·å', OKPAY_BOT_USERNAME) or '').strip().lstrip('@')


def refresh_okpay_entry_status():
    enabled = bool(get_okpay_shop_id() and get_okpay_shop_token() and get_okpay_name())
    set_text_config('OKPayå…¥å£å¼€å¯', 1 if enabled else 0)
    return enabled


def okpay_entry_enabled():
    row = shangtext.find_one({'projectname': 'OKPayå…¥å£å¼€å¯'})
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


def allocate_trc20_pay_amount(base_amount, user_id):
    base = Decimal(str(base_amount)).quantize(Decimal('0.0001'))
    pending_amounts = set()
    for row in topup.find({'type': 'trc20', 'state': {'$ne': 1}}, {'pay_amount_text': 1}):
        pay_amount_text = row.get('pay_amount_text')
        if pay_amount_text:
            pending_amounts.add(str(pay_amount_text))

    start = (int(user_id) % 9000) + 1
    for offset in range(9000):
        suffix = ((start + offset - 1) % 9000) + 1
        pay_amount = (base + (Decimal(suffix) / Decimal('10000'))).quantize(Decimal('0.0001'))
        pay_amount_text = format_usdt_amount(pay_amount)
        if pay_amount_text not in pending_amounts:
            return pay_amount, pay_amount_text
    raise RuntimeError('å½“å‰å¾…æ”¯ä»˜TRC20è®¢å•è¿‡å¤šï¼Œè¯·ç¨åŽé‡è¯•')


def okpay_enabled():
    return bool(get_okpay_shop_id() and get_okpay_shop_token())


def okpay_sign(data):
    shop_id = get_okpay_shop_id()
    shop_token = get_okpay_shop_token()
    if not shop_id or not shop_token:
        raise RuntimeError('OKPayæœªé…ç½®ï¼Œè¯·å…ˆåœ¨åŽå°è®¾ç½®å•†æˆ·IDå’ŒToken')
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
        'name': f'{okpay_name}å……å€¼',
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
    if order.get('state') == 1:
        return True, 'already_paid'

    user_id = order['user_id']
    money = float(amount or order['money'])
    user_list = user.find_one({'user_id': user_id})
    if user_list is None:
        return False, 'user_not_found'

    now_money = standard_num(float(user_list.get('USDT', 0)) + money)
    now_money = float(now_money) if str(now_money).count('.') > 0 else int(now_money)
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    user.update_one({'user_id': user_id}, {'$set': {'USDT': now_money}})
    topup.update_one({'bianhao': unique_id}, {'$set': {
        'state': 1,
        'status': 1,
        'paid_timer': timer,
        'okpay_order_id': order_id,
        'pay_user_id': pay_user_id,
        'coin': coin,
        'paid_amount': money
    }})
    user_logging(unique_id, 'OKPayå……å€¼', user_id, money, timer)

    if OKPAY_BOT is not None:
        try:
            OKPAY_BOT.send_message(
                chat_id=user_id,
                text=f'<b>âœ… OKPayå……å€¼åˆ°è´¦ï¼š{money} {coin}\n\nðŸ’³ å½“å‰ä½™é¢ï¼š{now_money} USDT</b>',
                parse_mode='HTML'
            )
        except Exception as exc:
            print(f'OKPayåˆ°è´¦é€šçŸ¥å¤±è´¥: {exc}')
    return True, 'paid'


def okpay_normalize_check_deposit_result(result):
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, dict):
        data = result if isinstance(result, dict) else {}
    unique_id = data.get('unique_id') or result.get('unique_id') if isinstance(result, dict) else None
    order_id = data.get('order_id') or result.get('order_id') if isinstance(result, dict) else None
    amount = data.get('amount') or result.get('amount') if isinstance(result, dict) else None
    status = str(data.get('status') or result.get('status') or '') if isinstance(result, dict) else ''
    return {
        'unique_id': unique_id,
        'order_id': order_id,
        'amount': amount,
        'status': status,
        'coin': 'USDT',
        'type': 'deposit',
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
        print('OKPayæœªé…ç½®ï¼Œè·³è¿‡å›žè°ƒæœåŠ¡')
        return
    if OKPAY_HTTPD is not None:
        return
    try:
        OKPAY_HTTPD = ThreadingHTTPServer((OKPAY_CALLBACK_HOST, OKPAY_CALLBACK_PORT), OkpayCallbackHandler)
        t = threading.Thread(target=OKPAY_HTTPD.serve_forever, daemon=True)
        t.start()
        print(f'OKPayå›žè°ƒæœåŠ¡å·²å¯åŠ¨: {OKPAY_CALLBACK_HOST}:{OKPAY_CALLBACK_PORT}')
    except Exception as exc:
        print(f'OKPayå›žè°ƒæœåŠ¡å¯åŠ¨å¤±è´¥: {exc}')


async def on_post_init(application):
    global APP_EVENT_LOOP
    APP_EVENT_LOOP = asyncio.get_running_loop()
    start_okpay_callback_server(SyncTelegramProxy(application.bot, lambda: APP_EVENT_LOOP))


def create_trc20_deposit_order(context, user_id, amount):
    trc20 = get_trc20_address()
    if not is_valid_trc20_address(trc20):
        context.bot.send_message(chat_id=user_id, text='TRC20å……å€¼åœ°å€æœªæ­£ç¡®é…ç½®ï¼Œè¯·å…ˆè”ç³»ç®¡ç†å‘˜è®¾ç½®æœ‰æ•ˆåœ°å€')
        return

    amount = Decimal(str(amount)).quantize(Decimal('0.0001'))
    if amount <= 0:
        context.bot.send_message(chat_id=user_id, text='å……å€¼é‡‘é¢å¿…é¡»å¤§äºŽ0')
        return

    created_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    deadline_time = (datetime.datetime.strptime(created_time, '%Y-%m-%d %H:%M:%S') + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
    bianhao = 'TRC20' + time.strftime('%Y%m%d%H%M%S', time.localtime()) + str(user_id)
    topup.delete_many({'user_id': user_id, 'state': {'$ne': 1}})

    reserved_id = None
    pay_amount = None
    pay_amount_text = None
    for _ in range(30):
        try:
            pay_amount, pay_amount_text = allocate_trc20_pay_amount(amount, user_id)
        except Exception as exc:
            context.bot.send_message(chat_id=user_id, text=f'åˆ›å»ºTRC20å……å€¼è®¢å•å¤±è´¥ï¼š{exc}')
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
                'message_id': 0,
                'message_kind': 'photo',
                'type': 'trc20',
                'state': 0,
                'status': 0,
                'to_address': trc20,
                'coin': 'USDT'
            })
            reserved_id = result.inserted_id
            break
        except DuplicateKeyError:
            continue

    if reserved_id is None:
        context.bot.send_message(chat_id=user_id, text='å½“å‰TRC20è®¢å•åˆ›å»ºäººæ•°è¾ƒå¤šï¼Œè¯·ç¨åŽé‡è¯•')
        return

    caption = f'''
<b>[emoji:6323075330189826977:ðŸ˜ƒ] å……å€¼è¯¦æƒ…</b>

[emoji:5350486389806868244:âœ…] å”¯ä¸€æ”¶æ¬¾åœ°å€ï¼š<code>{trc20}</code>
ï¼ˆæŽ¨èä½¿ç”¨æ‰«ç è½¬è´¦æ›´åŠ å®‰å…¨ ç‚¹å‡»ä¸Šæ–¹åœ°å€å³å¯å¿«é€Ÿå¤åˆ¶ç²˜è´´ï¼‰

[emoji:4965219701572503640:ðŸ’°] å®žé™…æ”¯ä»˜é‡‘é¢ï¼š<code>{pay_amount_text} USDT</code>
ï¼ˆ[emoji:5416117059207572332:âž¡ï¸] ç‚¹å‡»ä¸Šæ–¹é‡‘é¢å¯å¿«é€Ÿå¤åˆ¶ç²˜è´´ï¼‰

[emoji:5370715226209525171:ðŸ”‹]å……å€¼è®¢å•åˆ›å»ºæ—¶é—´ï¼š{created_time}
[emoji:5370688996844249600:ðŸª«]è½¬è´¦æœ€åŽæˆªæ­¢æ—¶é—´ï¼š{deadline_time}

â—ï¸è¯·ä¸€å®šæŒ‰ç…§é‡‘é¢åŽé¢å°æ•°ç‚¹è½¬è´¦ï¼Œå¦åˆ™æ— æ³•è‡ªåŠ¨åˆ°è´¦
â—ï¸ä»˜æ¬¾å‰è¯·å†æ¬¡æ ¸å¯¹åœ°å€ä¸Žé‡‘é¢ï¼Œé¿å…è½¬é”™
    '''
    keyboard = [
        [InlineKeyboardButton('âŒå–æ¶ˆè®¢å•', callback_data=f'qxdingdan {user_id}')]
    ]

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
        context.bot.send_message(chat_id=user_id, text=f'åˆ›å»ºTRC20å……å€¼è®¢å•å¤±è´¥ï¼š{exc}')
        return

    topup.update_one({'_id': reserved_id}, {'$set': {'message_id': message_id.message_id}})


def create_okpay_deposit_order(context, user_id, amount):
    if not refresh_okpay_entry_status():
        context.bot.send_message(chat_id=user_id, text='OKPayæœªé…ç½®ï¼Œè¯·å…ˆè”ç³»ç®¡ç†å‘˜åœ¨åŽå°é…ç½®å•†æˆ·IDã€Token å’Œ åç§°')
        return

    amount = standard_num(amount)
    amount = float(amount) if str(amount).count('.') > 0 else int(amount)
    if float(amount) <= 0:
        context.bot.send_message(chat_id=user_id, text='å……å€¼é‡‘é¢å¿…é¡»å¤§äºŽ0')
        return

    topup.delete_many({'user_id': user_id, 'state': {'$ne': 1}})
    bianhao = 'OKPAY' + time.strftime('%Y%m%d%H%M%S', time.localtime()) + str(user_id)
    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    try:
        result = okpay_pay_link(bianhao, amount, 'USDT', bot=context.bot)
    except Exception as exc:
        context.bot.send_message(chat_id=user_id, text=f'åˆ›å»ºOKPayå……å€¼è®¢å•å¤±è´¥ï¼š{exc}')
        return

    if isinstance(result, dict) and result.get('status') == 'error':
        msg = str(result.get('msg') or '')
        if 'callback_url' in msg and ('éªŒè¯å¤±è´¥' in msg or 'å®‰å…¨é£Žé™©' in msg):
            try:
                result = okpay_pay_link(bianhao, amount, 'USDT', include_callback=False, bot=context.bot)
            except Exception as exc:
                context.bot.send_message(chat_id=user_id, text=f'åˆ›å»ºOKPayå……å€¼è®¢å•å¤±è´¥ï¼š{exc}')
                return

    data = result.get('data') or {}
    pay_url = data.get('pay_url') or result.get('pay_url')
    okpay_order_id = data.get('order_id') or result.get('order_id')
    if not pay_url:
        context.bot.send_message(chat_id=user_id, text=f'åˆ›å»ºOKPayå……å€¼è®¢å•å¤±è´¥ï¼š{result}')
        return

    text = f'''
<b>OKPayå……å€¼è®¢å•å·²åˆ›å»º</b>

è®¢å•å·ï¼š<code>{bianhao}</code>
å……å€¼é‡‘é¢ï¼š<code>{amount} USDT</code>

è¯·ç‚¹å‡»ä¸‹é¢æŒ‰é’®å®Œæˆæ”¯ä»˜ï¼Œæ”¯ä»˜æˆåŠŸåŽç³»ç»Ÿä¼šè‡ªåŠ¨åŠ ä½™é¢ã€‚
    '''
    keyboard = [
        [InlineKeyboardButton('ðŸ’³ æ‰“å¼€OKPayæ”¯ä»˜', url=pay_url)],
        [InlineKeyboardButton('âœ… æˆ‘å·²æ”¯ä»˜', callback_data=f'okpay_paid {bianhao}')],
        [InlineKeyboardButton('âŒå–æ¶ˆè®¢å•', callback_data=f'qxdingdan {user_id}')]
    ]
    message_id = context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    topup.insert_one({
        'bianhao': bianhao,
        'user_id': user_id,
        'money': float(amount),
        'timer': timer,
        'message_id': message_id.message_id,
        'type': 'okpay',
        'state': 0,
        'status': 0,
        'okpay_order_id': okpay_order_id,
        'pay_url': pay_url,
        'coin': 'USDT'
    })


def dabaohao(context, user_id, folder_names, leixing, nowuid, erjiprojectname, fstext, yssj):
    if leixing == 'åè®®å·':
        shijiancuo = int(time.time())
        zip_filename = f"./åè®®å·å‘è´§/{user_id}_{shijiancuo}.zip"
        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            # å°†æ¯ä¸ªæ–‡ä»¶åŠå…¶å†…å®¹æ·»åŠ åˆ° zip æ–‡ä»¶ä¸­ 
            for file_name in folder_names:
                # æ£€æŸ¥æ˜¯å¦å­˜åœ¨ä»¥ .json æˆ– .session ç»“å°¾çš„æ–‡ä»¶
                json_file_path = os.path.join(f"./åè®®å·/{nowuid}", file_name + ".json")
                session_file_path = os.path.join(f"./åè®®å·/{nowuid}", file_name + ".session")
                if os.path.exists(json_file_path):
                    zipf.write(json_file_path, os.path.basename(json_file_path))
                if os.path.exists(session_file_path):
                    zipf.write(session_file_path, os.path.basename(session_file_path))
        current_time = datetime.datetime.now()

        # å°†å½“å‰æ—¶é—´æ ¼å¼åŒ–ä¸ºå­—ç¬¦ä¸²
        formatted_time = current_time.strftime("%Y%m%d%H%M%S")

        # æ·»åŠ æ—¶é—´æˆ³
        timestamp = str(current_time.timestamp()).replace(".", "")

        # ç»„åˆç¼–å·
        bianhao = formatted_time + timestamp
        timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        goumaijilua('åè®®å·', bianhao, user_id, erjiprojectname, zip_filename, fstext, timer)
        # å‘é€ zip æ–‡ä»¶ç»™ç”¨æˆ·
        context.bot.send_document(chat_id=user_id, document=open(zip_filename, "rb"))
    elif leixing == 'ç›´ç™»å·':
        shijiancuo = int(time.time())
        zip_filename = f"./å‘è´§/{user_id}_{shijiancuo}.zip"
        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            # å°†æ¯ä¸ªæ–‡ä»¶å¤¹åŠå…¶å†…å®¹æ·»åŠ åˆ° zip æ–‡ä»¶ä¸­
            for folder_name in folder_names:
                full_folder_path = os.path.join(f"./å·åŒ…/{nowuid}", folder_name)
                if os.path.exists(full_folder_path):
                    # æ·»åŠ æ–‡ä»¶å¤¹åŠå…¶å†…å®¹
                    for root, dirs, files in os.walk(full_folder_path):
                        for file in files:
                            file_path = os.path.join(root, file)
                            # ä½¿ç”¨ç›¸å¯¹è·¯å¾„åœ¨åŽ‹ç¼©åŒ…ä¸­æ·»åŠ æ–‡ä»¶ï¼Œå¹¶è®¾ç½®åŽ‹ç¼©åŒ…å†…éƒ¨çš„è·¯å¾„
                            zipf.write(file_path,
                                       os.path.join(folder_name, os.path.relpath(file_path, full_folder_path)))
                else:
                    # update.message.reply_text(f"æ–‡ä»¶å¤¹ '{folder_name}' ä¸å­˜åœ¨ï¼")
                    pass

        # å‘é€ zip æ–‡ä»¶ç»™ç”¨æˆ·

        folder_names = '\n'.join(folder_names)

        current_time = datetime.datetime.now()

        # å°†å½“å‰æ—¶é—´æ ¼å¼åŒ–ä¸ºå­—ç¬¦ä¸²
        formatted_time = current_time.strftime("%Y%m%d%H%M%S")

        # æ·»åŠ æ—¶é—´æˆ³
        timestamp = str(current_time.timestamp()).replace(".", "")

        # ç»„åˆç¼–å·
        bianhao = formatted_time + timestamp
        timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        goumaijilua('ç›´ç™»å·', bianhao, user_id, erjiprojectname, zip_filename, fstext, timer)

        context.bot.send_document(chat_id=user_id, document=open(zip_filename, "rb"))


def qrgaimai(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    bot_id = context.bot.id
    user_id = query.from_user.id
    fullname = query.from_user.full_name.replace('<', '').replace('>', '')
    username = query.from_user.username
    data = query.data.replace('qrgaimai ', '')
    nowuid = data.split(':')[0]
    gmsl = int(data.split(':')[1])
    zxymoney = float(data.split(':')[2])
    user_list = user.find_one({'user_id': user_id})
    USDT = user_list['USDT']
    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
    if kc < gmsl:
        context.bot.send_message(chat_id=user_id, text='å½“å‰åº“å­˜ä¸è¶³')
        return
    if zxymoney == 0:
        return
    keyboard = [[InlineKeyboardButton('âœ…å·²è¯»ï¼ˆç‚¹å‡»é”€æ¯æ­¤æ¶ˆæ¯ï¼‰', callback_data=f'close {user_id}')]]
    if USDT >= zxymoney:
        now_price = standard_num(float(USDT) - float(zxymoney))
        now_price = float(now_price) if str((now_price)).count('.') > 0 else int(standard_num(now_price))
        
        ejfl_list = ejfl.find_one({'nowuid': nowuid})
        
        fhtype = hb.find_one({'nowuid': nowuid})['leixing']
        projectname = ejfl_list['projectname']
        erjiprojectname = ejfl_list['projectname']
        yijiid = ejfl_list['uid']
        yiji_list = fenlei.find_one({'uid': yijiid})
        yijiprojectname = yiji_list['projectname']
        fstext = ejfl_list['text']
        if fhtype == 'åè®®å·':
            zgje = user_list['zgje']
            zgsl = user_list['zgsl']
            user.update_one({'user_id': user_id},
                            {"$set": {'USDT': now_price, 'zgje': zgje + zxymoney, 'zgsl': zgsl + gmsl}})
            user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
            del_message(query.message)
            # for j in list(hb.find({"nowuid": nowuid,'state': 0},limit=gmsl)):
            #     projectname = j['projectname']
            #     hbid = j['hbid']
            #     timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())

            #     hb.update_one({'hbid': hbid},{"$set":{'state': 1, 'yssj': timer, 'gmid': user_id}})
            #     folder_names.append(projectname)

            query_condition = {"nowuid": nowuid, "state": 0}

            pipeline = [
                {"$match": query_condition},
                {"$limit": gmsl}
            ]
            cursor = hb.aggregate(pipeline)
            document_ids = [doc['_id'] for doc in cursor]
            cursor = hb.aggregate(pipeline)
            folder_names = [doc['projectname'] for doc in cursor]
            
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            update_data = {"$set": {'state': 1, 'yssj': timer, 'gmid': user_id}}
            hb.update_many({"_id": {"$in": document_ids}}, update_data) 

 
            # timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            # update_data = {"$set": {'state': 1, 'yssj': timer, 'gmid': user_id}}

            # hb.update_many(query_condition, update_data, limit=gmsl)

            context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML', disable_web_page_preview=True,
                                     reply_markup=InlineKeyboardMarkup(keyboard))
            fstext = f'''
ç”¨æˆ·: <a href="tg://user?id={user_id}">{fullname}</a> @{username}
ç”¨æˆ·ID: <code>{user_id}</code>
è´­ä¹°å•†å“: {yijiprojectname}/{erjiprojectname}
è´­ä¹°æ•°é‡: {gmsl}
è´­ä¹°é‡‘é¢: {zxymoney}
            '''
            for i in list(user.find({"state": '4'})):
                try:
                    context.bot.send_message(chat_id=i['user_id'], text=fstext, parse_mode='HTML')
                except:
                    pass

            Timer(1, dabaohao,
                  args=[context, user_id, folder_names, 'åè®®å·', nowuid, erjiprojectname, fstext, timer]).start()
            # shijiancuo = int(time.time())
            # zip_filename = f"./åè®®å·å‘è´§/{user_id}_{shijiancuo}.zip"
            # with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            #     # å°†æ¯ä¸ªæ–‡ä»¶åŠå…¶å†…å®¹æ·»åŠ åˆ° zip æ–‡ä»¶ä¸­
            #     for file_name in folder_names:
            #         # æ£€æŸ¥æ˜¯å¦å­˜åœ¨ä»¥ .json æˆ– .session ç»“å°¾çš„æ–‡ä»¶
            #         json_file_path = os.path.join(f"./åè®®å·/{nowuid}", file_name + ".json")
            #         session_file_path = os.path.join(f"./åè®®å·/{nowuid}", file_name + ".session")
            #         if os.path.exists(json_file_path):
            #             zipf.write(json_file_path, os.path.basename(json_file_path))
            #         if os.path.exists(session_file_path):
            #             zipf.write(session_file_path, os.path.basename(session_file_path))
            # current_time = datetime.datetime.now()

            # # å°†å½“å‰æ—¶é—´æ ¼å¼åŒ–ä¸ºå­—ç¬¦ä¸²
            # formatted_time = current_time.strftime("%Y%m%d%H%M%S")

            # # æ·»åŠ æ—¶é—´æˆ³
            # timestamp = str(current_time.timestamp()).replace(".", "")

            # # ç»„åˆç¼–å·
            # bianhao = formatted_time + timestamp
            # timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            # goumaijilua('åè®®å·', bianhao, user_id, erjiprojectname,zip_filename,fstext, timer)
            # # å‘é€ zip æ–‡ä»¶ç»™ç”¨æˆ·
            # query.message.reply_document(open(zip_filename, "rb"))



        elif fhtype == 'è°·æ­Œ':
            zgje = user_list['zgje']
            zgsl = user_list['zgsl']
            user.update_one({'user_id': user_id},
                            {"$set": {'USDT': now_price, 'zgje': zgje + zxymoney, 'zgsl': zgsl + gmsl}})
            user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
            del_message(query.message)

            context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML', disable_web_page_preview=True,
                                     reply_markup=InlineKeyboardMarkup(keyboard))
            folder_names = []
            for j in list(hb.find({"nowuid": nowuid, 'state': 0, 'leixing': 'è°·æ­Œ'}, limit=gmsl)):
                projectname = j['projectname']
                hbid = j['hbid']
                timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                hb.update_one({'hbid': hbid}, {"$set": {'state': 1, 'yssj': timer, 'gmid': user_id}})
                data = j['data']
                us1 = data['è´¦æˆ·']
                us2 = data['å¯†ç ']
                us3 = data['å­é‚®ä»¶']
                fste23xt = f'è´¦æˆ·: {us1}\nå¯†ç : {us2}\nå­é‚®ä»¶: {us3}\n'
                folder_names.append(fste23xt)

            folder_names = '\n'.join(folder_names)

            shijiancuo = int(time.time())
            zip_filename = f"./è°·æ­Œå‘è´§/{user_id}_{shijiancuo}.txt"
            with open(zip_filename, "w") as f:
                f.write(folder_names)
            current_time = datetime.datetime.now()

            # å°†å½“å‰æ—¶é—´æ ¼å¼åŒ–ä¸ºå­—ç¬¦ä¸²
            formatted_time = current_time.strftime("%Y%m%d%H%M%S")

            # æ·»åŠ æ—¶é—´æˆ³
            timestamp = str(current_time.timestamp()).replace(".", "")

            # ç»„åˆç¼–å·
            bianhao = formatted_time + timestamp
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            goumaijilua('è°·æ­Œ', bianhao, user_id, erjiprojectname, zip_filename, fstext, timer)

            query.message.reply_document(open(zip_filename, "rb"))

            fstext = f'''
ç”¨æˆ·: <a href="tg://user?id={user_id}">{fullname}</a> @{username}
ç”¨æˆ·ID: <code>{user_id}</code>
è´­ä¹°å•†å“: {yijiprojectname}/{erjiprojectname}
è´­ä¹°æ•°é‡: {gmsl}
è´­ä¹°é‡‘é¢: {zxymoney}
            '''
            for i in list(user.find({"state": '4'})):
                try:
                    context.bot.send_message(chat_id=i['user_id'], text=fstext, parse_mode='HTML')
                except:
                    pass


        elif fhtype == 'API':
            zgje = user_list['zgje']
            zgsl = user_list['zgsl']
            user.update_one({'user_id': user_id},
                            {"$set": {'USDT': now_price, 'zgje': zgje + zxymoney, 'zgsl': zgsl + gmsl}})
            user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
            del_message(query.message)

            context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML', disable_web_page_preview=True,
                                     reply_markup=InlineKeyboardMarkup(keyboard))
            folder_names = []
            for j in list(hb.find({"nowuid": nowuid, 'state': 0}, limit=gmsl)):
                projectname = j['projectname']
                hbid = j['hbid']
                timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                hb.update_one({'hbid': hbid}, {"$set": {'state': 1, 'yssj': timer, 'gmid': user_id}})
                folder_names.append(projectname)

            shijiancuo = int(time.time())

            zip_filename = f"./æ‰‹æœºæŽ¥ç å‘è´§/{user_id}_{shijiancuo}.txt"
            with open(zip_filename, "w") as f:
                for folder_name in folder_names:
                    f.write(folder_name + "\n")

            current_time = datetime.datetime.now()

            # å°†å½“å‰æ—¶é—´æ ¼å¼åŒ–ä¸ºå­—ç¬¦ä¸²
            formatted_time = current_time.strftime("%Y%m%d%H%M%S")

            # æ·»åŠ æ—¶é—´æˆ³
            timestamp = str(current_time.timestamp()).replace(".", "")

            # ç»„åˆç¼–å·
            bianhao = formatted_time + timestamp
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            goumaijilua('APIé“¾æŽ¥', bianhao, user_id, erjiprojectname, zip_filename, fstext, timer)

            query.message.reply_document(open(zip_filename, "rb"))

            fstext = f'''
ç”¨æˆ·: <a href="tg://user?id={user_id}">{fullname}</a> @{username}
ç”¨æˆ·ID: <code>{user_id}</code>
è´­ä¹°å•†å“: {yijiprojectname}/{erjiprojectname}
è´­ä¹°æ•°é‡: {gmsl}
è´­ä¹°é‡‘é¢: {zxymoney}
            '''
            for i in list(user.find({"state": '4'})):
                try:
                    context.bot.send_message(chat_id=i['user_id'], text=fstext, parse_mode='HTML')
                except:
                    pass
        elif fhtype == 'ä¼šå‘˜é“¾æŽ¥':
            zgje = user_list['zgje']
            zgsl = user_list['zgsl']
            user.update_one({'user_id': user_id},
                            {"$set": {'USDT': now_price, 'zgje': zgje + zxymoney, 'zgsl': zgsl + gmsl}})
            user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
            del_message(query.message)
            folder_names = []
            for j in list(hb.find({"nowuid": nowuid, 'state': 0}, limit=gmsl)):
                projectname = j['projectname']
                hbid = j['hbid']
                timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                hb.update_one({'hbid': hbid}, {"$set": {'state': 1, 'yssj': timer, 'gmid': user_id}})
                folder_names.append(projectname)

            context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML', disable_web_page_preview=True,
                                     reply_markup=InlineKeyboardMarkup(keyboard))

            folder_names = '\n'.join(folder_names)

            current_time = datetime.datetime.now()

            # å°†å½“å‰æ—¶é—´æ ¼å¼åŒ–ä¸ºå­—ç¬¦ä¸²
            formatted_time = current_time.strftime("%Y%m%d%H%M%S")

            # æ·»åŠ æ—¶é—´æˆ³
            timestamp = str(current_time.timestamp()).replace(".", "")

            # ç»„åˆç¼–å·
            bianhao = formatted_time + timestamp
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            goumaijilua('ä¼šå‘˜é“¾æŽ¥', bianhao, user_id, erjiprojectname, folder_names, fstext, timer)

            context.bot.send_message(chat_id=user_id, text=folder_names, disable_web_page_preview=True)

            fstext = f'''
ç”¨æˆ·: <a href="tg://user?id={user_id}">{fullname}</a> @{username}
ç”¨æˆ·ID: <code>{user_id}</code>
è´­ä¹°å•†å“: {yijiprojectname}/{erjiprojectname}
è´­ä¹°æ•°é‡: {gmsl}
è´­ä¹°é‡‘é¢: {zxymoney}
            '''
            for i in list(user.find({"state": '4'})):
                try:
                    context.bot.send_message(chat_id=i['user_id'], text=fstext, parse_mode='HTML')
                except:
                    pass
        else:
            zgje = user_list['zgje']
            zgsl = user_list['zgsl']
            user.update_one({'user_id': user_id},
                            {"$set": {'USDT': now_price, 'zgje': zgje + zxymoney, 'zgsl': zgsl + gmsl}})
            user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
            del_message(query.message)

            # folder_names = []
            # for j in list(hb.find({"nowuid": nowuid, 'state': 0}, limit=gmsl)):
            #     projectname = j['projectname']
            #     hbid = j['hbid']
            #     timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            #     hb.update_one({'hbid': hbid}, {"$set": {'state': 1, 'yssj': timer, 'gmid': user_id}})
            #     folder_names.append(projectname)

            query_condition = {"nowuid": nowuid, "state": 0}

            pipeline = [
                {"$match": query_condition},
                {"$limit": gmsl}
            ]
            cursor = hb.aggregate(pipeline)
            document_ids = [doc['_id'] for doc in cursor]
            cursor = hb.aggregate(pipeline)
            folder_names = [doc['projectname'] for doc in cursor]
            
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            update_data = {"$set": {'state': 1, 'yssj': timer, 'gmid': user_id}}
            hb.update_many({"_id": {"$in": document_ids}}, update_data) 

 


            context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML', disable_web_page_preview=True,
                                     reply_markup=InlineKeyboardMarkup(keyboard))

            fstext = f'''
ç”¨æˆ·: <a href="tg://user?id={user_id}">{fullname}</a> @{username}
ç”¨æˆ·ID: <code>{user_id}</code>
è´­ä¹°å•†å“: {yijiprojectname}/{erjiprojectname}
è´­ä¹°æ•°é‡: {gmsl}
è´­ä¹°é‡‘é¢: {zxymoney}
            '''
            for i in list(user.find({"state": '4'})):
                try:
                    context.bot.send_message(chat_id=i['user_id'], text=fstext, parse_mode='HTML')
                except:
                    pass

            Timer(1, dabaohao,
                  args=[context, user_id, folder_names, 'ç›´ç™»å·', nowuid, erjiprojectname, fstext, timer]).start()
            # shijiancuo = int(time.time())
            # zip_filename = f"./å‘è´§/{user_id}_{shijiancuo}.zip"
            # with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            #     # å°†æ¯ä¸ªæ–‡ä»¶å¤¹åŠå…¶å†…å®¹æ·»åŠ åˆ° zip æ–‡ä»¶ä¸­
            #     for folder_name in folder_names:
            #         full_folder_path = os.path.join(f"./å·åŒ…/{nowuid}", folder_name)
            #         if os.path.exists(full_folder_path):
            #             # æ·»åŠ æ–‡ä»¶å¤¹åŠå…¶å†…å®¹
            #             for root, dirs, files in os.walk(full_folder_path):
            #                 for file in files:
            #                     file_path = os.path.join(root, file)
            #                     # ä½¿ç”¨ç›¸å¯¹è·¯å¾„åœ¨åŽ‹ç¼©åŒ…ä¸­æ·»åŠ æ–‡ä»¶ï¼Œå¹¶è®¾ç½®åŽ‹ç¼©åŒ…å†…éƒ¨çš„è·¯å¾„
            #                     zipf.write(file_path, os.path.join(folder_name, os.path.relpath(file_path, full_folder_path)))
            #         else:
            #             # update.message.reply_text(f"æ–‡ä»¶å¤¹ '{folder_name}' ä¸å­˜åœ¨ï¼")
            #             pass

            # # å‘é€ zip æ–‡ä»¶ç»™ç”¨æˆ·

            # folder_names = '\n'.join(folder_names)

            # current_time = datetime.datetime.now()

            # # å°†å½“å‰æ—¶é—´æ ¼å¼åŒ–ä¸ºå­—ç¬¦ä¸²
            # formatted_time = current_time.strftime("%Y%m%d%H%M%S")

            # # æ·»åŠ æ—¶é—´æˆ³
            # timestamp = str(current_time.timestamp()).replace(".", "")

            # # ç»„åˆç¼–å·
            # bianhao = formatted_time + timestamp
            # timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            # goumaijilua('ç›´ç™»å·', bianhao, user_id, erjiprojectname, zip_filename,fstext, timer)

            # query.message.reply_document(open(zip_filename, "rb"))




    else:
        context.bot.send_message(chat_id=user_id, text='âŒ ä½™é¢ä¸è¶³ï¼Œè¯·åŠæ—¶å……å€¼ï¼')
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
    if fhtype == 'åè®®å·':
        for j in list(hb.find({"nowuid": nowuid, 'state': 0})):
            projectname = j['projectname']
            hbid = j['hbid']
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            hb.delete_one({'hbid': hbid})
            folder_names.append(projectname)
        shijiancuo = int(time.time())
        zip_filename = f"./åè®®å·å‘è´§/{user_id}_{shijiancuo}.zip"
        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            # å°†æ¯ä¸ªæ–‡ä»¶åŠå…¶å†…å®¹æ·»åŠ åˆ° zip æ–‡ä»¶ä¸­
            for file_name in folder_names:
                # æ£€æŸ¥æ˜¯å¦å­˜åœ¨ä»¥ .json æˆ– .session ç»“å°¾çš„æ–‡ä»¶
                json_file_path = os.path.join(f"./åè®®å·/{nowuid}", file_name + ".json")
                session_file_path = os.path.join(f"./åè®®å·/{nowuid}", file_name + ".session")
                if os.path.exists(json_file_path):
                    zipf.write(json_file_path, os.path.basename(json_file_path))
                if os.path.exists(session_file_path):
                    zipf.write(session_file_path, os.path.basename(session_file_path))
        query.message.reply_document(open(zip_filename, "rb"))

    elif fhtype == 'API':
        for j in list(hb.find({"nowuid": nowuid, 'state': 0})):
            projectname = j['projectname']
            hbid = j['hbid']
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            hb.delete_one({'hbid': hbid})
            folder_names.append(projectname)

        shijiancuo = int(time.time())

        zip_filename = f"./æ‰‹æœºæŽ¥ç å‘è´§/{user_id}_{shijiancuo}.txt"
        with open(zip_filename, "w") as f:
            for folder_name in folder_names:
                f.write(folder_name + "\n")

        query.message.reply_document(open(zip_filename, "rb"))

    elif fhtype == 'è°·æ­Œ':
        for j in list(hb.find({"nowuid": nowuid, 'state': 0, 'leixing': 'è°·æ­Œ'})):
            projectname = j['projectname']
            hbid = j['hbid']
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            hb.update_one({'hbid': hbid}, {"$set": {'state': 1, 'yssj': timer, 'gmid': user_id}})
            data = j['data']
            us1 = data['è´¦æˆ·']
            us2 = data['å¯†ç ']
            us3 = data['å­é‚®ä»¶']
            fste23xt = f'login: {us1}\npassword: {us2}\nsubmail: {us3}\n'
            hb.delete_one({'hbid': hbid})
            folder_names.append(fste23xt)
        folder_names = '\n'.join(folder_names)
        shijiancuo = int(time.time())

        zip_filename = f"./è°·æ­Œå‘è´§/{user_id}_{shijiancuo}.txt"
        with open(zip_filename, "w") as f:

            f.write(folder_names)

        query.message.reply_document(open(zip_filename, "rb"))


    elif fhtype == 'ä¼šå‘˜é“¾æŽ¥':
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

        shijiancuo = int(time.time())
        zip_filename = f"./å‘è´§/{user_id}_{shijiancuo}.zip"
        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            # å°†æ¯ä¸ªæ–‡ä»¶å¤¹åŠå…¶å†…å®¹æ·»åŠ åˆ° zip æ–‡ä»¶ä¸­
            for folder_name in folder_names:
                full_folder_path = os.path.join(f"./å·åŒ…/{nowuid}", folder_name)
                if os.path.exists(full_folder_path):
                    # æ·»åŠ æ–‡ä»¶å¤¹åŠå…¶å†…å®¹
                    for root, dirs, files in os.walk(full_folder_path):
                        for file in files:
                            file_path = os.path.join(root, file)
                            # ä½¿ç”¨ç›¸å¯¹è·¯å¾„åœ¨åŽ‹ç¼©åŒ…ä¸­æ·»åŠ æ–‡ä»¶ï¼Œå¹¶è®¾ç½®åŽ‹ç¼©åŒ…å†…éƒ¨çš„è·¯å¾„
                            zipf.write(file_path,
                                       os.path.join(folder_name, os.path.relpath(file_path, full_folder_path)))
                else:
                    # update.message.reply_text(f"æ–‡ä»¶å¤¹ '{folder_name}' ä¸å­˜åœ¨ï¼")
                    pass

        query.message.reply_document(open(zip_filename, "rb"))

    ej_list = ejfl.find_one({'nowuid': nowuid})
    uid = ej_list['uid']
    ej_projectname = ej_list['projectname']
    money = ej_list['money']
    fl_pro = fenlei.find_one({'uid': uid})['projectname']
    keyboard = build_product_detail_keyboard(nowuid, uid, user_id)
    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
    ys = len(list(hb.find({'nowuid': nowuid, 'state': 1})))
    fstext = f'''
ä¸»åˆ†ç±»: {fl_pro}
äºŒçº§åˆ†ç±»: {ej_projectname}

ä»·æ ¼: {money}U
åº“å­˜: {kc}
å·²å”®: {ys}
    '''
    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


def qxdingdan(update: Update, context: CallbackContext):
    query = update.callback_query
    chat = query.message.chat
    query.answer()
    bot_id = context.bot.id
    chat_id = chat.id
    user_id = query.from_user.id

    topup.delete_one({'user_id': user_id, 'state': {'$ne': 1}})
    context.bot.delete_message(chat_id=query.from_user.id, message_id=query.message.message_id)


def okpay_paid(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer('æ­£åœ¨æ£€æŸ¥æ”¯ä»˜çŠ¶æ€ï¼Œè¯·ç¨å€™...')
    user_id = query.from_user.id
    unique_id = query.data.replace('okpay_paid ', '', 1).strip()
    order = topup.find_one({'bianhao': unique_id})
    if order is None or order.get('type') != 'okpay':
        context.bot.send_message(chat_id=user_id, text='æœªæ‰¾åˆ°å¯¹åº”çš„OKPayå……å€¼è®¢å•ï¼Œè¯·é‡æ–°åˆ›å»ºè®¢å•')
        return
    if order.get('user_id') != user_id:
        context.bot.send_message(chat_id=user_id, text='è¿™ç¬”è®¢å•ä¸å±žäºŽä½ ï¼Œæ— æ³•ä¸»åŠ¨æŸ¥å•')
        return
    if order.get('state') == 1:
        context.bot.send_message(chat_id=user_id, text='è¿™ç¬”OKPayè®¢å•å·²ç»åˆ°è´¦ï¼Œæ— éœ€é‡å¤æ£€æŸ¥')
        return

    try:
        ok, msg, result = okpay_check_and_credit(unique_id)
    except Exception as exc:
        context.bot.send_message(chat_id=user_id, text=f'æŸ¥è¯¢OKPayè®¢å•å¤±è´¥ï¼š{exc}')
        return

    if ok:
        keyboard = [[InlineKeyboardButton('âœ…å·²åˆ°è´¦ï¼ˆç‚¹å‡»å…³é—­ï¼‰', callback_data=f'close {user_id}')]]
        try:
            context.bot.edit_message_text(
                chat_id=user_id,
                message_id=query.message.message_id,
                text='âœ… OKPayè®¢å•å·²ç¡®è®¤æ”¯ä»˜ï¼Œä½™é¢å·²è‡ªåŠ¨åˆ°è´¦ã€‚',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            pass
        return

    if msg == 'already_paid':
        context.bot.send_message(chat_id=user_id, text='è¿™ç¬”OKPayè®¢å•å·²ç»åˆ°è´¦ï¼Œæ— éœ€é‡å¤æ£€æŸ¥')
        return

    context.bot.send_message(
        chat_id=user_id,
        text='æš‚æœªæŸ¥è¯¢åˆ°è¿™ç¬”OKPayè®¢å•å·²ä»˜æ¬¾ï¼Œè¯·ç¡®è®¤æ”¯ä»˜æˆåŠŸåŽç¨ç­‰å‡ ç§’å†ç‚¹ä¸€æ¬¡â€œæˆ‘å·²æ”¯ä»˜â€ã€‚'
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
        user_list = ensure_user_exists(user_id, username, fullname, lastname)
        creation_time = user_list['creation_time']
        state = user_list['state']
        sign = user_list['sign']
        USDT = user_list['USDT']
        zgje = user_list['zgje']
        zgsl = user_list['zgsl']
        raw_text = update.message.text or ''
        stored_text = get_message_storage_text(update.message) or raw_text
        text = get_message_match_text(update.message) or raw_text
        normalized_text = normalize_menu_text(text)
        zxh = update.message.text_html
        yyzt = shangtext.find_one({'projectname': 'è¥ä¸šçŠ¶æ€'})['text']
        if yyzt == 0:
            if state != '4':
                return

        get_key_list = list(get_key.find({}))
        get_prolist = []
        normalized_key_map = {}
        for i in get_key_list:
            projectname = i["projectname"]
            button_match_text = get_button_match_text(projectname)
            get_prolist.append(projectname)
            if button_match_text != projectname:
                get_prolist.append(button_match_text)
            normalized_key_map.setdefault(normalize_menu_text(projectname), i)
            normalized_key_map.setdefault(normalize_menu_text(button_match_text), i)
        if update.message.text:
            if (raw_text in get_prolist or text in get_prolist or normalized_text in normalized_key_map) and not should_preserve_sign_on_menu_match(sign):
                sign = 0
        if sign != 0:
            if update.message.text:

                if sign == 'addhb':
                    if is_number(text):

                        money = float(text) if text.count('.') > 0 else int(text)
                        if money < 1:
                            context.bot.send_message(chat_id=user_id, text='âš ï¸ è¾“å…¥é”™è¯¯ï¼Œæœ€å°‘é‡‘é¢ä¸èƒ½å°äºŽ1U')
                            return
                        if USDT >= money:
                            keyboard = [[InlineKeyboardButton('ðŸš«å–æ¶ˆ', callback_data=f'close {user_id}')]]
                            user.update_one({'user_id': user_id}, {"$set": {'sign': f'sethbsl {money}'}})
                            context.bot.send_message(chat_id=user_id, text='<b>ðŸ’¡ è¯·å›žå¤ä½ è¦å‘é€çš„çº¢åŒ…æ•°é‡</b>',
                                                     parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

                        else:
                            user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                            context.bot.send_message(chat_id=user_id, text='âš ï¸ æ“ä½œå¤±è´¥ï¼Œä½™é¢ä¸è¶³')
                    else:
                        context.bot.send_message(chat_id=user_id, text='âš ï¸ è¾“å…¥é”™è¯¯ï¼Œè¯·è¾“å…¥æ•°å­—ï¼')
                elif 'sethbsl' in sign:
                    money = sign.replace('sethbsl ', '')
                    money = float(money) if money.count('.') > 0 else int(money)

                    if is_number(text) and text.count('.') == 0:
                        hbsl = int(text)
                        if hbsl == 0:
                            context.bot.send_message(chat_id=user_id, text='çº¢åŒ…æ•°é‡ä¸èƒ½ä¸º0')
                            return
                        if hbsl > 100:
                            context.bot.send_message(chat_id=user_id, text='çº¢åŒ…æ•°é‡æœ€å¤§ä¸º100')
                            return
                        user_list = user.find_one({"user_id": user_id})
                        USDT = user_list['USDT']
                        if USDT < money:
                            user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                            context.bot.send_message(chat_id=user_id, text='âš ï¸ æ“ä½œå¤±è´¥ï¼Œä½™é¢ä¸è¶³')
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
                        fstext = f'''
ðŸ§§ <a href="tg://user?id={user_id}">{fullname}</a> å‘é€äº†ä¸€ä¸ªçº¢åŒ…
ðŸ’µæ€»é‡‘é¢:{money} USDTðŸ’° å‰©ä½™:{hbsl}/{hbsl}

âœ… çº¢åŒ…æ·»åŠ æˆåŠŸï¼Œè¯·ç‚¹å‡»æŒ‰é’®å‘é€
                        '''
                        keyboard = [
                            [InlineKeyboardButton('å‘é€çº¢åŒ…', switch_inline_query=f'redpacket {uid}')]
                        ]

                        context.bot.send_message(chat_id=user_id, text=fstext,
                                                 reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

                    else:
                        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                        context.bot.send_message(chat_id=user_id, text='âš ï¸ è¾“å…¥é”™è¯¯ï¼Œè¯·è¾“å…¥æ•°å­—ï¼')


                elif sign == 'startupdate':
                    welcome_text = stored_text or text
                    shangtext.update_one({"projectname": 'æ¬¢è¿Žè¯­'}, {"$set": {"text": welcome_text}})
                    shangtext.update_one({"projectname": 'æ¬¢è¿Žè¯­æ ·å¼'}, {"$set": {"text": pickle.dumps([])}})
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'å½“å‰æ¬¢è¿Žè¯­ä¸º:\n\n{welcome_text}')
                elif 'okzdycz' in sign:
                    if is_number(text):
                        del_message(update.message)
                        del_message_id = sign.replace('okzdycz ', '')
                        try:
                            context.bot.deleteMessage(chat_id=user_id, message_id=del_message_id)
                        except:
                            pass
                        money = float(text)
                        user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                        create_okpay_deposit_order(context, user_id, money)

                    else:
                        keyboard = [[InlineKeyboardButton('âŒå–æ¶ˆè¾“å…¥', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text='è¯·è¾“å…¥æ•°å­—',
                                                 reply_markup=InlineKeyboardMarkup(keyboard))
                elif 'zdycz' in sign:
                    if is_number(text):
                        del_message(update.message)
                        del_message_id = sign.replace('zdycz ', '')
                        try:
                            context.bot.deleteMessage(chat_id=user_id, message_id=del_message_id)
                        except:
                            pass
                        money = float(text)
                        user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                        create_trc20_deposit_order(context, user_id, money)

                    else:
                        keyboard = [[InlineKeyboardButton('âŒå–æ¶ˆè¾“å…¥', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text='è¯·è¾“å…¥æ•°å­—',
                                                 reply_markup=InlineKeyboardMarkup(keyboard))


                elif 'gmqq' in sign:
                    del_message(update.message)
                    data = sign.replace('gmqq ', '')
                    nowuid = data.split(':')[0]
                    del_message_id = data.split(':')[1]
                    try:
                        context.bot.deleteMessage(chat_id=user_id, message_id=del_message_id)
                    except:
                        pass

                    ejfl_list = ejfl.find_one({'nowuid': nowuid})
                    projectname = ejfl_list['projectname']
                    money = ejfl_list['money']
                    uid = ejfl_list['uid']
                    kc = len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                    clean_text = text.strip()
                    if clean_text.isdigit():
                        gmsl = int(clean_text)
                        if gmsl <= 0:
                            keyboard = [[InlineKeyboardButton('âŒå–æ¶ˆè´­ä¹°', callback_data=f'close {user_id}')]]
                            context.bot.send_message(chat_id=user_id, text='è´­ä¹°æ•°é‡åªèƒ½è¾“å…¥å¤§äºŽ0çš„æ•´æ•°',
                                                     reply_markup=InlineKeyboardMarkup(keyboard))
                            return

                        zxymoney = standard_num(gmsl * money)
                        zxymoney = float(zxymoney) if str((zxymoney)).count('.') > 0 else int(standard_num(zxymoney))
                        if kc < gmsl:
                            keyboard = [[InlineKeyboardButton('âŒå–æ¶ˆè´­ä¹°', callback_data=f'close {user_id}')]]
                            context.bot.send_message(chat_id=user_id, text='å½“å‰åº“å­˜ä¸è¶³ã€è¯·å†æ¬¡è¾“å…¥æ•°é‡ã€‘',
                                                     reply_markup=InlineKeyboardMarkup(keyboard))

                            return

                        fstext = f'''
<b>[emoji:5451937962629544243:ðŸ›]æ‚¨æ­£åœ¨è´­ä¹°ï¼š{projectname}

[emoji:5028746137645876535:ðŸ“ˆ] æ•°é‡ï¼š{gmsl}

ðŸ’°ä»·æ ¼ï¼š{zxymoney}

ðŸ‘›æ‚¨çš„ä½™é¢ï¼š{USDT}</b>
                        '''
                        keyboard = [
                            [InlineKeyboardButton('âŒå–æ¶ˆäº¤æ˜“', callback_data=f'close {user_id}'),
                             InlineKeyboardButton('ç¡®è®¤è´­ä¹°âœ…', callback_data=f'qrgaimai {nowuid}:{gmsl}:{zxymoney}')],
                            [InlineKeyboardButton('ðŸ ä¸»èœå•', callback_data='backzcd')]

                        ]
                        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                        context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                                 reply_markup=InlineKeyboardMarkup(keyboard))

                    else:
                        keyboard = [[InlineKeyboardButton('âŒå–æ¶ˆè´­ä¹°', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text='è´­ä¹°æ•°é‡åªèƒ½è¾“å…¥å¤§äºŽ0çš„æ•´æ•°ï¼Œä¸è´­ä¹°è¯·ç‚¹å‡»å–æ¶ˆ',
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
ä¸»åˆ†ç±»: {fl_pro}
äºŒçº§åˆ†ç±»: {ej_projectname}

ä»·æ ¼: {money}U
åº“å­˜: {kc}
å·²å”®: {ys}
                        '''
                        context.bot.send_message(chat_id=user_id, text=fstext,
                                                 reply_markup=InlineKeyboardMarkup(keyboard))

                    else:
                        context.bot.send_message(chat_id=user_id, text=f'è¯·è¾“å…¥æ•°å­—', parse_mode='HTML')

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

                    keyboard.append([InlineKeyboardButton('ä¿®æ”¹åˆ†ç±»å', callback_data=f'upspname {uid}'),
                                     InlineKeyboardButton('æ–°å¢žäºŒçº§åˆ†ç±»', callback_data=f'newejfl {uid}')])
                    keyboard.append([InlineKeyboardButton('è°ƒæ•´äºŒçº§åˆ†ç±»æŽ’åº', callback_data=f'paixuejfl {uid}'),
                                     InlineKeyboardButton('åˆ é™¤äºŒçº§åˆ†ç±»', callback_data=f'delejfl {uid}')])
                    keyboard.append([InlineKeyboardButton('âŒå…³é—­', callback_data=f'close {user_id}')])
                    fstext = f'''
åˆ†ç±»: {fl_pro}
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
                    keyboard.append([InlineKeyboardButton("æ–°å»ºä¸€è¡Œ", callback_data='newfl'),
                                     InlineKeyboardButton('è°ƒæ•´è¡ŒæŽ’åº', callback_data='paixufl'),
                                     InlineKeyboardButton('åˆ é™¤ä¸€è¡Œ', callback_data='delfl')])
                    context.bot.send_message(chat_id=user_id, text='å•†å“ç®¡ç†',
                                             reply_markup=InlineKeyboardMarkup(keyboard))
                elif sign == 'settrc20':
                    if not is_valid_trc20_address(text):
                        keyboard = [[InlineKeyboardButton('âŒå–æ¶ˆè¾“å…¥', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text='åœ°å€æ ¼å¼é”™è¯¯ï¼Œè¯·è¾“å…¥ä»¥ T å¼€å¤´ã€é•¿åº¦ 34 ä½çš„ TRC20 åœ°å€',
                                                 reply_markup=InlineKeyboardMarkup(keyboard))
                        return
                    shangtext.update_one({"projectname": 'å……å€¼åœ°å€'}, {"$set": {"text": text}})
                    img = qrcode.make(data=text)
                    with open(f'{text}.png', 'wb') as f:
                        img.save(f)
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'å½“å‰å……å€¼åœ°å€ä¸º: {text}', parse_mode='HTML')
                elif sign == 'setokpayid':
                    set_text_config('OKPayå•†æˆ·ID', text.strip())
                    if refresh_okpay_entry_status():
                        start_okpay_callback_server(SyncTelegramProxy(context.bot, lambda: APP_EVENT_LOOP))
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'OKPay å•†æˆ·ID å·²ä¿å­˜\n\n{build_okpay_config_text()}', parse_mode='HTML', reply_markup=InlineKeyboardMarkup(build_okpay_config_keyboard(user_id)))
                elif sign == 'setokpaytoken':
                    set_text_config('OKPayToken', text.strip())
                    if refresh_okpay_entry_status():
                        start_okpay_callback_server(SyncTelegramProxy(context.bot, lambda: APP_EVENT_LOOP))
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'OKPay Token å·²ä¿å­˜\n\n{build_okpay_config_text()}', parse_mode='HTML', reply_markup=InlineKeyboardMarkup(build_okpay_config_keyboard(user_id)))
                elif sign == 'setokpayname':
                    set_text_config('OKPayåç§°', text.strip())
                    if refresh_okpay_entry_status():
                        start_okpay_callback_server(SyncTelegramProxy(context.bot, lambda: APP_EVENT_LOOP))
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                    context.bot.send_message(chat_id=user_id, text=f'OKPay åç§°å·²ä¿å­˜\n\n{build_okpay_config_text()}', parse_mode='HTML', reply_markup=InlineKeyboardMarkup(build_okpay_config_keyboard(user_id)))
                elif sign == 'setcloneprice':
                    if not is_number(text):
                        keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆè¾“å…¥', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text='è¯·è¾“å…¥æ•°å­—ï¼Œè¾“å…¥ 0 è¡¨ç¤ºå…è´¹', reply_markup=InlineKeyboardMarkup(keyboard))
                        return
                    price = Decimal(str(text)).quantize(Decimal('0.01'))
                    if price < 0:
                        price = Decimal('0')
                    set_text_config('ä¸€é”®å…‹éš†ä»·æ ¼', format(price, 'f'))
                    user.update_one({'user_id': user_id}, {'$set': {'sign': 0}})
                    keyboard, total = build_clone_list_keyboard(user_id, 0)
                    text = f'<b>[emoji:5287684458881756303:ðŸ¤–] å…‹éš†åˆ—è¡¨</b>\n\nå½“å‰ä»˜è´¹ä»·æ ¼ï¼š<code>{format_clone_price(price)} USDT</code>\næ´»è·ƒå…‹éš†æ•°ï¼š<code>{total}</code>'
                    context.bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
                elif sign == 'clonebottoken':
                    if not can_use_clonebot(state):
                        user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                        context.bot.send_message(chat_id=user_id, text='å½“å‰æœªå¼€æ”¾ä¸€é”®å…‹éš†åŠŸèƒ½')
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
                        text='[emoji:5220195537520711716:âš¡ï¸] æ­£åœ¨å…‹éš†ä¸­ï¼Œè¯·ç¨ç­‰â€¦\n\n[emoji:5287684458881756303:ðŸ¤–] å·²æ”¶åˆ°æ–°çš„ Bot Tokenï¼Œæ­£åœ¨ä¸ºä½ åˆ›å»ºå¹¶å¯åŠ¨æ–° Botã€‚',
                        parse_mode='HTML'
                    )
                    try:
                        result = clone_bot_instance(text.strip(), user_id)
                    except Exception as exc:
                        keyboard = [[InlineKeyboardButton(f'{ADMIN_EMOJI_CLOSE}å–æ¶ˆè¾“å…¥', callback_data=f'close {user_id}')]]
                        context.bot.send_message(chat_id=user_id, text=f'ä¸€é”®å…‹éš†å¤±è´¥ï¼š{exc}',
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
[emoji:5312028599803460968:ðŸ†—] ä¸€é”®å…‹éš†æˆåŠŸ

[emoji:5287684458881756303:ðŸ¤–] æœºå™¨äººï¼š@{result['bot_username']}
[emoji:6321041414067068140:ðŸ‘¤] ç®¡ç†å‘˜ï¼š{user_id}
                    '''
                    context.bot.send_message(chat_id=user_id, text=clone_text, parse_mode='HTML')
                    send_clone_success_notice(context, user_id, result, fee_paid=(float(fee) if fee > 0 and not fee_exempt else 0))
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
                    keyboard.append([InlineKeyboardButton('æ–°å»ºä¸€è¡Œ', callback_data='newrow'),
                                     InlineKeyboardButton('åˆ é™¤ä¸€è¡Œ', callback_data='delrow'),
                                     InlineKeyboardButton('è°ƒæ•´è¡ŒæŽ’åº', callback_data='paixurow')])
                    keyboard.append([InlineKeyboardButton('ä¿®æ”¹æŒ‰é’®', callback_data='newkey')])
                    user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                    context.bot.send_message(chat_id=user_id, text='è‡ªå®šä¹‰æŒ‰é’®',
                                             reply_markup=InlineKeyboardMarkup(keyboard))
                elif 'settuwenset' in sign:
                    qudata = sign.replace('settuwenset ', '')
                    qudataall = qudata.split(':')
                    row = int(qudataall[0])
                    first = int(qudataall[1])
                    entities = update.message.entities
                    get_key.update_one({'Row': row, 'first': first}, {'$set': {'text': text}})
                    get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_id': ''}})
                    get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_type': 'text'}})
                    get_key.update_one({'Row': row, 'first': first}, {'$set': {'entities': pickle.dumps(entities)}})
                    user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                    message_id = context.bot.send_message(chat_id=user_id, text=text, entities=entities)
                    timer11 = Timer(3, del_message, args=[message_id])
                    timer11.start()
                elif 'setkeyboard' in sign:
                    qudata = sign.replace('setkeyboard ', '')
                    qudataall = qudata.split(':')
                    row = int(qudataall[0])
                    first = int(qudataall[1])
                    text = text.replace('ï½œ', '|').replace(' ', '')
                    keyboard = parse_urls(text)
                    dumped = pickle.dumps(keyboard)
                    try:
                        message_id = context.bot.send_message(chat_id=user_id, text=f'å°¾éšæŒ‰é’®è®¾ç½®',
                                                              reply_markup=InlineKeyboardMarkup(keyboard))
                        get_key.update_one({'Row': row, 'first': first}, {"$set": {'keyboard': dumped}})
                        get_key.update_one({'Row': row, 'first': first}, {"$set": {'key_text': text}})
                        timer11 = Timer(3, del_message, args=[message_id])
                        timer11.start()
                    except:
                        keyboard = [[InlineKeyboardButton('æ ¼å¼é…ç½®é”™è¯¯,è¯·æ£€æŸ¥', callback_data='ddd')]]
                        message_id = context.bot.send_message(chat_id=user_id, text='æ ¼å¼é…ç½®é”™è¯¯,è¯·æ£€æŸ¥',
                                                              reply_markup=InlineKeyboardMarkup(keyboard))
                        timer11 = Timer(3, del_message, args=[message_id])
                        timer11.start()
                    user.update_one({'user_id': user_id}, {"$set": {'sign': 0}})
                elif 'update_sysm' in sign:
                    nowuid = sign.replace('update_sysm ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']
                    ejfl.update_one({"nowuid": nowuid}, {"$set": {'sysm': zxh}})
                    fstext = f'''
æ–°çš„ä½¿ç”¨è¯´æ˜Žä¸º:
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
ä¸»åˆ†ç±»: {fl_pro}
äºŒçº§åˆ†ç±»: {ej_projectname}

ä»·æ ¼: {money}U
åº“å­˜: {kc}
å·²å”®: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))
                elif 'update_wbts' in sign:
                    nowuid = sign.replace('update_wbts ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']
                    ejfl.update_one({"nowuid": nowuid}, {"$set": {'text': zxh}})
                    fstext = f'''
æ–°çš„æç¤ºä¸º:
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
ä¸»åˆ†ç±»: {fl_pro}
äºŒçº§åˆ†ç±»: {ej_projectname}

ä»·æ ¼: {money}U
åº“å­˜: {kc}
å·²å”®: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))


                elif 'update_hy' in sign:
                    nowuid = sign.replace('update_hy ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']

                    text = text.split('\n')
                    count = 0
                    for i in text:
                        if 'https:' in i:
                            if hb.find_one({'nowuid': nowuid, 'projectname': i}) is None:
                                hbid = generate_24bit_uid()
                                shangchuanhaobao('ä¼šå‘˜é“¾æŽ¥',uid, nowuid, hbid, i, timer)
                                count += 1

                    update.message.reply_text(f'æœ¬æ¬¡ä¸Šä¼ äº†{count}ä¸ªé“¾æŽ¥')
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
ä¸»åˆ†ç±»: {fl_pro}
äºŒçº§åˆ†ç±»: {ej_projectname}

ä»·æ ¼: {money}U
åº“å­˜: {kc}
å·²å”®: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))

            elif update.message.document:
                if 'update_hb' in sign:
                    nowuid = sign.replace('update_hb ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']

                    file = update.message.document
                    # èŽ·å–æ–‡ä»¶å
                    filename = file.file_name

                    # èŽ·å–æ–‡ä»¶ID
                    file_id = file.file_id
                    # ä¸‹è½½æ–‡ä»¶
                    new_file = context.bot.get_file(file_id)
                    # å°†æ–‡ä»¶ä¿å­˜åˆ°æœ¬åœ°
                    new_file_path = f'./ä¸´æ—¶æ–‡ä»¶å¤¹/{filename}'
                    new_file.download(new_file_path)

                    context.bot.send_message(chat_id=user_id, text='ä¸Šä¼ ä¸­ï¼Œè¯·å‹¿é‡å¤æ“ä½œ')
                    # è§£åŽ‹ç¼©æ–‡ä»¶
                    count = 0
                    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    with zipfile.ZipFile(new_file_path, 'r') as zip_ref:
                        for file_info in zip_ref.infolist():
                            match = re.match(r'^([^/]+)/.*$', file_info.filename)
                            if match:
                                extracted_folder_name = match.group(1)

                                if hb.find_one({'nowuid': nowuid, 'projectname': extracted_folder_name}) is None:
                                    count += 1
                                    hbid = generate_24bit_uid()
                                    shangchuanhaobao('ç›´ç™»å·',uid, nowuid, hbid, extracted_folder_name, timer)
                            zip_ref.extract(file_info, f'å·åŒ…/{nowuid}')

                    update.message.reply_text(f'è§£åŽ‹å¹¶å¤„ç†å®Œæˆï¼æœ¬æ¬¡ä¸Šä¼ äº†{count}ä¸ªå·')
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
ä¸»åˆ†ç±»: {fl_pro}
äºŒçº§åˆ†ç±»: {ej_projectname}

ä»·æ ¼: {money}U
åº“å­˜: {kc}
å·²å”®: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))

                elif 'update_gg' in sign:
                    nowuid = sign.replace('update_gg ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']

                    file = update.message.document
                    # èŽ·å–æ–‡ä»¶å
                    filename = file.file_name

                    # èŽ·å–æ–‡ä»¶ID
                    file_id = file.file_id
                    # ä¸‹è½½æ–‡ä»¶
                    new_file = context.bot.get_file(file_id)
                    # å°†æ–‡ä»¶ä¿å­˜åˆ°æœ¬åœ°
                    new_file_path = f'./ä¸´æ—¶æ–‡ä»¶å¤¹/{filename}'
                    new_file.download(new_file_path)

                    context.bot.send_message(chat_id=user_id, text='ä¸Šä¼ ä¸­ï¼Œè¯·å‹¿é‡å¤æ“ä½œ')

                    with open(new_file_path, 'r', encoding='utf-8') as file:
                        link_list = file.read()

                    login = re.findall('login: (.*)', link_list)
                    password = re.findall('password: (.*)', link_list)
                    submail = re.findall('submail: (.*)', link_list)
                    # å°†åŒ¹é…ç»“æžœæ‰“åŒ…æˆå…ƒç»„åˆ—è¡¨
                    matches = list(zip(login, password, submail))

                    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    count = 0
                    for i in matches:
                        login = i[0]
                        password = i[1]
                        submail = i[2]
                        jihe12 = {'è´¦æˆ·': login, 'å¯†ç ': password, 'å­é‚®ä»¶': submail}
                        if hb.find_one({'nowuid': nowuid, 'projectname': login}) is None:
                            hbid = generate_24bit_uid()
                            shangchuanhaobao('è°·æ­Œ',uid, nowuid, hbid, login, timer)
                            hb.update_one({'hbid': hbid}, {"$set": {"leixing": 'è°·æ­Œ', 'data': jihe12}})
                            count += 1

                    update.message.reply_text(f'å¤„ç†å®Œæˆï¼æœ¬æ¬¡ä¸Šä¼ äº†{count}ä¸ªè°·æ­Œå·')
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
ä¸»åˆ†ç±»: {fl_pro}
äºŒçº§åˆ†ç±»: {ej_projectname}

ä»·æ ¼: {money}U
åº“å­˜: {kc}
å·²å”®: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))

                elif 'update_txt' in sign:
                    nowuid = sign.replace('update_txt ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']

                    file = update.message.document
                    # èŽ·å–æ–‡ä»¶å
                    filename = file.file_name

                    # èŽ·å–æ–‡ä»¶ID
                    file_id = file.file_id
                    # ä¸‹è½½æ–‡ä»¶
                    new_file = context.bot.get_file(file_id)
                    # å°†æ–‡ä»¶ä¿å­˜åˆ°æœ¬åœ°
                    new_file_path = f'./ä¸´æ—¶æ–‡ä»¶å¤¹/{filename}'
                    new_file.download(new_file_path)

                    context.bot.send_message(chat_id=user_id, text='ä¸Šä¼ ä¸­ï¼Œè¯·å‹¿é‡å¤æ“ä½œ')

                    link_list = []
                    with open(new_file_path, 'r', encoding='utf-8') as file:
                        # é€è¡Œè¯»å–æ–‡ä»¶å†…å®¹
                        for line in file:
                            # åŽ»é™¤æ¯è¡Œæœ«å°¾çš„æ¢è¡Œç¬¦å¹¶æ·»åŠ åˆ°åˆ—è¡¨ä¸­
                            link_list.append(line.strip())
                    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    count = 0
                    for i in link_list:
                        if hb.find_one({'nowuid': nowuid, 'projectname': i}) is None:
                            hbid = generate_24bit_uid()
                            shangchuanhaobao('API',uid, nowuid, hbid, i, timer)
                            count += 1

                    update.message.reply_text(f'å¤„ç†å®Œæˆï¼æœ¬æ¬¡ä¸Šä¼ äº†{count}ä¸ªapié“¾æŽ¥')
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
ä¸»åˆ†ç±»: {fl_pro}
äºŒçº§åˆ†ç±»: {ej_projectname}

ä»·æ ¼: {money}U
åº“å­˜: {kc}
å·²å”®: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))
                elif 'update_xyh' in sign:
                    nowuid = sign.replace('update_xyh ', '')
                    uid = ejfl.find_one({'nowuid': nowuid})['uid']

                    file = update.message.document
                    # èŽ·å–æ–‡ä»¶å
                    filename = file.file_name

                    # èŽ·å–æ–‡ä»¶ID
                    file_id = file.file_id
                    # ä¸‹è½½æ–‡ä»¶
                    new_file = context.bot.get_file(file_id)
                    # å°†æ–‡ä»¶ä¿å­˜åˆ°æœ¬åœ°
                    new_file_path = f'./ä¸´æ—¶æ–‡ä»¶å¤¹/{filename}'
                    new_file.download(new_file_path)

                    context.bot.send_message(chat_id=user_id, text='ä¸Šä¼ ä¸­ï¼Œè¯·å‹¿é‡å¤æ“ä½œ')
                    # è§£åŽ‹ç¼©æ–‡ä»¶
                    count = 0
                    tj_dict = {}
                    timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    with zipfile.ZipFile(new_file_path, 'r') as zip_ref:
                        for file_info in zip_ref.infolist():
                            filename = file_info.filename
                            if filename.endswith('.json') or filename.endswith('.session'):
                                # ä»…è§£åŽ‹ session æˆ–è€… json æ ¼å¼çš„æ–‡ä»¶
                                fli1 = filename.replace('.json', '').replace('.session', '')
                                if fli1 not in tj_dict.keys():

                                    hbid = generate_24bit_uid()
                                    if hb.find_one({'nowuid': nowuid, 'projectname': fli1}) is None:
                                        tj_dict[fli1] = 1
                                        shangchuanhaobao('åè®®å·',uid, nowuid, hbid, fli1, timer)

                                zip_ref.extract(member=file_info, path=f'åè®®å·/{nowuid}')
                                pass
                            else:
                                pass
                    for i in tj_dict:
                        count += 1

                    update.message.reply_text(f'è§£åŽ‹å¹¶å¤„ç†å®Œæˆï¼æœ¬æ¬¡ä¸Šä¼ äº†{count}ä¸ªåè®®å·')

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
ä¸»åˆ†ç±»: {fl_pro}
äºŒçº§åˆ†ç±»: {ej_projectname}

ä»·æ ¼: {money}U
åº“å­˜: {kc}
å·²å”®: {ys}
                    '''
                    context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))

            else:
                caption = update.message.caption
                entities = update.message.caption_entities

                if 'settuwenset' in sign:
                    qudata = sign.replace('settuwenset ', '')
                    qudataall = qudata.split(':')
                    row = int(qudataall[0])
                    first = int(qudataall[1])
                    if update.message.photo:
                        file = update.message.photo[-1].file_id
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'text': caption}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_id': file}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_type': 'photo'}})
                        user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'entities': pickle.dumps(entities)}})
                        message_id = context.bot.send_photo(chat_id=user_id, caption=caption, photo=file,
                                                            caption_entities=entities)
                        timer11 = Timer(3, del_message, args=[message_id])
                        timer11.start()
                    elif update.message.animation:
                        file = update.message.animation.file_id
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'text': caption}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_id': file}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_type': 'animation'}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'state': 1}})
                        user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'entities': pickle.dumps(entities)}})
                        message_id = context.bot.sendAnimation(chat_id=user_id, caption=caption, animation=file,
                                                               caption_entities=entities)
                        timer11 = Timer(3, del_message, args=[message_id])
                        timer11.start()
                    else:
                        file = update.message.video.file_id
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'text': caption}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_id': file}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'file_type': 'video'}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'state': 1}})
                        user.update_one({'user_id': user_id}, {"$set": {"sign": 0}})
                        get_key.update_one({'Row': row, 'first': first}, {'$set': {'entities': pickle.dumps(entities)}})
                        message_id = context.bot.sendVideo(chat_id=user_id, caption=caption, video=file,
                                                           caption_entities=entities)
                        timer11 = Timer(3, del_message, args=[message_id])
                        timer11.start()
        else:
            if text == 'å¼€å§‹è¥ä¸š':
                if state == '4':
                    shangtext.update_one({'projectname': 'è¥ä¸šçŠ¶æ€'}, {"$set": {"text": 1}})
                    context.bot.send_message(chat_id=user_id, text='å¼€å§‹è¥ä¸š')
            elif text == 'åœæ­¢è¥ä¸š':
                if state == '4':
                    shangtext.update_one({'projectname': 'è¥ä¸šçŠ¶æ€'}, {"$set": {"text": 0}})
                    context.bot.send_message(chat_id=user_id, text='åœæ­¢è¥ä¸š')

            key_list = get_key.find_one({"projectname": raw_text})
            if key_list is None and text != raw_text:
                key_list = get_key.find_one({"projectname": text})
            if key_list is None and normalized_text:
                key_list = normalized_key_map.get(normalized_text)
            if normalized_text in (normalize_menu_text('ðŸ¤–ä¸€é”®å…‹éš†åŒæ¬¾'), normalize_menu_text('ðŸ¤–ä¸€é”®å…‹éš†Bot')):
                del_message(update.message)
                send_clonebot_prompt(context, user_id)
            elif normalized_text == normalize_menu_text('ðŸ‘¤ä¸ªäººä¸­å¿ƒ'):
                del_message(update.message)
                if username is None:
                    username = fullname
                else:
                    username = f'<a href="https://t.me/{username}">{username}</a>'
                fstext = f'''
<b>[emoji:6321041414067068140:ðŸ‘¤] æ‚¨çš„ID:</b>  <code>{user_id}</code>
<b>[emoji:5287684458881756303:ðŸ¤–] æ‚¨çš„ç”¨æˆ·å:</b>  {username}
<b>[emoji:5217818964612108191:âœ¨] æ³¨å†Œæ—¥æœŸ:</b>  {creation_time}

<b>[emoji:5220064167356025824:â­ï¸] æ€»è´­æ•°é‡:</b>  {zgsl}

<b>[emoji:5028746137645876535:ðŸ“ˆ] æ€»è´­é‡‘é¢:</b>  {standard_num(zgje)} USDT

<b>[emoji:4972482444025398275:ðŸ‘›] æ‚¨çš„ä½™é¢:</b>  {USDT} USDT
                '''

                keyboard = [[InlineKeyboardButton('ðŸ›’è´­ä¹°è®°å½•', callback_data=f'gmaijilu {user_id}')],
                            [InlineKeyboardButton('å…³é—­', callback_data=f'close {user_id}')]]
                context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                         reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
            elif normalized_text == normalize_menu_text('ðŸ’¸æˆ‘è¦å……å€¼'):
                del_message(update.message)
                send_recharge_method_menu(context, user_id)

            elif 'çº¢åŒ…' in text:
                del_message(update.message)
                fstext = f'''
ä»Žä¸‹é¢çš„åˆ—è¡¨ä¸­é€‰æ‹©ä¸€ä¸ªçº¢åŒ…
                '''
                keyboard = [
                    [InlineKeyboardButton('â—¾ï¸è¿›è¡Œä¸­', callback_data='jxzhb'),
                     InlineKeyboardButton('å·²ç»“æŸ', callback_data='yjshb')],
                    [InlineKeyboardButton('âž•æ·»åŠ ', callback_data='addhb')],
                    [InlineKeyboardButton('å…³é—­', callback_data=f'close {user_id}')]
                ]
                context.bot.send_message(chat_id=user_id, text=fstext, reply_markup=InlineKeyboardMarkup(keyboard))

            elif normalized_text == normalize_menu_text('ðŸ›’å•†å“åˆ—è¡¨'):
                del_message(update.message)
                keylist = list(fenlei.find({}, sort=[('row', 1)]))
                keyboard = [[], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                            [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                            [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                            [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [],
                            [], [], [], [], []]
                for i in keylist:
                    uid = i['uid']
                    projectname = i['projectname']

                    row = i['row']
                    hsl = 0
                    for j in list(ejfl.find({'uid': uid})):
                        nowuid = j['nowuid']
                        hsl += len(list(hb.find({'nowuid': nowuid, 'state': 0})))
                    keyboard[row - 1].append(
                        InlineKeyboardButton(f'{projectname}({hsl})', callback_data=f'catejflsp {uid}:{hsl}'))
                fstext = f'''
<b>ðŸ›’è¿™æ˜¯å•†å“åˆ—è¡¨  é€‰æ‹©ä½ éœ€è¦çš„å•†å“ï¼š

â—ï¸æ²¡ä½¿ç”¨è¿‡çš„æœ¬åº—å•†å“çš„ï¼Œè¯·å…ˆå°‘é‡è´­ä¹°æµ‹è¯•ï¼Œä»¥å…é€ æˆä¸å¿…è¦çš„äº‰æ‰§ï¼è°¢è°¢åˆä½œï¼

â—ï¸è´¦æˆ·æ”¾ä¹…éš¾å…ä¼šæ­»ï¼Œæœ‰å·®å¼‚ï¼Œè¯·è”ç³»å®¢æœå”®åŽï¼æœ›ç†è§£ï¼</b>
                '''
                keyboard.append([InlineKeyboardButton('âŒå…³é—­', callback_data=f'close {user_id}')])
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
                    keyboard = [[InlineKeyboardButton("å…³é—­", callback_data=f'close {user_id}')]]
                    if context.bot.username in ['TelergamKFbot', 'Tclelgnam_bot']:
                        pass
                    else:
                        if print_text == '' and file_id == '':
                            context.bot.send_message(chat_id=user_id, text=text)
                        else:
                            if file_type == 'text':
                                message_id = context.bot.send_message(chat_id=user_id, text=print_text,
                                                                      reply_markup=InlineKeyboardMarkup(keyboard),
                                                                      entities=entities)
                            else:
                                if file_type == 'photo':
                                    message_id = context.bot.send_photo(chat_id=user_id, caption=print_text,
                                                                        photo=file_id,
                                                                        reply_markup=InlineKeyboardMarkup(keyboard),
                                                                        caption_entities=entities)
                                else:
                                    message_id = context.bot.sendAnimation(chat_id=user_id, caption=print_text,
                                                                           animation=file_id,
                                                                           reply_markup=InlineKeyboardMarkup(keyboard),
                                                                           caption_entities=entities)


def del_message(message):
    try:
        message.delete()
    except:
        pass


def standard_num(num):
    value = Decimal(str(num)).quantize(Decimal("0.01"))
    return value.to_integral() if value == value.to_integral() else value.normalize()


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
        quant123 = Decimal(str(quant)) / Decimal('1000000')
        today_money = abs(quant123.quantize(Decimal('0.0001')))
        pay_amount_text = format_usdt_amount(today_money)
        dj_list = topup.find_one(
            {'type': 'trc20', 'state': {'$ne': 1}, 'to_address': trc20, 'pay_amount_text': pay_amount_text},
            sort=[('timer', 1)]
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
            keyboard = [[InlineKeyboardButton("âœ…å·²è¯»ï¼ˆç‚¹å‡»é”€æ¯æ­¤æ¶ˆæ¯ï¼‰", callback_data=f'close {user_id}')]]
            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            order_id = dj_list['bianhao']

            user_logging(order_id, 'TRC20å……å€¼', user_id, float(today_money), timer)
            us_list = user.find_one({"user_id": user_id})
            user.update_one({'user_id': user_id}, {"$set": {'USDT': now_price}})
            topup.update_one({'_id': dj_list['_id']}, {'$set': {
                'state': 1,
                'status': 1,
                'paid_timer': timer,
                'paid_amount': float(today_money),
                'txid': txid,
                'from_address': from_address,
                'quant_raw': str(quant)
            }})
            text = f'''
<b>âœ… TRC20å……å€¼åˆ°è´¦</b>

è®¢å•å·ï¼š<code>{dj_list['bianhao']}</code>
åˆ°è´¦é‡‘é¢ï¼š<code>{pay_amount_text} USDT</code>
äº¤æ˜“å“ˆå¸Œï¼š<code>{txid}</code>

ðŸ’³ å½“å‰ä½™é¢ï¼š<code>{now_price} USDT</code>
            '''
            try:
                context.bot.edit_message_caption(chat_id=user_id, message_id=message_id, caption=text,
                                                 reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            except:
                pass
            us_firstname = us_list['fullname'].replace('<', '').replace('>', '')
            us_username = us_list['username']
            text = f'''
ç”¨æˆ·: <a href="tg://user?id={user_id}">{us_firstname}</a> @{us_username} TRC20å……å€¼æˆåŠŸ
åœ°å€: <code>{from_address}</code>
å……å€¼: {pay_amount_text} USDT
<a href="https://tronscan.org/#/transaction/{txid}">å……å€¼è¯¦ç»†</a>
            '''
            for us in list(user.find({'state': '4'})):
                try:
                    context.bot.send_message(chat_id=us['user_id'], text=text, parse_mode='HTML',
                                             disable_web_page_preview=True)
                except:
                    continue
            qukuai.update_one({'txid': txid}, {"$set": {"state": 1, 'match_order': dj_list['bianhao'], 'match_user_id': user_id}})
        else:
            qukuai.update_one({'txid': txid}, {"$set": {"state": 2, 'reason': 'order_not_found'}})


def jianceguoqi(context: CallbackContext):
    while 1:
        for i in topup.find({'state': {'$ne': 1}}):
            timer = i['timer']
            user_id = i['user_id']
            message_id = i['message_id']
            dt = datetime.datetime.strptime(timer, '%Y-%m-%d %H:%M:%S')
            new_dt = dt + timedelta(minutes=10)
            new_time_str = new_dt.strftime('%Y-%m-%d %H:%M:%S')

            keyboard = [[InlineKeyboardButton("âœ…å·²è¯»ï¼ˆç‚¹å‡»é”€æ¯æ­¤æ¶ˆæ¯ï¼‰", callback_data=f'close {user_id}')]]

            timer = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            if timer >= new_time_str:
                try:
                    if i.get('type') == 'okpay':
                        context.bot.edit_message_text(chat_id=user_id, message_id=message_id,
                                                      text='âŒ OKPayå……å€¼è®¢å•å·²è¶…æ—¶ï¼Œè¯·é‡æ–°åˆ›å»ºè®¢å•ã€‚',
                                                      reply_markup=InlineKeyboardMarkup(keyboard))
                    elif i.get('type') == 'trc20':
                        context.bot.edit_message_caption(chat_id=user_id, message_id=message_id,
                                                         caption='âŒ TRC20å……å€¼è®¢å•å·²è¶…æ—¶ï¼Œè¯·é‡æ–°åˆ›å»ºè®¢å•ã€‚',
                                                         reply_markup=InlineKeyboardMarkup(keyboard))
                    else:
                        context.bot.edit_message_media(chat_id=user_id, message_id=message_id, media=InputMediaPhoto(media='AgACAgQAAxkBAAI4Nmagu-8nD4AQrv6ftlzrLjLSxlOnAAJavzEbAZYIUch6ykGfk6CaAQADAgADeQADNQQ', caption='âŒ è®¢å•æ”¯ä»˜è¶…æ—¶(æˆ–é‡‘é¢é”™è¯¯)'),reply_markup=InlineKeyboardMarkup(keyboard))

                except:
                    pass
                topup.delete_one({'user_id': user_id, 'state': {'$ne': 1}})
        time.sleep(3)

def suoyouchengxu(context: CallbackContext):
    # Timer(1, jiexi, args=[context]).start()
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

            context.bot.send_message(chat_id=user_id, text='å¼€å§‹å‘é€å¹¿å‘Š')
            fstext = text.replace('/gg ', '')
            for i in user.find({}):
                yh_id = i['user_id']
                keyboard = [[InlineKeyboardButton("âœ…å·²è¯»ï¼ˆç‚¹å‡»é”€æ¯æ­¤æ¶ˆæ¯ï¼‰", callback_data=f'close {yh_id}')]]
                try:
                    context.bot.send_message(chat_id=i['user_id'], text=fstext,
                                             reply_markup=InlineKeyboardMarkup(keyboard))
                except:
                    pass
                time.sleep(3)
            context.bot.send_message(chat_id=user_id, text='å¹¿å‘Šå‘é€å®Œæˆ')


def adm(update: Update, context: CallbackContext):
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
        text1 = text.split(' ')
        user_list = user.find_one({'user_id': user_id})
        USDT = user_list['USDT']
        state = user_list['state']
        if state == '4':
            if len(text1) == 3:
                df_id = int(text1[1])
                money = text1[2]
                if user.find_one({'user_id': df_id}) is None:
                    context.bot.send_message(chat_id=chat_id, text='ç”¨æˆ·ä¸å­˜åœ¨')
                    return
                if '+' in money:
                    money = money.replace('+', '')
                    if not is_number(money):
                        context.bot.send_message(chat_id=chat_id, text='éžæ•°å­—ï¼Œæ“ä½œå¤±è´¥')
                        return
                    hyh_list = user.find_one({'user_id': df_id})
                    hyh_money = hyh_list['USDT']
                    now_money = standard_num(hyh_money + float(money))
                    now_money = float(now_money) if str((now_money)).count('.') > 0 else int(standard_num(now_money))

                    order_id = generate_24bit_uid()
                    user_logging(order_id, 'å……å€¼', df_id, money, timer)
                    user.update_one({'user_id': df_id}, {'$set': {'USDT': now_money}})
                    hyh_list = user.find_one({"user_id": df_id})
                    fullname = hyh_list['fullname']
                    USDT = hyh_list['USDT']
                    fstext = f'''
ID: {df_id}
æ˜µç§°: {fullname}
ä½™é¢: {USDT}
                    '''
                    context.bot.send_message(chat_id=chat_id, text=fstext)

                    fstext = f'''
<b>âœ…Â  Â Â é€šè¿‡ç®¡ç†å‘˜å……å€¼ï¼š{money} USDT

ðŸ’³Â  Â Â æ‚¨çš„ä½™é¢ï¼š{USDT}  USDT</b>
                    '''
                    context.bot.send_message(chat_id=df_id, text=fstext, parse_mode='HTML')
                else:
                    money = money.replace('-', '')
                    if not is_number(money):
                        context.bot.send_message(chat_id=chat_id, text='éžæ•°å­—ï¼Œæ“ä½œå¤±è´¥')
                        return
                    hyh_list = user.find_one({'user_id': df_id})
                    hyh_money = hyh_list['USDT']
                    now_money = standard_num(hyh_money - float(money))
                    now_money = float(now_money) if str((now_money)).count('.') > 0 else int(standard_num(now_money))

                    order_id = generate_24bit_uid()
                    user_logging(order_id, 'æ‰£æ¬¾', df_id, money, timer)
                    user.update_one({'user_id': df_id}, {'$set': {'USDT': now_money}})
                    hyh_list = user.find_one({"user_id": df_id})
                    fullname = hyh_list['fullname']
                    USDT = hyh_list['USDT']
                    fstext = f'''
ID: {df_id}
æ˜µç§°: {fullname}
ä½™é¢: {USDT}
                    '''
                    context.bot.send_message(chat_id=chat_id, text=fstext)

                    fstext = f'''
<b>âœ…Â  Â Â é€šè¿‡ç®¡ç†å‘˜æ‰£æ¬¾ï¼š{money} USDT

ðŸ’³Â  Â Â æ‚¨çš„ä½™é¢ï¼š{USDT}  USDT</b>
                    '''
                    context.bot.send_message(chat_id=df_id, text=fstext, parse_mode='HTML')
            else:
                context.bot.send_message(chat_id=chat_id, text='æ ¼å¼ä¸º: /add id +-æ•°å€¼ï¼Œæœ‰ä¸¤ä¸ªç©ºæ ¼')


def cha(update: Update, context: CallbackContext):
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
        text1 = text.split(' ')
        user_list = user.find_one({'user_id': user_id})
        USDT = user_list['USDT']
        state = user_list['state']
        if state == '4':
            if len(text1) == 2:
                jieguo = text1[1]
                if is_number(jieguo):
                    df_id = int(jieguo)
                    df_list = user.find_one({'user_id': df_id})
                    if df_list is None:
                        context.bot.send_message(chat_id=chat_id, text='ç”¨æˆ·ä¸å­˜åœ¨')
                        return
                else:
                    df_list = user.find_one({'username': jieguo.replace('@', '')})
                    if df_list is None:
                        context.bot.send_message(chat_id=chat_id, text='ç”¨æˆ·ä¸å­˜åœ¨')
                        return
                    df_id = df_list['user_id']
                df_fullname = df_list['fullname']
                df_username = df_list['username']
                if df_username is None:
                    df_username = df_fullname
                else:
                    df_username = f'<a href="https://t.me/{df_username}">{df_username}</a>'
                creation_time = df_list['creation_time']
                zgsl = df_list['zgsl']
                zgje = df_list['zgje']
                USDT = df_list['USDT']
                fstext = f'''
<b>[emoji:6321041414067068140:ðŸ‘¤] ç”¨æˆ·ID:</b>  <code>{df_id}</code>
<b>[emoji:5287684458881756303:ðŸ¤–] ç”¨æˆ·å:</b>  {df_username}
<b>[emoji:5217818964612108191:âœ¨] æ³¨å†Œæ—¥æœŸ:</b>  {creation_time}

<b>[emoji:5220064167356025824:â­ï¸] æ€»è´­æ•°é‡:</b>  {zgsl}

<b>[emoji:5028746137645876535:ðŸ“ˆ] æ€»è´­é‡‘é¢:</b>  {standard_num(zgje)} USDT

<b>[emoji:4972482444025398275:ðŸ‘›] æ‚¨çš„ä½™é¢:</b>  {USDT} USDT
                '''
                keyboard = [[InlineKeyboardButton('ðŸ›’è´­ä¹°è®°å½•', callback_data=f'gmaijilu {df_id}')],
                            [InlineKeyboardButton('å…³é—­', callback_data=f'close {df_id}')]]
                context.bot.send_message(chat_id=user_id, text=fstext, parse_mode='HTML',
                                         reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)



            else:
                context.bot.send_message(chat_id=chat_id, text='æ ¼å¼ä¸º: /cha idæˆ–ç”¨æˆ·åï¼Œæœ‰ä¸€ä¸ªç©ºæ ¼')


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
        (title, url) = ("æ ¼å¼é”™è¯¯ï¼Œç‚¹å‡»è”ç³»ç®¡ç†å‘˜", "www.baidu.com")
    else:
        (title, url) = (args[0].strip(), (None if len(args) < 1 else args[1].strip()))
    return create_keyboard(title, url)


def create_keyboard(title, url=None, callback_data=None, inline_query=None):
    return [InlineKeyboardButton(title, url=url, callback_data=callback_data,
                                 switch_inline_query_current_chat=inline_query)]


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
        raise RuntimeError('ç¼ºå°‘ BOT_TOKENï¼Œè¯·å…ˆåœ¨ .env é‡Œé…ç½® Telegram Bot Token')

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
        ('startupdate', startupdate), ('clonebot', clonebot), ('clonepay', clonepay), ('clonelist', clonelist), ('cloneinfo ', cloneinfo), ('clonedelete ', clonedelete), ('setcloneprice', setcloneprice), ('okpaycfg', okpaycfg), ('setokpayid', setokpayid), ('setokpaytoken', setokpaytoken), ('setokpayname', setokpayname), ('delrow', delrow), ('newrow', newrow), ('newkey', newkey),
        ('backstart', backstart), ('paixurow', paixurow), ('addzdykey', addzdykey),
        ('qrscdelrow ', qrscdelrow), ('addhangkey ', addhangkey), ('delhangkey ', delhangkey),
        ('qrdelliekey ', qrdelliekey), ('keyxq ', keyxq), ('setkeyname ', setkeyname),
        ('settuwenset ', settuwenset), ('setkeyboard ', setkeyboard), ('cattuwenset ', cattuwenset),
        ('paixuyidong ', paixuyidong), ('close ', close), ('yuecz ', yuecz), ('okyuecz ', okyuecz), ('settrc20', settrc20),
        ('spgli', spgli), ('newfl', newfl), ('flxxi ', flxxi), ('upspname ', upspname),
        ('newejfl ', newejfl), ('fejxxi ', fejxxi), ('upejflname ', upejflname),
        ('catejflsp ', catejflsp), ('backzcd', backzcd), ('paixufl', paixufl), ('flpxyd ', flpxyd),
        ('delfl', delfl), ('qrscflrow ', qrscflrow), ('paixuejfl ', paixuejfl), ('ejfpaixu ', ejfpaixu),
        ('delejfl ', delejfl), ('qrscejrow ', qrscejrow), ('update_hb ', update_hb), ('gmsp ', gmsp),
        ('upmoney ', upmoney), ('gmqq', gmqq), ('qrgaimai ', qrgaimai),
        ('update_xyh ', update_xyh), ('update_hy ', update_hy), ('yhnext ', yhnext), ('yhlist', yhlist),
        ('gmaijilu', gmaijilu), ('zcfshuo', zcfshuo), ('gmainext ', gmainext), ('update_txt ', update_txt),
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
    application.job_queue.run_repeating(sync_job(jiexi), interval=3, first=1, name='chongzhi')
    application.run_polling(timeout=600)


if __name__ == '__main__':

    for i in ['å‘è´§', 'åè®®å·å‘è´§', 'æ‰‹æœºæŽ¥ç å‘è´§', 'ä¸´æ—¶æ–‡ä»¶å¤¹', 'è°·æ­Œå‘è´§', 'åè®®å·', 'å·åŒ…']:
        create_folder_if_not_exists(i)
    main()
