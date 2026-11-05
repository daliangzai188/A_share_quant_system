# coding: utf-8
"""vm_scripts/qmt_notify_forwarder.py against fake QMT logs (GBK) and a fake Bark server:
backlog skipped on first start, live lines sent once, shadow lines ignored, half-written lines
waited for, next day's file picked up, exported files rebuilt byte for byte; no status line of the
live copy for 10 minutes -> one alarm (none for another strategy's lines), then one recovery push.

Usage: python3 test_forwarder.py   (Mac Python 3, no QMT needed)
"""
import base64
import datetime
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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

    state_file = os.path.join(local, 'A_System', 'qmt_native', 'qmt_notify_forwarder.json')
    day2 = os.path.join(logs, 'XtClient_FormulaOutput_20261023.log')

    def status(strategy):
        with open(day2, 'ab') as f:
            f.write(('2026-10-23 18:04:02,425 [INFO] [0x00001f48] reqid = 0C:\\QMT\\GJQMT\\python\\%s.py_000300119, '
                     'output = [A_SYSTEM] 18:04:02 status: daemon_alive=True 中文\r\n' % strategy).encode('gbk'))

    def edit_state(**values):
        state = json.load(open(state_file, encoding='utf-8'))
        state.update(values)
        json.dump(state, open(state_file, 'w', encoding='utf-8'))
        return state

    def titles():
        return [g.split('/')[2] for g in GOT[len(want):]]

    status('A_SYSTEM_QMT_INNER')
    run()
    fresh = time.time() - json.load(open(state_file, encoding='utf-8'))['alive_at'] < 30
    edit_state(alive_at=time.time() - 700)  # 11+ minutes without a status line
    run()
    run()
    status('A_SYSTEM_SHADOW')              # another strategy's status line is no sign of life
    run()
    alarm = titles()
    print('stall: alive_at fresh after a status line:', fresh, '| pushes while silent:', alarm)
    ok &= fresh and alarm == ['🛑 A_System策略已停止输出'] and '11分钟' in GOT[-1]
    edit_state(stall_alarm_at=time.time() - 3700)  # an hour later: a reminder, except at night (Beijing)
    run()
    hhmm = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%H%M')
    reminder = ['🛑 A_System策略已停止输出'] if '0700' <= hhmm < '2300' else []
    print('reminder after an hour (Beijing %s):' % hhmm, titles()[1:])
    ok &= titles()[1:] == reminder and (not reminder or '第2次提醒' in GOT[-1])
    status('A_SYSTEM_QMT_INNER')
    run()
    run()
    print('after output resumes:', titles()[1 + len(reminder):])
    ok &= titles()[1 + len(reminder):] == ['✅ A_System策略已恢复输出']
    # reminders by the Beijing clock, independent of when this test runs
    import importlib.util
    spec = importlib.util.spec_from_file_location('fwd', FORWARDER)
    fwd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fwd)
    pushed = []
    fwd.send = lambda base, path: pushed.append(urllib.parse.unquote(path)) or True
    fwd.log = lambda text: None
    bj = datetime.timezone(datetime.timedelta(hours=8))
    day = datetime.datetime(2026, 10, 27, 10, 0, tzinfo=bj).timestamp()
    night = datetime.datetime(2026, 10, 27, 0, 30, tzinfo=bj).timestamp()
    pushed[:] = []
    state = {'alive_at': day - 700}
    fwd.check_stall(state, 'http://b', day, 600, stopped_at=day - 690)      # stop_windows.py just after the last line
    stopped_quiet = not pushed
    state = {'alive_at': day - 700}
    fwd.check_stall(state, 'http://b', day, 600, stopped_at=day - 5000)     # old marker, strategy ran again since
    print('stall after a user stop: %d pushes; with an old stop marker: %d' % (0 if stopped_quiet else 1, len(pushed)))
    ok &= stopped_quiet and len(pushed) == 1
    for label, now, want_n in (('10:00', day, 2), ('00:30', night, 1)):
        pushed[:] = []
        state = {'alive_at': now - 700}
        fwd.check_stall(state, 'http://b', now, 600)
        fwd.check_stall(state, 'http://b', now + 1800, 600)   # half an hour later: nothing new
        fwd.check_stall(state, 'http://b', now + 3700, 600)   # an hour later: reminder unless at night
        print('stall at %s: %d pushes, last: %s' % (label, len(pushed), pushed[-1][:60]))
        ok &= len(pushed) == want_n and ('第2次提醒' in pushed[-1]) == (want_n == 2)
    # QMT 2.1.33's network monitor stopping the strategy (lines as in QMT's own log of 11-04): pushed
    # once at once; backlog, a stop while not running and a stop clicked by the user are not pushed
    client = os.path.join(logs, 'XtClient_20261104.log')

    def qline(clock, thread, text):
        return ('2026-11-04 %s,231 [INFO] [0x00002f30] [%s] [%s,230] %s\r\n' % (clock, thread, clock, text)).encode('utf-8')
    mon, ui = '0x00000fcc', '0x00002b4c'
    tcp = '[monitor]TCP - PID:5364 remoteIP:[142.250.198.131:80], localIP:[172.16.136.128:56309]'
    stop = ('[0x1f9d7b085d0] [TC::CTradeStrategyData::writeStrategyRunLog] [TradeStrategy]try stop : ID:1, '
            'fromula:A_SYSTEM_QMT_INNER, gbkfromula:A_SYSTEM_QMT_INNER, account:1234567890, log:')
    done = '[0x1f9d7b085d0] [TC::CTradeStrategyData::doStop] [CTradeStrategyData]stop success, m_qsRequestID:x.py_0003007'
    fail = '[0x1f9d7b085d0] [TC::CTradeStrategyData::doStop] [CTradeStrategyData]stop error, m_qsRequestID:'
    open(client, 'wb').write(qline('21:00:00', mon, tcp) + qline('21:00:00', mon, stop) + qline('21:00:00', mon, done))
    pattern = [os.path.join(logs, 'XtClient_2*.log')]
    pushed[:] = []
    st = {}
    fwd.watch_client_stops(pattern, 'A_SYSTEM_QMT_INNER', st, 'http://b', start_at_end=True)
    backlog_quiet = not pushed
    with open(client, 'ab') as f:
        f.write(qline('22:48:24', mon, tcp) + qline('22:48:24', mon, stop) + qline('22:48:24', mon, fail)
                + qline('22:48:24', mon, '[monitor] illegal ip: 142.250.198.131:80')
                + qline('22:48:40', ui, '[TradeStrategy]try run : ID:1, fromula:A_SYSTEM_QMT_INNER, log:auto run timeout')
                + qline('22:48:42', mon, tcp) + qline('22:48:42', mon, stop) + qline('22:48:42', mon, done)
                + qline('23:51:42', ui, stop) + qline('23:51:42', ui, done))
    fwd.watch_client_stops(pattern, 'A_SYSTEM_QMT_INNER', st, 'http://b', start_at_end=False)
    fwd.watch_client_stops(pattern, 'A_SYSTEM_QMT_INNER', st, 'http://b', start_at_end=False)
    print('QMT monitor stop: backlog quiet %s, pushes %s' % (backlog_quiet, [p[:70] for p in pushed]))
    ok &= (backlog_quiet and len(pushed) == 1 and pushed[0].startswith('/⛔ QMT误判非法IP')
           and '11-04 22:48:42' in pushed[0] and '142.250.198.131:80' in pushed[0])
    # trade calendar for QMT (11-05): Tushare rows -> registry feed, every 6 h, an hour after a failure,
    # never with a token missing; a gap or a weekend marked open is refused
    def cal_rows(first, last, holidays=()):
        d, rows = datetime.date(*first), []
        while d <= datetime.date(*last):
            key = d.strftime('%Y%m%d')
            rows.append((key, d.weekday() < 5 and key not in holidays))
            d += datetime.timedelta(days=1)
        return rows
    rows = cal_rows((2026, 8, 7), (2027, 12, 10), ('20261001', '20261002', '20270101', '20270211', '20270212'))
    feed = fwd.calendar_feed(rows)
    head, kind, first, bits, digest = feed.split('|')
    import hashlib
    feed_ok = (head, kind, first) == ('v1', 'SSE', '20260807') and len(bits) == len(rows) and \
        digest == hashlib.sha256((first + bits).encode()).hexdigest()[:8] and bits[rows.index(('20270101', False))] == '0'
    refused = []
    for bad in (rows[:100] + rows[101:], [(d, True) for d, _ in rows]):
        try:
            fwd.calendar_feed(bad)
        except ValueError as exc:
            refused.append(str(exc)[:30])
    written, calls = [], {'n': 0}

    def fetch(token, today):
        calls['n'] += 1
        if calls['n'] == 2:
            raise RuntimeError('tushare down')
        return rows
    fwd.env_value = lambda name: 'TOKEN' if name == 'TUSHARE_TOKEN' else ''
    st, t0 = {}, 1_800_000_000.0
    seq = [fwd.refresh_calendar(st, t0 + dt_, fetch=fetch, write=written.append) for dt_ in (0, 60, 6 * 3600 + 1, 7 * 3600 + 2)]
    fwd.env_value = lambda name: ''
    no_token = fwd.refresh_calendar({}, t0, fetch=fetch, write=written.append)
    print('calendar feed: format ok %s; refused %s; refreshes %s (fetches %d); without token %s; writes %d' % (
        feed_ok, refused, seq, calls['n'], no_token, len(written)))
    ok &= feed_ok and len(refused) == 2 and seq == [True, False, False, True] and calls['n'] == 3 and \
        not no_token and written == [feed, feed]
    # QMT's process watch (11-05 23:29: QMT closed, first push 5 minutes later)
    pushed[:] = []
    st, t = {}, 1_800_000_000.0
    seq = [True, False, False, False, False, None, True, True]
    for i, running in enumerate(seq):
        fwd.watch_qmt_process(st, 'http://b', t + 10 * i, running, manual_stop=False)
    titles2 = [p.split('/')[1] for p in pushed]
    st2 = {}
    fwd.watch_qmt_process(st2, 'http://b', t, False, manual_stop=True)
    fwd.watch_qmt_process(st2, 'http://b', t + 10, False, manual_stop=True)
    print('QMT process watch: pushes %s; with a manual stop marker: %s' % (titles2, '暂停' in pushed[-1]))
    ok &= titles2 == ['⛔ QMT已关闭', '🔑 QMT已重新打开，请登录'] and '暂停' in pushed[-1]
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
