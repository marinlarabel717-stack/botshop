import hashlib
import logging
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / '.env')
load_dotenv(PROJECT_ROOT / '.env.local', override=False)
load_dotenv(SCRIPT_DIR / '.env', override=True)
load_dotenv(SCRIPT_DIR / '.env.local', override=True)
if os.getenv('STORE_BOT_TOKEN') and not os.getenv('BOT_TOKEN'):
    os.environ['BOT_TOKEN'] = os.getenv('STORE_BOT_TOKEN', '')

from mongo import ejfl, fenlei, hb, mydb  # noqa: E402
from haopubot import (  # noqa: E402
    build_custom_emoji_text_entities,
    build_product_purchase_deep_link,
    build_restock_push_broadcast_text,
    get_product_purchase_payload,
    get_restock_push_target,
)

UPLOAD_BOT_TOKEN = (os.getenv('UPLOAD_BOT_TOKEN') or os.getenv('SHANGCHUAN_BOT_TOKEN') or '').strip()
STORE_BOT_TOKEN = (os.getenv('STORE_BOT_TOKEN') or os.getenv('BOT_TOKEN') or '').strip()
ADMIN_USER_IDS = {
    int(item.strip())
    for item in (os.getenv('UPLOAD_ADMIN_USER_IDS') or os.getenv('ADMIN_USER_IDS') or '').split(',')
    if item.strip().isdigit()
}
CANDIDATE_PAGE_SIZE = max(int(os.getenv('UPLOAD_CANDIDATE_PAGE_SIZE', '8') or 8), 1)

TEMP_DIR = SCRIPT_DIR / '上传临时'
RETURN_DIR = SCRIPT_DIR / '回传文件'
PROTOCOL_DIR = PROJECT_ROOT / '协议号'
TDATA_DIR = PROJECT_ROOT / '号包'

UPLOAD_TASKS = mydb['upload_tasks']
UPLOAD_FINGERPRINTS = mydb['upload_inventory_fingerprints']
HYDRATED_TYPES = set()

CUSTOM_EMOJI_RE = re.compile(r'\[emoji:\d+:(.*?)\]')
EXT_RE = re.compile(r'\.(zip|rar|7z|txt|json|session)$', re.IGNORECASE)
NON_TEXT_EMOJI_RE = re.compile(
    '['
    '\U0001F000-\U0001FAFF'
    '\U00002600-\U000027BF'
    '\U0000FE00-\U0000FE0F'
    '\U0001F1E6-\U0001F1FF'
    ']',
    flags=re.UNICODE,
)

logging.basicConfig(
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def ensure_dirs() -> None:
    for folder in [TEMP_DIR, RETURN_DIR, PROTOCOL_DIR, TDATA_DIR]:
        folder.mkdir(parents=True, exist_ok=True)


def ensure_indexes() -> None:
    UPLOAD_TASKS.create_index([('user_id', 1), ('created_at', -1)], name='upload_task_user_created')
    UPLOAD_FINGERPRINTS.create_index(
        [('leixing', 1), ('fingerprint', 1)],
        name='upload_fingerprint_unique',
        unique=True,
    )
    UPLOAD_FINGERPRINTS.create_index([('nowuid', 1)], name='upload_fingerprint_nowuid')


def normalize_match_name(text: str) -> str:
    raw = str(text or '').strip()
    raw = CUSTOM_EMOJI_RE.sub(lambda m: m.group(1) or '', raw)
    raw = unicodedata.normalize('NFKC', raw)
    raw = NON_TEXT_EMOJI_RE.sub('', raw)
    raw = EXT_RE.sub('', raw)
    raw = raw.replace('（', '(').replace('）', ')')
    raw = re.sub(r'[\s\-_/\\]+', '', raw)
    return raw.casefold()


def is_admin(user_id: int) -> bool:
    return int(user_id or 0) in ADMIN_USER_IDS


def custom_emoji_plain_text(text: str) -> str:
    raw = str(text or '').strip()
    raw = CUSTOM_EMOJI_RE.sub(lambda m: m.group(1) or '', raw)
    raw = unicodedata.normalize('NFKC', raw)
    raw = re.sub(r'\s+', ' ', raw).strip()
    return raw


async def reply_rendered(message, text: str, reply_markup=None):
    rendered_text, entities = build_custom_emoji_text_entities(str(text or ''))
    return await message.reply_text(text=rendered_text, entities=entities, reply_markup=reply_markup)


async def send_rendered(bot, chat_id, text: str, reply_markup=None):
    rendered_text, entities = build_custom_emoji_text_entities(str(text or ''))
    return await bot.send_message(chat_id=chat_id, text=rendered_text, entities=entities, reply_markup=reply_markup)


async def edit_rendered(query, text: str, reply_markup=None):
    rendered_text, entities = build_custom_emoji_text_entities(str(text or ''))
    return await query.edit_message_text(text=rendered_text, entities=entities, reply_markup=reply_markup)


def detect_entry_type(category_name: str, project_name: str) -> Optional[str]:
    text = f'{category_name} {project_name}'.lower()
    if '协议号' in text:
        return '协议号'
    if '直登号' in text or 'tdata' in text:
        return '直登号'
    return None


def list_products() -> List[Dict[str, str]]:
    categories = {
        str(item.get('uid')): str(item.get('projectname') or '').strip()
        for item in fenlei.find({}, {'uid': 1, 'projectname': 1})
    }
    rows = []
    for item in ejfl.find({}, {'uid': 1, 'nowuid': 1, 'projectname': 1}):
        uid = str(item.get('uid') or '')
        nowuid = str(item.get('nowuid') or '')
        project_name = str(item.get('projectname') or '').strip()
        category_name = categories.get(uid, '').strip()
        if not nowuid or not project_name:
            continue
        rows.append({
            'uid': uid,
            'nowuid': nowuid,
            'category_name': category_name,
            'project_name': project_name,
            'match_name': normalize_match_name(project_name),
            'entry_type': detect_entry_type(category_name, project_name),
        })
    rows.sort(key=lambda item: (item['category_name'], item['project_name'], item['nowuid']))
    return rows


def match_products(file_name: str) -> List[Dict[str, str]]:
    target = normalize_match_name(Path(file_name).stem)
    return [item for item in list_products() if item['match_name'] == target]


def build_confirm_text(file_name: str, product: Dict[str, str]) -> str:
    return (
        f'检测到文件名：{Path(file_name).stem}\n'
        f'匹配分类：{product["category_name"]}\n'
        f'匹配商品：{product["project_name"]}\n\n'
        '是否确认上传？'
    )


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('确认上传', callback_data='upload:confirm')],
        [InlineKeyboardButton('取消', callback_data='upload:cancel')],
    ])


def get_candidate_page(products: List[Dict[str, str]], page: int) -> Tuple[List[Dict[str, str]], int]:
    total_pages = max((len(products) + CANDIDATE_PAGE_SIZE - 1) // CANDIDATE_PAGE_SIZE, 1)
    page = min(max(page, 0), total_pages - 1)
    start = page * CANDIDATE_PAGE_SIZE
    end = start + CANDIDATE_PAGE_SIZE
    return products[start:end], total_pages


def build_candidate_text(file_name: str, products: List[Dict[str, str]], page: int) -> str:
    page_items, total_pages = get_candidate_page(products, page)
    lines = [
        f'检测到文件名：{Path(file_name).stem}',
        '',
        '找到多个匹配商品，请选择：',
        '',
    ]
    for idx, item in enumerate(page_items, start=page * CANDIDATE_PAGE_SIZE + 1):
        lines.append(f'{idx}. {item["category_name"]} -> {item["project_name"]}')
    lines.extend(['', f'第 {page + 1}/{total_pages} 页'])
    return '\n'.join(lines)


def build_candidate_keyboard(products: List[Dict[str, str]], page: int) -> InlineKeyboardMarkup:
    page_items, total_pages = get_candidate_page(products, page)
    keyboard = []
    for idx, product in enumerate(page_items, start=page * CANDIDATE_PAGE_SIZE + 1):
        label = f'{idx}. {custom_emoji_plain_text(product["category_name"])} -> {custom_emoji_plain_text(product["project_name"])}'
        keyboard.append([InlineKeyboardButton(label[:60], callback_data=f'pick:{product["nowuid"]}')])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('上一页', callback_data=f'pickpage:{page - 1}'))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton('下一页', callback_data=f'pickpage:{page + 1}'))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton('取消', callback_data='upload:cancel')])
    return InlineKeyboardMarkup(keyboard)


def hash_bytes(parts: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


def hash_file(path: Path) -> Optional[str]:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def hash_protocol_entry_from_storage(nowuid: str, project_name: str) -> Optional[str]:
    folder = PROTOCOL_DIR / str(nowuid)
    session_path = folder / f'{project_name}.session'
    json_path = folder / f'{project_name}.json'
    parts: List[bytes] = []
    if session_path.exists():
        parts.append(b'session\0')
        session_hash = hash_file(session_path)
        if session_hash:
            parts.append(session_hash.encode('utf-8'))
    if json_path.exists():
        parts.append(b'json\0')
        json_hash = hash_file(json_path)
        if json_hash:
            parts.append(json_hash.encode('utf-8'))
    if not parts:
        return None
    return hash_bytes(parts)


def hash_tdata_entry_from_storage(nowuid: str, project_name: str) -> Optional[str]:
    folder = TDATA_DIR / str(nowuid) / project_name
    if not folder.exists() or not folder.is_dir():
        return None
    parts: List[bytes] = []
    for file_path in sorted(p for p in folder.rglob('*') if p.is_file()):
        parts.append(str(file_path.relative_to(folder)).replace('\\', '/').encode('utf-8'))
        file_hash = hash_file(file_path)
        if file_hash:
            parts.append(file_hash.encode('utf-8'))
    if not parts:
        return None
    return hash_bytes(parts)


def hydrate_fingerprint_index(entry_type: str) -> None:
    if entry_type in HYDRATED_TYPES:
        return
    cursor = hb.find({'leixing': entry_type}, {'nowuid': 1, 'projectname': 1, 'hbid': 1, 'leixing': 1})
    for row in cursor:
        nowuid = str(row.get('nowuid') or '')
        project_name = str(row.get('projectname') or '')
        if not nowuid or not project_name:
            continue
        fingerprint = (
            hash_protocol_entry_from_storage(nowuid, project_name)
            if entry_type == '协议号'
            else hash_tdata_entry_from_storage(nowuid, project_name)
        )
        if not fingerprint:
            continue
        try:
            UPLOAD_FINGERPRINTS.update_one(
                {'leixing': entry_type, 'fingerprint': fingerprint},
                {'$setOnInsert': {
                    'nowuid': nowuid,
                    'projectname': project_name,
                    'hbid': row.get('hbid'),
                    'leixing': entry_type,
                    'created_at': int(time.time()),
                }},
                upsert=True,
            )
        except Exception:
            continue
    HYDRATED_TYPES.add(entry_type)


def duplicate_exists(entry_type: str, fingerprint: str) -> bool:
    hydrate_fingerprint_index(entry_type)
    return UPLOAD_FINGERPRINTS.find_one({'leixing': entry_type, 'fingerprint': fingerprint}) is not None


def store_fingerprint(entry_type: str, fingerprint: str, nowuid: str, project_name: str, hbid: str) -> None:
    UPLOAD_FINGERPRINTS.update_one(
        {'leixing': entry_type, 'fingerprint': fingerprint},
        {'$set': {
            'nowuid': nowuid,
            'projectname': project_name,
            'hbid': hbid,
            'leixing': entry_type,
            'created_at': int(time.time()),
        }},
        upsert=True,
    )


def gen_uid() -> str:
    return str(int(time.time() * 1000))[-8:] + os.urandom(3).hex()


def build_result_text(product: Dict[str, str], added: int, duplicated: int, failed: int = 0) -> str:
    now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    return (
        '✅ 文件处理完成\n\n'
        '📁 文件信息：\n'
        f'• 分类：{product["category_name"]}/{product["project_name"]}\n'
        f'• 类型：{product["entry_type"]}\n\n'
        '📊 处理结果：\n\n'
        f'• ✅ 新增：{added} 个\n\n'
        f'• 🔄 重复：{duplicated} 个\n\n'
        f'• ❌ 失败：{failed} 个\n\n'
        '• 📝 状态：处理完成\n\n'
        f'⏰ 处理时间：{now}'
    )


async def send_store_restock_notice(nowuid: str, added_count: int) -> None:
    if added_count <= 0 or not STORE_BOT_TOKEN:
        return
    target = get_restock_push_target()
    if not target:
        return
    payload = get_product_purchase_payload(nowuid)
    if not payload:
        return

    text = build_restock_push_broadcast_text(
        payload['category_name'],
        payload['projectname'],
        payload['money'],
        added_count,
        payload['stock_count'],
    )
    text, entities = build_custom_emoji_text_entities(text)
    store_bot = Bot(STORE_BOT_TOKEN)
    keyboard = None
    try:
        store_me = await store_bot.get_me()
        buy_url = build_product_purchase_deep_link(str(store_me.username or '').strip(), nowuid)
        if buy_url:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('[emoji:5451937962629544243:🛍]购买商品', url=buy_url)]])
    except Exception:
        keyboard = None

    try:
        await store_bot.send_message(chat_id=target, text=text, entities=entities, reply_markup=keyboard)
    except Exception as exc:
        logger.warning('restock broadcast failed: %s', exc)


class ReturnBundle:
    def __init__(self, path: Path):
        self.path = path
        self.writer: Optional[zipfile.ZipFile] = None
        self.counts = {'duplicate': 0, 'failed': 0}

    def __enter__(self):
        self.writer = zipfile.ZipFile(self.path, 'w', zipfile.ZIP_DEFLATED)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.writer:
            self.writer.close()
        if sum(self.counts.values()) == 0 and self.path.exists():
            self.path.unlink(missing_ok=True)

    def write_bytes(self, rel_path: str, data: bytes) -> None:
        if not self.writer:
            raise RuntimeError('return bundle not opened')
        self.writer.writestr(rel_path, data)

    def add_duplicate(self, reason: str, rel_path: str, data: bytes) -> None:
        self.counts['duplicate'] += 1
        self.write_bytes(f'重复文件/{reason}/{rel_path}', data)

    def add_failed(self, reason: str, rel_path: str, data: bytes) -> None:
        self.counts['failed'] += 1
        self.write_bytes(f'失败文件/{reason}/{rel_path}', data)

    def add_text_report(self, rel_path: str, text: str) -> None:
        self.write_bytes(rel_path, text.encode('utf-8'))


def protocol_entries_from_zip(bundle: ReturnBundle, zip_file: zipfile.ZipFile) -> Dict[str, List[Tuple[str, bytes]]]:
    entries: Dict[str, List[Tuple[str, bytes]]] = defaultdict(list)
    for info in zip_file.infolist():
        if info.is_dir():
            continue
        suffix = Path(info.filename).suffix.lower()
        if suffix not in {'.session', '.json'}:
            bundle.add_failed('不支持的文件', f'协议号/{Path(info.filename).name}', zip_file.read(info))
            continue
        stem = Path(info.filename).stem
        entries[stem].append((suffix, zip_file.read(info)))
    return entries


def tdata_entries_from_zip(bundle: ReturnBundle, zip_file: zipfile.ZipFile) -> Dict[str, List[Tuple[str, bytes]]]:
    entries: Dict[str, List[Tuple[str, bytes]]] = defaultdict(list)
    for info in zip_file.infolist():
        if info.is_dir():
            continue
        parts = Path(info.filename).parts
        if len(parts) < 2:
            bundle.add_failed('目录结构不正确', f'直登号/{Path(info.filename).name}', zip_file.read(info))
            continue
        top = parts[0]
        rel_path = '/'.join(parts[1:])
        if not top or not rel_path:
            bundle.add_failed('目录结构不正确', f'直登号/{Path(info.filename).name}', zip_file.read(info))
            continue
        entries[top].append((rel_path, zip_file.read(info)))
    return entries


def fingerprint_protocol_bundle(files: List[Tuple[str, bytes]]) -> str:
    parts: List[bytes] = []
    for suffix, data in sorted(files, key=lambda item: item[0]):
        parts.append(suffix.encode('utf-8'))
        parts.append(hashlib.sha256(data).hexdigest().encode('utf-8'))
    return hash_bytes(parts)


def fingerprint_tdata_bundle(files: List[Tuple[str, bytes]]) -> str:
    parts: List[bytes] = []
    for rel_path, data in sorted(files, key=lambda item: item[0]):
        parts.append(rel_path.encode('utf-8'))
        parts.append(hashlib.sha256(data).hexdigest().encode('utf-8'))
    return hash_bytes(parts)


def save_protocol_files(nowuid: str, project_name: str, files: List[Tuple[str, bytes]]) -> None:
    folder = PROTOCOL_DIR / str(nowuid)
    folder.mkdir(parents=True, exist_ok=True)
    for suffix, data in files:
        (folder / f'{project_name}{suffix}').write_bytes(data)


def save_tdata_files(nowuid: str, project_name: str, files: List[Tuple[str, bytes]]) -> None:
    folder = TDATA_DIR / str(nowuid) / project_name
    folder.mkdir(parents=True, exist_ok=True)
    for rel_path, data in files:
        target = folder / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def add_duplicate_protocol(bundle: ReturnBundle, reason: str, project_name: str, files: List[Tuple[str, bytes]]) -> None:
    for suffix, data in files:
        bundle.add_duplicate(reason, f'协议号/{project_name}{suffix}', data)


def add_duplicate_tdata(bundle: ReturnBundle, reason: str, project_name: str, files: List[Tuple[str, bytes]]) -> None:
    for rel_path, data in files:
        bundle.add_duplicate(reason, f'直登号/{project_name}/{rel_path}', data)


def add_failed_protocol(bundle: ReturnBundle, reason: str, project_name: str, files: List[Tuple[str, bytes]]) -> None:
    for suffix, data in files:
        bundle.add_failed(reason, f'协议号/{project_name}{suffix}', data)


def add_failed_tdata(bundle: ReturnBundle, reason: str, project_name: str, files: List[Tuple[str, bytes]]) -> None:
    for rel_path, data in files:
        bundle.add_failed(reason, f'直登号/{project_name}/{rel_path}', data)


def create_hb_record(product: Dict[str, str], project_name: str, entry_type: str) -> str:
    hbid = gen_uid()
    hb.insert_one({
        'leixing': entry_type,
        'uid': product['uid'],
        'nowuid': product['nowuid'],
        'hbid': hbid,
        'projectname': project_name,
        'state': 0,
        'timer': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
    })
    return hbid


def process_protocol_zip(upload_path: Path, product: Dict[str, str], return_zip_path: Path) -> Tuple[int, int, int, Optional[Path]]:
    added = 0
    duplicated = 0
    failed = 0
    seen = set()

    with ReturnBundle(return_zip_path) as bundle:
        with zipfile.ZipFile(upload_path, 'r') as zip_file:
            entries = protocol_entries_from_zip(bundle, zip_file)
            if not entries:
                bundle.add_text_report('失败文件/处理报告.txt', '未找到可用的协议号文件（.session / .json）。')
                failed += 1
            for project_name, files in entries.items():
                if not files:
                    failed += 1
                    continue
                fingerprint = fingerprint_protocol_bundle(files)
                if fingerprint in seen:
                    duplicated += 1
                    add_duplicate_protocol(bundle, '当前批次重复', project_name, files)
                    continue
                if duplicate_exists('协议号', fingerprint):
                    duplicated += 1
                    add_duplicate_protocol(bundle, '服务器库存重复', project_name, files)
                    continue
                try:
                    save_protocol_files(product['nowuid'], project_name, files)
                    hbid = create_hb_record(product, project_name, '协议号')
                    store_fingerprint('协议号', fingerprint, product['nowuid'], project_name, hbid)
                    seen.add(fingerprint)
                    added += 1
                except Exception:
                    logger.exception('save protocol failed: %s', project_name)
                    failed += 1
                    add_failed_protocol(bundle, '保存失败', project_name, files)
        bundle.add_text_report(
            '处理报告.txt',
            f'新增: {added}\n重复: {duplicated}\n失败: {failed}\n商品: {product["category_name"]}/{product["project_name"]}\n',
        )

    return added, duplicated, failed, return_zip_path if return_zip_path.exists() else None


def process_tdata_zip(upload_path: Path, product: Dict[str, str], return_zip_path: Path) -> Tuple[int, int, int, Optional[Path]]:
    added = 0
    duplicated = 0
    failed = 0
    seen = set()

    with ReturnBundle(return_zip_path) as bundle:
        with zipfile.ZipFile(upload_path, 'r') as zip_file:
            entries = tdata_entries_from_zip(bundle, zip_file)
            if not entries:
                bundle.add_text_report('失败文件/处理报告.txt', '未找到可用的直登号目录结构。')
                failed += 1
            for project_name, files in entries.items():
                if not files:
                    failed += 1
                    continue
                fingerprint = fingerprint_tdata_bundle(files)
                if fingerprint in seen:
                    duplicated += 1
                    add_duplicate_tdata(bundle, '当前批次重复', project_name, files)
                    continue
                if duplicate_exists('直登号', fingerprint):
                    duplicated += 1
                    add_duplicate_tdata(bundle, '服务器库存重复', project_name, files)
                    continue
                try:
                    save_tdata_files(product['nowuid'], project_name, files)
                    hbid = create_hb_record(product, project_name, '直登号')
                    store_fingerprint('直登号', fingerprint, product['nowuid'], project_name, hbid)
                    seen.add(fingerprint)
                    added += 1
                except Exception:
                    logger.exception('save tdata failed: %s', project_name)
                    failed += 1
                    add_failed_tdata(bundle, '保存失败', project_name, files)
        bundle.add_text_report(
            '处理报告.txt',
            f'新增: {added}\n重复: {duplicated}\n失败: {failed}\n商品: {product["category_name"]}/{product["project_name"]}\n',
        )

    return added, duplicated, failed, return_zip_path if return_zip_path.exists() else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else 0):
        await reply_rendered(update.effective_message, '你没有使用权限。')
        return
    await reply_rendered(
        update.effective_message,
        '把 zip 文件直接发给我就行。\n\n'
        '规则：\n'
        '1. 按文件名匹配商品名（忽略 emoji）\n'
        '2. 多个候选时可翻页选择\n'
        '3. 确认后再正式上传\n'
        '4. 和服务器库存重复的账号不会入库，会分类打包回传'
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    message = update.effective_message
    if not tg_user or not message or not message.document:
        return
    if not is_admin(tg_user.id):
        await reply_rendered(message, '你没有使用权限。')
        return

    file_name = message.document.file_name or '未命名文件.zip'
    matched = match_products(file_name)
    if not matched:
        await reply_rendered(
            message,
            f'未找到匹配商品。\n\n检测到文件名：{Path(file_name).stem}\n规则：忽略 emoji 后，文件名与商品名文字必须一致。'
        )
        return

    context.user_data['pending_upload'] = {
        'file_id': message.document.file_id,
        'file_name': file_name,
        'products': {item['nowuid']: item for item in matched},
        'product_order': [item['nowuid'] for item in matched],
        'page': 0,
    }

    if len(matched) > 1:
        await reply_rendered(
            message,
            build_candidate_text(file_name, matched, 0),
            reply_markup=build_candidate_keyboard(matched, 0),
        )
        return

    product = matched[0]
    context.user_data['pending_upload']['selected_nowuid'] = product['nowuid']
    await reply_rendered(message, build_confirm_text(file_name, product), reply_markup=build_confirm_keyboard())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    pending = context.user_data.get('pending_upload') or {}
    product_map = pending.get('products') or {}
    product_order = pending.get('product_order') or []
    products = [product_map[nowuid] for nowuid in product_order if nowuid in product_map]

    if query.data == 'upload:cancel':
        context.user_data.pop('pending_upload', None)
        await edit_rendered(query, '已取消本次上传。')
        return

    if query.data.startswith('pickpage:'):
        if not products:
            await edit_rendered(query, '候选商品已失效，请重新发送文件。')
            return
        page = int(query.data.split(':', 1)[1])
        pending['page'] = page
        context.user_data['pending_upload'] = pending
        await edit_rendered(
            query,
            build_candidate_text(str(pending.get('file_name') or ''), products, page),
            reply_markup=build_candidate_keyboard(products, page),
        )
        return

    if query.data.startswith('pick:'):
        nowuid = query.data.split(':', 1)[1]
        product = product_map.get(nowuid)
        if not product:
            await edit_rendered(query, '候选商品已失效，请重新发送文件。')
            return
        pending['selected_nowuid'] = nowuid
        context.user_data['pending_upload'] = pending
        await edit_rendered(
            query,
            build_confirm_text(str(pending.get('file_name') or ''), product),
            reply_markup=build_confirm_keyboard(),
        )
        return

    if query.data == 'upload:confirm':
        selected_nowuid = pending.get('selected_nowuid')
        product = product_map.get(selected_nowuid)
        if not product:
            await edit_rendered(query, '没有找到待确认的商品，请重新发送文件。')
            return
        await edit_rendered(query, '开始处理文件，请稍等…')
        await run_upload_task(update, context, product, pending)
        context.user_data.pop('pending_upload', None)


async def run_upload_task(update: Update, context: ContextTypes.DEFAULT_TYPE, product: Dict[str, str], pending: Dict[str, object]) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    entry_type = product.get('entry_type')
    if entry_type not in {'协议号', '直登号'}:
        await send_rendered(context.bot, chat_id, '这个商品类型暂时还不支持自动上传。当前先支持：协议号 / 直登号。')
        return

    file_id = str(pending.get('file_id') or '')
    file_name = str(pending.get('file_name') or 'upload.zip')
    if not file_id:
        await send_rendered(context.bot, chat_id, '文件信息丢了，请重新发送。')
        return

    tg_file = await context.bot.get_file(file_id)
    task_id = gen_uid()
    upload_path = TEMP_DIR / f'{task_id}_{Path(file_name).name}'
    return_zip_path = RETURN_DIR / f'处理回包_{Path(file_name).stem}_{task_id}.zip'

    UPLOAD_TASKS.insert_one({
        'task_id': task_id,
        'user_id': update.effective_user.id if update.effective_user else 0,
        'file_name': file_name,
        'nowuid': product['nowuid'],
        'project_name': product['project_name'],
        'created_at': int(time.time()),
        'state': 'processing',
    })

    try:
        await tg_file.download_to_drive(custom_path=str(upload_path))
        if not zipfile.is_zipfile(upload_path):
            await send_rendered(context.bot, chat_id, '目前只支持 zip 批量上传。')
            UPLOAD_TASKS.update_one({'task_id': task_id}, {'$set': {'state': 'failed', 'reason': 'not_zip'}})
            return

        if entry_type == '协议号':
            added, duplicated, failed, return_file = process_protocol_zip(upload_path, product, return_zip_path)
        else:
            added, duplicated, failed, return_file = process_tdata_zip(upload_path, product, return_zip_path)

        UPLOAD_TASKS.update_one(
            {'task_id': task_id},
            {'$set': {
                'state': 'done',
                'added': added,
                'duplicated': duplicated,
                'failed': failed,
                'finished_at': int(time.time()),
            }},
        )
        await send_rendered(context.bot, chat_id, build_result_text(product, added, duplicated, failed))
        if return_file and return_file.exists():
            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(str(return_file)),
                filename=return_file.name,
                caption='重复文件 / 失败文件 已按分类打包回传，请查收。',
            )
        if added > 0:
            await send_store_restock_notice(product['nowuid'], added)
    except Exception as exc:
        logger.exception('upload task failed')
        UPLOAD_TASKS.update_one(
            {'task_id': task_id},
            {'$set': {'state': 'failed', 'reason': str(exc), 'finished_at': int(time.time())}},
        )
        await send_rendered(context.bot, chat_id, f'处理失败：{exc}')
    finally:
        upload_path.unlink(missing_ok=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception('unhandled error', exc_info=context.error)


def main() -> None:
    if not UPLOAD_BOT_TOKEN:
        raise RuntimeError('缺少 UPLOAD_BOT_TOKEN / SHANGCHUAN_BOT_TOKEN')
    ensure_dirs()
    ensure_indexes()
    app = ApplicationBuilder().token(UPLOAD_BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Document.ALL, handle_document))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
