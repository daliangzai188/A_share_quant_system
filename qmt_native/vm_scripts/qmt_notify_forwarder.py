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

Usage: py -3.11 qmt_notify_forwarder.py [--logs GLOB] [--mode live] [--from-start] [--once]
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import time
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
CHECKS = Path(__file__).resolve().with_name('qmt_external_checks.py')
CHECKS_DIR = BASE / 'external_checks'
KEEP_IDS = 5000
BJ = dt.timezone(dt.timedelta(hours=8))


def log(text: str) -> None:
    line = '%s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), text)
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')
    except OSError:
        pass


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
        pass
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


def scan(patterns: list[str], mode: str, state: dict, base: str, start_at_end: bool, pending: dict) -> int:
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
        collect_exports(chunk[:end], mode, pending)
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
    args = parser.parse_args()
    args.logs = args.logs or [r'C:\QMT\GJQMT\userdata\log\XtClient_FormulaOutput_*.log']
    state = load_state()
    first = not state.get('offsets')
    log('notification forwarder started: mode=%s logs=%s bark=%s' % (
        args.mode, args.logs, 'configured' if bark_base() else 'MISSING'))
    beat = 0.0
    pending, runner = {}, {}
    while True:
        base = bark_base()
        scan(args.logs, args.mode, state, base, start_at_end=first and not args.from_start, pending=pending)
        first = False
        save_state(state)
        if not args.once:
            maybe_run_checks(args.mode, runner)
        if time.time() - beat > 600:
            beat = time.time()
            log('alive: %d log files watched' % len(state.get('offsets', {})))
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == '__main__':
    sys.exit(main())
