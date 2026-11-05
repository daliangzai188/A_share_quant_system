# coding: gbk
# A_System probe: read-only check of the live state store (QMT key-value store, prefix AS_QNL).
# Reads the two snapshot pointers, their manifests and every chunk of every file body, and
# prints which chunks are missing or short, QMT's return code for them and slow reads. Writes nothing. Paste into a scratch strategy
# (not A_SYSTEM_QMT_INNER) and press Run; read the [QN_STORE] lines in the log window.
import base64
import hashlib
import json
import time
import zlib

PREFIX = 'AS_QNL'


def _log(msg):
    print('[QN_STORE] ' + msg)


SLOW = []


def _get(key):
    t0 = time.time()
    r = globals()['get_py_key'](key)
    took = time.time() - t0
    if took > 3:
        SLOW.append((key[-30:], round(took, 1)))
    if isinstance(r, (tuple, list)) and len(r) == 3:
        return (r[0] or ''), r[1], str(r[2])[:80]
    return (r or ''), 0, ''


def _manifest(pointer):
    slot, n, digest = pointer.split(':')
    parts = [_get('%s_M%s_%d' % (PREFIX, slot, i)) for i in range(int(n))]
    packed = ''.join(p[0] for p in parts)
    ok = hashlib.sha256(packed.encode('ascii')).hexdigest()[:32] == digest
    _log('manifest %s: chunks %s digest ok=%s' % (pointer[:40], [len(p[0]) for p in parts], ok))
    if not ok:
        return None
    return json.loads(zlib.decompress(base64.b64decode(packed)).decode('utf-8'))


def _bodies(meta, label):
    files = meta.get('files', {})
    bad = 0
    total_keys = 0
    for rel, (sha, mtime, size, n) in sorted(files.items()):
        if sha is None:
            continue
        lens, codes, values = [], [], []
        for i in range(n):
            v, code, msg = _get('%s_F_%s_%d' % (PREFIX, sha, i))
            lens.append(len(v))
            values.append(v)
            if code:
                codes.append('%d:%s' % (code, msg))
            total_keys += 1
        packed = ''
        good = True
        try:
            packed = ''.join(values)
            data = zlib.decompress(base64.b64decode(packed))
            good = hashlib.sha256(data).hexdigest()[:32] == sha[:32]  # body id = sha32 + 'x' + tag (10-23 on)
        except Exception as exc:
            good = False
        if not good:
            bad += 1
            empty = [i for i, x in enumerate(lens) if x == 0]
            _log('%s BAD %s id=%s chunks=%s empty=%s lens=%s codes=%s mtime=%s' % (
                label, rel, sha[-20:], n, empty[:10], lens[:12], codes[:3], mtime))
    _log('%s: files=%d bodies_bad=%d chunk_keys=%d saved_at=%s' % (label, len(files), bad, total_keys, meta.get('saved_at')))


def init(ContextInfo):
    for key in ('_PTR', '_PTRPREV'):
        v, code, msg = _get(PREFIX + key)
        _log('%s = %r code=%s %s' % (key, v[:60], code, msg))
        if v:
            try:
                meta = _manifest(v)
                if meta:
                    _bodies(meta, key)
            except Exception as exc:
                _log('%s error %r' % (key, exc))
    for slot in ('A', 'B'):
        lens = [len(_get('%s_M%s_%d' % (PREFIX, slot, i))[0]) for i in range(6)]
        _log('slot %s first chunk lengths %s' % (slot, lens))
    _log('slow reads (>3 s): %d %s' % (len(SLOW), SLOW[:10]))
    _log('done')


def handlebar(ContextInfo):
    pass
