import json, os, subprocess, sys
from collections import OrderedDict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from replay import steps_for, trade_days
HERE = os.path.dirname(os.path.abspath(__file__))
ws = os.path.join(HERE, 'trace_ws')
day = sys.argv[1]
cal = trade_days(__import__('pathlib').Path(ws))
start = cal[max(0, cal.index(day) - 2)]
for name, cmd in steps_for(day, start):
    log = os.path.join(HERE, 'trace_logs', '%s_%s.json' % (day, name))
    env = {'PATH': '/usr/bin:/bin', 'HOME': ws, 'PYTHONUTF8': '1', 'TZ': 'Asia/Shanghai', 'TRACE_LOG': log}
    p = subprocess.run([os.path.join(HERE, 'venv/bin/python'), '-B', os.path.join(HERE, 'trace_run.py')] + cmd,
                       cwd=ws, env=env, capture_output=True, text=True)
    ev = json.load(open(log))
    reads, writes, lists = OrderedDict(), OrderedDict(), OrderedDict()
    for kind, path, mode in ev:
        if path.startswith(('src/', 'scripts/')) and path.endswith('.py'):
            continue
        if kind == 'open':
            (writes if any(c in mode for c in 'wax+') else reads)[path] = 1
        else:
            lists[kind + ' ' + path] = 1
    print('=== %s rc=%d' % (name, p.returncode))
    if p.returncode:
        print(p.stdout[-1500:], p.stderr[-1500:])
    def summarize(d):
        groups = OrderedDict()
        for k in d:
            parts = k.split('/')
            key = '/'.join(parts[:-1]) + '/*' if len(parts) > 2 and parts[-1][:8].isdigit() else k
            groups[key] = groups.get(key, 0) + 1
        return ['%s%s' % (k, (' x%d' % n) if n > 1 else '') for k, n in groups.items()]
    print(' R:', '\n    '.join(summarize(reads)))
    print(' W:', '\n    '.join(summarize(writes)))
    print(' L:', '\n    '.join(lists))
