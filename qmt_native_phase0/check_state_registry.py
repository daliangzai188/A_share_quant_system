# coding: utf-8
"""Read-only: read the committed A_System snapshot back from HKCU\\Software\\A_System\\QmtNative\\StateLive
the way the strategy does at a start (manifest and every file body checked against its checksum).
Prints and uploads a short summary (file names and sizes only). Usage: py -3.11 check_state_registry.py
"""
import base64, hashlib, json, os, sys, tempfile, urllib.request, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qn_state_sink as q

reg = q.WinRegistry('live')
sink = q.StateSink(reg, mode='live')
out = []
pointer, prev = reg.get('Pointer'), reg.get('PointerPrev')
out.append('Pointer %s  PointerPrev %s  CommittedAt %s  Missing %s' % (
    (pointer or '-')[:12], (prev or '-')[:12], reg.get('CommittedAt'), (reg.get('Missing') or '-')[:40]))
ok = False
if pointer:
    mpacked = sink._get_blob('M_' + pointer)
    good_m = mpacked is not None and hashlib.sha256(mpacked.encode('ascii')).hexdigest()[:32] == pointer
    meta = json.loads(zlib.decompress(base64.b64decode(mpacked)).decode('utf-8')) if good_m else {}
    files = meta.get('files', {})
    bad, total = [], 0
    for rel, (bid, mtime, size, _n) in sorted(files.items()):
        if not bid:
            continue
        packed = sink._get_blob('B_' + bid)
        try:
            data = zlib.decompress(base64.b64decode(packed))
            if hashlib.sha256(data).hexdigest()[:32] != bid:
                raise ValueError('checksum')
            total += len(data)
        except Exception as exc:
            bad.append('%s (%s)' % (rel, exc))
    out.append('manifest ok %s, saved_at %s, files %d, bytes %d, bad %d' % (good_m, meta.get('saved_at'), len(files), total, len(bad)))
    out += ['  bad: ' + b for b in bad[:10]]
    out.append('largest: %s' % sorted(((v[2] or 0), k) for k, v in files.items())[-3:])
    ok = good_m and not bad and len(files) > 0
out.append('registry values: %d' % len(reg.names()))
out.append('RESULT %s' % ('PASS' if ok else 'FAIL'))
text = '\n'.join(out)
print(text)
req = urllib.request.Request('http://172.16.136.1:8765/upload/check_state_registry.txt', data=text.encode('utf-8'), method='PUT')
urllib.request.urlopen(req, timeout=60).read()
