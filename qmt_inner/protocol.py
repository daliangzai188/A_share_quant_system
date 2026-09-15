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
        # Windows readers/Defender can briefly hold the destination without
        # FILE_SHARE_DELETE. Retry only the atomic rename, never a broker action.
        for attempt in range(12):
            try:
                os.replace(str(tmp), str(path))
                break
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(0.005 * (attempt + 1))
    finally:
        if tmp.exists():
            tmp.unlink()


def read_json(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        return json.load(stream)


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
                response_path.unlink()
                if not result.get('ok'):
                    raise RuntimeError('QMT_INNER: ' + str(result.get('error', 'unknown result')))
                return result['result']
            time.sleep(0.02)
        # NEVER replay a timed-out mutation. The server checks expiry immediately
        # before touching the broker; anything already submitted needs reconciliation.
        raise TimeoutError('QMT inner request timed out; result UNKNOWN; do not resubmit: ' + method)
