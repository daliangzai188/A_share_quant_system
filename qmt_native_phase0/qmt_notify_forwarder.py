# -*- coding: utf-8 -*-
"""Forward A_System phone notifications from QMT's log to Bark (and rebuild exported files).

From 2026-10-23 QMT's built-in Python may not use the network. The QMT-native A_System
turns every Bark request of the production notifier into one marked line in QMT's log:

    [A_SYSTEM_NOTIFY live] <id> /<title>/<body>?<query>      (URL-encoded, ASCII only)

This program runs OUTSIDE QMT (normal Windows Python, network allowed), reads new lines of
QMT's log files, and sends each line of the live copy once to the real Bark address from
C:\\A_System\\.env. It never talks to QMT and cannot place or cancel orders.

QMT also writes files a program outside QMT needs as marked chunks,
    [A_SYSTEM_EXPORT live] <path> <id> <i>/<n> <base64 zlib chunk>
which are rebuilt into %LOCALAPPDATA%\\A_System\\qmt_native\\qmt_export\\. Once such an export of the live copy
exists, the checks that cannot run inside QMT (qmt_external_checks.py: factor health and the
trade-by-trade alignment) are started once per trading day after 18:40 Beijing time.

QMT writes strategy output to userdata\\log\\XtClient_FormulaOutput_YYYYMMDD.log and keeps the
file open: the size and time shown in the directory lag behind, so the real length is taken
from an open handle.

QMT's built-in Python cannot see future trading days nor reach Tushare, so every 6 hours this
program writes the SSE trade calendar (Tushare, TUSHARE_TOKEN from .env) into the registry key of
the trading switch (value TradeCalendar); the strategy merges the future days.

QMT 2.1.33 stops every Python strategy while its own process holds a connection outside its
whitelist; such a stop (in QMT's own log XtClient_YYYYMMDD.log) is pushed at once with what to do.

The live copy's state (11-08: no longer in the broker's key-value store, whose calls froze QMT when
the trade server dropped) comes as [A_SYSTEM_STATE live] lines; qn_state_sink.StateSink (same
folder) stores it in the registry key HKCU\\Software\\A_System\\QmtNative\\StateLive and a disk copy
under %LOCALAPPDATA%\\A_System\\qmt_native\\state\\, where the strategy reads it back when it starts.

QMT's process is checked every 10 s: when it is gone, "⛔ QMT已关闭" is pushed at once, and
"🔑 QMT已重新打开，请登录" when it is back.

The live copy prints a status line every 20 s. When none arrives for 10 minutes (QMT's Python
frozen, the strategy stopped, or QMT closed) this program pushes an alarm itself, repeats it
hourly (not 23:00-07:00) and reports the recovery: every other push comes out of QMT, so a frozen
QMT is otherwise silent (10-24 18:04 to 10-27 00:05 passed without a single push). A stop by the
user (stop_windows.py leaves C:\\A_System\\.manual_stop.json until start_windows.py) raises no alarm.

Usage: py -3.11 qmt_notify_forwarder.py [--logs GLOB] [--mode live] [--from-start] [--once]
                                        [--strategy A_SYSTEM_QMT_INNER] [--stall-minutes 10]
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(os.environ.get('A_SYSTEM_ROOT', r'C:\A_System'))
BASE = Path(os.environ.get('LOCALAPPDATA', str(ROOT / 'runtime'))) / 'A_System' / 'qmt_native'
STATE = BASE / 'qmt_notify_forwarder.json'
LOG = ROOT / 'logs' / 'qmt_notify_forwarder.log'
MARK = re.compile(rb'\[A_SYSTEM_NOTIFY (live|shadow)\] ([0-9A-Za-z]+-[0-9]+) (/[\x21-\x7e]+)')
EXPORT = re.compile(rb'\[A_SYSTEM_EXPORT (live|shadow)\] ([\x21-\x7e]+) ([0-9A-Za-z]+-[0-9a-f]+) (\d+)/(\d+) ([A-Za-z0-9+/=]*)')
EXPORT_DIR = BASE / 'qmt_export'
STATE_DIR = BASE / 'state'
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qn_state_sink  # noqa: E402
CHECKS = Path(__file__).resolve().with_name('qmt_external_checks.py')
CHECKS_DIR = BASE / 'external_checks'
# 2026-11-14：实盘成交汇总在 C:\A_System 里再放一份，随两端双向同步实时到 Mac（AppData 不在同步文件夹里）
MIRROR_DIR = ROOT / 'reports' / 'qmt_native' / 'qmt_export'
KEEP_IDS = 5000
BJ = dt.timezone(dt.timedelta(hours=8))
REMIND_SECONDS = 3600
QUIET_HOURS = ('2300', '0700')  # no repeated stall reminders at night (the first alarm still goes out)
STALL_QUERY = '?' + urllib.parse.urlencode({'group': 'A股实盘', 'level': 'timeSensitive', 'sound': 'alarm'})


def log(text: str) -> None:
    line = '%s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), text)
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')
    except OSError:
        pass  # 可以忽略：转发程序自己的日志写不了时不能影响转发


CAL_KEY = r'Software\A_System\QmtNative'
CAL_VALUE = 'TradeCalendar'
CAL_REFRESH_SECONDS = 6 * 3600
CAL_RETRY_SECONDS = 3600


def env_value(name: str) -> str:
    env = ROOT / '.env'
    if env.is_file():
        for raw in env.read_text(encoding='utf-8-sig', errors='replace').splitlines():
            key, sep, val = raw.strip().partition('=')
            if sep and key.strip() == name:
                return val.strip().strip('"').strip("'")
    return ''


def calendar_feed(rows) -> str:
    """QMT's built-in Python knows no future trading day and may not use the network (11-05), so
    this program hands it the SSE calendar through the registry key of the trading switch:
    "v1|SSE|<first day>|<one 0/1 per consecutive calendar day>|<sha256[:8]>"."""
    days = dict((str(d), bool(o)) for d, o in rows)
    ordered = sorted(days)
    if len(ordered) < 120:
        raise ValueError('calendar too short: %d days' % len(ordered))
    first = dt.datetime.strptime(ordered[0], '%Y%m%d').date()
    bits = []
    for i in range(len(ordered)):
        day = first + dt.timedelta(days=i)
        key = day.strftime('%Y%m%d')
        if key not in days:
            raise ValueError('calendar has a gap at %s' % key)
        if days[key] and day.weekday() >= 5:
            raise ValueError('weekend %s marked open' % key)
        bits.append('1' if days[key] else '0')
    for year in sorted(set(d[:4] for d in ordered)):
        if ordered[0] <= year + '0101' and year + '1231' <= ordered[-1]:
            opens = sum(1 for d in ordered if d.startswith(year) and days[d])
            if not 230 <= opens <= 255:
                raise ValueError('%s has %d trading days' % (year, opens))
    text = ''.join(bits)
    return 'v1|SSE|%s|%s|%s' % (ordered[0], text, hashlib.sha256((ordered[0] + text).encode('ascii')).hexdigest()[:8])


def fetch_calendar(token: str, today: dt.date) -> list:
    import tushare as ts
    frame = ts.pro_api(token).trade_cal(exchange='SSE', start_date=(today - dt.timedelta(days=90)).strftime('%Y%m%d'),
                                         end_date=(today + dt.timedelta(days=400)).strftime('%Y%m%d'))
    return [(str(d), str(o).strip() in ('1', '1.0')) for d, o in zip(frame['cal_date'], frame['is_open'])]


def write_calendar_registry(value: str) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CAL_KEY) as key:
        winreg.SetValueEx(key, CAL_VALUE, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, CAL_VALUE + 'At', 0, winreg.REG_SZ, dt.datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S'))


def refresh_calendar(state: dict, now: float, fetch=fetch_calendar, write=write_calendar_registry) -> bool:
    """Every 6 hours (an hour after a failure) write the SSE trade calendar for QMT."""
    if now < state.get('calendar_next', 0):
        return False
    state['calendar_next'] = now + CAL_RETRY_SECONDS
    token = env_value('TUSHARE_TOKEN') or os.environ.get('TUSHARE_TOKEN', '')
    if not token:
        log('trade calendar: TUSHARE_TOKEN missing in %s; QMT calendar not refreshed' % (ROOT / '.env'))
        return False
    try:
        rows = fetch(token, dt.datetime.fromtimestamp(now, BJ).date())
        value = calendar_feed(rows)
        write(value)
    except Exception as exc:
        log('trade calendar refresh failed (retry in 1 h): %s: %s' % (type(exc).__name__, str(exc)[:200]))
        return False
    state['calendar_next'] = now + CAL_REFRESH_SECONDS
    last_open = max(d for d, o in rows if o)
    log('trade calendar written for QMT: %d days from %s, open days through %s' % (len(rows), min(d for d, _ in rows), last_open))
    return True


QMT_IMAGE = 'XtItClient.exe'
QMT_CHECK_SECONDS = 10


def qmt_running():
    """True/False whether QMT's process exists (tasklist sees elevated processes too), None if unknown."""
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ' + QMT_IMAGE, '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    except Exception:
        return None
    return QMT_IMAGE.lower() in out.lower()


def watch_qmt_process(state: dict, base: str, now: float, running, manual_stop: bool) -> None:
    """11-05 23:29: QMT was closed and the first push came 5 minutes later. Push at once when QMT's
    process is gone (two checks in a row, 10 s apart) and again when it is back."""
    if running is None:
        return
    if running:
        state['qmt_absent'] = 0
        closed_at = state.pop('qmt_closed_at', None)
        if closed_at is not None and base:
            body = ('QMT在 %s（北京时间）重新打开，之前关闭了 %d 分钟。请在虚拟机里登录（新版要先点“用户账户控制”的“是”），'
                    '登录后A_SYSTEM会自动启动；已经登录就不用管。' % (bj_clock(now), max(0, now - closed_at) / 60))
            if send(base, stall_path('🔑 QMT已重新打开，请登录', body)):
                log('QMT reopened push sent')
        return
    state['qmt_absent'] = state.get('qmt_absent', 0) + 1
    if state['qmt_absent'] < 2 or 'qmt_closed_at' in state:
        return
    state['qmt_closed_at'] = now
    if not base:
        log('BARK_URL missing; QMT closed push not sent')
        return
    body = ('QMT交易端在 %s（北京时间）关闭或退出，A_System不在运行：不交易，也不执行止损、止盈、到期卖出。守护程序会自动重新打开QMT：'
            '请在虚拟机里点“用户账户控制”的“是”，再输入密码登录；登录后A_SYSTEM自动启动。' % bj_clock(now))
    if manual_stop:
        body += '（之前执行过 stop_windows.py，交易处于暂停。）'
    if send(base, stall_path('⛔ QMT已关闭', body)):
        log('QMT closed push sent')


def bark_base() -> str:
    value = os.environ.get('BARK_URL', '')
    env = ROOT / '.env'
    if not value and env.is_file():
        for raw in env.read_text(encoding='utf-8-sig', errors='replace').splitlines():
            key, sep, val = raw.strip().partition('=')
            if sep and key.strip() == 'BARK_URL':
                value = val.strip().strip('"').strip("'")
    return value.rstrip('/')


def load_state() -> dict:
    try:
        state = json.loads(STATE.read_text(encoding='utf-8'))
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass  # 可以忽略：状态文件读不了按初始状态重新开始
    return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, STATE)


def send(base: str, path: str) -> bool:
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, method='GET'), timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True
                log('Bark returned HTTP %s' % resp.status)
        except Exception as exc:  # network trouble: retry with a short backoff
            log('Bark send failed (attempt %d): %s' % (attempt + 1, exc))
        time.sleep(min(30, 2 ** attempt))
    return False


def store_export(rel: str, ident: str, chunks: dict, total: int) -> None:
    data = zlib.decompress(base64.b64decode(''.join(chunks[i] for i in range(1, total + 1))))
    target = EXPORT_DIR / Path(rel).name
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, target)
    meta = {'path': rel, 'id': ident, 'bytes': len(data), 'received_at': dt.datetime.now(BJ).isoformat()}
    (EXPORT_DIR / (Path(rel).name + '.json')).write_text(json.dumps(meta), encoding='utf-8')
    try:  # 同步用的副本写不成不影响原来的导出（检查只读 AppData 那份）
        MIRROR_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MIRROR_DIR / (target.name + '.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, MIRROR_DIR / target.name)
        (MIRROR_DIR / (target.name + '.json')).write_text(json.dumps(meta), encoding='utf-8')
    except OSError as exc:
        log('export mirror failed: %s' % exc)


def collect_exports(chunk: bytes, mode: str, pending: dict) -> None:
    for match in EXPORT.finditer(chunk):
        line_mode, rel, ident, index, total, part = (g.decode('ascii') for g in match.groups())
        if line_mode != mode:
            continue
        item = pending.setdefault((rel, ident), {})
        item[int(index)] = part
        if len(item) == int(total):
            try:
                store_export(rel, ident, item, int(total))
                log('export rebuilt: %s (%s)' % (rel, ident))
            except Exception as exc:
                log('export %s %s unreadable: %s' % (rel, ident, exc))
            del pending[(rel, ident)]


def open_dates() -> list[str]:
    try:
        lines = (ROOT / 'data' / 'raw' / 'trade_calendar.csv').read_text(encoding='utf-8-sig').splitlines()
    except OSError:
        return []
    head = lines[0].split(',')
    i_date, i_open = head.index('cal_date'), head.index('is_open')
    return sorted(r.split(',')[i_date] for r in lines[1:] if r.split(',')[i_open].strip() in ('1', '1.0'))


def maybe_run_checks(mode: str, runner: dict) -> None:
    """Start the outside-QMT checks once per trading day after 18:40 (retry every 30 min until 23:00)."""
    proc = runner.get('proc')
    if proc is not None:
        if proc.poll() is None:
            return
        log('external checks finished with exit code %s' % proc.returncode)
        runner['proc'] = None
    if mode != 'live' or not CHECKS.is_file() or not (EXPORT_DIR / 'trade_completion_summary.csv.json').is_file():
        return
    now = dt.datetime.now(BJ)
    day = now.strftime('%Y%m%d')
    if day not in open_dates() or not ('1840' <= now.strftime('%H%M') < '2300'):
        return
    if (CHECKS_DIR / ('done_%s.json' % day)).is_file() or time.time() - runner.get('started', 0) < 1800:
        return
    CHECKS_DIR.mkdir(parents=True, exist_ok=True)
    out = open(CHECKS_DIR / ('run_%s.log' % day), 'ab')
    runner['proc'] = subprocess.Popen([sys.executable, str(CHECKS), '--signal-date', day], cwd=str(ROOT),
                                      stdout=out, stderr=subprocess.STDOUT)
    runner['started'] = time.time()
    log('external checks started for %s' % day)


def alive_pattern(strategy: str) -> re.Pattern:
    """A status line of the live copy: printed by QMT's strategy thread every 20 s."""
    return re.compile(re.escape(strategy.encode('ascii')) + rb'[^\n]*\[A_SYSTEM\] \d\d:\d\d:\d\d status: ')


def bj_clock(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, BJ).strftime('%m-%d %H:%M')


CLIENT_LINE = re.compile(rb'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \[\w+\] \[0x[0-9a-fA-F]+\] \[(0x[0-9a-fA-F]+)\] (.*)$')
ILLEGAL = re.compile(rb'\[monitor\] illegal ip: (\S+)')
REMOTE = re.compile(rb'\[monitor\]TCP - PID:\d+ remoteIP:\[([^\]]+)\]')


def watch_client_stops(patterns: list[str], strategy: str, state: dict, base: str, start_at_end: bool) -> int:
    """QMT 2.1.33 watches the TCP connections of its own process every 3 s and stops every Python
    strategy while one goes to an address outside its whitelist (11-04 22:48: Windows fetched a
    certificate from i.pki.goog inside QMT, the starting A_SYSTEM was stopped 2 s later). The stop
    and its reason are in QMT's own log (XtClient_YYYYMMDD.log): push at once with what to do,
    instead of only the stall alarm 10 minutes later. A stop clicked by the user comes from the
    UI thread, not from the monitor thread, and is not reported."""
    offsets = state.setdefault('client_offsets', {})
    watch = state.setdefault('client_watch', {})
    threads = set(watch.get('monitor_threads', []))
    target = b'fromula:' + strategy.encode('ascii') + b','
    pushed = 0
    files = sorted(set(f for p in patterns for f in glob.glob(p)))[-3:]
    for name in files:
        try:
            with open(name, 'rb') as handle:
                size = handle.seek(0, os.SEEK_END)
                if name not in offsets:
                    offsets[name] = size if start_at_end else 0
                start = offsets[name] if offsets[name] <= size else 0
                if start == size:
                    continue
                handle.seek(start)
                chunk = handle.read(size - start)
        except OSError:
            continue
        end = chunk.rfind(b'\n') + 1
        for raw in chunk[:end].splitlines():
            match = CLIENT_LINE.match(raw.rstrip(b'\r'))
            if not match:
                continue
            stamp, thread, rest = match.group(1).decode('ascii'), match.group(2).decode('ascii'), match.group(3)
            if b'[monitor]' in rest:
                threads.add(thread)
                ip = ILLEGAL.search(rest) or REMOTE.search(rest)  # the flagged connection is logged every 3 s
                if ip:
                    watch['illegal_ip'] = ip.group(1).decode('ascii', 'replace')
                    watch['illegal_at'] = stamp
            elif b'[TradeStrategy]try stop' in rest and target in rest and thread in threads:
                watch['stop'] = {'thread': thread, 'at': stamp}
            elif b'stop success' in rest and watch.get('stop', {}).get('thread') == thread:
                stop = watch.pop('stop')
                if watch.get('pushed_at') == stop['at']:
                    continue
                body = ('QMT新版网络监控在%s（北京时间）以“非法IP %s”停止了A_SYSTEM：是QMT自己的连接被误判，不是stop，'
                        '交易开关不变。处理：在QMT“模型交易”的A_SYSTEM行点 ▶；若2秒内又被停，等2-3分钟那条连接断开后再点。'
                        '启动后自动补跑。' % (stop['at'][5:], watch.get('illegal_ip', '?')))
                if not base:
                    log('BARK_URL missing; QMT monitor stop at %s not pushed' % stop['at'])
                elif send(base, stall_path('⛔ QMT误判非法IP，停止了A_SYSTEM', body)):
                    log('QMT monitor stop pushed: %s illegal ip %s' % (stop['at'], watch.get('illegal_ip', '?')))
                    watch['pushed_at'] = stop['at']
                    pushed += 1
        offsets[name] = start + end
    watch['monitor_threads'] = sorted(threads)[-8:]
    for name in [n for n in offsets if n not in files]:
        del offsets[name]
    return pushed


def stall_path(title: str, body: str) -> str:
    return '/%s/%s%s' % (urllib.parse.quote(title), urllib.parse.quote(body), STALL_QUERY)


def check_stall(state: dict, base: str, now: float, stall_seconds: float, stopped_at: float | None = None) -> None:
    """Push an alarm when the live copy has printed no status line for stall_seconds.

    stopped_at: time of C:\\A_System\\.manual_stop.json (written by stop_windows.py, removed by
    start_windows.py). A stop by the user is no stall; once status lines come again well after
    it (strategy restarted some other way), alarms apply again."""
    alive = state.setdefault('alive_at', now)  # first start: count from now
    alarm = state.get('stall_alarm_at')
    silent = now - alive
    if stopped_at is not None and stopped_at >= alive - 60:
        if state.get('stall_quiet') != stopped_at:
            state['stall_quiet'] = stopped_at
            log('strategy stopped by the user at %s (Beijing): no stall alarm until it runs again' % bj_clock(stopped_at))
        return
    if silent < stall_seconds:
        if alarm and base:
            body = 'QMT内A_SYSTEM在%s（北京时间）后停止输出，%s恢复，期间%d分钟。' % (
                bj_clock(state.get('stall_since', alive)), bj_clock(alive), (alive - state.get('stall_since', alive)) / 60)
            if send(base, stall_path('✅ A_System策略已恢复输出', body)):
                log('stall recovery pushed')
                state.pop('stall_alarm_at', None)
                state.pop('stall_count', None)
        return
    if alarm and (now - alarm < REMIND_SECONDS or not QUIET_HOURS[1] <= dt.datetime.fromtimestamp(now, BJ).strftime('%H%M') < QUIET_HOURS[0]):
        return
    if not base:
        log('BARK_URL missing; stall alarm not sent')
        return
    count = state.get('stall_count', 0) + 1
    body = ('QMT内A_SYSTEM已%d分钟没有任何输出（最后一次：%s 北京时间）。QMT内置Python可能卡死、策略被停止或QMT已关闭，'
            '下单和推送都已中断。请检查QMT：停止再启动策略无效时，需要退出并重新登录QMT。' % (silent / 60, bj_clock(alive)))
    if count > 1:
        body += '（第%d次提醒）' % count
    if send(base, stall_path('🛑 A_System策略已停止输出', body)):
        log('stall alarm pushed: no status line for %d s (#%d)' % (silent, count))
        state.update(stall_alarm_at=now, stall_count=count, stall_since=alive)


def make_state_sink(mode: str):
    try:
        registry = qn_state_sink.WinRegistry(mode)
    except ImportError:  # not Windows (tests): a JSON file stands in for the registry key
        BASE.mkdir(parents=True, exist_ok=True)
        registry = qn_state_sink.JsonRegistry(str(BASE / ('state_registry_%s.json' % mode)))
    sink = qn_state_sink.StateSink(registry, mode=mode, disk_dir=str(STATE_DIR), log=log)
    note = sink.restore_from_disk()
    if note:
        log(note)
    return sink


def scan(patterns: list[str], mode: str, state: dict, base: str, start_at_end: bool, pending: dict,
         alive: re.Pattern | None = None, sink=None) -> int:
    offsets = state.setdefault('offsets', {})
    sent = state.setdefault('sent', [])
    seen = set(sent)
    forwarded = 0
    files = sorted(set(f for p in patterns for f in glob.glob(p)))[-20:]  # dated names sort by day
    for name in files:
        try:
            with open(name, 'rb') as handle:
                size = handle.seek(0, os.SEEK_END)  # directory metadata of an open file lags
                if name not in offsets:
                    offsets[name] = size if start_at_end else 0
                start = offsets[name] if offsets[name] <= size else 0  # file rotated/truncated
                if start == size:
                    continue
                handle.seek(start)
                chunk = handle.read(size - start)
        except OSError:
            continue
        end = chunk.rfind(b'\n') + 1  # only complete lines; the rest is read next time
        if alive is not None and alive.search(chunk[:end]):
            state['alive_at'] = time.time()
        collect_exports(chunk[:end], mode, pending)
        if sink is not None:
            try:
                for note in sink.feed(chunk[:end]):
                    log(note)
            except Exception as exc:  # never let state lines stop the notifications
                log('state lines in %s not processed: %s: %s' % (os.path.basename(name), type(exc).__name__, exc))
        for match in MARK.finditer(chunk[:end]):
            line_mode, ident, path = (g.decode('ascii') for g in match.groups())
            if line_mode != mode or ident in seen:
                continue
            if not base:
                log('BARK_URL missing in %s; notification %s not sent' % (ROOT / '.env', ident))
                return forwarded
            if not send(base, path):
                return forwarded  # keep the offset: this line is retried next scan
            seen.add(ident)
            sent.append(ident)
            del sent[:-KEEP_IDS]
            forwarded += 1
            log('forwarded %s %s' % (ident, path.split('?', 1)[0][:120]))
        offsets[name] = start + end
    for name in [n for n in offsets if n not in files]:
        del offsets[name]
    return forwarded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--logs', action='append', help='glob of QMT log files (repeatable)')
    parser.add_argument('--mode', default='live', choices=('live', 'shadow'))
    parser.add_argument('--from-start', action='store_true', help='also forward lines already in the logs')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=3.0)
    parser.add_argument('--strategy', default='A_SYSTEM_QMT_INNER', help='QMT file name of the live copy')
    parser.add_argument('--stall-minutes', type=float, default=10.0, help='alarm after this long without output; 0 = off')
    parser.add_argument('--client-logs', action='append', help="glob of QMT's own logs (strategy stops by its network monitor)")
    args = parser.parse_args()
    watch = args.mode == 'live' and args.stall_minutes > 0
    alive = alive_pattern(args.strategy) if watch else None
    args.logs = args.logs or [r'C:\QMT\GJQMT\userdata\log\XtClient_FormulaOutput_*.log']
    args.client_logs = args.client_logs or [r'C:\QMT\GJQMT\userdata\log\XtClient_2*.log']
    state = load_state()
    first = not state.get('offsets')
    log('notification forwarder started: mode=%s logs=%s bark=%s' % (
        args.mode, args.logs, 'configured' if bark_base() else 'MISSING'))
    beat = 0.0
    pending, runner = {}, {}
    sink = make_state_sink(args.mode)
    while True:
        base = bark_base()
        scan(args.logs, args.mode, state, base, start_at_end=first and not args.from_start, pending=pending,
             alive=alive, sink=sink)
        if watch and time.time() - state.get('qmt_checked', 0) >= QMT_CHECK_SECONDS:
            state['qmt_checked'] = time.time()
            watch_qmt_process(state, base, time.time(), qmt_running(), (ROOT / '.manual_stop.json').is_file())
        if watch:
            refresh_calendar(state, time.time())
            watch_client_stops(args.client_logs, args.strategy, state, base,
                               start_at_end='client_offsets' not in state and not args.from_start)
        first = False
        if watch:
            marker = ROOT / '.manual_stop.json'
            check_stall(state, base, time.time(), args.stall_minutes * 60,
                        marker.stat().st_mtime if marker.is_file() else None)
        save_state(state)
        if not args.once:
            maybe_run_checks(args.mode, runner)
        if time.time() - beat > 600:
            beat = time.time()
            log('alive: %d log files watched%s' % (len(state.get('offsets', {})), (
                '; last strategy status %d s ago' % (time.time() - state['alive_at'])) if 'alive_at' in state else ''))
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == '__main__':
    sys.exit(main())
