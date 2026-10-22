# coding: utf-8
"""A timed-out embedded script killed by the daemon must stop like a killed child process:
its thread ends within about a second, the exit code is -9, its queued QMT calls are dropped,
and other threads are not affected.

Usage: python test_process_kill.py CORE   (QMT's Python 3.6)
"""
import os, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mock_qmt import MockMarket, mock_globals


def main():
    ns = {'__name__': 'qn_core'}
    for name in ('qn_runtime.py', sys.argv[1]):
        exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
    vfs = ns['QN_VFS']
    for rel, text in ns['QN_STATIC_FILES'].items():
        vfs.put_text(rel, text)
    vfs.put_bytes('data/raw/trade_calendar.csv', open(os.path.join(os.path.dirname(HERE), 'vm_raw/data/raw/trade_calendar.csv'), 'rb').read())
    service = ns['QN_NATIVE_SERVICE']
    service.bind(MockMarket(), mock_globals())  # nobody pumps: the script's first QMT call waits
    cb = ns['QnCodeBase'](ns['QN_MODULES'])
    other = ns['QnThread'](target=lambda: ns['qn_time'].sleep(3), name='other', daemon=True)
    other.start()
    proc = ns['QnProcess'](cb, 'scripts.collect_all_data', ['--start-date', '20261020', '--end-date', '20261021'], True)
    time.sleep(1.5)
    queued = service.waiting()
    t0 = time.time()
    proc.kill()
    proc._thread.join(5)
    ended = time.time() - t0
    print('queued before kill', queued, '| thread ended in %.2fs' % ended, 'alive', proc._thread.is_alive(),
          '| returncode', proc.poll(), '| queued after', service.waiting(), '| other thread alive', other.is_alive())
    ok = queued >= 1 and not proc._thread.is_alive() and ended < 2 and proc.poll() == -9 and service.waiting() == 0 \
        and other.is_alive()
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
