# coding: utf-8
"""The save before an order no longer waits for a slow trade server (11-07): on 11-03..11-06 single
key-value calls took 5-60 s 21-55 times a session, the save before an order makes about 7 calls,
and an order could wait minutes. Since 11-05 a start holds trading for an order of today that the
restored journal does not know, so:
  1. fast store: the save runs before the order (key-value calls before the broker call);
  2. a call slower than the limit within STORE_SLOW_RECENT: the order goes ahead with no
     key-value call before it; the next periodic save writes the journal;
  3. the store turns slow during the save before an order: the save gives up after that one call,
     the order still goes ahead;
  4. trading session, only logs/ changed since a recent save: a periodic save makes no key-value
     call; another file changed: it saves;
  5. outside the session a health-file change is still saved (unchanged behaviour).

Usage: python test_order_save_bound.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


def main():
    kv = {}
    ns = load(sys.argv[1], kv)
    state = dict(calls=0, slow=0.0)
    real_get, real_save = ns['get_py_key'], ns['save_py_key']

    def get(k):
        state['calls'] += 1
        return real_get(k)

    def save(k, v):
        state['calls'] += 1
        if state['slow']:
            time.sleep(state['slow'])
        return real_save(k, v)
    ns['get_py_key'], ns['save_py_key'] = get, save
    C = rs.MockContext()
    ns['init'](C)
    host = ns['QN_HOST'][0]
    drive(ns, C, 20)
    host.PERSIST_INTERVAL = host.PERSIST_INTERVAL_BUSY = 1e9  # the state thread's own saves stay out of the way
    orders = []
    host.broker.passorder = lambda *a, **k: orders.append(state['calls'])
    passorder = host._trading_api()['passorder']

    def journal_change():
        with host.persist_lock:
            ns['QN_VFS'].put_text('data/state/test_order_marker.json', '{"t": %r}' % time.time())

    def lines_since(mark):
        return [l for l in host.messages[mark:]]
    ok = True

    host.store.last_slow_at = 0.0
    journal_change()
    before = state['calls']
    passorder(23, 1101, 'acct', '600000.SH', 11, 10.0, 100, 'A_SYSTEM', 2, 'AS' + 'a' * 28, C)
    r1 = len(orders) == 1 and orders[-1] > before
    print('1. fast store: key-value calls before the order %d, order placed %s' % (orders[-1] - before, len(orders) == 1))
    ok &= r1

    host.store.last_slow_at, host.store.last_slow_seconds = time.time() - 60, 12.0
    journal_change()
    before, mark = state['calls'], len(host.messages)
    passorder(23, 1101, 'acct', '600001.SH', 11, 10.0, 100, 'A_SYSTEM', 2, 'AS' + 'b' * 28, C)
    skipped = orders[-1] == before and any('without the save before it' in l for l in lines_since(mark))
    host.persist_last, host.persist_retry_at = -1e10, 0.0
    host.persist(reason='periodic after order')
    saved_after = state['calls'] > before
    print('2. slow call 60 s ago: calls before the order %d, logged %s; next periodic save writes: %s' % (
        orders[-1] - before, skipped, saved_after))
    ok &= len(orders) == 2 and skipped and saved_after

    host.store.last_slow_at = 0.0
    host.STORE_SLOW_CALL = 0.5
    host.store.slow_mark = 0.5
    journal_change()
    state['slow'] = 1.0
    before, mark = state['calls'], len(host.messages)
    passorder(23, 1101, 'acct', '600002.SH', 11, 10.0, 100, 'A_SYSTEM', 2, 'AS' + 'c' * 28, C)
    state['slow'] = 0.0
    gave_up = any('did not complete' in l for l in lines_since(mark))
    print('3. slow during the save: calls before the order %d (gave up after the slow one: %s), order placed %s' % (
        orders[-1] - before, gave_up, len(orders) == 3))
    ok &= len(orders) == 3 and gave_up and orders[-1] - before <= 2
    host.STORE_SLOW_CALL = 5.0
    host.store.slow_mark = 5.0
    host.store.last_slow_at = 0.0

    session = host.TRADING_SESSION
    host.TRADING_SESSION = ('0000', '2400')
    host.persist_retry_at = 0.0
    host.persist_last = -1e10
    host.persist(reason='baseline')  # a committed snapshot to compare with
    with host.persist_lock:
        ns['QN_VFS'].put_text('logs/broker_health.json', '{"t": %r}' % time.time())
    before = state['calls']
    host.persist_last = -1e10
    host.persist(reason='health only')
    health_calls = state['calls'] - before
    journal_change()
    before = state['calls']
    host.persist_last = -1e10
    host.persist(reason='state changed')
    state_calls = state['calls'] - before
    print('4. session: health file only -> %d key-value calls; another file -> %d' % (health_calls, state_calls))
    ok &= health_calls == 0 and state_calls > 0

    host.TRADING_SESSION = ('0000', '0000')
    with host.persist_lock:
        ns['QN_VFS'].put_text('logs/broker_health.json', '{"t": %r}' % time.time())
    before = state['calls']
    host.persist_last = -1e10
    host.persist(reason='outside session')
    off_calls = state['calls'] - before
    host.TRADING_SESSION = session
    print('5. outside the session: health file only -> %d key-value calls' % off_calls)
    ok &= off_calls > 0
    ns['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
