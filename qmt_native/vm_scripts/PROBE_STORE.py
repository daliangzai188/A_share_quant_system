# coding: gbk
# A_System probe: read-only check of the live state store (QMT key-value store, prefix AS_QNL).
# Reads the two snapshot pointers, their manifests and every chunk of every file body, and
# prints which chunks are missing or short. Writes nothing. Paste into a scratch strategy
# (not A_SYSTEM_QMT_INNER) and press Run; read the [QN_STORE] lines in the log window.
import base64
import hashlib
import json
import zlib

PREFIX = 'AS_QNL'


def _log(msg):
    print('[QN_STORE] ' + msg)


def _get(key):
    r = globals()['get_py_key'](key)
    if isinstance(r, (tuple, list)) and len(r) == 3:
        return (r[0] or ''), r[1], str(r[2])[:60]
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
        lens, codes = [], []
        for i in range(n):
            v, code, msg = _get('%s_F_%s_%d' % (PREFIX, sha, i))
            lens.append(len(v))
            if code:
                codes.append('%d:%s' % (code, msg))
            total_keys += 1
        packed = ''
        good = True
        try:
            packed = ''.join(_get('%s_F_%s_%d' % (PREFIX, sha, i))[0] for i in range(n))
            data = zlib.decompress(base64.b64decode(packed))
            good = hashlib.sha256(data).hexdigest()[:32] == sha
        except Exception as exc:
            good = False
        if not good:
            bad += 1
            _log('%s BAD %s sha=%s chunks=%s lens=%s codes=%s mtime=%s' % (label, rel, sha[:12], n, lens, codes[:2], mtime))
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
    _log('done')


def handlebar(ContextInfo):
    pass
