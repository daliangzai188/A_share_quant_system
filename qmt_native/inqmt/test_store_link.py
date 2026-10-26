# coding: utf-8
"""10-24 17:48 the broker trade server dropped; periodic saves failed at once ("200000 未建立连接")
and were retried 1, 2, 4, 8 minutes apart; the fifth call never returned and froze QMT's Python.
  1. the account status fields of the engine's own account queries are logged (no extra QMT call);
  2. while the account shows a status the store has not worked with, periodic saves wait (no
     key-value call at all), forced saves (before an order) still run, and saves resume when
     the status is back;
  3. after an "unreachable" failure the next periodic try waits 30 minutes, then 1 hour; a call
     that was merely slow keeps the short backoff.

Usage: python test_store_link.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


class Row(object):
    def __init__(self, status):
        self.m_strAccountID = 'x'
        self.m_strStatus = status
        self.m_dBalance = 1.0


def main():
    path = sys.argv[1]
    kv, state = {}, dict(calls=0, fail=None)
    C = rs.MockContext()
    ns = load(path, kv)

    def save(k, v):
        state['calls'] += 1
        if state['fail']:
            return state['fail']
        kv[k] = v
        return (0, '')
    ns['save_py_key'] = save
    ns['init'](C)
    host = ns['QN_HOST'][0]
    drive(ns, C, 30)
    seen = [m for m in host.messages if 'trade account status:' in m]
    print('1. account status logged from the engine queries:', [m.split('status: ', 1)[1][:40] for m in seen[:2]])
    ok = bool(seen)

    def periodic():
        with host.persist_lock:
            ns['QN_VFS'].put_text('logs/broker_health.json', '{"t": %r}' % time.time())
            host.persist_last, host.persist_retry_at = -1e10, 0.0
        before = state['calls']
        host.persist(reason='test')
        return state['calls'] - before

    host.account_status_learn = True
    host._note_account_status([Row(u'正常')])
    up = periodic()
    host._note_account_status([Row(u'连接断开')])
    down = periodic()
    host._note_account_status([Row(u'正常')])
    back = periodic()
    host._note_account_status([Row(u'连接断开')])
    with host.persist_lock:
        ns['QN_VFS'].put_text('logs/broker_health.json', '{"forced": %r}' % time.time())
    before = state['calls']
    host.persist(force=True, reason='before passorder')  # an order: tried whatever the account shows
    forced = state['calls'] - before
    learned = periodic()  # the store worked with that status: no longer a reason to wait
    waits = [m for m in host.messages if 'periodic state saves wait' in m or 'periodic state saves resume' in m]
    print('2. key-value calls of a periodic save: link up %d, account shows another status %d, back %d; '
          'forced save %d; periodic after it %d' % (up, down, back, forced, learned))
    for m in waits:
        print('   ', m[:140].encode('ascii', 'backslashreplace').decode())
    ok &= up > 0 and down == 0 and back > 0 and forced > 0 and learned > 0 and len(waits) == 2

    delays = []
    state['fail'] = (200000, u'未建立连接')
    for _ in range(2):
        periodic()
        delays.append(host.persist_retry_at - time.time())
    host.persist_failures = 0
    host.store.slow_limit = 0.0
    state['fail'] = None
    real_save, slow = ns['save_py_key'], {'n': 0}
    host.STORE_SLOW_CALL = host.STORE_SLOW_CALL_OFF_HOURS = 0.2

    def slow_save(k, v):
        time.sleep(0.5)
        return real_save(k, v)
    ns['save_py_key'] = slow_save  # the store looks the QMT function up at call time
    periodic()
    slow_delay = host.persist_retry_at - time.time()
    print('3. retry after "未建立连接": %s s; after a slow call: %.0f s (%s)' % (
        [int(round(d, -1)) for d in delays], slow_delay, host.persist_error[:60].encode('ascii', 'backslashreplace').decode()))
    ok &= 1700 < delays[0] < 1810 and 3500 < delays[1] < 3610 and 'given up' in host.persist_error and slow_delay < 120
    ns['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
