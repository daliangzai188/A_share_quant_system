# coding: utf-8
"""A broker key-value call that hangs while the trade server is flaky (10-22 19:05-19:08 in real
QMT: 31 s and 62 s) blocks the callback thread AND, holding the GIL, every other thread. Right
after it returns the daemon reads the engine heartbeat before the engine pumps again; with the
external bridge's 5 s limit it reported a lost bridge and pushed an alarm pair.
Here the hanging call is a C loop that holds the GIL, like the real one:
  1. no bridge-lost alarm after the stall, daemon alive, account checks continue;
  2. the save that hit the slow call is given up and backs off (no second hang in that save).

Usage: python test_save_stall.py STRATEGY_FILE [STALL_SECONDS]   (QMT's Python 3.6, ~3 minutes)
"""
import os, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


def gil_hold_loops(seconds):
    t = time.time()
    sum(range(10 ** 7))  # builtin sum over range runs in C without releasing the GIL
    per = max(time.time() - t, 1e-3)
    return int(seconds / per * 10 ** 7)


def main():
    path = sys.argv[1]
    stall = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0  # the calibration runs short: ~60 s
    loops = gil_hold_loops(stall)
    kv, state = {}, dict(armed=False, hung=0, calls_after=0)
    C = rs.MockContext()
    ns = load(path, kv)

    def save(k, v):
        if state['armed']:
            if not state['hung']:
                state['hung'] = time.time()
                sum(range(loops))  # the hang: nothing else in this interpreter runs meanwhile
                state['hung'] = time.time() - state['hung']
            else:
                state['calls_after'] += 1
        if len(v) > 5000:
            return (-1, 'too long')
        kv[k] = v
        return (0, '')
    ns['save_py_key'] = save
    ns['init'](C)
    host = ns['QN_HOST'][0]
    drive(ns, C, 30)
    print('daemon up before the stall:', bool(host.daemon_thread and host.daemon_thread.is_alive()))
    result = {}

    def forced_save():
        try:
            host.persist(force=True, reason='test')
            result['ok'] = True
        except Exception as exc:
            result['error'] = str(exc)
    host.PERSIST_INTERVAL = 1e9  # only the forced save below touches the store from here on
    with host.persist_lock:
        state['armed'] = True
    log_mark = len(ns['QN_LOG_LINES'])
    saver = threading.Thread(target=forced_save)
    saver.start()
    drive(ns, C, stall + 45)
    saver.join(5)
    state['armed'] = False
    after = [l for l in ns['QN_LOG_LINES'][log_mark:]]
    stale = [l for l in after if u'心跳已过期' in l or 'heartbeat is stale' in l.lower()]
    accounts = [l for l in after if u'[账户]' in l]
    print('hang lasted %.0fs; calls in the same save after it: %d' % (state['hung'], state['calls_after']))
    print('save result:', result)
    print('bridge-lost lines after the stall: %d; account checks after it: %d' % (len(stale), len(accounts)))
    for l in stale[:3]:
        print('   ', l[:160].encode('ascii', 'backslashreplace').decode())
    alive = bool(host.daemon_thread and host.daemon_thread.is_alive())
    print('daemon alive:', alive, '| store retry in %.0fs' % max(0.0, host.persist_retry_at - time.time()))
    ok = state['hung'] >= min(stall, 20.0) and not stale and accounts and alive
    # given up after the slow call, or (hang over the 60 s wait for the callback thread) timed out
    ok &= bool(result.get('error')) and state['calls_after'] == 0 and host.persist_retry_at > time.time()
    ns['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
