# coding: utf-8
"""Collector against a QMT cache missing 10% of the bars of a past day (20261016; rows stay above the old 4000-row floor): it must refuse
that day, and pass once the bars are back.

Usage: python test_partial_day.py   (QMT's Python 3.6; inqmt/qn_core_full.py + the mock QMT exports)
"""
import os, sys, threading, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__)); BASE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mock_qmt
ns = {'__name__': 'qn_core'}
for name in ('qn_runtime.py', 'qn_core_full.py'):
    exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
vfs = ns['QN_VFS']
for rel, text in ns['QN_STATIC_FILES'].items():
    vfs.put_text(rel, text)
vfs.put_bytes('data/raw/trade_calendar.csv', open(os.path.join(BASE, 'vm_raw/data/raw/trade_calendar.csv'), 'rb').read())
lists = '/Users/eass/Desktop/A_System/data/raw/limit_list'
for f in sorted(os.listdir(lists)):
    if '20260701' <= f[:8] <= '20261008':
        vfs.put_bytes('data/raw/limit_list/' + f, open(os.path.join(lists, f), 'rb').read())
ns['QN_ENVIRON']['QN_TEST_MISSING_TICKS_ARE_UNSEALED'] = '1'
market = mock_qmt.MockMarket(); market.today = '20261019'
full = dict(market.bars)
codes = sorted(full)
for c in codes[::10]:          # 10% of the stocks lose 20261016 (rows stay above the 4000 floor)
    market.bars[c] = full[c][full[c]['date'] != '20261016']
service = ns['QN_NATIVE_SERVICE']; service.bind(market, mock_qmt.mock_globals())
stop = threading.Event()
threading.Thread(target=lambda: [service.pump() or time.sleep(0.002) for _ in iter(stop.is_set, True)], daemon=True).start()
codebase = ns['QnCodeBase'](ns['QN_MODULES'])
def run():
    try:
        ns['QnLoader'](codebase).run_main('scripts.collect_all_data', ['--start-date', '20261016', '--end-date', '20261019', '--require-end-date-limit'])
        return 'ok'
    except BaseException as exc:
        return 'refused' if 'QMT 本地缓存未补全' in ''.join(ns['QN_PRINT_LINES'][-40:]) or 'QMT 本地缓存未补全' in str(exc) else 'error %r' % exc
first = run()
refusal = [l for l in ns['QN_PRINT_LINES'] if '本地缓存未补全' in l]
market.bars = full
second = run()
stop.set()
print('partial 20261016:', first, '|', (refusal[-1][-90:] if refusal else ''))
print('bars restored:', second)
print('RESULT', 'PASS' if first == 'refused' and second == 'ok' else 'FAIL')
