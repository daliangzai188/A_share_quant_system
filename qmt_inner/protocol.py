# coding: utf-8
"""Local, authenticated file transport. Python 3.6 compatible; no xtquant."""
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

PROTOCOL = 1

# Windows Defender、索引器或另一个 Python 进程可能会短暂以不共享删除/读取的
# 方式打开文件。文件通道本身仍然正常时，这类 sharing violation 不能被上层
# 解释成 QMT 账户断开。总等待约 1.5 秒，仍失败则保持原异常并 fail-closed。
_FILE_BUSY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.12, 0.18, 0.25, 0.35, 0.50)


def _is_file_busy(exc):
    return isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in (5, 32, 33)


def _retry_file_operation(operation):
    for attempt in range(len(_FILE_BUSY_DELAYS) + 1):
        try:
            return operation()
        except OSError as exc:
            if not _is_file_busy(exc) or attempt >= len(_FILE_BUSY_DELAYS):
                raise
            time.sleep(_FILE_BUSY_DELAYS[attempt])


def remove_file(path):
    """删除协议临时文件；仅重试 Windows 短暂文件占用。"""
    path = Path(path)
    try:
        _retry_file_operation(path.unlink)
        return True
    except FileNotFoundError:
        return False


def _read_bytes_shared(path):
    """Windows 读取句柄显式共享删除，允许服务端同时原子替换心跳文件。"""
    path = Path(path)
    if os.name != 'nt':
        with path.open('rb') as stream:
            return stream.read()

    # CPython/Windows 的普通 open 在部分版本上没有 FILE_SHARE_DELETE；当另一端
    # 对 heartbeat.json 做 os.replace 时会产生 WinError 5/32。直接用 Win32
    # 共享读句柄消除协议双方自身的竞争，外层重试只处理 Defender 等外部占用。
    import ctypes
    import msvcrt
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path), 0x80000000, 0x00000001 | 0x00000002 | 0x00000004,
        None, 3, 0x00000080, None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        exc = ctypes.WinError(error)
        exc.filename = str(path)
        raise exc
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
    except Exception:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise
    with os.fdopen(fd, 'rb') as stream:
        return stream.read()


def lock_owner(stream):
    """Non-blocking exclusive ownership, shared by engine and installer."""
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        if stream.read(1) == b'':
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def signature(value, token):
    return hmac.new(token.encode('utf-8'), canonical(value).encode('utf-8'), hashlib.sha256).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        # 这里只重试本地原子换名，绝不重放券商查询、下单或撤单动作。
        _retry_file_operation(lambda: os.replace(str(tmp), str(path)))
    finally:
        if tmp.exists():
            try:
                remove_file(tmp)
            except OSError:
                # 清理失败不能覆盖真正的写入结果/异常；残留 .tmp 不会被协议消费。
                pass


def read_json(path):
    path = Path(path)

    def _read():
        return json.loads(_read_bytes_shared(path).decode('utf-8-sig'))

    return _retry_file_operation(_read)


def config_path():
    explicit = os.environ.get('QMT_INNER_CONFIG', '')
    if explicit:
        return Path(explicit)
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        raise RuntimeError('QMT_INNER_CONFIG or Windows LOCALAPPDATA is required')
    return Path(local) / 'A_System' / 'qmt_inner' / 'config.json'


def load_settings(path=None):
    cfg = read_json(path or config_path())
    if len(str(cfg.get('token', ''))) < 32:
        raise RuntimeError('Invalid local bridge token')
    if not cfg.get('account_id') or cfg.get('account_type', 'STOCK').upper() != 'STOCK':
        raise RuntimeError('Only an explicitly configured STOCK account is supported')
    return cfg


class FileClient:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings['spool_dir'])
        self.token = settings['token']
        self.instance = None
        self.timeout = float(settings.get('request_timeout_seconds', 8))
        self.query_timeout = float(settings.get('query_timeout_seconds', self.timeout))
        if not 0 < min(self.timeout, self.query_timeout) <= max(self.timeout, self.query_timeout) <= 30:
            raise ValueError('Bridge timeouts must be positive and at most 30 seconds')

    def heartbeat(self):
        envelope = read_json(self.root / 'heartbeat.json')
        body = envelope['body']
        if not hmac.compare_digest(envelope['signature'], signature(body, self.token)):
            raise RuntimeError('Bridge heartbeat authentication failed')
        age = time.time() - body['time']
        if age < -2 or age > float(self.settings.get('heartbeat_max_age_seconds', 5)):
            raise RuntimeError('QMT inner bridge heartbeat is stale')
        if body.get('account_id') != self.settings['account_id'] or body.get('protocol') != PROTOCOL:
            raise RuntimeError('QMT inner bridge account/protocol mismatch')
        return body

    def connect(self):
        self.instance = self.heartbeat()['instance']
        return self.call('hello', {})

    def call(self, method, params):
        hb = self.heartbeat()
        if self.instance != hb['instance']:
            raise RuntimeError('QMT inner bridge restarted; reconnect and reconcile before trading')
        rid = uuid.uuid4().hex
        now = time.time()
        timeout = self.timeout if method in ('submit', 'cancel') else self.query_timeout
        body = dict(protocol=PROTOCOL, id=rid, instance=self.instance,
                    account_id=self.settings['account_id'], created=now,
                    expires=now + timeout, method=method, params=params)
        request_path = self.root / 'requests' / (rid + '.json')
        response_path = self.root / 'responses' / (rid + '.json')
        atomic_json(request_path, dict(body=body, signature=signature(body, self.token)))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if response_path.exists():
                reply = read_json(response_path)
                result = reply['body']
                if not hmac.compare_digest(reply['signature'], signature(result, self.token)):
                    raise RuntimeError('Bridge response authentication failed')
                if result.get('id') != rid or result.get('instance') != self.instance:
                    raise RuntimeError('Bridge response identity mismatch')
                # 响应已经完成验签并匹配本次请求，清理文件失败不能把一次成功的
                # 账户查询误报为断链。残留响应使用唯一 rid，不会被后续请求复用。
                try:
                    remove_file(response_path)
                except OSError:
                    pass
                if not result.get('ok'):
                    raise RuntimeError('QMT_INNER: ' + str(result.get('error', 'unknown result')))
                return result['result']
            time.sleep(0.02)
        # NEVER replay a timed-out mutation. The server checks expiry immediately
        # before touching the broker; anything already submitted needs reconciliation.
        raise TimeoutError('QMT inner request timed out; result UNKNOWN; do not resubmit: ' + method)
