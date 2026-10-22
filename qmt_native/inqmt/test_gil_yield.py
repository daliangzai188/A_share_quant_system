# coding: utf-8
"""In 模型交易 QMT keeps Python's lock (GIL) between the strategy's timer callbacks; background
threads (daemon, pipeline scripts) then only run while a callback runs. Here the "QMT thread"
holds the lock between ticks with a C loop (builtin sum over range never releases it) while a
CPU-bound pipeline script counts its progress:
  1. without pending pipeline work a tick only opens a short window (the engine keeps its pace);
  2. with a script running, ticks hand the lock over: several times more script work;
  3. the running strategy reports the background wake-up lag in its status line.

Usage: python test_gil_yield.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import os, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive


def hold_loops(seconds):
    t = time.time()
    sum(range(10 ** 6))
    return int(seconds / max(time.time() - t, 1e-4) * 10 ** 6)


class FakeScript(object):
    """Stands in for a running pipeline script: a CPU-bound thread counting its work."""
    def __init__(self):
        self.done = 0
        self.stop = False
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def run(self):
        while not self.stop:
            sum(range(2000))
            self.done += 1


def qmt_loop(host, seconds, loops, use_host):
    """QMT's thread: our tick (here only its yield step), then ~95 ms holding the lock."""
    t0 = time.time()
    while time.time() - t0 < seconds:
        if use_host:
            host._yield_to_background()
        sum(range(loops))


def main():
    path = sys.argv[1]
    ns = load(path, {})
    Host = ns['QnHost']
    host = Host.__new__(Host)
    host.yield_ticks, host.bg_lag = [0, 0], []
    loops = hold_loops(0.095)

    t = time.time()
    host._yield_to_background()
    idle_tick = time.time() - t
    qmt_loop(host, 2, loops, True)
    print('1. idle: long yields %d of %d ticks; idle window %.0f ms' % (
        host.yield_ticks[0], host.yield_ticks[1], idle_tick * 1000))
    ok = host.yield_ticks[0] == 0 and host.yield_ticks[1] > 0 and idle_tick < 0.04

    results = {}
    for label, use_host in (('off', False), ('on', True)):
        script = FakeScript()
        ns['QN_PROCESSES'][-1] = script
        qmt_loop(host, 8, loops, use_host)
        script.stop = True
        script._thread.join(5)
        del ns['QN_PROCESSES'][-1]
        results[label] = script.done
    ratio = results['on'] / float(max(results['off'], 1))
    print('2. script work in 8 s: yield off %d, on %d (x%.1f)' % (results['off'], results['on'], ratio))
    ok &= ratio >= 3

    C = rs.MockContext()
    live = load(path, {})
    live['init'](C)
    drive(live, C, 25)
    lines = [l for l in live['QN_HOST'][0].messages if 'status:' in l]
    print('3. status:', lines[-1][:150].encode('ascii', 'backslashreplace').decode() if lines else 'none')
    ok &= bool(lines) and 'bg_lag=' in lines[-1] and ' yield=' in lines[-1]
    live['stop'](C)
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
