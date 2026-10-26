# coding: utf-8
"""Since the broker's file/network restriction (QMT restart of 10-27) QMT logs every file a strategy's
Python opens ("python open file: ..."). The embedded modules are compiled as /A_System/<rel>, and on
the VM C:\\A_System is the old production checkout, so source-line lookups (status line stack,
tracebacks, warnings) opened C:/A_System/scripts/trading_daemon.py every 20 s. Here linecache is
made to see those paths as existing files, like on the VM, and every file opened for source lines
is recorded while the strategy runs:
  1. no file is opened for source lines (status lines, a traceback, a warning);
  2. the status line still names the daemon's place, and tracebacks still show the source line,
     taken from the embedded sources.

Usage: python test_no_disk_reads.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import linecache
import os
import sys
import tokenize
import traceback
import warnings
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load, drive

OPENED = []
_real_open = tokenize.open
_real_stat = os.stat


class _VmOs(object):
    """os for linecache: paths under /A_System exist, as C:\\A_System does on the VM."""
    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def stat(path, *args, **kwargs):
        if str(path).replace('\\', '/').startswith('/A_System/'):
            return _real_stat(__file__)
        return _real_stat(path, *args, **kwargs)


def _recording_open(path):
    OPENED.append(path)
    return _real_open(path)


def main():
    path = sys.argv[1]
    linecache.os = _VmOs()
    tokenize.open = _recording_open
    C = rs.MockContext()
    ns = load(path, {})
    ns['init'](C)
    host = ns['QN_HOST'][0]
    drive(ns, C, 45)
    sources = ns.get('QN_SOURCE_LINES', {})
    daemon_file = [f for f in sources if f.endswith('/scripts/trading_daemon.py')]
    target = daemon_file[0] if daemon_file else '/A_System/scripts/trading_daemon.py'
    src = sources.get(target, [])
    lineno = next((i + 1 for i, line in enumerate(src) if len(line.strip()) > 12), 10)
    want = src[lineno - 1].strip() if src else ''
    code = compile('\n' * (lineno - 1) + 'raise ValueError("boom")\n', target, 'exec')
    try:
        exec(code, {})
    except ValueError:
        tb = traceback.format_exc()
    shown = warnings.formatwarning('test warning', UserWarning, target, lineno)  # what QMT's log would show
    lines = [m for m in host.messages if 'status:' in m]
    where = lines[-1].split('daemon_at=', 1)[-1] if lines else ''
    ns['stop'](C)
    tokenize.open = _real_open
    print('1. files opened for source lines while running: %d %s' % (len(OPENED), sorted(set(OPENED))[:5]))
    ok = not OPENED
    print('2. status lines: %d, daemon at: %s' % (len(lines), where[:120].encode('ascii', 'backslashreplace').decode()))
    print('   traceback / warning show the embedded source line %d: %s / %s' % (lineno, bool(want) and want in tb, bool(want) and want in shown))
    ok &= bool(lines) and 'trading_daemon.py:' in where and bool(want) and want in tb and want in shown
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
