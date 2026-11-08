# coding: utf-8
"""State in QMT's log + the registry instead of the broker's key-value store (11-08: a key-value
call made while the trade server drops can freeze QMT's Python until QMT is restarted and logged
in; 10-24, 11-07 11:54, 11-08 11:08). With vm_scripts/qn_state_sink.py as the forwarder:
  1. moving over: a start with an empty registry reads the broker store once (its state comes
     back unchanged), the next save prints the whole snapshot and the forwarder commits it;
  2. every later start reads the registry only: not one key-value call, even while the account
     shows 登录失败 and get_py_key would never return; files byte for byte as saved;
  3. saves, the save before an order and the save on stop make no key-value call, also at night
     and while the account shows 登录失败;
  4. forwarder not running: the snapshot waits in the log; once it reads the lines it commits;
     a snapshot not confirmed for long is reported;
  5. lines lost before the forwarder read them: it names what it lacks (Missing), the strategy
     prints those again, the snapshot commits;
  6. the latest snapshot damaged in the registry: a start takes the one before it.

Usage: python test_log_store.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive

UP, DOWN = u'登录成功', u'登录失败'


class Row(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


def boot(path, kv, status, calls, store, hang=None):
    ns = load(path, kv, store=store)
    ns['QN_DEPLOY']['trading_switch'] = True
    ns['QN_DEPLOY']['store_day_hours'] = [0, 24]
    ns['QN_SWITCH_READER'][0] = lambda: 'run'
    real_get, real_save = ns['get_py_key'], ns['save_py_key']

    def get(k):
        calls.append(k)
        if hang and hang[0]:
            raise RuntimeError('get_py_key would never return here (test)')
        return real_get(k)

    def save(k, v):
        calls.append(k)
        if hang and hang[0]:
            raise RuntimeError('save_py_key would never return here (test)')
        return real_save(k, v)
    ns['get_py_key'], ns['save_py_key'] = get, save
    real_init = ns['QnHost'].__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self.BOOT_LINK_STABLE, self.BOOT_LINK_POLL = 1.0, 0.5
        acct = str(self.deploy['account_id'])
        self.broker.get_trade_detail_data = lambda account, market, kind, *r: (
            [Row(m_strAccountID=acct, m_strStatus=status[0])] if kind == 'account' else [])
    ns['QnHost'].__init__ = init
    C = rs.MockContext()
    ns['init'](C)
    return ns, C, ns['QN_HOST'][0]


def files_of(ns, host):
    return dict((rel, v[0]) for rel, v in host._persist_files().items())


def put(ns, host, rel, text):
    with host.persist_lock:
        ns['QN_VFS'].put_text(rel, text)


def main():
    path = sys.argv[1]
    kv, ok = {}, True
    status, calls = [UP], []

    # a state in the broker store (the release before 11-08)
    ns, C, host = boot(path, kv, status, calls, 'broker')
    drive(ns, C, 6)
    put(ns, host, 'data/state/test_log_marker.json', '{"v": "broker"}')
    host.persist_on_callback_thread(reason='broker state')
    before = files_of(ns, host)
    ns['stop'](C)

    # 1. move over
    calls[:] = []
    ns, C, host = boot(path, kv, status, calls, 'log')
    drive(ns, C, 6)
    st = ns['QN_TEST_STATE']
    moved = files_of(ns, host)
    # logs/ health files are rewritten by the new run every minute
    same = all(moved.get(rel) == data for rel, data in before.items() if data is not None and not rel.startswith('logs/'))
    from_broker = len(calls) > 0
    calls[:] = []
    host.persist_on_callback_thread(reason='first save in the log store')
    for _ in range(3):
        note = host.store.service()
    committed = st['values'].get('Pointer') == host.store.pointer and host.store.confirmed()
    printed = sum(1 for l in st['lines'] if ' B ' in l)
    print('1. move over: read the broker store %s, files unchanged (logs/ aside) %s; first save: key-value calls %d, '
          'bodies printed %d, committed %s' % (from_broker, same, len(calls), printed, committed))
    ok &= from_broker and same and not calls and printed > 0 and committed
    put(ns, host, 'data/state/test_log_marker.json', '{"v": "log 1"}')
    host.persist_on_callback_thread(reason='second save')
    host.store.service()
    saved = files_of(ns, host)
    ns['stop'](C)

    # 2. later starts: registry only, even with the trade server down and py_key hanging
    status[0], calls[:] = DOWN, []
    hang = [True]
    ns, C, host = boot(path, kv, status, calls, 'log', hang)
    drive(ns, C, 4)
    got = files_of(ns, host)
    identical = all(got.get(rel) == data for rel, data in saved.items() if data is not None and not rel.startswith('logs/'))
    print('2. start while %s, get_py_key hanging: started %s, key-value calls %d, files identical (logs/ aside) %s' % (
        '登录失败', not host.boot_pending and host.engine is not None, len(calls), identical))
    ok &= not host.boot_pending and host.engine is not None and not calls and identical

    # 3. saves at night and while 登录失败, before an order, on stop: no key-value call
    host.STORE_DAY_START, host.STORE_DAY_END = 25, 26  # night all day
    host.PERSIST_INTERVAL = host.PERSIST_INTERVAL_BUSY = 0.0
    host.SESSION_LOG_SAVE_SECONDS = 0.0
    st = ns['QN_TEST_STATE']
    put(ns, host, 'data/state/test_log_marker.json', '{"v": "night"}')
    host.persist_last, host.persist_retry_at = -1e10, 0.0
    lines_before = len(st['lines'])
    host.persist(reason='periodic')
    host.store.service()
    night_ok = len(st['lines']) > lines_before and host.store.confirmed()
    orders = []
    host.broker.passorder = lambda *a, **k: orders.append(1)
    passorder = host._trading_api()['passorder']
    put(ns, host, 'data/state/test_log_marker.json', '{"v": "order"}')
    passorder(23, 1101, 'acct', '600000.SH', 11, 10.0, 100, 'A_SYSTEM', 2, 'AS' + 'e' * 28, C)
    host.store.service()
    order_saved = host.store.confirmed() and orders
    put(ns, host, 'data/state/test_log_marker.json', '{"v": "stop"}')
    ns['stop'](C)
    ns2, C2, host2 = boot(path, kv, [UP], calls, 'log', hang)
    drive(ns2, C2, 3)
    after_stop = ns2['QN_VFS'].files.get(ns2['QN_VFS'].abspath('data/state/test_log_marker.json'))
    print('3. night/登录失败: periodic save committed %s; save before an order + order %s; state saved on stop: %s; '
          'key-value calls %d' % (night_ok, bool(order_saved), after_stop, len(calls)))
    ok &= night_ok and bool(order_saved) and after_stop == b'{"v": "stop"}' and not calls
    host = host2
    ns, C = ns2, C2

    # 4. forwarder not running
    st = ns['QN_TEST_STATE']
    host.PERSIST_INTERVAL = host.PERSIST_INTERVAL_BUSY = 1e9
    st['hold'][0] = True
    put(ns, host, 'data/state/test_log_marker.json', '{"v": "held"}')
    host.persist_on_callback_thread(reason='forwarder down')
    host.store.UNACKED_WARN_SECONDS = 0.0
    waiting = not host.store.confirmed()
    warn = host.store.service()
    st['hold'][0] = False
    st['release']()
    host.store.service()
    print('4. forwarder down: waiting %s, reported %s; after it reads the lines: committed %s' % (
        waiting, warn[:40], host.store.confirmed()))
    ok &= waiting and 'NOT CONFIRMED' in warn and host.store.confirmed()

    # 5. lines lost before the forwarder read them
    st['hold'][0] = True
    put(ns, host, 'data/state/test_log_lost_%d.json' % 1, '{"lost": "%s"}' % ('y' * 3000))
    host.persist_on_callback_thread(reason='lines lost')
    lost = [l for l in st['held'] if ' B ' in l]
    for line in lost:
        st['held'].remove(line)
    st['hold'][0] = False
    st['release']()
    missing = st['values'].get('Missing', '')
    host.store.RESEND_SECONDS = 0.0
    note = host.store.service()
    host.store.service()
    print('5. %d body lines lost: forwarder named %d missing, strategy: "%s", committed %s' % (
        len(lost), len(missing.split()) - 1, note[:60], host.store.confirmed()))
    ok &= len(lost) > 0 and len(missing.split()) > 1 and 'printed again' in note and host.store.confirmed()
    final = files_of(ns, host)
    ns['stop'](C)

    # 6. latest snapshot damaged in the registry: the one before it
    values = kv['__registry_shadow__']
    latest = values['Pointer']
    values['M_' + latest] = values['M_' + latest][:-40] + 'A' * 40
    ns, C, host = boot(path, kv, [UP], calls, 'log', hang)
    drive(ns, C, 3)
    prev_ok = not host.boot_pending and host.engine is not None
    print('6. latest snapshot damaged: started from the one before it %s (%s)' % (
        prev_ok, (host.persist_meta or {}).get('restored_from', '')[:60]))
    ok &= prev_ok and 'before the latest' in (host.persist_meta or {}).get('restored_from', '')
    ns['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
