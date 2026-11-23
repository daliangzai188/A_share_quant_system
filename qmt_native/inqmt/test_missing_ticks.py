# coding: utf-8
"""Step 1 never lets partial data count as complete (2026-11-23, user requirement: a speed change must not
make "only part of the limit-up stocks fetched, time is up, treat it as done" possible).

The QMT-native collector (close pipeline step 1) on 20261013, a day whose limit-up candidates (67) all
have ticks in the mock export (the whole-day drill shows "无逐笔 0"):
1. half of the candidates have no ticks on the end date: it retries, then refuses; no limit list for the day;
2. killed (what the pipeline does at the step's time limit) after 20 candidates' ticks: the script ends, its
   queued QMT calls are dropped, and no limit list for the day appears afterwards;
3. all ticks present: the limit list is written with every candidate (all sealed, none without ticks) and the
   progress line is printed every 20 candidates.
QN_TEST_MISSING_TICKS_ARE_UNSEALED stays unset (inside QMT it is never set). Only the 30 s wait between tick
retry rounds is set to 0 here, so scenario 1 does not take 3 minutes.

Usage: python test_missing_ticks.py [CORE]   (QMT's Python 3.6; default inqmt/qn_core_full.py)
"""
import os, re, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__)); BASE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mock_qmt

DAY = '20261013'

ns = {'__name__': 'qn_core'}
for name in ('qn_runtime.py', sys.argv[1] if len(sys.argv) > 1 else 'qn_core_full.py'):
    exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
assert 'QN_TEST_MISSING_TICKS_ARE_UNSEALED' not in ns['QN_ENVIRON']
path, pkg, src = ns['QN_MODULES']['scripts.collect_all_data']
assert src.count('TICK_RETRY_WAIT = 30') == 1
ns['QN_MODULES']['scripts.collect_all_data'] = (path, pkg, src.replace('TICK_RETRY_WAIT = 30', 'TICK_RETRY_WAIT = 0'))
vfs = ns['QN_VFS']
for rel, text in ns['QN_STATIC_FILES'].items():
    vfs.put_text(rel, text)
vfs.put_bytes('data/raw/trade_calendar.csv', open(os.path.join(BASE, 'vm_raw/data/raw/trade_calendar.csv'), 'rb').read())
lists = '/Users/eass/Desktop/A_System/data/raw/limit_list'
for f in sorted(os.listdir(lists)):
    if '20260701' <= f[:8] <= '20261008':
        vfs.put_bytes('data/raw/limit_list/' + f, open(os.path.join(lists, f), 'rb').read())
market = mock_qmt.MockMarket(); market.today = DAY
real_tick_frame = market._tick_frame
mode = {'drop_half': False, 'slow': 0.0}
seen_day = []
day_calls = [0]


def tick_frame(day, code):
    if day == DAY:
        day_calls[0] += 1
        if code not in seen_day:
            seen_day.append(code)
        if mode['slow']:
            time.sleep(mode['slow'])
        if mode['drop_half'] and seen_day.index(code) % 2 == 0:
            return None
    return real_tick_frame(day, code)


market._tick_frame = tick_frame
service = ns['QN_NATIVE_SERVICE']; service.bind(market, mock_qmt.mock_globals())
stop = threading.Event()
threading.Thread(target=lambda: [service.pump() or time.sleep(0.002) for _ in iter(stop.is_set, True)], daemon=True).start()
codebase = ns['QnCodeBase'](ns['QN_MODULES'])
LIST = 'data/raw/limit_list/%s.csv' % DAY
ARGS = ['--start-date', '20261009', '--end-date', DAY, '--require-end-date-limit']


def limit_rows():
    if not vfs.isfile(LIST):
        return None
    return [l for l in vfs.get_text(LIST).splitlines()[1:] if l.strip()]


def run_direct():
    try:
        ns['QnLoader'](codebase).run_main('scripts.collect_all_data', ARGS)
        return 'ok', ''
    except BaseException as exc:
        return 'raised', str(exc)


ok = True
# 1. half of the candidates without ticks: refused, no limit list
mode['drop_half'] = True
result, why = run_direct()
one = result == 'raised' and '缺少逐笔数据' in why and '拒绝生成涨停池' in why and limit_rows() is None
retries = [l for l in ns['QN_PRINT_LINES'] if '涨停候选没有逐笔数据' in l]
print('1. half without ticks: %s | retry lines %d | limit list %s | %s' % (
    result, len(retries), 'absent' if limit_rows() is None else 'PRESENT', 'ok' if one else 'FAIL'))
print('   ', why[:90])
ok &= one

# 2. killed after 20 candidates' ticks: ends, nothing written afterwards
mode['drop_half'] = False
mode['slow'] = 0.15
del seen_day[:]
lines = []
proc = ns['QnProcess'](codebase, 'scripts.collect_all_data', ARGS, True)


def reader():
    while True:
        line = proc.readline()
        if not line:
            return
        lines.append(line)


threading.Thread(target=reader, daemon=True).start()
t0 = time.time()
while not any('%s 逐笔 20/' % DAY in l for l in lines) and time.time() - t0 < 300 and proc.poll() is None:
    time.sleep(0.05)
progress_seen = any('%s 逐笔 20/' % DAY in l for l in lines)
proc.kill()
proc._thread.join(5)
calls_at_kill = day_calls[0]
time.sleep(3)
two = (progress_seen and not proc._thread.is_alive() and proc.poll() == -9 and limit_rows() is None
       and day_calls[0] - calls_at_kill <= 1 and service.waiting() == 0)
print('2. killed at "%s": returncode %s | tick calls after kill %d | limit list %s | %s' % (
    [l for l in lines if '逐笔' in l][-1:] or 'no progress line', proc.poll(), day_calls[0] - calls_at_kill,
    'absent' if limit_rows() is None else 'PRESENT', 'ok' if two else 'FAIL'))
ok &= two

# 3. all ticks: written with every candidate
mode['slow'] = 0.0
del seen_day[:]
before = len(ns['QN_PRINT_LINES'])
result, why = run_direct()
out = ns['QN_PRINT_LINES'][before:]
stats = [l for l in out if '%s 日线' % DAY in l and '涨停候选' in l]
progress = [l for l in out if '%s 逐笔 ' % DAY in l]
rows = limit_rows() or []
m = re.search(r'涨停候选 (\d+)，收盘封住 (\d+)，未封住 (\d+)，无逐笔 (\d+)', stats[-1] if stats else '')
n = int(m.group(1)) if m else 0
three = (result == 'ok' and m is not None and n >= 40 and m.groups()[1:] == (str(n), '0', '0')
         and len(rows) == n and len(progress) == (n - 1) // 20)
print('3. all ticks: %s | limit list rows %d | progress lines %d | %s' % (
    result, len(rows), len(progress), 'ok' if three else 'FAIL'))
print('   ', (stats[-1] if stats else why)[-70:])
for l in progress:
    print('   ', l)
ok &= bool(three)
stop.set()
print('RESULT', 'PASS' if ok else 'FAIL')
