# coding: utf-8
"""Run the bundled incremental step 13 day by day on the in-memory store (Python 3.6)."""
import os, sys, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__)); BASE = os.path.dirname(HERE)
def main():
    core, ws, seed, out = sys.argv[1:5]; days = sys.argv[5:]
    ns = {'__name__': 'qn_core'}
    for name in ('qn_runtime.py', core):
        exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
    vfs = ns['QN_VFS']
    for rel, text in ns['QN_STATIC_FILES'].items(): vfs.put_text(rel, text)
    for sub in ('daily', 'daily_basic', 'limit_list'):
        d = os.path.join(ws, 'data/raw', sub)
        for f in sorted(os.listdir(d)):
            if '20260901' <= f[:8] <= '20261231': vfs.put_bytes('data/raw/%s/%s' % (sub, f), open(os.path.join(d, f), 'rb').read())
    vfs.put_bytes('data/raw/trade_calendar.csv', open(os.path.join(ws, 'data/raw/trade_calendar.csv'), 'rb').read())
    for root, _, files in os.walk(seed):
        for f in files:
            full = os.path.join(root, f); vfs.put_bytes(os.path.relpath(full, seed), open(full, 'rb').read())
    cb = ns['QnCodeBase'](ns['QN_MODULES'])
    for day in days:
        t0 = time.time()
        try:
            ns['QnLoader'](cb).run_main('scripts.update_equity_curve_stop', ['--signal-date', day])
        except Exception:
            traceback.print_exc(); print('\n'.join(ns['QN_PRINT_LINES'][-15:]).encode('ascii', 'backslashreplace').decode()); break
        last = [l for l in ns['QN_PRINT_LINES'] if l.startswith('EQUITY_CURVE_STOP ')][-1:]
        print(day, '%.1fs' % (time.time() - t0), (last[0] if last else '').encode('ascii', 'backslashreplace').decode()[:60])
    open(out, 'wb').write(vfs.read('reports/equity_curve_stop/shadow_nav_latest.csv'))
main()
