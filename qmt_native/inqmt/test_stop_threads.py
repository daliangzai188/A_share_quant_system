# coding: utf-8
"""QMT re-executes the strategy file on every run while the interpreter (and its threads)
lives on. Run copy A, stop it mid-pipeline, start copy B in a fresh namespace: every thread
of A must end, and none of A's log records may reach B's log.

Usage: python test_stop_threads.py OLD_OR_SAME_FILE NEW_FILE SECONDS_A SECONDS_B
"""
import os, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs


def load(path, kv):
    ns = {'__name__': '__main__', 'download_history_data': lambda *a: None,
          'get_py_key': lambda k: (kv.get(k, ''), 0, ''),
          'save_py_key': lambda k, v: (kv.__setitem__(k, v) if len(v) <= 5000 else None) or ((0, '') if len(v) <= 5000 else (-1, 'too long'))}
    exec(compile(open(path, encoding='gbk').read(), path, 'exec'), ns)
    ns['QN_ENVIRON']['QN_TEST_MISSING_TICKS_ARE_UNSEALED'] = '1'
    ns['QN_DEPLOY']['debug_export_dir'] = ''
    ns['QN_DEPLOY']['store_day_hours'] = [0, 24]
    return ns


def drive(ns, C, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        ns['a_system_pump'](C)
        time.sleep(0.1)


def system_threads(ns_threads):
    return [t for t in ns_threads if t.is_alive()]


def main():
    a_path, b_path, sa, sb = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
    C = rs.MockContext()
    kv = {}
    a = load(a_path, kv)
    a['init'](C)
    drive(a, C, sa)
    a_threads = list(a['QN_THREADS'])
    print('A alive before stop:', sorted(t.name for t in system_threads(a_threads)))
    a['stop'](C)
    b = load(b_path, kv)
    b['init'](C)
    drive(b, C, sb)
    left = system_threads(a_threads)
    print('A alive after B ran %.0fs:' % sb, sorted(t.name for t in left))
    b_host = b['QN_HOST'][0]
    a_ident = set(t.ident for t in a_threads)
    print('B host messages:')
    for m in b_host.messages[-8:]:
        print('  ', m.encode('ascii', 'backslashreplace').decode())
    print('B daemon alive:', b_host.daemon_thread.is_alive(), 'B log lines:', len(b['QN_LOG_LINES']))
    b['stop'](C)
    time.sleep(3)
    b_left = system_threads(list(b['QN_THREADS']))
    print('B alive 3s after its stop:', sorted(t.name for t in b_left))
    print('RESULT', 'PASS' if not left and not b_left else 'FAIL')


if __name__ == '__main__':
    main()
