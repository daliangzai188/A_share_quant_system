# coding: utf-8
"""Load the packaged strategy file exactly like QMT does (one namespace with QMT globals)
and drive init / a_system_pump against the mock market."""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs

def main():
    path, seconds = sys.argv[1], float(sys.argv[2])
    C = rs.MockContext()
    kv = {}
    ns = {'__name__': '__main__', 'download_history_data': lambda *a: None,
          'get_py_key': lambda k: (kv.get(k, ''), 0, ''),
          'save_py_key': lambda k, v: (kv.__setitem__(k, v) if len(v) <= 5000 else None) or ((0, '') if len(v) <= 5000 else (-1, 'too long'))}
    t0 = time.time()
    exec(compile(open(path, encoding='gbk').read(), path, 'exec'), ns)
    ns['QN_ENVIRON']['QN_TEST_MISSING_TICKS_ARE_UNSEALED'] = '1'
    ns['QN_DEPLOY']['debug_export_dir'] = os.environ.get('QN_TEST_EXPORT', ''); ns['QN_SQLITE_NATIVE_UPSERT'][0] = os.environ.get('QN_OLD_SQLITE') != '1'
    print('loaded %.1fs' % (time.time() - t0))
    ns['init'](C)
    while time.time() - t0 < seconds:
        ns['a_system_pump'](C)
        time.sleep(0.1)
    host = ns['QN_HOST'][0]
    out = ns['QN_LOG_LINES'][-40:] + ['--- host'] + host.messages[-15:]
    sys.stdout.buffer.write(('\n'.join(out) + '\n').encode('utf-8'))
    print('kv keys', len(kv), 'max value', max([len(v) for v in kv.values()] or [0]), 'persist', host.persist_meta, host.persist_error)
    ns['stop'](C)

if __name__ == '__main__':
    main()
