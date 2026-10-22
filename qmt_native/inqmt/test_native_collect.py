# coding: utf-8
"""Run the bundled QMT-native collector (step 1 replacement) day by day against the
mock QMT API and compare its raw tables with the validated native tables and Tushare.

Usage: python test_native_collect.py CORE_FILE OUT_DIR DAY [DAY ...]
"""
import csv
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
    core, out = sys.argv[1], sys.argv[2]
    days = sys.argv[3:]
    ns = {'__name__': 'qn_core'}
    for name in ('qn_runtime.py', core):
        exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
    vfs = ns['QN_VFS']
    for rel, text in ns['QN_STATIC_FILES'].items():
        vfs.put_text(rel, text)
    vfs.put_bytes('data/raw/trade_calendar.csv', open(os.path.join(BASE, 'vm_raw/data/raw/trade_calendar.csv'), 'rb').read())
    tushare_lists = '/Users/eass/Desktop/A_System/data/raw/limit_list'
    for f in sorted(os.listdir(tushare_lists)):
        if '20260701' <= f[:8] <= '20261008':
            vfs.put_bytes('data/raw/limit_list/' + f, open(os.path.join(tushare_lists, f), 'rb').read())
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
    codebase = ns['QnCodeBase'](ns['QN_MODULES'])
    rows = list(csv.DictReader(open(os.path.join(BASE, 'vm_raw/data/raw/trade_calendar.csv'), encoding='utf-8-sig')))
    cal = sorted(set(r['cal_date'] for r in rows if str(r.get('is_open')) in ('1', '1.0')))
    ok = True
    for day in days:
        market.today = day
        start = cal[cal.index(day) - 2]
        t0 = time.time()
        try:
            ns['QnLoader'](codebase).run_main('scripts.collect_all_data',
                                              ['--start-date', start, '--end-date', day, '--require-end-date-limit'])
        except Exception:
            traceback.print_exc()
            ok = False
            break
        print(day, 'collected in %.1fs' % (time.time() - t0))
    stop.set()
    for line in ns['QN_PRINT_LINES']:
        if line.startswith('QMT'):
            sys.stdout.buffer.write((line + '\n').encode('utf-8'))
    sys.stdout.flush()
    for rel in vfs.walk_files('data/raw'):
        dest = os.path.join(out, rel)
        if not os.path.isdir(os.path.dirname(dest)):
            os.makedirs(os.path.dirname(dest))
        open(dest, 'wb').write(vfs.read('data/raw/' + rel))
    print('calls', market.calls, 'ok', ok)


if __name__ == '__main__':
    main()
