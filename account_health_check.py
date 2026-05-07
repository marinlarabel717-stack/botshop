import asyncio
import os
import tempfile
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
except Exception:
    TelethonClient = None


SESSION_CHECK_API_ID = os.getenv('ACCOUNT_CHECK_API_ID', '').strip()
SESSION_CHECK_API_HASH = os.getenv('ACCOUNT_CHECK_API_HASH', '').strip()
DEFAULT_TIMEOUT_SECONDS = max(5, int(os.getenv('ACCOUNT_CHECK_TIMEOUT_SECONDS', '25') or '25'))


class DependencyUnavailable(RuntimeError):
    pass


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

    client = None
    if SESSION_CHECK_API_ID and SESSION_CHECK_API_HASH and TelethonClient is not None:
        try:
            client = TelethonClient(str(session_path), int(SESSION_CHECK_API_ID), SESSION_CHECK_API_HASH, receive_updates=False)
        except Exception as exc:
            return {'status': 'timeout', 'reason': f'session_client_init_failed:{exc}'}
    elif OpenTeleClient is not None and API is not None:
        try:
            client = OpenTeleClient(str(session_path), api=API.TelegramDesktop, receive_updates=False)
        except Exception as exc:
            return {'status': 'timeout', 'reason': f'session_client_init_failed:{exc}'}
    else:
        raise DependencyUnavailable('missing_session_check_dependencies')

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


def check_account_inventory_item(entry_type: str, target_path: str, timeout_seconds: int | None = None) -> Dict[str, Any]:
    timeout_seconds = max(5, int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS))
    path = Path(target_path)
    try:
        if entry_type == '协议号':
            result = _run_async(_check_session_async(path, timeout_seconds))
        elif entry_type == '直登号':
            result = _run_async(_check_tdata_async(path, timeout_seconds))
        else:
            result = {'status': 'timeout', 'reason': f'unsupported_entry_type:{entry_type}'}
    except Exception as exc:
        result = _classify_exception(exc)

    result['path'] = str(path)
    result['entry_type'] = entry_type
    return result
