# coding: utf-8
"""Restored state vs the broker (11-05): QMT's key-value store is per trade server and not replicated,
so a start may restore an older snapshot from another server. Before trading starts:
  1. flat account, no order today: passes, the trading daemon starts;
  2. an order of this system today (remark = our tag) that the restored journal does not know: trading
     held, one push; running start_windows.py after the hold (switch ChangedAt later) starts it;
  3. positions held and the snapshot older than the last trading day's close: held; a snapshot saved
     after that close: passes;
  4. the broker not answering yet: trading waits; once it answers (flat): the daemon starts.

Usage: python test_state_check.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import os, sys, time, urllib.parse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


class Row(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


def boot(path, broker_rows, changed_at):
    ns = load(path, {})
    ns['QN_DEPLOY']['trading_switch'] = True
    ns['QN_DEPLOY']['state_check'] = True
    ns['QN_SWITCH_READER'][0] = lambda: 'run'
    ns['QN_SWITCH_CHANGED_READER'][0] = lambda: changed_at[0]
    ns['QnHost'].STATE_CHECK_RETRY_SECONDS = 2.0
    real_init = ns['QnHost'].__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        acct = str(self.deploy['account_id'])
        self.broker.get_trade_detail_data = lambda account, market, kind: broker_rows(acct, kind)
    ns['QnHost'].__init__ = init
    C = rs.MockContext()
    ns['init'](C)
    return ns, C, ns['QN_HOST'][0]


def pushes(ns):
    return [urllib.parse.unquote(l.split(' /', 1)[1]).split('/')[0] for l in ns['QN_PRINT_LINES'] if '[A_SYSTEM_NOTIFY' in l]


def alive(host):
    return bool(host.daemon_thread and host.daemon_thread.is_alive())


def main():
    path = sys.argv[1]
    ok = True
    changed = ['2026-01-01 00:00:00']
    flat = lambda acct, kind: [Row(m_strAccountID=acct)] if kind == 'account' else []
    ns, C, host = boot(path, flat, changed)
    drive(ns, C, 8)
    r1 = alive(host) and host.state_hold == ''
    print('1. flat, no order today: hold %r, daemon alive %s' % (host.state_hold, alive(host)))
    ok &= r1
    ns['stop'](C)

    tag = 'AS' + 'f' * 28
    with_order = lambda acct, kind: ([Row(m_strAccountID=acct)] if kind == 'account' else
                                      [Row(m_strAccountID=acct, m_strRemark=tag, m_nOrderStatus=56)] if kind == 'order' else [])
    ns, C, host = boot(path, with_order, changed)
    drive(ns, C, 8)
    held = not alive(host) and 'not' not in host.state_hold and bool(host.state_hold)
    first = pushes(ns)
    changed[0] = '2099-01-01 00:00:00'  # start_windows.py after the hold
    drive(ns, C, 8)
    released = alive(host) and host.state_hold == ''
    print('2. unknown order today: held %s (%s), pushes %s; after start_windows.py: daemon alive %s' % (
        held, host.state_hold[:40] or '-', first, released))
    ok &= held and first.count('🛑 A_System恢复的状态可能不是最新，交易未启动') == 1 and released
    ns['stop'](C)

    changed[0] = '2026-01-01 00:00:00'
    holding = lambda acct, kind: ([Row(m_strAccountID=acct)] if kind == 'account' else
                                   [Row(m_strAccountID=acct, m_strInstrumentID='600000', m_strExchangeID='SH', m_nVolume=100)]
                                   if kind == 'position' else [])
    ns, C, host = boot(path, holding, changed)
    last_close = host._last_close_before_today()
    host.persist_meta = {'saved_at': '2020-01-01 10:00:00'}
    old = host._check_restored_state()
    host.persist_meta = {'saved_at': last_close[:10] + ' 16:00:00'}
    recent = host._check_restored_state()
    print('3. positions held: snapshot older than %s -> %s; saved after it -> %r' % (last_close, old[:50] or 'passes', recent))
    ok &= bool(old) and old != 'pending' and recent == ''
    ns['stop'](C)

    answering = [False]
    slow = lambda acct, kind: (flat(acct, kind) if answering[0] else [])
    ns, C, host = boot(path, slow, changed)
    drive(ns, C, 5)
    waiting = not alive(host) and host.state_hold == 'pending'
    answering[0] = True
    drive(ns, C, 8)
    print('4. broker not answering: waiting %s; once it answers: daemon alive %s' % (waiting, alive(host)))
    ok &= waiting and alive(host) and host.state_hold == ''
    ns['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
