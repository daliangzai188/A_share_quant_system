# coding: utf-8
"""The host writes reports/execution_tracking/trade_completion_summary.csv to QMT's log as
marked chunks; vm_scripts/qmt_notify_forwarder.py rebuilds it byte for byte outside QMT.
Unchanged content is not written again within 30 minutes.

Usage: python test_export.py STRATEGY_FILE   (QMT's Python 3.6; the forwarder runs on python3)
"""
import contextlib, io, os, random, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_system_local as rs
from test_stop_threads import load


def main():
    ns = load(sys.argv[1], {})
    C = rs.MockContext()
    ns['init'](C)
    host = ns['QN_HOST'][0]
    random.seed(1)
    rows = ['trade_key,entry_date,ts_code,entry_filled_qty,strategy_leg'] + [
        '20261%03d|%06d.SZ|A,2026-10-%02d,%06d.SZ,%d,A' % (i, i, 1 + i % 28, i, random.randint(0, 900)) for i in range(400)]
    data = ('\r\n'.join(rows) + '\r\n').encode('utf-8-sig')
    ns['QN_VFS'].put_bytes('reports/execution_tracking/trade_completion_summary.csv', data)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        host._export_files()
        first = len(buf.getvalue().splitlines())
        host._export_files()
    lines = [l for l in buf.getvalue().splitlines() if l.startswith('[A_SYSTEM_EXPORT')]
    ns['stop'](C)
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, 'log'))
    open(os.path.join(tmp, 'log', 'XtClient_FormulaOutput_20261022.log'), 'wb').write(
        u'旧行\r\n'.encode('gbk') + b''.join(l.encode('ascii') + b'\r\n' for l in lines))
    env = dict(os.environ, A_SYSTEM_ROOT=os.path.join(tmp, 'root'), LOCALAPPDATA=os.path.join(tmp, 'local'))
    os.makedirs(env['A_SYSTEM_ROOT'])
    subprocess.check_call(['/usr/bin/python3', os.path.join(os.path.dirname(HERE), 'vm_scripts', 'qmt_notify_forwarder.py'),
                           '--logs', os.path.join(tmp, 'log', '*.log'), '--mode', 'shadow', '--from-start', '--once'],
                          env=env, stdout=subprocess.DEVNULL)
    out = os.path.join(env['LOCALAPPDATA'], 'A_System', 'qmt_native', 'qmt_export', 'trade_completion_summary.csv')
    same = os.path.exists(out) and open(out, 'rb').read() == data
    print('export lines', len(lines), 'repeated', len(lines) - first, 'rebuilt identical', same)
    print('RESULT', 'PASS' if same and len(lines) == first and len(lines) > 1 else 'FAIL')


if __name__ == '__main__':
    main()
