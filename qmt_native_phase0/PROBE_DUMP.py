# coding: gbk
# A_System probe: read-only copy of selected files from the live state store (QMT key-value store,
# prefix AS_QNL) for a strategy review on the Mac. Reads the committed snapshot pointer, its
# manifest and the bodies of the selected files, checks every body against its checksum and prints
# one bundle as [QN_DUMP] lines (zlib + base64, 1500 characters each). Writes nothing.
# Paste into a scratch strategy (not A_SYSTEM_QMT_INNER) and press Run.
import base64
import hashlib
import json
import re
import time
import zlib

PREFIX = 'AS_QNL'
CHUNK = 1500
WANT = (
    r'^reports/equity_curve_stop/',
    r'^data/state/[^/]+\.json$',
    r'^qn_state/equity_stop/state\.json$',
    r'^reports/account_risk_shadow/',
    r'^reports/strategy_e/[^/]+\.json$',
    r'^reports/execution_tracking/[^/]+\.(csv|json)$',
    r'^reports/live_trade/combined/combined_(decisions|planned_orders|state)_[0-9]{8}\.csv$',
    r'^reports/live_trade/qmt_live_order_submitted_orders\.csv$',
)
MAX_FILE = 3000000


def _log(msg):
    print('[QN_DUMP] ' + msg)


def _get(key):
    r = globals()['get_py_key'](key)
    if isinstance(r, (tuple, list)) and len(r) == 3:
        return (r[0] or ''), r[1], str(r[2])[:80]
    return (r or ''), 0, ''


def _manifest(pointer):
    slot, n, digest = pointer.split(':')
    packed = ''.join(_get('%s_M%s_%d' % (PREFIX, slot, i))[0] for i in range(int(n)))
    if hashlib.sha256(packed.encode('ascii')).hexdigest()[:32] != digest:
        return None
    return json.loads(zlib.decompress(base64.b64decode(packed)).decode('utf-8'))


def init(ContextInfo):
    t0 = time.time()
    want = [re.compile(p) for p in WANT]
    meta = None
    for key in ('_PTR', '_PTRPREV'):
        pointer = _get(PREFIX + key)[0]
        if pointer:
            meta = _manifest(pointer)
            if meta:
                _log('snapshot %s saved_at=%s files=%d' % (key, meta.get('saved_at'), len(meta.get('files', {}))))
                break
            _log('snapshot %s manifest unreadable' % key)
    if not meta:
        _log('no readable snapshot')
        return
    bundle, bad, skipped = {}, [], []
    for rel, (sha, mtime, size, n) in sorted(meta.get('files', {}).items()):
        if sha is None or not any(p.search(rel) for p in want):
            continue
        if size and size > MAX_FILE:
            skipped.append(rel)
            continue
        try:
            packed = ''.join(_get('%s_F_%s_%d' % (PREFIX, sha, i))[0] for i in range(n))
            data = zlib.decompress(base64.b64decode(packed))
            if hashlib.sha256(data).hexdigest()[:32] != sha[:32]:
                raise ValueError('checksum')
            bundle[rel] = [base64.b64encode(data).decode('ascii'), mtime]
        except Exception as exc:
            bad.append('%s (%s)' % (rel, str(exc)[:40]))
    raw = json.dumps({'saved_at': meta.get('saved_at'), 'files': bundle}).encode('utf-8')
    packed = base64.b64encode(zlib.compress(raw, 9)).decode('ascii')
    chunks = [packed[i:i + CHUNK] for i in range(0, len(packed), CHUNK)]
    ident = hashlib.sha256(packed.encode('ascii')).hexdigest()[:12]
    _log('bundle %s files=%d bad=%d skipped=%d chunks=%d read_s=%.0f' % (
        ident, len(bundle), len(bad), len(skipped), len(chunks), time.time() - t0))
    for item in bad + ['skipped ' + s for s in skipped]:
        _log('not copied: ' + item)
    for i, chunk in enumerate(chunks):
        print('[QN_DUMP %s %d/%d] %s' % (ident, i + 1, len(chunks), chunk))
    _log('done %s' % ident)


def handlebar(ContextInfo):
    pass
