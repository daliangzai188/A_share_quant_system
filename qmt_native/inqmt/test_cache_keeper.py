# coding: utf-8
"""The host's daily-bar cache keeper against a mock QMT whose cache lacks the previous trading
day: stale stocks are found and downloaded once each, a second pass does nothing, nothing
happens outside the allowed hours, and the thread ends with the strategy.

Usage: python test_cache_keeper.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import csv, io, os, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


def yields_to_collector(ns, host, host_cls, service, vfs):
    """2026-11-23: caching TODAY's bars waits while the close pipeline's step 1 (thread
    qn-script-collect_all_data) runs and goes on after it ends; caching a past day never waits; the
    wait ends without any QMT call when the allowed hours end."""
    rows = csv.DictReader(io.StringIO(vfs.get_text('data/raw/trade_calendar.csv')))
    days = sorted(r['cal_date'] for r in rows if str(r.get('is_open', '')).strip() in ('1', '1.0'))
    today, past = days[-1], days[-2]
    seen = []

    def fake_call(fn, *args, **kwargs):
        name = getattr(fn, '__name__', '')
        seen.append(name)
        if name == 'qmt_universe':
            return ['000001.SZ', '000002.SZ']
        if name == 'qmt_daily_bars':
            codes, start, end = args
            fresh = 'qmt_download_daily' in seen
            return dict((c, [{'date': end if fresh else start}]) for c in codes)
        if name == 'qmt_download_daily':
            return None
        raise AssertionError(name)

    class FakeNow(object):
        def strftime(self, f):
            return {'%Y%m%d': today, '%H%M': '1600'}.get(f) or real_now().strftime(f)

    real_now = ns['_bj_now']
    choice = {}
    host._cache_target = lambda d, t: choice['target']
    ns['_bj_now'] = lambda: FakeNow()
    service.call = fake_call
    host_cls.CACHE_WINDOWS = (('0000', '2400'),)
    errors = []

    def run_pass():
        try:
            host._cache_keeper_pass()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    def collector():
        gate = threading.Event()
        t = threading.Thread(target=gate.wait, name=host.COLLECTOR_THREAD)
        t.daemon = True
        t.start()
        return gate, t

    ok = True
    try:
        # 1. today's bars, step 1 running: no QMT call at all until it ends, then the pass completes
        choice['target'] = today
        host.cache_done = ''
        del seen[:]
        gate, col = collector()
        th = threading.Thread(target=run_pass, name='keeper-test')
        th.start()
        time.sleep(2)
        during = list(seen)
        gate.set()
        col.join()
        th.join(20)
        one = (during == [] and not th.is_alive() and host.cache_done == today
               and seen.count('qmt_download_daily') == 4)
        print('1. today, step 1 running: calls while it ran %d; after it ended done=%s downloads=%d -> %s' % (
            len(during), host.cache_done, seen.count('qmt_download_daily'), 'ok' if one else 'FAIL'))
        # 2. a past day: no waiting even while step 1 runs (the collector may need those bars)
        choice['target'] = past
        host.cache_done = ''
        del seen[:]
        gate, col = collector()
        th = threading.Thread(target=run_pass, name='keeper-test')
        th.start()
        th.join(20)
        two = not th.is_alive() and col.is_alive() and host.cache_done == past and seen.count('qmt_download_daily') == 2
        gate.set()
        col.join()
        print('2. past day while step 1 runs: done=%s downloads=%d -> %s' % (
            host.cache_done, seen.count('qmt_download_daily'), 'ok' if two else 'FAIL'))
        # 3. hours end while waiting: the pass returns without calling QMT
        choice['target'] = today
        host.cache_done = ''
        del seen[:]
        gate, col = collector()
        th = threading.Thread(target=run_pass, name='keeper-test')
        th.start()
        time.sleep(1)
        host_cls.CACHE_WINDOWS = ()
        th.join(20)
        three = not th.is_alive() and seen == [] and host.cache_done == ''
        gate.set()
        col.join()
        print('3. hours end while waiting: calls %d, done=%r -> %s' % (len(seen), host.cache_done, 'ok' if three else 'FAIL'))
        ok = one and two and three and not errors
        msgs = [m for m in host.messages if 'collect' in m and 'cache keeper' in m]
        print('   keeper lines:', [m.encode('ascii', 'backslashreplace').decode()[-70:] for m in msgs[-4:]])
        if errors:
            print('   errors:', errors)
    finally:
        ns['_bj_now'] = real_now
        del host._cache_target
        host_cls.CACHE_WINDOWS = ()
    return ok


def main():
    C = rs.MockContext()
    ns = load(sys.argv[1], {})
    ns['QN_DEPLOY']['cache_keeper'] = True
    downloads = []
    ns['download_history_data'] = lambda code, period, start, end: downloads.append((code, period, start, end))
    host_cls = ns['QnHost']
    host_cls.CACHE_WINDOWS = (('0000', '2400'),)
    # which thread asked for each download: the keeper ('a-system-cache') or the close pipeline's
    # collector, which may download the same end date (e.g. on a trading day before its close)
    callers = []
    service = ns['QN_NATIVE_SERVICE']
    real_call = service.call

    def call(fn, *args, **kwargs):
        if getattr(fn, '__name__', '') == 'qmt_download_daily':
            callers.append((threading.current_thread().name,) + tuple(args))
        return real_call(fn, *args, **kwargs)
    service.call = call
    ns['init'](C)
    host = ns['QN_HOST'][0]

    def pump(seconds, until=None):
        t = time.time()
        while time.time() - t < seconds and not (until and until()):
            ns['a_system_pump'](C)
            time.sleep(0.002)
    pump(600, until=lambda: host.cache_done)
    msgs = [m for m in host.messages if 'cache keeper' in m]
    for m in msgs:
        print('  ', m.encode('ascii', 'backslashreplace').decode()[:150])
    mine = [c[1:] for c in callers if c[0] == 'a-system-cache']
    ranges = set((d[1], d[2]) for d in mine)
    first = len(mine)
    codes = set(d[0] for d in mine)
    print('keeper downloads', first, 'distinct', len(codes), 'ranges', ranges, 'period', set(d[1] for d in downloads),
          '| collector downloads', len(callers) - first, '| done', host.cache_done)
    ok = host.cache_done != '' and first > 0 and len(codes) == first and ranges and all(r[1] == host.cache_done for r in ranges)
    ok &= len(ranges) == 1 and all(d[1] == '1d' for d in downloads)
    pump(15)
    ok &= len([c for c in callers if c[0] == 'a-system-cache']) == first  # current: no second round
    host.cache_done = ''
    host_cls.CACHE_WINDOWS = ()
    before = len(callers)
    host._cache_keeper_pass()  # returns before any QMT call outside the allowed hours
    ok &= len(callers) == before and host.cache_done == ''
    # which day is kept: the previous open day; today only after 15:35 once today's raw daily
    # table exists (today's close pipeline is done, the cache can no longer change its result)
    # dates far from the real calendar: the pipeline of this run writes real raw tables up to today
    days = ['20301026', '20301027', '20301028']
    vfs, root = ns['QN_VFS'], ns['QN_ROOT']
    clock = {'hm': '1600'}
    real_now = ns['_bj_now']
    ns['_bj_now'] = lambda: type('T', (), {'strftime': lambda self, f: clock['hm'] if f == '%H%M' else real_now().strftime(f)})()
    try:
        before_file = host._cache_target(days, '20301028')
        vfs.put_text('data/raw/daily/20301028.csv', 'ts_code,trade_date\n000001.SZ,20301028\n')
        after_file = host._cache_target(days, '20301028')
        clock['hm'] = '1520'
        before_1535 = host._cache_target(days, '20301028')
        clock['hm'] = '0600'
        morning = host._cache_target(days, '20301029')
    finally:
        ns['_bj_now'] = real_now
    print('target: before the close table %s, after it %s, at 15:20 %s, next morning %s' % (
        before_file, after_file, before_1535, morning))
    ok &= (before_file, after_file, before_1535, morning) == ('20301027', '20301028', '20301027', '20301028')
    ok &= yields_to_collector(ns, host, host_cls, service, vfs)
    service.call = call
    thread = host.cache_thread
    ns['stop'](C)
    time.sleep(6)
    print('thread alive after stop:', thread.is_alive())
    ok &= not thread.is_alive()
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
