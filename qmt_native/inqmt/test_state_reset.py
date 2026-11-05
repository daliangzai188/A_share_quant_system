# coding: utf-8
"""One-off operator reset (QN_RESET_STATE_IF_BROKEN, 10-26): a strategy start that finds no complete
state snapshot three times in a row starts from the shipped seed, but only for a snapshot broken in
content; a store that cannot be read is waited for, and without the flag nothing ever resets.
  1. broken snapshot + flag: reset after 3 starts, engine and daemon run, STATE RESET logged;
  2. broken snapshot, no flag: still waiting;
  3. store not answering + flag: still waiting, no reset;
  4. (11-05) automatic, no flag: the broker shows the account flat -> reset after 3 starts, with a
     push when it starts checking and one when it rebuilt;
  5. automatic, the broker shows a position -> never resets, one "需人工处理" push.

Usage: python test_state_reset.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import os, sys, time, urllib.parse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


def broken_store(ns, kv):
    st = ns['QnStateStore'](lambda k: (kv[k], 0, '') if k in kv else ('', -1, 'KeyError: not exist'),
                            lambda k, v: kv.__setitem__(k, v) or (0, ''), 'AS_QNS')
    data = b'{"x": 1}' * 300
    st.save({'data/state/old.json': (data, 1.0, len(data))}, {})
    st.save({'data/state/old.json': (data + b' ', 2.0, len(data) + 1)}, {})
    for k in list(kv):
        if k.startswith('AS_QNS_F_'):
            kv[k] = ''                              # every body gone: nothing complete


def run(path, flag, answering, seconds=25, auto=False, position=False):
    kv = {}
    ns = load(path, kv)
    broken_store(ns, kv)
    if not answering:
        ns['get_py_key'] = lambda k: ('', 200000, u'未建立连接') if k.startswith('AS_QNS_F_') else (
            (kv[k], 0, '') if k in kv else ('', -1, 'KeyError: not exist'))
    else:
        ns['get_py_key'] = lambda k: (kv[k], 0, '') if k in kv else ('', -1, 'KeyError: not exist')
    ns['QN_DEPLOY']['reset_if_broken'] = flag
    ns['QN_DEPLOY']['auto_reset_when_flat'] = auto
    ns['QnHost'].BOOT_RETRY_SECONDS = 2.0
    ns['QnStateStore'].EMPTY_CONFIRM_WAIT = 0.0
    C = rs.MockContext()
    ns['init'](C)
    host = ns['QN_HOST'][0]
    if position:
        host.broker.positions['000001.SZ'] = dict(volume=100, today_buy=0, cost=10.0)
    drive(ns, C, seconds)
    started = host.engine is not None and not host.boot_pending
    resets = [m for m in host.messages if 'STATE RESET' in m]
    waits = [m for m in host.messages if 'not readable' in m]
    pushes = [urllib.parse.unquote(l.split(' /', 1)[1]).split('/')[0] for l in ns['QN_PRINT_LINES']
              if '[A_SYSTEM_NOTIFY' in l]
    ns['stop'](C)
    if auto:
        return started, len(resets), len(waits), pushes
    return started, len(resets), len(waits)


def main():
    path = sys.argv[1]
    ok = True
    started, resets, waits = run(path, True, True)
    print('1. broken + flag: started=%s resets=%d waits_before=%d' % (started, resets, waits))
    ok &= started and resets == 1 and waits == 2
    started, resets, waits = run(path, False, True)
    print('2. broken, no flag: started=%s resets=%d waits=%d' % (started, resets, waits))
    ok &= not started and resets == 0 and waits >= 3
    started, resets, waits = run(path, True, False)
    print('3. not answering + flag: started=%s resets=%d waits=%d' % (started, resets, waits))
    ok &= not started and resets == 0 and waits >= 3
    started, resets, waits, pushes = run(path, False, True, auto=True)
    print('4. broken, automatic, broker flat: started=%s resets=%d waits_before=%d pushes=%s' % (started, resets, waits, pushes))
    ok &= started and resets == 1 and waits == 2 and pushes[:2] == ['⛔ A_System状态读不全，交易未启动', '♻️ A_System已从初始状态自动重建']
    started, resets, waits, pushes = run(path, False, True, auto=True, position=True)
    print('5. broken, automatic, broker holds a position: started=%s resets=%d waits=%d pushes=%s' % (started, resets, waits, pushes))
    ok &= not started and resets == 0 and waits >= 3 and pushes == ['⛔ A_System状态读不全，交易未启动',
                                                                    '🛑 A_System状态丢失且账户非空仓，需人工处理']
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
