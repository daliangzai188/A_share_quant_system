# coding: utf-8
"""End-to-end chain inside the bundle (Python 3.6, in-memory files, mock QMT):
native collection -> clean/features/fill/A-C/E -> incremental step 13, day by day.

Usage: python test_chain36.py CORE EQ_SEED_DIR OUT DAY [DAY ...]
"""
import os
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from mock_qmt import MockMarket, mock_globals  # noqa: E402


def main():
    core, seed, out = sys.argv[1:4]
    days = sys.argv[4:]
    ns = {'__name__': 'qn_core'}
    for name in ('qn_runtime.py', core):
        exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
    vfs = ns['QN_VFS']
    for rel, text in ns['QN_STATIC_FILES'].items():
        vfs.put_text(rel, text)
    vfs.put_bytes('data/raw/trade_calendar.csv', open(os.path.join(BASE, 'vm_raw/data/raw/trade_calendar.csv'), 'rb').read())
    lists = '/Users/eass/Desktop/A_System/data/raw/limit_list'
    for f in sorted(os.listdir(lists)):
        if '20260701' <= f[:8] <= '20261008':
            vfs.put_bytes('data/raw/limit_list/' + f, open(os.path.join(lists, f), 'rb').read())
    for root, _, files in os.walk(seed):
        if 'tmp_replay' in root:
            continue
        for f in files:
            full = os.path.join(root, f)
            vfs.put_bytes(os.path.relpath(full, seed), open(full, 'rb').read())
    ns['QN_ENVIRON']['QN_TEST_MISSING_TICKS_ARE_UNSEALED'] = '1'
    market = MockMarket()
    service = ns['QN_NATIVE_SERVICE']
    service.bind(market, mock_globals())
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            service.pump()
            time.sleep(0.002)
    threading.Thread(target=pump, daemon=True).start()
    cb = ns['QnCodeBase'](ns['QN_MODULES'])
    import csv
    rows = list(csv.DictReader(open(os.path.join(BASE, 'vm_raw/data/raw/trade_calendar.csv'), encoding='utf-8-sig')))
    cal = sorted(set(r['cal_date'] for r in rows if str(r.get('is_open')) in ('1', '1.0')))
    for day in days:
        market.today = day
        start = cal[cal.index(day) - 2]
        t0 = time.time()
        try:
            ns['QnLoader'](cb).run_main('scripts.collect_all_data', ['--start-date', start, '--end-date', day,
                                                                     '--require-end-date-limit'])
            steps = ns['qn_run_close_pipeline'](ns['QnLoader'](cb), day, start)
            ns['QnLoader'](cb).run_main('scripts.update_equity_curve_stop', ['--signal-date', day])
        except Exception:
            traceback.print_exc()
            print('\n'.join(ns['QN_PRINT_LINES'][-12:]).encode('ascii', 'backslashreplace').decode())
            break
        eq = [l for l in ns['QN_PRINT_LINES'] if l.startswith('EQUITY_CURVE_STOP ')][-1:]
        print(day, 'chain %.0fs' % (time.time() - t0), steps, (eq[0][:40] if eq else '').encode('ascii', 'backslashreplace').decode())
    stop.set()
    for rel in vfs.walk_files():
        if rel.startswith(('reports/', 'data/state/', 'qn_state/equity_stop/state', 'data/raw/limit_list/')):
            dest = os.path.join(out, rel)
            if not os.path.isdir(os.path.dirname(dest)):
                os.makedirs(os.path.dirname(dest))
            open(dest, 'wb').write(vfs.read(rel))


if __name__ == '__main__':
    main()
