# coding: utf-8
"""When the trading daemon thread dies (here: an injected RuntimeError, like the startup check
that refuses to start), the threads and scripts it started must end with it, as when a daemon
process exits; the host restarts a fresh daemon and only that one's pipeline runs.

Usage: python test_daemon_crash.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import ctypes, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load


def names(threads):
    return sorted(t.name for t in threads if t.is_alive())


def main():
    C = rs.MockContext()
    ns = load(sys.argv[1], {})
    ns['QnHost'].DAEMON_RESTART_DELAY = 5.0
    ns['init'](C)
    host = ns['QN_HOST'][0]

    def pump(seconds):
        t = time.time()
        while time.time() - t < seconds:
            ns['a_system_pump'](C)
            time.sleep(0.02)
    pump(20)
    first = host.daemon_thread
    started = [t for t in ns['QN_THREADS'] if t.is_alive() and t is not first and getattr(first, '_qn_kill', ())[-1] in getattr(t, '_qn_kill', ())]
    print('daemon children before crash:', names(started))
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(first.ident), ctypes.py_object(RuntimeError))
    pump(4)
    print('first daemon alive:', first.is_alive(), '| its children still alive:', names(started))
    ok = not first.is_alive() and started and not names(started)
    pump(12)
    second = host.daemon_thread
    pipelines = [t for t in ns['QN_THREADS'] if t.is_alive() and t.name in ('post-market-pipeline', 'qn-script-collect_all_data')]
    print('restarted:', second is not first and second.is_alive(), '| crash logged:',
          any('trading daemon crashed' in m for m in host.messages), '| pipeline threads now:', names(pipelines))
    ok &= second is not first and second.is_alive()
    ok &= all(getattr(second, '_qn_kill', ())[-1] in getattr(t, '_qn_kill', ()) for t in pipelines)
    # 10-28: the daemon's process recovery calls os._exit from a helper thread. In QMT that must
    # end the daemon (and restart it), never the QMT process (this test process stands for it).
    import threading
    loader_os = ns['QnLoader'](ns['QnCodeBase'](ns['QN_MODULES'])).shims['os']
    victim = host.daemon_thread
    restarts = host.daemon_restarts
    helper = threading.Thread(target=lambda: loader_os._exit(70))
    helper.start()
    helper.join(5)
    pump(4)
    print('os._exit(70) from a helper thread: process alive True | daemon ended:', not victim.is_alive(),
          '| logged:', any('requested a process exit' in m for m in host.messages))
    ok &= not victim.is_alive() and any('requested a process exit' in m for m in host.messages)
    pump(12)
    print('fresh daemon after os._exit:', host.daemon_restarts > restarts and host.daemon_thread.is_alive())
    ok &= host.daemon_restarts > restarts and host.daemon_thread is not victim and host.daemon_thread.is_alive()
    ns['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
