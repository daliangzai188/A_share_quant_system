# coding: utf-8
"""QMT's key-value store goes through the broker trade server, which disconnects at night.
1. state store unreadable at start -> no trading started, retried, then normal start;
2. daytime save failures -> backoff (no 5-second retry storm);
3. night -> no periodic saves and no save on stop.

Usage: python test_night_store.py STRATEGY_FILE
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


def run(path, hours, get_fail, save_fail, seconds, kv):
    calls = {'get': 0, 'save': 0}
    C = rs.MockContext()
    ns = load(path, kv)

    def get(k):
        calls['get'] += 1
        if calls['get'] <= get_fail:
            return ('', 200000, u'未建立连接')
        return (kv.get(k, ''), 0, '')

    def save(k, v):
        calls['save'] += 1
        if save_fail:
            return (200000, u'未建立连接')
        kv[k] = v
        return (0, '')
    ns['get_py_key'], ns['save_py_key'] = get, save
    ns['QN_DEPLOY']['store_day_hours'] = hours
    ns['init'](C)
    host = ns['QN_HOST'][0]
    host.BOOT_RETRY_SECONDS = 3.0
    host.boot_retry_at = min(host.boot_retry_at, time.time() + 3.0)
    drive(ns, C, seconds)
    state = dict(engine=host.engine is not None, daemon=bool(host.daemon_thread and host.daemon_thread.is_alive()),
                 failures=host.persist_failures, meta=bool(host.persist_meta))
    saves_before_stop = calls['save']
    ns['stop'](C)
    msgs = [m.encode('ascii', 'backslashreplace').decode()[:150] for m in host.messages
            if 'state' in m.lower() or 'night' in m or 'store' in m]
    return state, calls, saves_before_stop, msgs


def main():
    path = sys.argv[1]
    ok = True
    st, calls, _, msgs = run(path, [0, 24], 2, False, 30, {})
    print('1. unreadable store at start:', st, calls)
    for m in msgs[:4]:
        print('     ', m)
    ok &= st['engine'] and st['daemon'] and st['meta'] and any('not readable' in m for m in msgs)
    st, calls, before, msgs = run(path, [0, 24], 0, True, 75, {})
    print('2. daytime save failures:', st, 'save calls', before)
    for m in msgs[:4]:
        print('     ', m)
    ok &= st['daemon'] and st['failures'] >= 1 and before <= 6
    kv = {}
    st, calls, before, msgs = run(path, [0, 0], 0, False, 40, kv)
    print('3. night:', st, 'save calls before stop', before, 'after stop', calls['save'])
    for m in msgs[:4]:
        print('     ', m)
    ok &= st['daemon'] and calls['save'] == 0
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
