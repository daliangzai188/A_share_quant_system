# coding: utf-8
"""11-05 11:49: stopping the strategy in the daytime ran the final state save first; the trade server
was slow, the save took minutes, and the still-running trading daemon saw the engine heartbeat stop
and pushed false "账户断连" / "内置桥接失联" alarms. The stop now ends the trading daemon before the
final save:
  1. the daemon runs (switch "run");
  2. stop() with every key-value write taking 1 s: no write happens while the daemon is alive;
  3. the save still completes and every thread ends; one "⏹ A_System策略已停止" push at the stop
     (11-05 23:29: QMT closed without any push).

Usage: python test_stop_slow_save.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


def main():
    kv = {}
    C = rs.MockContext()
    ns = load(sys.argv[1], kv)
    ns['QN_DEPLOY']['trading_switch'] = True
    ns['QN_DEPLOY']['stop_push'] = True
    ns['QN_SWITCH_READER'][0] = lambda: 'run'
    ns['init'](C)
    host = ns['QN_HOST'][0]
    drive(ns, C, 25)
    running = bool(host.daemon_thread and host.daemon_thread.is_alive())
    print('1. trading daemon alive before the stop: %s' % running)
    ok = running
    writes, real = [], ns['save_py_key']

    def slow_save(k, v):
        writes.append(bool(host.daemon_thread and host.daemon_thread.is_alive()))
        time.sleep(1.0)
        return real(k, v)
    ns['save_py_key'] = slow_save
    with host.persist_lock:
        ns['QN_VFS'].put_text('logs/broker_health.json', '{"t": %r}' % time.time())  # something to save
    t0 = time.time()
    ns['stop'](C)
    took = time.time() - t0
    alive_during = sum(writes)
    print('2. final save: %d key-value writes in %.0f s, daemon alive during %d of them' % (len(writes), took, alive_during))
    ok &= len(writes) > 0 and alive_during == 0
    import urllib.parse
    pushes = [urllib.parse.unquote(l.split(' /', 1)[1]).split('/')[0] for l in ns['QN_PRINT_LINES'] if '[A_SYSTEM_NOTIFY' in l]
    print('4. pushes at the stop: %s' % [p for p in pushes if p.startswith('⏹')])
    ok &= pushes.count('⏹ A_System策略已停止') == 1
    time.sleep(2)
    left = [t.name for t in list(ns['QN_THREADS']) if t.is_alive()]
    saved = any('strategy stop' in m or 'saved' in m for m in host.messages)
    print('3. threads still alive after the stop: %s; stop logged: %s' % (left or 'none', saved))
    ok &= not left
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
