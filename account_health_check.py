import asyncio
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

try:
    from opentele.api import API, UseCurrentSession
    from opentele.td import TDesktop
    from opentele.tl import TelegramClient as OpenTeleClient
except Exception:
    API = None
    UseCurrentSession = None
    TDesktop = None
    OpenTeleClient = None

try:
    from telethon import TelegramClient as TelethonClient
    from telethon import errors as telethon_errors
    from telethon.tl import functions, types
except Exception:
    TelethonClient = None
    telethon_errors = None
    functions = None
    types = None


SESSION_CHECK_API_ID = os.getenv('ACCOUNT_CHECK_API_ID', '').strip()
SESSION_CHECK_API_HASH = os.getenv('ACCOUNT_CHECK_API_HASH', '').strip()
DEFAULT_TIMEOUT_SECONDS = max(5, int(os.getenv('ACCOUNT_CHECK_TIMEOUT_SECONDS', '25') or '25'))
AGENT_ACCOUNT_TTL_DAYS = max(30, int(os.getenv('AGENT_ACCOUNT_TTL_DAYS', '730') or '730'))


class DependencyUnavailable(RuntimeError):
    pass


def _json_value_to_python(value: Any):
    if value is None:
        return None
    if types is not None:
        if isinstance(value, types.JsonObject):
            return {item.key: _json_value_to_python(item.value) for item in value.value}
        if isinstance(value, types.JsonArray):
            return [_json_value_to_python(item) for item in value.value]
        if isinstance(value, types.JsonObjectValue):
            return {value.key: _json_value_to_python(value.value)}
        if isinstance(value, types.JsonString):
            return value.value
        if isinstance(value, types.JsonNumber):
            return value.value
        if isinstance(value, types.JsonBool):
            return value.value
        if isinstance(value, types.JsonNull):
            return None
    if isinstance(value, list):
        return [_json_value_to_python(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value_to_python(item) for key, item in value.items()}
    return value


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _format_unix_ts(timestamp: Any) -> str:
    ts = _safe_int(timestamp)
    if ts <= 0:
        return ''
    try:
        return datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S UTC')
    except Exception:
        return ''


def _extract_rpc_error_name(exc: Exception) -> str:
    for attr in ('message', 'name'):
        value = getattr(exc, attr, '')
        if isinstance(value, str) and value:
            return value.upper()
    return str(exc).upper()


def _is_frozen_rpc_error(exc: Exception) -> bool:
    name = _extract_rpc_error_name(exc)
    return 'FROZEN_METHOD_INVALID' in name or 'FROZEN_PARTICIPANT_MISSING' in name


async def _fetch_freeze_metadata(client: Any, timeout_seconds: int) -> Dict[str, Any]:
    if functions is None:
        return {}
    try:
        result = await asyncio.wait_for(client(functions.help.GetAppConfigRequest(hash=0)), timeout=timeout_seconds)
    except TypeError:
        result = await asyncio.wait_for(client(functions.help.GetAppConfigRequest(0)), timeout=timeout_seconds)
    except Exception:
        return {}

    config_obj = getattr(result, 'config', None)
    config_map = _json_value_to_python(config_obj)
    if not isinstance(config_map, dict):
        return {}

    freeze_since_date = _safe_int(config_map.get('freeze_since_date'))
    freeze_until_date = _safe_int(config_map.get('freeze_until_date'))
    freeze_appeal_url = str(config_map.get('freeze_appeal_url') or '').strip()
    return {
        'freeze_since_date': freeze_since_date,
        'freeze_until_date': freeze_until_date,
        'freeze_since_text': _format_unix_ts(freeze_since_date),
        'freeze_until_text': _format_unix_ts(freeze_until_date),
        'freeze_appeal_url': freeze_appeal_url,
    }


async def _detect_frozen_status(client: Any, timeout_seconds: int) -> Dict[str, Any]:
    probe_text = f'health-check-{uuid.uuid4().hex[:8]}'
    try:
        message = await asyncio.wait_for(client.send_message('me', probe_text), timeout=timeout_seconds)
        try:
            await asyncio.wait_for(client.delete_messages('me', [message.id]), timeout=timeout_seconds)
        except Exception:
            pass
        return {'status': 'alive'}
    except Exception as exc:
        if _is_frozen_rpc_error(exc):
            metadata = await _fetch_freeze_metadata(client, timeout_seconds)
            metadata.update({
                'status': 'frozen',
                'reason': _extract_rpc_error_name(exc),
            })
            return metadata
        raise


async def _probe_client(client: Any, timeout_seconds: int) -> Dict[str, Any]:
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout_seconds)
        authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=timeout_seconds)
        if not authorized:
            return {'status': 'invalid', 'reason': 'session_not_authorized'}
        me = await asyncio.wait_for(client.get_me(), timeout=timeout_seconds)
        if me is None:
            return {'status': 'invalid', 'reason': 'account_not_found'}
        display_name = ' '.join(filter(None, [getattr(me, 'first_name', ''), getattr(me, 'last_name', '')])).strip()
        freeze_metadata = await _fetch_freeze_metadata(client, timeout_seconds)
        if freeze_metadata.get('freeze_since_date') or freeze_metadata.get('freeze_until_date'):
            return {
                'status': 'frozen',
                'reason': 'FREEZE_STATE_IN_APP_CONFIG',
                'user_id': getattr(me, 'id', None),
                'display_name': display_name,
                'username': getattr(me, 'username', None),
                'phone': getattr(me, 'phone', None),
                'freeze_since_date': freeze_metadata.get('freeze_since_date', 0),
                'freeze_until_date': freeze_metadata.get('freeze_until_date', 0),
                'freeze_since_text': freeze_metadata.get('freeze_since_text', ''),
                'freeze_until_text': freeze_metadata.get('freeze_until_text', ''),
                'freeze_appeal_url': freeze_metadata.get('freeze_appeal_url', ''),
            }
        frozen_status = await _detect_frozen_status(client, timeout_seconds)
        if frozen_status.get('status') == 'frozen':
            return {
                'status': 'frozen',
                'reason': frozen_status.get('reason', 'FROZEN_METHOD_INVALID'),
                'user_id': getattr(me, 'id', None),
                'display_name': display_name,
                'username': getattr(me, 'username', None),
                'phone': getattr(me, 'phone', None),
                'freeze_since_date': frozen_status.get('freeze_since_date', 0),
                'freeze_until_date': frozen_status.get('freeze_until_date', 0),
                'freeze_since_text': frozen_status.get('freeze_since_text', ''),
                'freeze_until_text': frozen_status.get('freeze_until_text', ''),
                'freeze_appeal_url': frozen_status.get('freeze_appeal_url', ''),
            }
        return {
            'status': 'alive',
            'reason': 'ok',
            'user_id': getattr(me, 'id', None),
            'display_name': display_name,
            'username': getattr(me, 'username', None),
            'phone': getattr(me, 'phone', None),
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _check_session_async(session_path: Path, timeout_seconds: int) -> Dict[str, Any]:
    if not session_path.exists():
        return {'status': 'invalid', 'reason': 'session_file_missing'}

    if not (SESSION_CHECK_API_ID and SESSION_CHECK_API_HASH and TelethonClient is not None):
        raise DependencyUnavailable('missing_session_api_or_telethon')

    try:
        client = TelethonClient(str(session_path), int(SESSION_CHECK_API_ID), SESSION_CHECK_API_HASH, receive_updates=False)
    except Exception as exc:
        return {'status': 'timeout', 'reason': f'session_client_init_failed:{exc}'}

    return await _probe_client(client, timeout_seconds)


async def _build_tdata_client(tdata_dir: Path, timeout_seconds: int):
    if TDesktop is None or API is None or UseCurrentSession is None:
        raise DependencyUnavailable('missing_tdata_check_dependencies')
    tdesk = TDesktop(str(tdata_dir))
    if not tdesk.isLoaded():
        return None
    temp_dir = tempfile.TemporaryDirectory(prefix='tdata-check-')
    try:
        temp_session = str(Path(temp_dir.name) / 'temp_session')
        try:
            client = await asyncio.wait_for(
                tdesk.ToTelethon(session=temp_session, flag=UseCurrentSession, api=API.TelegramDesktop),
                timeout=timeout_seconds,
            )
        except TypeError:
            client = await asyncio.wait_for(
                tdesk.ToTelethon(session=temp_session, flag=UseCurrentSession),
                timeout=timeout_seconds,
            )
        return client, temp_dir
    except Exception:
        temp_dir.cleanup()
        raise


async def _check_tdata_async(tdata_dir: Path, timeout_seconds: int) -> Dict[str, Any]:
    if not tdata_dir.exists() or not tdata_dir.is_dir():
        return {'status': 'invalid', 'reason': 'tdata_folder_missing'}

    build_result = await _build_tdata_client(tdata_dir, timeout_seconds)
    if build_result is None:
        return {'status': 'invalid', 'reason': 'tdata_not_loaded'}

    client, temp_dir = build_result
    try:
        return await _probe_client(client, timeout_seconds)
    finally:
        temp_dir.cleanup()


async def _probe_client_with_ttl_update(client: Any, timeout_seconds: int, ttl_days: int = AGENT_ACCOUNT_TTL_DAYS) -> Dict[str, Any]:
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout_seconds)
        authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=timeout_seconds)
        if not authorized:
            return {'status': 'invalid', 'reason': 'session_not_authorized'}
        me = await asyncio.wait_for(client.get_me(), timeout=timeout_seconds)
        if me is None:
            return {'status': 'invalid', 'reason': 'account_not_found'}
        display_name = ' '.join(filter(None, [getattr(me, 'first_name', ''), getattr(me, 'last_name', '')])).strip()
        freeze_metadata = await _fetch_freeze_metadata(client, timeout_seconds)
        if freeze_metadata.get('freeze_since_date') or freeze_metadata.get('freeze_until_date'):
            return {
                'status': 'frozen',
                'reason': 'FREEZE_STATE_IN_APP_CONFIG',
                'user_id': getattr(me, 'id', None),
                'display_name': display_name,
                'username': getattr(me, 'username', None),
                'phone': getattr(me, 'phone', None),
                'freeze_since_date': freeze_metadata.get('freeze_since_date', 0),
                'freeze_until_date': freeze_metadata.get('freeze_until_date', 0),
                'freeze_since_text': freeze_metadata.get('freeze_since_text', ''),
                'freeze_until_text': freeze_metadata.get('freeze_until_text', ''),
                'freeze_appeal_url': freeze_metadata.get('freeze_appeal_url', ''),
            }
        frozen_status = await _detect_frozen_status(client, timeout_seconds)
        if frozen_status.get('status') == 'frozen':
            return {
                'status': 'frozen',
                'reason': frozen_status.get('reason', 'FROZEN_METHOD_INVALID'),
                'user_id': getattr(me, 'id', None),
                'display_name': display_name,
                'username': getattr(me, 'username', None),
                'phone': getattr(me, 'phone', None),
                'freeze_since_date': frozen_status.get('freeze_since_date', 0),
                'freeze_until_date': frozen_status.get('freeze_until_date', 0),
                'freeze_since_text': frozen_status.get('freeze_since_text', ''),
                'freeze_until_text': frozen_status.get('freeze_until_text', ''),
                'freeze_appeal_url': frozen_status.get('freeze_appeal_url', ''),
            }
        if functions is None or types is None:
            raise DependencyUnavailable('missing_account_ttl_dependencies')
        try:
            ttl_result = await asyncio.wait_for(
                client(functions.account.SetAccountTTLRequest(ttl=types.AccountDaysTTL(days=int(ttl_days)))),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            if _is_frozen_rpc_error(exc):
                metadata = await _fetch_freeze_metadata(client, timeout_seconds)
                metadata.update({
                    'status': 'frozen',
                    'reason': _extract_rpc_error_name(exc),
                    'user_id': getattr(me, 'id', None),
                    'display_name': display_name,
                    'username': getattr(me, 'username', None),
                    'phone': getattr(me, 'phone', None),
                })
                return metadata
            raise
        if ttl_result is False:
            return {'status': 'invalid', 'reason': 'set_account_ttl_returned_false'}
        return {
            'status': 'alive',
            'reason': 'set_account_ttl_ok',
            'user_id': getattr(me, 'id', None),
            'display_name': display_name,
            'username': getattr(me, 'username', None),
            'phone': getattr(me, 'phone', None),
            'ttl_days': int(ttl_days),
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _check_session_async_with_ttl_update(session_path: Path, timeout_seconds: int, ttl_days: int = AGENT_ACCOUNT_TTL_DAYS) -> Dict[str, Any]:
    if not session_path.exists():
        return {'status': 'invalid', 'reason': 'session_file_missing'}
    if not (SESSION_CHECK_API_ID and SESSION_CHECK_API_HASH and TelethonClient is not None):
        raise DependencyUnavailable('missing_session_api_or_telethon')
    try:
        client = TelethonClient(str(session_path), int(SESSION_CHECK_API_ID), SESSION_CHECK_API_HASH, receive_updates=False)
    except Exception as exc:
        return {'status': 'timeout', 'reason': f'session_client_init_failed:{exc}'}
    return await _probe_client_with_ttl_update(client, timeout_seconds, ttl_days=ttl_days)


async def _check_tdata_async_with_ttl_update(tdata_dir: Path, timeout_seconds: int, ttl_days: int = AGENT_ACCOUNT_TTL_DAYS) -> Dict[str, Any]:
    if not tdata_dir.exists() or not tdata_dir.is_dir():
        return {'status': 'invalid', 'reason': 'tdata_folder_missing'}
    build_result = await _build_tdata_client(tdata_dir, timeout_seconds)
    if build_result is None:
        return {'status': 'invalid', 'reason': 'tdata_not_loaded'}
    client, temp_dir = build_result
    try:
        return await _probe_client_with_ttl_update(client, timeout_seconds, ttl_days=ttl_days)
    finally:
        temp_dir.cleanup()


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()


def _classify_exception(exc: Exception) -> Dict[str, Any]:
    message = str(exc) or exc.__class__.__name__
    lower_message = message.lower()
    timeout_markers = ('timeout', 'timed out', 'deadline', 'floodwait', 'temporarily unavailable', 'connection lost')
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, DependencyUnavailable)) or any(marker in lower_message for marker in timeout_markers):
        return {'status': 'timeout', 'reason': message}
    return {'status': 'invalid', 'reason': message}


def get_account_check_runtime_status(entry_type: str) -> Dict[str, Any]:
    if entry_type == '协议号':
        if SESSION_CHECK_API_ID and SESSION_CHECK_API_HASH and TelethonClient is not None:
            return {'ready': True, 'backend': 'telethon'}
        return {'ready': False, 'reason': 'missing_session_api_or_telethon'}
    if entry_type == '直登号':
        if TDesktop is not None and API is not None and UseCurrentSession is not None:
            return {'ready': True, 'backend': 'opentele_tdata'}
        return {'ready': False, 'reason': 'missing_tdata_check_dependencies'}
    return {'ready': False, 'reason': f'unsupported_entry_type:{entry_type}'}


def _check_account_inventory_item_inner(entry_type: str, path: Path, timeout_seconds: int) -> Dict[str, Any]:
    if entry_type == '协议号':
        return _run_async(_check_session_async(path, timeout_seconds))
    if entry_type == '直登号':
        return _run_async(_check_tdata_async(path, timeout_seconds))
    return {'status': 'timeout', 'reason': f'unsupported_entry_type:{entry_type}'}


def check_account_inventory_item(entry_type: str, target_path: str, timeout_seconds: int | None = None) -> Dict[str, Any]:
    timeout_seconds = max(5, int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS))
    path = Path(target_path)
    result_box: Dict[str, Any] = {}
    error_box: Dict[str, Exception] = {}

    def worker():
        try:
            result_box['result'] = _check_account_inventory_item_inner(entry_type, path, timeout_seconds)
        except Exception as exc:
            error_box['error'] = exc

    thread = threading.Thread(target=worker, name=f'account-check-{entry_type}', daemon=True)
    thread.start()
    thread.join(timeout_seconds + 2)

    if thread.is_alive():
        result = {'status': 'timeout', 'reason': f'hard_timeout_after_{timeout_seconds}s'}
    elif 'error' in error_box:
        result = _classify_exception(error_box['error'])
    else:
        result = result_box.get('result', {'status': 'timeout', 'reason': 'empty_check_result'})

    result['path'] = str(path)
    result['entry_type'] = entry_type
    return result


def check_account_inventory_item_with_ttl_update(entry_type: str, target_path: str, timeout_seconds: int | None = None, ttl_days: int = AGENT_ACCOUNT_TTL_DAYS) -> Dict[str, Any]:
    timeout_seconds = max(5, int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS))
    path = Path(target_path)
    result_box: Dict[str, Any] = {}
    error_box: Dict[str, Exception] = {}

    def worker():
        try:
            if entry_type == '协议号':
                result_box['result'] = _run_async(_check_session_async_with_ttl_update(path, timeout_seconds, ttl_days=ttl_days))
            elif entry_type == '直登号':
                result_box['result'] = _run_async(_check_tdata_async_with_ttl_update(path, timeout_seconds, ttl_days=ttl_days))
            else:
                result_box['result'] = {'status': 'invalid', 'reason': f'unsupported_entry_type:{entry_type}'}
        except Exception as exc:
            error_box['error'] = exc

    thread = threading.Thread(target=worker, name=f'account-check-ttl-{entry_type}', daemon=True)
    thread.start()
    thread.join(timeout_seconds + 2)

    if thread.is_alive():
        result = {'status': 'timeout', 'reason': f'hard_timeout_after_{timeout_seconds}s'}
    elif 'error' in error_box:
        result = _classify_exception(error_box['error'])
    else:
        result = result_box.get('result', {'status': 'timeout', 'reason': 'empty_check_result'})

    result['path'] = str(path)
    result['entry_type'] = entry_type
    result['ttl_days'] = int(ttl_days)
    return result
