# coding: utf-8
"""Trading switch (10-27): start_windows.py / stop_windows.py leave "run" / "pause" in the Windows
registry; the live copy loads with QMT but runs its trading daemon only on "run":
  1. switch not set: A_System waits (no trading daemon), status lines say trading=pause;
  2. "run": the trading daemon starts;
  3. "pause": it ends (its scripts with it), no keeper-style restart, QMT/strategy stay up;
  4. "run" again: it starts again;
  5. every trading-daemon log line is echoed to QMT's log as "[A_SYSTEM_LOG] <line>" (start_windows.py
     follows those in the terminal, as it followed logs/trading_daemon.log before 10-23).

Usage: python test_trading_switch.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import io, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


def main():
    path = sys.argv[1]
    switch = {'value': ''}
    C = rs.MockContext()
    ns = load(path, {})
    ns['QN_DEPLOY']['trading_switch'] = True
    ns['QN_SWITCH_READER'][0] = lambda: switch['value']
    ns['init'](C)
    host = ns['QN_HOST'][0]
    host.DAEMON_RESTART_DELAY = 2.0

    def alive():
        return bool(host.daemon_thread and host.daemon_thread.is_alive())

    def last_status():
        lines = [m for m in host.messages if ' status: ' in m]
        return lines[-1] if lines else ''

    drive(ns, C, 25)
    waiting = not alive() and 'trading=pause' in last_status() and any('A_System waits' in m for m in host.messages) \
        and any('⏸ 交易已暂停' in m and '仍显示“运行中”是正常的' in m for m in host.messages)
    print('1. switch not set: daemon alive=%s, status says pause=%s, waits + plain notice logged=%s' % (
        alive(), 'trading=pause' in last_status(), waiting))
    ok = waiting
    switch['value'] = 'run'
    drive(ns, C, 25)
    started = alive() and 'trading=run' in last_status() and any('▶ 交易中' in m for m in host.messages)
    print('2. run: daemon alive=%s, status says run=%s' % (alive(), 'trading=run' in last_status()))
    ok &= started
    switch['value'] = 'pause'
    drive(ns, C, 30)  # longer than the restart delay
    paused = not alive() and 'trading=pause' in last_status() and \
        any('paused by the trading switch' in m for m in host.messages)
    print('3. pause: daemon alive=%s, ended by the switch=%s, restarted=%s' % (
        alive(), any('paused by the trading switch' in m for m in host.messages),
        any('restarting in' in m for m in host.messages)))
    ok &= paused and not any('restarting in' in m for m in host.messages)
    switch['value'] = 'run'
    captured, real_stdout = io.StringIO(), sys.stdout
    sys.stdout = captured
    try:
        drive(ns, C, 25)
    finally:
        sys.stdout = real_stdout
    again = alive()
    print('4. run again: daemon alive=%s' % again)
    ok &= again
    echoed = [l for l in captured.getvalue().splitlines() if l.startswith('[A_SYSTEM_LOG] ')]
    sample = [l for l in ns['QN_LOG_LINES'] if l][-3:]
    print('5. daemon lines echoed to the QMT log: %d; recent ones present: %s' % (
        len(echoed), all(any(x in e for e in echoed) for x in sample)))
    ok &= len(echoed) > 3 and all(any(x in e for e in echoed) for x in sample)
    ns['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
