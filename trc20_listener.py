import os
import time
import json
import sys
import logging
from decimal import Decimal
from pathlib import Path

import pymongo
import requests
from dotenv import load_dotenv


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / 'data'
STATE_FILE = STATE_DIR / 'trc20_listener_state.json'
LOG_DIR = BASE_DIR / 'logs'
LOG_FILE = LOG_DIR / 'trc20_listener.log'

MONGO_URI = os.getenv('MONGO_URI', 'mongodb://127.0.0.1:27018/')
MONGO_USER = os.getenv('MONGO_USER', '')
MONGO_PASSWORD = os.getenv('MONGO_PASSWORD', '')
MONGO_AUTH_DB = os.getenv('MONGO_AUTH_DB', 'admin')
MONGO_DB_NAME = os.getenv('MONGO_DB_NAME', '999bot')
MONGO_CHAIN_DB_NAME = os.getenv('MONGO_CHAIN_DB_NAME', MONGO_DB_NAME)

TRONGRID_API_BASE = os.getenv('TRONGRID_API_BASE', 'https://api.trongrid.io/v1').rstrip('/')
TRONGRID_API_KEY = os.getenv('TRONGRID_API_KEY', '').strip()
TRONGRID_API_KEYS = os.getenv('TRONGRID_API_KEYS', '').strip()
TRONGRID_REQUEST_TIMEOUT = int(os.getenv('TRONGRID_REQUEST_TIMEOUT', '20'))
TRONGRID_POLL_SECONDS = int(os.getenv('TRONGRID_POLL_SECONDS', '3'))
TRONGRID_PAGE_LIMIT = max(1, min(int(os.getenv('TRONGRID_PAGE_LIMIT', '100')), 200))
TRONGRID_MAX_PAGES = max(1, int(os.getenv('TRONGRID_MAX_PAGES', '20')))
TRONGRID_LOOKBACK_MINUTES = max(1, int(os.getenv('TRONGRID_LOOKBACK_MINUTES', '30')))
TRONGRID_MONITOR_ADDRESSES = os.getenv('TRONGRID_MONITOR_ADDRESSES', '')
TRC20_USDT_CONTRACT = os.getenv('TRC20_USDT_CONTRACT', 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t').strip()


mongo_client_kwargs = {}
if MONGO_USER:
    mongo_client_kwargs['username'] = MONGO_USER
if MONGO_PASSWORD:
    mongo_client_kwargs['password'] = MONGO_PASSWORD
if MONGO_USER and MONGO_AUTH_DB:
    mongo_client_kwargs['authSource'] = MONGO_AUTH_DB

teleclient = pymongo.MongoClient(MONGO_URI, **mongo_client_kwargs)
mydb = teleclient[MONGO_DB_NAME]
chain_db = teleclient[MONGO_CHAIN_DB_NAME]
shangtext = mydb['shangtext']
qukuai = chain_db['qukuai']


def setup_logging():
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)
    except Exception:
        pass

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('trc20_listener')
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


logger = setup_logging()


def parse_trongrid_api_keys():
    keys = []
    for raw in [TRONGRID_API_KEYS, TRONGRID_API_KEY]:
        if not raw:
            continue
        for item in str(raw).replace('\n', ',').split(','):
            key = item.strip()
            if key and key not in keys:
                keys.append(key)
    return keys


TRONGRID_API_KEY_POOL = parse_trongrid_api_keys()
TRONGRID_API_KEY_INDEX = 0


def mask_api_key(api_key):
    api_key = str(api_key or '').strip()
    if len(api_key) <= 8:
        return api_key or '未配置'
    return f'{api_key[:4]}***{api_key[-4:]}'


def get_trongrid_api_key_candidates():
    global TRONGRID_API_KEY_INDEX
    if not TRONGRID_API_KEY_POOL:
        return [None]
    start = TRONGRID_API_KEY_INDEX % len(TRONGRID_API_KEY_POOL)
    TRONGRID_API_KEY_INDEX = (start + 1) % len(TRONGRID_API_KEY_POOL)
    return TRONGRID_API_KEY_POOL[start:] + TRONGRID_API_KEY_POOL[:start]


def now_ts_ms():
    return int(time.time() * 1000)


def now_str():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())


def is_valid_trc20_address(address):
    if not isinstance(address, str):
        return False
    address = address.strip()
    if len(address) != 34 or not address.startswith('T'):
        return False
    allowed = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    return all(ch in allowed for ch in address)


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def ensure_indexes():
    try:
        qukuai.create_index('txid', unique=True)
    except Exception:
        pass
    try:
        qukuai.create_index([('state', 1), ('to_address', 1)])
    except Exception:
        pass


def get_monitor_addresses():
    addresses = set()

    env_addresses = [x.strip() for x in TRONGRID_MONITOR_ADDRESSES.split(',') if x.strip()]
    for address in env_addresses:
        if is_valid_trc20_address(address):
            addresses.add(address)

    for row in shangtext.find({'projectname': '充值地址'}):
        address = str(row.get('text', '') or '').strip()
        if is_valid_trc20_address(address):
            addresses.add(address)

    return sorted(addresses)


def build_headers(api_key=None):
    headers = {'Accept': 'application/json'}
    if api_key:
        headers['TRON-PRO-API-KEY'] = api_key
    return headers


def trongrid_get(url, params):
    last_exc = None
    candidates = get_trongrid_api_key_candidates()
    for idx, api_key in enumerate(candidates, start=1):
        try:
            response = requests.get(url, params=params, headers=build_headers(api_key), timeout=TRONGRID_REQUEST_TIMEOUT)
            if response.status_code in (403, 429) and idx < len(candidates):
                logger.warning('TronGrid key %s 请求受限(status=%s)，切换下一个 key 重试', mask_api_key(api_key), response.status_code)
                continue
            response.raise_for_status()
            if api_key:
                logger.info('本次 TronGrid 请求使用 key: %s', mask_api_key(api_key))
            return response
        except requests.RequestException as exc:
            last_exc = exc
            status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
            if api_key and status_code in (403, 429) and idx < len(candidates):
                logger.warning('TronGrid key %s 异常受限(status=%s)，切换下一个 key 重试', mask_api_key(api_key), status_code)
                continue
            if idx < len(candidates):
                logger.warning('TronGrid key %s 请求失败：%s，切换下一个 key 重试', mask_api_key(api_key), exc)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError('TronGrid 请求失败')


def fetch_trc20_transactions(address, min_timestamp):
    url = f'{TRONGRID_API_BASE}/accounts/{address}/transactions/trc20'
    params = {
        'only_confirmed': 'true',
        'only_to': 'true',
        'limit': str(TRONGRID_PAGE_LIMIT),
        'order_by': 'block_timestamp,desc',
        'min_timestamp': str(max(0, int(min_timestamp or 0))),
    }
    if TRC20_USDT_CONTRACT:
        params['contract_address'] = TRC20_USDT_CONTRACT

    items = []
    fingerprint = None
    seen_fingerprints = set()
    page_count = 0
    while True:
        if fingerprint:
            if fingerprint in seen_fingerprints:
                logger.warning('监听地址 %s 遇到重复 fingerprint，提前结束本轮翻页，避免卡死', address)
                break
            seen_fingerprints.add(fingerprint)
        page_count += 1
        if page_count > TRONGRID_MAX_PAGES:
            logger.warning('监听地址 %s 本轮已拉取 %s 页，提前结束，避免长时间卡住', address, TRONGRID_MAX_PAGES)
            break
        req_params = dict(params)
        if fingerprint:
            req_params['fingerprint'] = fingerprint
        response = trongrid_get(url, req_params)
        response.raise_for_status()
        payload = response.json()
        data = payload.get('data') or []
        items.extend(data)
        fingerprint = ((payload.get('meta') or {}).get('fingerprint'))
        if not fingerprint or not data:
            break
    return items


def normalize_quant_raw(value, decimals):
    raw = Decimal(str(value or '0'))
    decimals = int(decimals or 0)
    if decimals == 6:
        return str(int(raw))
    if decimals > 6:
        scaled = raw / (Decimal(10) ** (decimals - 6))
    else:
        scaled = raw * (Decimal(10) ** (6 - decimals))
    return str(int(scaled))


def upsert_transfer(item, address):
    txid = item.get('transaction_id') or item.get('transactionId') or item.get('id')
    if not txid:
        return 'ignored'

    event_type = str(item.get('type') or item.get('event_type') or '').strip().lower()
    if any(keyword in event_type for keyword in ('approve', 'approval', 'authorize', 'authorization')):
        return 'ignored'

    if item.get('confirmed') is False:
        return 'ignored'

    tx_result = str(item.get('result') or item.get('transaction_result') or '').strip().upper()
    if tx_result and tx_result not in ('SUCCESS', 'SUCESS'):
        return 'ignored'

    to_address = (item.get('to') or item.get('to_address') or address or '').strip()
    from_address = (item.get('from') or item.get('from_address') or '').strip()
    token_info = item.get('token_info') or {}
    contract_address = (token_info.get('address') or item.get('contract_address') or '').strip()
    symbol = (token_info.get('symbol') or item.get('tokenName') or 'USDT').strip()
    decimals = int(token_info.get('decimals', 6) or 6)
    block_timestamp = int(item.get('block_timestamp') or item.get('block_ts') or 0)
    quant_raw = normalize_quant_raw(item.get('value') or '0', decimals)

    if TRC20_USDT_CONTRACT and contract_address != TRC20_USDT_CONTRACT:
        return 'ignored'
    if to_address != address:
        return 'ignored'
    if not from_address:
        return 'ignored'
    if Decimal(quant_raw) <= 0:
        return 'ignored'

    doc = {
        'txid': txid,
        'quant': quant_raw,
        'from_address': from_address,
        'to_address': to_address,
        'state': 0,
        'token_symbol': symbol,
        'token_decimals': decimals,
        'contract_address': contract_address,
        'block_timestamp': block_timestamp,
        'confirmed': True,
        'event_type': event_type or 'transfer',
        'listener': 'trongrid',
        'timer': now_str(),
    }
    result = qukuai.update_one({'txid': txid}, {'$setOnInsert': doc}, upsert=True)
    return 'inserted' if result.upserted_id else 'duplicate'


def run_once(state):
    inserted = 0
    addresses = get_monitor_addresses()
    if not addresses:
        logger.warning('未发现有效TRC20监听地址，等待管理员配置充值地址...')
        return state, inserted

    logger.info('本轮开始监听，地址数=%s，地址=%s', len(addresses), ', '.join(addresses))

    lookback_ms = TRONGRID_LOOKBACK_MINUTES * 60 * 1000
    for address in addresses:
        last_ts = int(state.get(address, 0) or 0)
        min_timestamp = max(0, last_ts - 60 * 1000)
        if min_timestamp == 0:
            min_timestamp = now_ts_ms() - lookback_ms

        try:
            txs = fetch_trc20_transactions(address, min_timestamp)
        except Exception as exc:
            logger.exception('监听地址 %s 拉取失败: %s', address, exc)
            continue

        inserted_for_address = 0
        duplicate_for_address = 0
        ignored_for_address = 0

        max_ts = last_ts
        for item in txs:
            block_timestamp = int(item.get('block_timestamp') or item.get('block_ts') or 0)
            if block_timestamp > max_ts:
                max_ts = block_timestamp
            try:
                result = upsert_transfer(item, address)
                if result == 'inserted':
                    inserted += 1
                    inserted_for_address += 1
                    txid = item.get('transaction_id') or item.get('transactionId') or item.get('id')
                    logger.info('发现新TRC20入账: address=%s txid=%s', address, txid)
                elif result == 'duplicate':
                    duplicate_for_address += 1
                else:
                    ignored_for_address += 1
            except Exception as exc:
                logger.exception('写入交易失败: %s', exc)

        if max_ts:
            state[address] = max_ts
        logger.info('地址 %s 本轮统计：拉取=%s，新增=%s，重复=%s，忽略=%s，min_timestamp=%s',
                    address, len(txs), inserted_for_address, duplicate_for_address, ignored_for_address, min_timestamp)

    return state, inserted


def main():
    ensure_indexes()
    state = load_state()
    logger.info('TronGrid API Key 数量：%s', len(TRONGRID_API_KEY_POOL))
    logger.info('TRC20监听器已启动，日志文件：%s', LOG_FILE)
    while True:
        try:
            state, inserted = run_once(state)
            save_state(state)
            if inserted:
                logger.info('本轮新增 %s 笔交易', inserted)
        except KeyboardInterrupt:
            logger.info('TRC20监听器已停止')
            break
        except Exception as exc:
            logger.exception('监听主循环异常: %s', exc)
        time.sleep(TRONGRID_POLL_SECONDS)


if __name__ == '__main__':
    main()
