# coding: utf-8
"""Two copies of the strategy in one QMT interpreter.
1. shadow copy runs, live copy starts: shadow's threads end, shadow goes inactive and its
   later stop() saves nothing.
2. live copy runs, a shadow copy is started: the shadow refuses, live keeps running.

Usage: python test_takeover.py SHADOW_FILE LIVE_FILE
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


def alive(threads):
    return sorted(t.name for t in threads if t.is_alive())


def main():
    shadow_path, live_path = sys.argv[1], sys.argv[2]
    C = rs.MockContext()
    if not hasattr(C, 'set_account'):
        C.set_account = lambda acc: None
    kv = {}
    s1 = load(shadow_path, kv)
    s1['QN_DEPLOY']['store_day_hours'] = [0, 24]  # saves allowed, so 'stop saved nothing' is a real check
    s1['init'](C)
    drive(s1, C, 25)
    paper = s1['QnPaperBroker']('8880000001', 1000000.0, C.quote)
    live = load(live_path, kv)
    live['QN_DEPLOY']['store_day_hours'] = [0, 24]
    live.update(get_trade_detail_data=paper.get_trade_detail_data, passorder=paper.passorder, cancel=paper.cancel)
    live['init'](C)
    for _ in range(250):  # both copies keep getting QMT callbacks, as in QMT
        s1['a_system_pump'](C)
        live['a_system_pump'](C)
        time.sleep(0.1)
    shadow_keys = dict((k, v) for k, v in kv.items() if k.startswith('AS_QNS'))
    print('1. shadow threads after live start:', alive(s1['QN_THREADS']))
    print('   shadow host stopped:', s1['QN_HOST'][0].stopped, '| last shadow msgs:')
    for m in s1['QN_HOST'][0].messages[-3:]:
        print('     ', m.encode('ascii', 'backslashreplace').decode()[:160])
    s1['stop'](C)
    same = shadow_keys == dict((k, v) for k, v in kv.items() if k.startswith('AS_QNS'))
    print('   shadow stop saved nothing:', same)
    print('   live daemon alive:', live['QN_HOST'][0].daemon_thread.is_alive(), 'live keys:', len([k for k in kv if k.startswith('AS_QNL')]))
    s2 = load(shadow_path, kv)
    try:
        s2['init'](C)
        refused = False
    except RuntimeError as exc:
        refused = True
        print('2. shadow refused:', str(exc).encode('ascii', 'backslashreplace').decode()[:160])
    drive(live, C, 5)
    live_ok = live['QN_HOST'][0].daemon_thread.is_alive() and not live['QN_HOST'][0].stopped
    print('   live still running:', live_ok, '| shadow QN_HOST:', s2['QN_HOST'][0])
    ok = not alive(s1['QN_THREADS']) and s1['QN_HOST'][0] is None and same and refused and live_ok
    live['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
