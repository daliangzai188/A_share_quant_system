# coding: gbk
# A_System QMT capability probe (read-only, no orders, no files, no network).
# Checks: background threads keep running between callbacks; whether a thread can
# call read-only ContextInfo APIs; get_py_key/save_py_key size limit and restart
# persistence; runtime versions; compile time of a large embedded module.
# Paste into a NEW strategy (not A_SYSTEM_QMT_INNER), run it, read the log window.
import sys
import threading
import time

PROBE = {'ticks': 0, 'worker': 0, 'cpu_rounds': 0, 'thread_api': 'not run', 'started': 0.0, 'errors': []}
KEY = 'AS_PROBE_V1'


def _log(msg):
    print('[QN_PROBE] ' + msg)


def _worker():
    while True:
        PROBE['worker'] += 1
        time.sleep(0.2)


def _cpu_worker():
    try:
        import pandas as pd
        import numpy as np
        while True:
            df = pd.DataFrame({'a': np.arange(200000) % 97, 'b': np.random.rand(200000)})
            df.groupby('a')['b'].mean()
            PROBE['cpu_rounds'] += 1
            time.sleep(0.05)
    except Exception as exc:
        PROBE['errors'].append('cpu:' + repr(exc))


def _thread_api_probe(ContextInfo):
    try:
        data = ContextInfo.get_market_data_ex(['close'], ['600000.SH'], period='1d', count=3,
                                              dividend_type='none', fill_data=False, subscribe=False)
        PROBE['thread_api'] = 'ok rows=%s' % (len(data.get('600000.SH', [])) if hasattr(data, 'get') else '?')
    except Exception as exc:
        PROBE['thread_api'] = 'error ' + repr(exc)[:200]


def _py_key_tests():
    out = []
    g = globals()
    getter = g.get('get_py_key')
    saver = g.get('save_py_key')
    out.append('api get=%s save=%s' % (callable(getter), callable(saver)))
    if not (callable(getter) and callable(saver)):
        return out
    try:
        prev = getter(KEY)
        out.append('previous value from last run: %r' % (prev if not isinstance(prev, tuple) else (str(prev[0])[:80], prev[1], prev[2]),))
    except Exception as exc:
        out.append('get previous error ' + repr(exc)[:200])
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        r = saver(KEY, 'written ' + stamp)
        out.append('save stamp -> %r' % (r,))
    except Exception as exc:
        out.append('save stamp error ' + repr(exc)[:200])
    for size in (65536, 524288, 2097152, 4194304, 8388608):
        key = 'AS_PROBE_SIZE_%d' % size
        value = ('0123456789abcdef' * (size // 16 + 1))[:size]
        t0 = time.time()
        try:
            saver(key, value)
            got = getter(key)
            text = got[0] if isinstance(got, tuple) else got
            ok = isinstance(text, str) and text == value
            out.append('size %d: roundtrip=%s err=%r %.2fs' % (size, ok, got[1:] if isinstance(got, tuple) else None, time.time() - t0))
            saver(key, '')
        except Exception as exc:
            out.append('size %d: error %s' % (size, repr(exc)[:160]))
    return out


def _compile_probe():
    body = '\n'.join('def f%d(x):\n    return [i * x for i in range(10)]\n' % i for i in range(12000))
    t0 = time.time()
    code = compile(body, '<qn_probe>', 'exec')
    ns = {}
    exec(code, ns)
    return 'compiled %d bytes in %.2fs' % (len(body), time.time() - t0)


def init(ContextInfo):
    PROBE['started'] = time.time()
    _log('python %s' % sys.version.split()[0])
    try:
        import pandas, numpy
        _log('pandas %s numpy %s' % (pandas.__version__, numpy.__version__))
    except Exception as exc:
        _log('pandas import error ' + repr(exc))
    for name in ('sqlite3', 'ctypes', 'queue', 'concurrent.futures', 'decimal', 'hashlib', 'zlib', 'base64', 'json'):
        try:
            __import__(name)
            _log('import %s ok' % name)
        except Exception as exc:
            _log('import %s FAILED %r' % (name, exc))
    threading.Thread(target=_worker, name='qn_probe_worker', daemon=True).start()
    threading.Thread(target=_cpu_worker, name='qn_probe_cpu', daemon=True).start()
    threading.Thread(target=_thread_api_probe, args=(ContextInfo,), name='qn_probe_api', daemon=True).start()
    for line in _py_key_tests():
        _log('py_key ' + line)
    try:
        _log(_compile_probe())
    except Exception as exc:
        _log('compile probe error ' + repr(exc))
    ContextInfo.run_time('qn_probe_tick', '5nSecond', '2020-01-01 00:00:00')
    _log('init done; watch qn_probe_tick lines for 30 seconds')


def qn_probe_tick(ContextInfo):
    PROBE['ticks'] += 1
    if PROBE['ticks'] <= 12:
        _log('tick %d elapsed=%.1fs worker=%d (expect ~5/s) cpu_rounds=%d thread_api=%s errors=%s threads=%s' % (
            PROBE['ticks'], time.time() - PROBE['started'], PROBE['worker'], PROBE['cpu_rounds'],
            PROBE['thread_api'], PROBE['errors'][:2], [t.name for t in threading.enumerate()][:6]))


def handlebar(ContextInfo):
    pass
