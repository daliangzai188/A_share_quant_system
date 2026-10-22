# coding: gbk
# A_System probe 2: get_py_key / save_py_key semantics (read-only otherwise).
# Prints one short line per test so the QMT log window shows everything.
import time

KEY = 'AS_PROBE_V1'
SIZES = [16, 200, 1000, 4000, 8000, 16000, 32000, 64000]
S = {'ticks': 0, 'saved': {}}


def _log(msg):
    print('[QN_PK] ' + msg)


def _desc(result):
    if isinstance(result, (tuple, list)) and len(result) == 3:
        v = result[0]
        return 'len=%s head=%r err=%r msg=%r' % (len(v) if isinstance(v, str) else type(v).__name__,
                                                 (v[:12] if isinstance(v, str) else v), result[1], result[2])
    return 'raw=%r' % (result,)


def init(ContextInfo):
    g = globals()
    getter, saver = g.get('get_py_key'), g.get('save_py_key')
    _log('previous stamp: ' + _desc(getter(KEY)))
    stamp = 'run ' + time.strftime('%H:%M:%S')
    _log('save stamp %r -> %r' % (stamp, saver(KEY, stamp)))
    _log('stamp now: ' + _desc(getter(KEY)))
    for n in SIZES:
        key = 'AS_PK_%d' % n
        value = ('abcdefghijklmnop' * (n // 16 + 1))[:n]
        t0 = time.time()
        r = saver(key, value)
        t1 = time.time()
        got = getter(key)
        t2 = time.time()
        ok = isinstance(got, (tuple, list)) and got[0] == value
        S['saved'][key] = value
        _log('n=%d save=%r %.3fs get %.3fs ok=%s %s' % (n, r, t1 - t0, t2 - t1, ok, _desc(got)))
    ContextInfo.run_time('qn_pk_tick', '3nSecond', '2020-01-01 00:00:00')


def qn_pk_tick(ContextInfo):
    S['ticks'] += 1
    if S['ticks'] in (1, 4):
        getter = globals().get('get_py_key')
        for key, value in sorted(S['saved'].items(), key=lambda kv: len(kv[1])):
            got = getter(key)
            ok = isinstance(got, (tuple, list)) and got[0] == value
            _log('later t%d %s ok=%s %s' % (S['ticks'], key, ok, _desc(got)))


def handlebar(ContextInfo):
    pass
