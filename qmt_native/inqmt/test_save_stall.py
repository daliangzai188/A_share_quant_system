# coding: utf-8
"""A broker key-value call that hangs while the trade server is flaky (10-22 19:05-19:08 in real
QMT: 31 s and 62 s; 10-23 morning 5-18 s several times an hour) blocks the callback thread AND,
holding the GIL, every other thread. Right after it returns the daemon reads the engine heartbeat
before the engine pumps again; with the external bridge's 5 s limit it reported a lost bridge.
Here the hanging call is a C loop that holds the GIL, like the real one:
  1. periodic save: no bridge-lost alarm after the stall, daemon alive, account checks continue,
     and the save that hit the slow call is given up and backs off (no second hang in it);
  2. forced save (the one before an order, or on stop): a slow call does not give it up,
     the save completes;
  3. periodic saves give up after 5 s in the trading session, 30 s outside it.

Usage: python test_save_stall.py STRATEGY_FILE [STALL_SECONDS]   (QMT's Python 3.6, ~4 minutes)
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
    kv, state = {}, dict(armed=False, loops=0, hung=0, calls_after=0)
    C = rs.MockContext()
    ns = load(path, kv)

    def save(k, v):
        if state['armed']:
            if not state['hung']:
                state['hung'] = time.time()
                sum(range(state['loops']))  # the hang: nothing else in this interpreter runs meanwhile
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
    host.PERSIST_INTERVAL = 1e9  # the state thread's own periodic saves stay out of the way

    def run_save(force, seconds, settle):
        result = {}

        def target():
            try:
                host.persist(force=force, reason='test')
                result['ok'] = True
            except Exception as exc:
                result['error'] = str(exc)
        with host.persist_lock:
            ns['QN_VFS'].put_text('logs/broker_health.json', '{"test": %r}' % time.time())  # something to save
            host.persist_last, host.persist_retry_at = -1e10, 0.0
            state.update(armed=True, loops=gil_hold_loops(seconds), hung=0, calls_after=0)
        mark = len(ns['QN_LOG_LINES'])
        saver = threading.Thread(target=target)
        saver.start()
        drive(ns, C, seconds + settle)
        saver.join(60)
        state['armed'] = False
        return result, ns['QN_LOG_LINES'][mark:]

    result, after = run_save(False, stall, 45)
    stale = [l for l in after if u'心跳已过期' in l or 'heartbeat is stale' in l.lower()]
    accounts = [l for l in after if u'[账户]' in l]
    error = host.persist_error  # a periodic save logs its failure and backs off; it does not raise
    print('1. periodic: hang %.0fs; calls in the same save after it: %d; save error: %s' % (
        state['hung'], state['calls_after'], error[:90]))
    print('   bridge-lost lines after the stall: %d; account checks after it: %d' % (len(stale), len(accounts)))
    for l in stale[:3]:
        print('   ', l[:160].encode('ascii', 'backslashreplace').decode())
    alive = bool(host.daemon_thread and host.daemon_thread.is_alive())
    print('   daemon alive:', alive, '| store retry in %.0fs' % max(0.0, host.persist_retry_at - time.time()))
    ok = state['hung'] >= min(stall, 20.0) and not stale and accounts and alive
    # given up after the slow call, or (hang over the 60 s wait for the callback thread) timed out
    ok &= ('given up' in error or 'timed out' in error) and state['calls_after'] == 0 and host.persist_retry_at > time.time()

    result, _ = run_save(True, 8.0, 10)
    print('2. forced: hang %.0fs; calls after it: %d; result %s' % (state['hung'], state['calls_after'], result))
    ok &= result.get('ok') is True and state['calls_after'] > 0 and state['hung'] > 5.0
    session = host.TRADING_SESSION
    host.TRADING_SESSION = ('0000', '2400')
    inside = host.periodic_slow_limit()
    host.TRADING_SESSION = ('0000', '0000')
    outside = host.periodic_slow_limit()
    host.TRADING_SESSION = session
    print('3. periodic slow limit: trading session %.0fs, outside %.0fs' % (inside, outside))
    ok &= inside == 5.0 and outside == 30.0
    ns['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
