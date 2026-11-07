# coding: utf-8
"""No key-value call while the trade server is down (11-07): a start retried reading the state
store at 11:54 while the trade server was down (11:52-11:59); get_py_key never returned and QMT's
Python stayed frozen until QMT was restarted (as on 10-24). The account status (a local read)
says when the trade server is down:
  1. a start while the account shows 登录失败: no key-value call; it waits and says so;
  2. the status turns to 登录成功: still no call until it has said so for BOOT_LINK_STABLE, then
     the state is read and the start completes;
  3. the connection drops during the read and the read returns cut values: not counted as a
     broken store (no push, no reset count); read again once the connection is back;
  4. an order while the account shows 登录失败: no key-value call before it, the order goes ahead;
  5. stopping in the day while the account shows 登录失败: no key-value call, saved state kept.

Usage: python test_link_gate.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import os, sys, time, urllib.parse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive

UP, DOWN = u'登录成功', u'登录失败'


class Row(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


def boot(path, kv, status, calls, cut=None):
    ns = load(path, kv)
    ns['QN_DEPLOY']['trading_switch'] = True
    ns['QN_DEPLOY']['store_day_hours'] = [0, 24]
    ns['QN_SWITCH_READER'][0] = lambda: 'run'
    real_get, real_save = ns['get_py_key'], ns['save_py_key']

    def get(k):
        calls.append(k)
        value = real_get(k)
        if cut and cut[0] and k.startswith('AS_QNS_F_'):
            status[0] = DOWN  # the connection drops during the read
            v = value[0] if isinstance(value, tuple) else value
            return (v[:len(v) // 2], 0, '') if isinstance(value, tuple) else v[:len(v) // 2]
        return value

    def save(k, v):
        calls.append(k)
        return real_save(k, v)
    ns['get_py_key'], ns['save_py_key'] = get, save
    real_init = ns['QnHost'].__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self.BOOT_LINK_STABLE, self.BOOT_LINK_POLL, self.BOOT_RETRY_SECONDS = 2.0, 0.5, 1.0
        acct = str(self.deploy['account_id'])
        self.broker.get_trade_detail_data = lambda account, market, kind, *r: (
            [Row(m_strAccountID=acct, m_strStatus=status[0])] if kind == 'account' else [])
    ns['QnHost'].__init__ = init
    C = rs.MockContext()
    ns['init'](C)
    return ns, C, ns['QN_HOST'][0]


def pushes(ns):
    return [urllib.parse.unquote(l.split(' /', 1)[1]).split('/')[0] for l in ns['QN_PRINT_LINES'] if '[A_SYSTEM_NOTIFY' in l]


def main():
    path = sys.argv[1]
    kv, ok = {}, True
    status, calls = [UP], []
    ns, C, host = boot(path, kv, status, calls)
    drive(ns, C, 8)
    with host.persist_lock:
        ns['QN_VFS'].put_text('data/state/test_link_marker.json', '{"v": 1}')
    host.persist_on_callback_thread(reason='first state')
    ns['stop'](C)
    saved = bool(kv.get('AS_QNS_PTR'))

    status[0], calls[:] = DOWN, []
    ns, C, host = boot(path, kv, status, calls)
    drive(ns, C, 4)
    waited = host.boot_pending and not calls and any('waiting for the trade connection' in m for m in host.messages)
    print('1. start while %s: key-value calls %d, waiting %s' % ('登录失败', len(calls), waited))
    ok &= saved and waited

    status[0] = UP
    drive(ns, C, 1.0)
    early = len(calls)
    drive(ns, C, 4)
    started = not host.boot_pending and host.engine is not None
    restored = ns['QN_VFS'].files.get(ns['QN_VFS'].abspath('data/state/test_link_marker.json')) == b'{"v": 1}'
    print('2. 登录成功: calls within the first second %d; started %s, state restored %s' % (early, started, restored))
    ok &= early == 0 and started and restored
    ns['stop'](C)

    status[0], calls[:] = UP, []
    cut = [True]
    ns, C, host = boot(path, kv, status, calls, cut)
    drive(ns, C, 4)
    cut[0] = False
    not_counted = host.broken_boots == 0 and host.boot_pending and not pushes(ns)
    status[0] = UP
    drive(ns, C, 6)
    print('3. connection dropped during the read: broken count %d, pushes %s; started after it is back %s' % (
        host.broken_boots, pushes(ns), not host.boot_pending))
    ok &= not_counted and not host.boot_pending and host.engine is not None

    orders = []
    host.broker.passorder = lambda *a, **k: orders.append(len(calls))
    passorder = host._trading_api()['passorder']
    host.PERSIST_INTERVAL = host.PERSIST_INTERVAL_BUSY = 1e9
    drive(ns, C, 1)
    status[0] = DOWN
    with host.persist_lock:
        ns['QN_VFS'].put_text('data/state/test_link_marker.json', '{"v": 2}')
    before = len(calls)
    passorder(23, 1101, 'acct', '600000.SH', 11, 10.0, 100, 'A_SYSTEM', 2, 'AS' + 'd' * 28, C)
    print('4. order while 登录失败: key-value calls before it %d, order placed %s' % (orders[-1] - before if orders else -1, bool(orders)))
    ok &= bool(orders) and orders[-1] == before

    before = len(calls)
    ns['stop'](C)
    kept = any('trade server not connected' in m for m in host.messages)
    print('5. stop while 登录失败: key-value calls %d, saved state kept %s' % (len(calls) - before, kept))
    ok &= len(calls) == before and kept
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
