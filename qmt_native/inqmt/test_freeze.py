# coding: utf-8
"""10-28 16:05 and 10-30 15:21 in real QMT: a broker call on QMT's callback thread hung for 52 s / 60 s
holding the GIL, so all of QMT's Python stood still. The trading daemon's account query, waiting
35 s for that thread, woke with its time used up: "执行通道超时中毒" -> process restart (which,
before the os._exit fix, took QMT down). Here the engine's account query holds the GIL for 45 s
(a builtin sum over range never releases it) while the daemon waits for it:
  1. the freeze is seen (probe gap) and does not count against the 35 s broker command timeout;
  2. no poisoned channel, no process restart, the same daemon keeps running, and no "account
     unverified / 断连" either (read-only bridge requests that ran out during the freeze are resent);
  3. account checks continue after the freeze;
  4. the 10-30 shape: the freeze comes from a hanging state save (save_py_key on the callback
     thread) while account checks run: same result.

Usage: python test_freeze.py STRATEGY_FILE [FREEZE_SECONDS]   (QMT's Python 3.6, ~2.5 minutes)
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


def gil_hold_loops(seconds):
    t = time.time()
    sum(range(10 ** 7))
    return int(seconds / max(time.time() - t, 1e-3) * 10 ** 7)


def main():
    path = sys.argv[1]
    freeze = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    C = rs.MockContext()
    ns = load(path, {})
    state = dict(armed=False, hung=0.0, loops=gil_hold_loops(freeze))
    broker_cls = ns['QnPaperBroker']
    real_query = broker_cls.get_trade_detail_data

    def query(self, account, market, kind):
        if state['armed'] and kind == 'account' and not state['hung']:
            t0 = time.time()
            sum(range(state['loops']))  # QMT's Python stands still, as during the hung key-value call
            state['hung'] = time.time() - t0
        return real_query(self, account, market, kind)
    broker_cls.get_trade_detail_data = query
    ns['init'](C)
    host = ns['QN_HOST'][0]
    drive(ns, C, 40)
    daemon = host.daemon_thread
    restarts = host.daemon_restarts
    mark_log, mark_msg = len(ns['QN_LOG_LINES']), len(host.messages)
    state['armed'] = True
    t0 = time.time()
    while not state['hung'] and time.time() - t0 < 150:
        drive(ns, C, 1)
    drive(ns, C, 75)
    after = list(ns['QN_LOG_LINES'])[mark_log:] + host.messages[mark_msg:]
    poisoned = [l for l in after if u'中毒' in l or 'process exit' in l or u'进程级交易恢复' in l
                or u'未验证/不可用' in l or u'账户断连' in l or 'timed out' in l]
    accounts = [l for l in after if u'[账户]' in l]
    print('1. freeze %.0fs; frozen seconds seen by the probe: %.0f' % (state['hung'], ns['qn_frozen_seconds']()))
    print('2. poisoned/restart lines: %d; same daemon alive: %s; restarts %d -> %d' % (
        len(poisoned), host.daemon_thread is daemon and daemon.is_alive(), restarts, host.daemon_restarts))
    for l in poisoned[:3]:
        print('   ', l[:150].encode('ascii', 'backslashreplace').decode())
    print('3. account checks after the freeze: %d' % len(accounts))
    ok = state['hung'] >= freeze * 0.8 and ns['qn_frozen_seconds']() >= freeze * 0.8
    ok &= not poisoned and host.daemon_thread is daemon and daemon.is_alive() and host.daemon_restarts == restarts
    ok &= len(accounts) >= 1

    # 4. the hang inside a state save, as on 10-30 15:21
    kv_state = dict(armed=True, hung=0.0)
    real_save = ns['save_py_key']

    def hanging_save(k, v):
        if kv_state['armed'] and not kv_state['hung']:
            t1 = time.time()
            sum(range(state['loops']))
            kv_state['hung'] = time.time() - t1
        return real_save(k, v)
    ns['save_py_key'] = hanging_save
    mark_log, mark_msg = len(ns['QN_LOG_LINES']), len(host.messages)
    with host.persist_lock:
        ns['QN_VFS'].put_text('logs/broker_health.json', '{"freeze": %r}' % time.time())
        host.persist_last, host.persist_retry_at = -1e10, 0.0
    t0 = time.time()
    while not kv_state['hung'] and time.time() - t0 < 200:
        drive(ns, C, 1)
    drive(ns, C, 75)
    after = list(ns['QN_LOG_LINES'])[mark_log:] + host.messages[mark_msg:]
    bad = [l for l in after if u'中毒' in l or 'process exit' in l or u'未验证/不可用' in l or u'账户断连' in l]
    accounts = [l for l in after if u'[账户]' in l]
    print('4. hanging state save %.0fs: poisoned/unverified lines %d, same daemon alive %s, account checks after %d' % (
        kv_state['hung'], len(bad), host.daemon_thread is daemon and daemon.is_alive(), len(accounts)))
    for l in bad[:3]:
        print('   ', l[:150].encode('ascii', 'backslashreplace').decode())
    ok &= kv_state['hung'] >= freeze * 0.8 and not bad and host.daemon_thread is daemon and len(accounts) >= 1
    ns['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
