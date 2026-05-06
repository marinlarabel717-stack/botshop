import os
import time
import json
from decimal import Decimal
from pathlib import Path

import pymongo
import requests
from dotenv import load_dotenv


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / 'data'
STATE_FILE = STATE_DIR / 'trc20_listener_state.json'

MONGO_URI = os.getenv('MONGO_URI', 'mongodb://127.0.0.1:27018/')
MONGO_USER = os.getenv('MONGO_USER', '')
MONGO_PASSWORD = os.getenv('MONGO_PASSWORD', '')
MONGO_AUTH_DB = os.getenv('MONGO_AUTH_DB', 'admin')
MONGO_DB_NAME = os.getenv('MONGO_DB_NAME', '999bot')
MONGO_CHAIN_DB_NAME = os.getenv('MONGO_CHAIN_DB_NAME', MONGO_DB_NAME)

TRONGRID_API_BASE = os.getenv('TRONGRID_API_BASE', 'https://api.trongrid.io/v1').rstrip('/')
TRONGRID_API_KEY = os.getenv('TRONGRID_API_KEY', '').strip()
TRONGRID_REQUEST_TIMEOUT = int(os.getenv('TRONGRID_REQUEST_TIMEOUT', '20'))
TRONGRID_POLL_SECONDS = int(os.getenv('TRONGRID_POLL_SECONDS', '3'))
TRONGRID_PAGE_LIMIT = max(1, min(int(os.getenv('TRONGRID_PAGE_LIMIT', '100')), 200))
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


def build_headers():
    headers = {'Accept': 'application/json'}
    if TRONGRID_API_KEY:
        headers['TRON-PRO-API-KEY'] = TRONGRID_API_KEY
    return headers


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
    while True:
        req_params = dict(params)
        if fingerprint:
            req_params['fingerprint'] = fingerprint
        response = requests.get(url, params=req_params, headers=build_headers(), timeout=TRONGRID_REQUEST_TIMEOUT)
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
        return False

    event_type = str(item.get('type') or item.get('event_type') or '').strip().lower()
    if any(keyword in event_type for keyword in ('approve', 'approval', 'authorize', 'authorization')):
        return False

    if item.get('confirmed') is False:
        return False

    tx_result = str(item.get('result') or item.get('transaction_result') or '').strip().upper()
    if tx_result and tx_result not in ('SUCCESS', 'SUCESS'):
        return False

    to_address = (item.get('to') or item.get('to_address') or address or '').strip()
    from_address = (item.get('from') or item.get('from_address') or '').strip()
    token_info = item.get('token_info') or {}
    contract_address = (token_info.get('address') or item.get('contract_address') or '').strip()
    symbol = (token_info.get('symbol') or item.get('tokenName') or 'USDT').strip()
    decimals = int(token_info.get('decimals', 6) or 6)
    block_timestamp = int(item.get('block_timestamp') or item.get('block_ts') or 0)
    quant_raw = normalize_quant_raw(item.get('value') or '0', decimals)

    if TRC20_USDT_CONTRACT and contract_address != TRC20_USDT_CONTRACT:
        return False
    if to_address != address:
        return False
    if not from_address:
        return False
    if Decimal(quant_raw) <= 0:
        return False

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
    return bool(result.upserted_id)


def run_once(state):
    inserted = 0
    addresses = get_monitor_addresses()
    if not addresses:
        print('未发现有效TRC20监听地址，等待管理员配置充值地址...')
        return state, inserted

    lookback_ms = TRONGRID_LOOKBACK_MINUTES * 60 * 1000
    for address in addresses:
        last_ts = int(state.get(address, 0) or 0)
        min_timestamp = max(0, last_ts - 60 * 1000)
        if min_timestamp == 0:
            min_timestamp = now_ts_ms() - lookback_ms

        try:
            txs = fetch_trc20_transactions(address, min_timestamp)
        except Exception as exc:
            print(f'监听地址 {address} 拉取失败: {exc}')
            continue

        max_ts = last_ts
        for item in txs:
            block_timestamp = int(item.get('block_timestamp') or item.get('block_ts') or 0)
            if block_timestamp > max_ts:
                max_ts = block_timestamp
            try:
                if upsert_transfer(item, address):
                    inserted += 1
                    txid = item.get('transaction_id') or item.get('transactionId') or item.get('id')
                    print(f'发现新TRC20入账: {address} txid={txid}')
            except Exception as exc:
                print(f'写入交易失败: {exc}')

        if max_ts:
            state[address] = max_ts

    return state, inserted


def main():
    ensure_indexes()
    state = load_state()
    print('TRC20监听器已启动')
    while True:
        try:
            state, inserted = run_once(state)
            save_state(state)
            if inserted:
                print(f'本轮新增 {inserted} 笔交易')
        except KeyboardInterrupt:
            print('TRC20监听器已停止')
            break
        except Exception as exc:
            print(f'监听主循环异常: {exc}')
        time.sleep(TRONGRID_POLL_SECONDS)


if __name__ == '__main__':
    main()
