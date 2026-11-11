# coding: utf-8
"""Read-only: copy files of the committed A_System snapshot (registry StateLive) into a zip and
upload it to the Mac receiver, for reviews. Every body is checked against its checksum.
Usage: py -3.11 dump_state_registry.py [regex ...]   (default: review files)
"""
import base64, hashlib, io, json, os, re, sys, urllib.request, zipfile, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qn_state_sink as q

patterns = sys.argv[1:] or [r'^reports/equity_curve_stop/', r'^data/state/[^/]+\.json$', r'^qn_state/equity_stop/',
                            r'^reports/live_trade/combined/', r'^reports/strategy_e/', r'^reports/account_risk_shadow/',
                            r'^reports/execution_tracking/']
reg = q.WinRegistry('live')
sink = q.StateSink(reg, mode='live')
pointer = reg.get('Pointer')
mpacked = sink._get_blob('M_' + pointer)
assert hashlib.sha256(mpacked.encode('ascii')).hexdigest()[:32] == pointer, 'manifest checksum'
meta = json.loads(zlib.decompress(base64.b64decode(mpacked)).decode('utf-8'))
buf, n, bad = io.BytesIO(), 0, []
with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
    for rel, (bid, mtime, size, _n) in sorted(meta['files'].items()):
        if not bid or not any(re.search(p, rel) for p in patterns):
            continue
        data = zlib.decompress(base64.b64decode(sink._get_blob('B_' + bid)))
        if hashlib.sha256(data).hexdigest()[:32] != bid:
            bad.append(rel)
            continue
        z.writestr(rel, data)
        n += 1
    z.writestr('_snapshot.json', json.dumps({'pointer': pointer, 'saved_at': meta.get('saved_at'), 'files': n, 'bad': bad}))
req = urllib.request.Request('http://172.16.136.1:8765/upload/state_dump.zip', data=buf.getvalue(), method='PUT')
urllib.request.urlopen(req, timeout=120).read()
print('uploaded state_dump.zip: %d files, saved_at %s, bad %s' % (n, meta.get('saved_at'), bad))
