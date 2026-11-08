# coding: utf-8
"""Check qn_state_sink on this Windows machine with the real registry: feed a snapshot (one body
over 60000 characters, so it is split) into HKCU\\Software\\A_System\\QmtNative\\StateShadow (not
used by the live copy), read it back the way the strategy does, then remove every value it wrote.
Uploads the result to the Mac receiver. Usage: py -3.11 check_state_sink.py
"""
import base64, hashlib, json, os, sys, tempfile, urllib.request, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qn_state_sink as q

out = []
reg = q.WinRegistry('shadow')
if reg.names():
    out.append('StateShadow not empty (%d values): left alone, check not run' % len(reg.names()))
else:
    files = {'a.json': b'{"a": 1}', 'big.bin': b''.join(hashlib.sha256(str(i).encode()).digest() for i in range(3000))}
    lines, manifest = [], {}
    for rel, data in sorted(files.items()):
        bid = hashlib.sha256(data).hexdigest()[:32]
        manifest[rel] = [bid, 1.0, len(data), 0]
        packed = base64.b64encode(zlib.compress(data, 9)).decode('ascii')
        chunks = [packed[i:i + 1500] for i in range(0, len(packed), 1500)]
        lines += ['[A_SYSTEM_STATE shadow] B %s %d/%d %s' % (bid, i + 1, len(chunks), c) for i, c in enumerate(chunks)]
    mpacked = base64.b64encode(zlib.compress(json.dumps(dict(files=manifest), sort_keys=True).encode(), 9)).decode('ascii')
    mid = hashlib.sha256(mpacked.encode('ascii')).hexdigest()[:32]
    lines += ['[A_SYSTEM_STATE shadow] M %s 1/1 %s' % (mid, mpacked), '[A_SYSTEM_STATE shadow] C %s' % mid]
    sink = q.StateSink(reg, mode='shadow', disk_dir=tempfile.mkdtemp())
    notes = sink.feed(('\r\n'.join(lines) + '\r\n').encode('ascii'))
    out.append('notes: %s' % notes)
    pointer = reg.get('Pointer')
    out.append('pointer committed: %s' % (pointer == mid))
    ok = pointer == mid
    for rel, data in files.items():
        bid = manifest[rel][0]
        packed = sink._get_blob('B_' + bid)
        same = packed is not None and zlib.decompress(base64.b64decode(packed)) == data
        out.append('%s read back identical: %s (registry value %s)' % (rel, same, reg.get('B_' + bid)[:12]))
        ok &= same
    names = reg.names()
    out.append('values written: %d' % len(names))
    for name in names:
        reg.delete(name)
    out.append('values left after clean-up: %d' % len(reg.names()))
    out.append('RESULT %s' % ('PASS' if ok and not reg.names() else 'FAIL'))
text = '\n'.join(out)
print(text)
path = os.path.join(tempfile.gettempdir(), 'check_state_sink.txt')
open(path, 'w', encoding='utf-8').write(text)
req = urllib.request.Request('http://172.16.136.1:8765/upload/check_state_sink.txt', data=text.encode('utf-8'), method='PUT')
urllib.request.urlopen(req, timeout=60).read()
