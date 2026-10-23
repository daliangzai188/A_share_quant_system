# coding: utf-8
"""The host's daily-bar cache keeper against a mock QMT whose cache lacks the previous trading
day: stale stocks are found and downloaded once each, a second pass does nothing, nothing
happens outside the allowed hours, and the thread ends with the strategy.

Usage: python test_cache_keeper.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import os, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


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
    thread = host.cache_thread
    ns['stop'](C)
    time.sleep(6)
    print('thread alive after stop:', thread.is_alive())
    ok &= not thread.is_alive()
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
