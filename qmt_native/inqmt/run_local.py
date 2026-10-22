# coding: utf-8
"""Run the bundled close pipeline from the in-memory file store, outside QMT.

Seeds the store from a pristine replay workspace (the same initial state the
reference replay started from), runs the days in order and writes every file the
pipeline produced into OUT for comparison. Python 3.6 compatible.

Usage: python run_local.py SEED_WS OUT DAY [DAY ...]
"""
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))


def load_core():
    ns = {'__name__': 'qn_core'}
    for name in ('qn_runtime.py', 'qn_core_generated.py'):
        path = os.path.join(HERE, name)
        exec(compile(open(path, encoding='utf-8').read(), path, 'exec'), ns)
    return ns


def seed(vfs, ws, first_day, static_files):
    count = 0
    for rel, text in static_files.items():
        vfs.put_text(rel, text)
        count += 1
    wanted_dirs = ['data/processed', 'reports/strategy_e', 'reports/paper_trade/ab_filtered_daily_ops']
    for d in wanted_dirs:
        base = os.path.join(ws, d)
        for root, _, files in os.walk(base):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, ws).replace(os.sep, '/')
                vfs.put_bytes(rel, open(full, 'rb').read())
                count += 1
    for sub in ('daily', 'daily_basic', 'limit_list'):
        base = os.path.join(ws, 'data/raw', sub)
        for f in sorted(os.listdir(base)):
            if f.endswith('.csv') and f[:8] >= first_day:
                vfs.put_bytes('data/raw/%s/%s' % (sub, f), open(os.path.join(base, f), 'rb').read())
                count += 1
    vfs.put_bytes('data/raw/trade_calendar.csv', open(os.path.join(ws, 'data/raw/trade_calendar.csv'), 'rb').read())
    return count + 1


def trade_days(vfs):
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(vfs.get_text('data/raw/trade_calendar.csv'))))
    return sorted(set(r['cal_date'] for r in rows if str(r.get('is_open')) in ('1', '1.0', 'True')))


def _show_warning(message, category, filename, lineno, file=None, line=None):
    import traceback as tb
    stack = [f for f in tb.extract_stack()[:-1] if '/A_System/' in f[0] or 'qn_runtime' in f[0]]
    where = ' <- '.join('%s:%s' % (f[0].split('/')[-1], f[1]) for f in reversed(stack[-4:]))
    sys.stderr.write('WARNING %s: %s | %s\n' % (category.__name__, str(message).splitlines()[0], where))


def main():
    import warnings
    warnings.simplefilter('always')
    warnings.showwarning = _show_warning
    seed_ws, out = sys.argv[1], sys.argv[2]
    days = sys.argv[3:]
    ns = load_core()
    vfs = ns['QN_VFS']
    t0 = time.time()
    loader = ns['QnLoader'](ns['QN_MODULES'])
    print('core loaded and compiled in %.1fs' % (time.time() - t0))
    import csv as _csv
    cal_rows = list(_csv.DictReader(open(os.path.join(seed_ws, 'data/raw/trade_calendar.csv'), encoding='utf-8-sig')))
    cal0 = sorted(set(r['cal_date'] for r in cal_rows if str(r.get('is_open')) in ('1', '1.0', 'True')))
    first = cal0[max(0, cal0.index(days[0]) - 10)]
    n = seed(vfs, seed_ws, first, ns['QN_STATIC_FILES'])
    print('seeded %d files' % n)
    cal = trade_days(vfs)
    status = {}
    for day in days:
        start = cal[max(0, cal.index(day) - 2)]
        try:
            status[day] = ns['qn_run_close_pipeline'](loader, day, start)
            print(day, status[day])
        except Exception:
            status[day] = 'ERROR'
            traceback.print_exc()
            tail = '\n'.join(ns['QN_LOG_LINES'][-15:] + ns['QN_PRINT_LINES'][-10:])
            print('LOG TAIL:\n' + tail.encode('ascii', 'backslashreplace').decode('ascii'))
            break
        snap = os.path.join(out, 'snapshots', day)
        os.makedirs(snap, exist_ok=True)
        for rel in list(ns['LIVE'].values()) + ['reports/strategy_e/e_signal_runs_recent.json']:
            if vfs.isfile(rel):
                open(os.path.join(snap, rel.split('/')[-1]), 'wb').write(vfs.read(rel))
    for rel in vfs.walk_files():
        dest = os.path.join(out, 'vfs', rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, 'wb').write(vfs.read(rel))
    json.dump({'python': sys.version.split()[0], 'pandas': ns['_pd'].__version__, 'status': status},
              open(os.path.join(out, 'status.json'), 'w'), indent=1)
    print('total %.1fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
