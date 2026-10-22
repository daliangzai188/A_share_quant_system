# coding: utf-8
"""vm_scripts/qmt_notify_forwarder.py against fake QMT logs (GBK) and a fake Bark server:
backlog skipped on first start, live lines sent once, shadow lines ignored, half-written lines
waited for, next day's file picked up, exported files rebuilt byte for byte.

Usage: python3 test_forwarder.py   (Mac Python 3, no QMT needed)
"""
import base64
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FORWARDER = os.path.join(os.path.dirname(HERE), 'vm_scripts', 'qmt_notify_forwarder.py')
GOT = []


class Bark(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        GOT.append(urllib.parse.unquote(self.path))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"code":200}')

    def log_message(self, *args):
        pass


def notify_line(mode, ident, title, body='正文'):
    return ('[2026-10-22 09:00:00][A_SYSTEM] [A_SYSTEM_NOTIFY %s] %s /%s/%s?level=active' % (
        mode, ident, urllib.parse.quote(title), urllib.parse.quote(body))).encode('gbk') + b'\r\n'


def main():
    server = http.server.HTTPServer(('127.0.0.1', 0), Bark)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp()
    root, local, logs = (os.path.join(tmp, d) for d in ('root', 'local', 'log'))
    for d in (root, local, logs):
        os.makedirs(d)
    open(os.path.join(root, '.env'), 'w').write('BARK_URL=http://127.0.0.1:%d/KEY\n' % server.server_port)
    env = dict(os.environ, A_SYSTEM_ROOT=root, LOCALAPPDATA=local)
    day1 = os.path.join(logs, 'XtClient_FormulaOutput_20261022.log')

    def run():
        subprocess.run([sys.executable, FORWARDER, '--logs', os.path.join(logs, 'XtClient_FormulaOutput_*.log'),
                        '--once'], env=env, stdout=subprocess.DEVNULL, check=True)

    open(day1, 'wb').write('旧行\r\n'.encode('gbk') + notify_line('live', 'a-1', '旧推送'))
    run()
    ok = GOT == []
    data = ('trade_key,entry_filled_qty\r\n' + ''.join('k%d,%d\r\n' % (i, i) for i in range(3000))).encode('utf-8-sig')
    packed = base64.b64encode(zlib.compress(data, 9)).decode('ascii')
    parts = [packed[i:i + 1500] for i in range(0, len(packed), 1500)]
    export = b''.join(('[A_SYSTEM_EXPORT live] reports/execution_tracking/trade_completion_summary.csv '
                       'b1-0123456789ab %d/%d %s\r\n' % (i + 1, len(parts), p)).encode('ascii') for i, p in enumerate(parts))
    with open(day1, 'ab') as f:
        f.write(notify_line('live', 'a-2', '今日收盘汇总') + notify_line('shadow', 's-1', '影子')
                + notify_line('live', 'a-2', '今日收盘汇总') + export + b'[A_SYSTEM_NOTIFY live] a-3 /half')
    open(os.path.join(logs, 'XtClient_FormulaOutput_20261023.log'), 'wb').write(notify_line('live', 'a-4', '新的一天'))
    run()
    with open(day1, 'ab') as f:
        f.write(b'_line/x?level=active\r\n')
    run()
    run()
    want = ['/KEY/今日收盘汇总/正文?level=active', '/KEY/新的一天/正文?level=active', '/KEY/half_line/x?level=active']
    print('received:', GOT)
    ok &= GOT == want
    rebuilt = os.path.join(local, 'A_System', 'qmt_native', 'qmt_export', 'trade_completion_summary.csv')
    same = os.path.exists(rebuilt) and open(rebuilt, 'rb').read() == data
    print('export parts', len(parts), 'rebuilt identical:', same)
    ok &= same
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
