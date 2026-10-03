# -*- coding: utf-8 -*-
"""只读排查 QMT 客户端定时重启的触发来源（在 Windows 交易机上运行一次）。

背景：2026-09-15~09-28 期间，QMT 客户端每天约 08:51、20:52 自行重启；多数情况
自动登录并运行内置模型，1 分钟内恢复；自动登录遇到验证码时停在登录页，内置
桥接一直断到人工登录（两次跨过开盘）。9/28 10:35 Windows 重启后未再出现。

本脚本只读取，不连接券商、不调用 QMT 接口、不修改任何文件、进程或设置：
1. 当前 QMT 相关进程：启动时间、可见窗口标题、客户端文件版本。
2. Windows 事件日志：应用崩溃/无响应（1000/1001/1002）中与 QMT 相关的记录；
   系统关机/重启（41/1074/6005/6006/6008）。
3. QMT 安装目录里近期变化的程序文件（判断是否发生过自动更新）。
4. QMT 日志里 08:40~09:05、20:40~21:05 的重启/登录/更新/退出相关行，以及
   全时段的强关键词行（重启、崩溃、升级、验证码、定时、守护）。
5. QMT 配置文件里与重启、定时、自动运行、自动登录相关的设置项，以及近期改动过的配置文件。
6. 计划任务、开机启动项里与 QMT 相关的条目。
7. 本项目 daemon 日志里的失联时间线，并把以上证据按每次失联前后 5 分钟对齐。

隐私：跳过含密码、口令、Token、姓名、证件、手机号等字样的行；资金账号原文替换为
末两位；7 位以上数字串（日期时间格式除外）只保留末两位。

输出：reports/runtime/qmt_client_restart_diagnosis.json（Syncthing 同步到 Mac，未纳入 git）。

运行（Windows）：
    cd C:\\A_System
    py -3.11 scripts\\diagnose_qmt_client_restarts.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "reports" / "runtime" / "qmt_client_restart_diagnosis.json"
DAEMON_LOG = ROOT / "logs" / "trading_daemon.log"
ENV_REPORT = ROOT / "reports" / "qmt_inner_migration" / "windows_environment.json"
DEFAULT_SINCE = dt.date(2026, 9, 14)
# 2026-09-15~09-28 的客户端重启都落在这两个时间窗内。
SCAN_WINDOWS = ((dt.time(8, 40), dt.time(9, 5)), (dt.time(20, 40), dt.time(21, 5)))
CORRELATE_MINUTES = 5

EVENT_KEYWORDS = re.compile(
    r"重启|重新启动|restart|reboot|退出|exit|quit|关闭|close|登录|登陆|login|logon|验证码|captcha|"
    r"断开|断线|disconnect|reconnect|重连|升级|更新|update|崩溃|crash|dump|exception|异常|"
    r"定时|timer|schedule|shutdown|启动|startup|kill|守护|watchdog|内存|memory",
    re.I,
)
STRONG_KEYWORDS = re.compile(
    r"重启|重新启动|restart|reboot|崩溃|crash|dump|升级|update|验证码|captcha|定时|schedule|watchdog|守护",
    re.I,
)
CONFIG_KEYWORDS = re.compile(
    r"restart|reboot|重启|auto_?run|auto_?start|auto_?login|auto_?logon|自动|定时|timer|schedule|"
    r"exit|退出|quit|关闭|close|update|升级|更新|watchdog|守护",
    re.I,
)
SENSITIVE = re.compile(
    r"pass|pwd|密码|口令|token|secret|cookie|姓名|户名|账户名|客户名|证件|身份证|手机|phone|mobile|email|邮箱",
    re.I,
)
LOG_SUFFIXES = {".log", ".txt"}
CONFIG_SUFFIXES = {".ini", ".xml", ".cfg", ".conf", ".json", ".properties"}
PROGRAM_SUFFIXES = {".exe", ".dll", ".pyd"}
PROCESS_NAMES = (
    "xtitclient.exe", "xtminiqmt.exe", "xtupdate.exe", "crashui.exe",
    "minibroker.exe", "miniquote.exe", "brokerproxy.exe",
)

_DATE = re.compile(r"(20\d{2})[-/.年]?(0[1-9]|1[0-2])[-/.月]?(0[1-9]|[12]\d|3[01])")
_TIME = re.compile(r"(?<!\d)([01]\d|2[0-3]):([0-5]\d):([0-5]\d)")
_LONG_DIGITS = re.compile(r"\d{7,}")


# ---------------------------------------------------------------- 纯函数（可离线测试）

def mask(text: str, secrets: Iterable[str] = ()) -> str:
    """资金账号原文只留末两位；其他7位以上数字串（日期时间除外）同样处理。"""
    for secret in secrets:
        secret = str(secret or "")
        if len(secret) >= 4:
            text = text.replace(secret, "***" + secret[-2:])

    def _replace(match: re.Match[str]) -> str:
        digits = match.group(0)
        if digits.startswith("20") and len(digits) in (8, 12, 14, 17) and _DATE.fullmatch(digits[:8]):
            return digits
        return "***" + digits[-2:]

    return _LONG_DIGITS.sub(_replace, text)


def decode_line(raw: bytes) -> str:
    """QMT 日志 UTF-8 与 GBK 混用，逐行判定。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def date_from_text(text: str) -> dt.date | None:
    match = _DATE.search(text)
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_line_datetime(line: str, current_date: dt.date | None) -> tuple[dt.datetime | None, dt.date | None]:
    """返回（本行时间，更新后的当前日期）。行内无日期时沿用上一行或文件名的日期。"""
    time_match = _TIME.search(line)
    head = line[: time_match.start()] if time_match else line[:40]
    line_date = date_from_text(head)
    if line_date is not None:
        current_date = line_date
    if time_match is None or current_date is None:
        return None, current_date
    stamp = dt.datetime.combine(
        current_date,
        dt.time(int(time_match.group(1)), int(time_match.group(2)), int(time_match.group(3))),
    )
    return stamp, current_date


def in_scan_window(stamp: dt.datetime) -> bool:
    return any(start <= stamp.time() <= end for start, end in SCAN_WINDOWS)


def scan_log_file(
    path: Path,
    *,
    since: dt.date,
    until: dt.date,
    secrets: Iterable[str] = (),
    per_window_cap: int = 40,
    strong_cap: int = 200,
) -> dict[str, Any]:
    secrets = tuple(secrets)
    current = date_from_text(path.name)
    window_lines: list[dict[str, str]] = []
    window_counts: dict[str, int] = {}
    strong_lines: list[dict[str, str]] = []
    first_line = last_line = ""
    total = 0
    with path.open("rb") as stream:
        for raw in stream:
            line = decode_line(raw).strip()
            if not line:
                continue
            total += 1
            if not first_line:
                first_line = line
            last_line = line
            stamp, current = parse_line_datetime(line, current)
            if SENSITIVE.search(line):
                continue
            if stamp is not None and not (since <= stamp.date() <= until):
                continue
            text = mask(line[:400], secrets)
            stamp_text = stamp.strftime("%Y-%m-%d %H:%M:%S") if stamp else ""
            if stamp is not None and in_scan_window(stamp) and EVENT_KEYWORDS.search(line):
                key = f"{stamp:%Y-%m-%d}{'上午' if stamp.hour < 12 else '晚上'}"
                if window_counts.get(key, 0) < per_window_cap:
                    window_counts[key] = window_counts.get(key, 0) + 1
                    window_lines.append({"time": stamp_text, "line": text})
            elif STRONG_KEYWORDS.search(line) and len(strong_lines) < strong_cap:
                strong_lines.append({"time": stamp_text, "line": text})
    return {
        "lines_total": total,
        "first_line": mask(first_line[:300], secrets) if not SENSITIVE.search(first_line) else "（含敏感字样，略）",
        "last_line": mask(last_line[:300], secrets) if not SENSITIVE.search(last_line) else "（含敏感字样，略）",
        "window_lines": window_lines,
        "strong_lines": strong_lines,
    }


def scan_config_text(text: str, secrets: Iterable[str] = (), cap: int = 80) -> list[str]:
    hits: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or SENSITIVE.search(line) or not CONFIG_KEYWORDS.search(line):
            continue
        hits.append(mask(line[:300], secrets))
        if len(hits) >= cap:
            break
    return hits


def extract_outage_episodes(lines: Iterable[str], since: dt.date, gap_minutes: int = 10) -> list[dict[str, str]]:
    """从 daemon 日志取内置桥接失联时间线（心跳过期、模型实例变化）。"""
    stamp_re = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \|")
    records: list[tuple[dt.datetime, str]] = []
    for line in lines:
        lowered = line.lower()
        if "heartbeat is stale" not in lowered and "bridge restarted" not in lowered:
            continue
        match = stamp_re.match(line)
        if not match:
            continue
        stamp = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        if stamp.date() >= since:
            records.append((stamp, "模型实例变化" if "bridge restarted" in lowered else "心跳过期"))
    episodes: list[dict[str, Any]] = []
    for stamp, kind in sorted(records):
        if episodes and (stamp - episodes[-1]["_end"]).total_seconds() <= gap_minutes * 60:
            episodes[-1]["_end"] = stamp
            episodes[-1]["kinds"].add(kind)
            continue
        episodes.append({"_start": stamp, "_end": stamp, "kinds": {kind}})
    return [
        {
            "start": item["_start"].strftime("%Y-%m-%d %H:%M:%S"),
            "last_seen": item["_end"].strftime("%Y-%m-%d %H:%M:%S"),
            "minutes": round((item["_end"] - item["_start"]).total_seconds() / 60.0, 1),
            "kinds": sorted(item["kinds"]),
        }
        for item in episodes
    ]


def correlate(episodes: list[dict[str, Any]], evidence: list[dict[str, str]],
              minutes: int = CORRELATE_MINUTES, cap: int = 30) -> list[dict[str, Any]]:
    """每次失联开始前后 minutes 分钟内的全部证据，按时间排序。"""
    parsed = []
    for item in evidence:
        try:
            parsed.append((dt.datetime.strptime(item["time"], "%Y-%m-%d %H:%M:%S"), item))
        except (KeyError, ValueError):
            continue
    parsed.sort(key=lambda pair: pair[0])
    span = dt.timedelta(minutes=minutes)
    result = []
    for episode in episodes:
        start = dt.datetime.strptime(episode["start"], "%Y-%m-%d %H:%M:%S")
        near = [item for stamp, item in parsed if start - span <= stamp <= start + span]
        result.append({**episode, "evidence_count": len(near), "evidence": near[:cap]})
    return result


# ---------------------------------------------------------------- Windows 只读采集

def _powershell(script: str, timeout: int = 120) -> str:
    """单项查询失败或超时只让该项为空，不中断整份报告。"""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + script],
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"PowerShell查询失败（该项留空）：{exc}")
        return ""
    return completed.stdout.strip()


def _safe(label: str, func: Any, default: Any) -> Any:
    try:
        return func()
    except Exception as exc:  # 只读排查：单项失败如实记录，其余照常输出
        print(f"{label}失败（该项留空）：{exc}")
        return {"error": f"{type(exc).__name__}: {exc}"} if default == {} else default


def _json_or_empty(text: str) -> Any:
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {"unparsed": text[:2000]}


def _as_list(value: Any) -> list[Any]:
    if value in (None, "", {}):
        return []
    return value if isinstance(value, list) else [value]


def _account_secrets() -> list[str]:
    try:
        from qmt_inner.protocol import load_settings

        return [str(load_settings().get("account_id", ""))]
    except Exception:
        return []


def find_qmt_roots() -> list[Path]:
    roots: set[Path] = set()
    running = _powershell(
        "Get-Process XtItClient,XtMiniQmt -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty Path"
    )
    for line in running.splitlines():
        path = Path(line.strip())
        if path.suffix.lower() == ".exe":
            roots.add(path.parent.parent)
    if ENV_REPORT.exists():
        try:
            recorded = json.loads(ENV_REPORT.read_text(encoding="utf-8")).get("qmt_executables", {})
            roots.update(Path(directory).parent for directory in recorded)
        except (OSError, json.JSONDecodeError):
            pass
    return sorted(root for root in roots if root.exists())


def _walk_files(root: Path, max_depth: int) -> Iterable[Path]:
    base_depth = len(root.parts)
    for current, dirs, files in os.walk(root):
        if len(Path(current).parts) - base_depth >= max_depth:
            dirs[:] = []
        for name in files:
            yield Path(current) / name


def _mtime(path: Path) -> dt.datetime:
    return dt.datetime.fromtimestamp(path.stat().st_mtime)


def collect_qmt_files(root: Path, since: dt.date, until: dt.date, secrets: list[str]) -> dict[str, Any]:
    since_dt = dt.datetime.combine(since, dt.time())
    logs, configs, programs, changed_configs = [], [], [], []
    config_hits: list[dict[str, Any]] = []
    for path in _walk_files(root, max_depth=6):
        try:
            suffix = path.suffix.lower()
            stat = path.stat()
            modified = dt.datetime.fromtimestamp(stat.st_mtime)
        except OSError:
            continue
        in_log_dir = any("log" in part.lower() for part in path.relative_to(root).parts[:-1])
        if suffix in LOG_SUFFIXES and (in_log_dir or "log" in path.name.lower()):
            if modified >= since_dt and stat.st_size <= 2 * 1024 ** 3:
                logs.append(path)
        elif suffix in CONFIG_SUFFIXES and not in_log_dir and stat.st_size <= 2 * 1024 ** 2:
            configs.append(path)
            if modified >= since_dt:
                changed_configs.append({"path": str(path.relative_to(root)),
                                        "modified": modified.strftime("%Y-%m-%d %H:%M:%S")})
        elif suffix in PROGRAM_SUFFIXES and modified >= since_dt:
            programs.append({"path": str(path.relative_to(root)), "size": stat.st_size,
                             "modified": modified.strftime("%Y-%m-%d %H:%M:%S")})

    for path in sorted(configs)[:3000]:
        try:
            text = decode_line(path.read_bytes())
        except OSError:
            continue
        hits = scan_config_text(text, secrets)
        if hits:
            config_hits.append({"path": str(path.relative_to(root)),
                                "modified": _mtime(path).strftime("%Y-%m-%d %H:%M:%S"), "lines": hits})
        if sum(len(item["lines"]) for item in config_hits) >= 600:
            break

    log_reports = []
    for path in sorted(logs, key=_mtime)[-300:]:
        try:
            report = scan_log_file(path, since=since, until=until, secrets=secrets)
        except OSError as exc:
            report = {"error": str(exc)}
        report.update({"path": str(path.relative_to(root)), "size": path.stat().st_size,
                       "modified": _mtime(path).strftime("%Y-%m-%d %H:%M:%S")})
        log_reports.append(report)

    programs.sort(key=lambda item: item["modified"], reverse=True)
    changed_configs.sort(key=lambda item: item["modified"], reverse=True)
    return {
        "root": str(root),
        "log_files": log_reports,
        "config_hits": config_hits,
        "changed_configs": changed_configs[:200],
        "changed_programs": programs[:200],
    }


_EVENTS_PS = r"""
$since=[datetime]'__SINCE__'
$fmt='yyyy-MM-dd HH:mm:ss'
$app=@(Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000,1001,1002; StartTime=$since} -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -match 'Xt|QMT|minibroker|miniquote|BrokerProxy|Crashui|pythonw' } |
  Select-Object -First 300 @{n='time';e={$_.TimeCreated.ToString($fmt)}}, Id, ProviderName, @{n='message';e={$_.Message}})
$sys=@(Get-WinEvent -FilterHashtable @{LogName='System'; Id=41,1074,6005,6006,6008; StartTime=$since} -ErrorAction SilentlyContinue |
  Select-Object -First 300 @{n='time';e={$_.TimeCreated.ToString($fmt)}}, Id, ProviderName, @{n='message';e={$_.Message}})
[ordered]@{application=$app; system=$sys} | ConvertTo-Json -Depth 4 -Compress
"""

_AUTOSTART_PS = r"""
$pattern='Xt|QMT|国金|bin\.x64'
$tasks=@(Get-ScheduledTask -ErrorAction SilentlyContinue | ForEach-Object {
  $t=$_; $acts=(($t.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' | ')
  if ($acts -match $pattern) {
    [pscustomobject]@{name=$t.TaskName; path=$t.TaskPath; state=[string]$t.State; actions=$acts;
      triggers=(($t.Triggers | ForEach-Object { "$($_.CimClass.CimClassName) $($_.StartBoundary) $($_.DaysInterval)" }) -join ' | ')}
  } })
$run=@()
foreach ($k in 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run','HKLM:\Software\Microsoft\Windows\CurrentVersion\Run','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run') {
  if (Test-Path $k) { $p=Get-ItemProperty $k
    foreach ($prop in $p.PSObject.Properties) { if ("$($prop.Value)" -match $pattern) { $run += [pscustomobject]@{key=$k; name=$prop.Name; value="$($prop.Value)"} } } } }
$startup=@(Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
[ordered]@{scheduled_tasks=$tasks; run_keys=$run; startup_folder=$startup} | ConvertTo-Json -Depth 4 -Compress
"""


def collect_windows_events(since: dt.date, secrets: list[str]) -> dict[str, list[dict[str, Any]]]:
    raw = _json_or_empty(_powershell(_EVENTS_PS.replace("__SINCE__", since.isoformat())))
    result: dict[str, list[dict[str, Any]]] = {}
    for key in ("application", "system"):
        rows = []
        for item in _as_list(raw.get(key) if isinstance(raw, dict) else None):
            rows.append({
                "time": str(item.get("time", "")),
                "id": item.get("Id"),
                "provider": item.get("ProviderName"),
                "message": mask(str(item.get("message", ""))[:800], secrets),
            })
        result[key] = rows
    return result


def collect_processes() -> list[dict[str, Any]]:
    from src.qmt_client_probe import list_qmt_client_processes

    rows = []
    for item in list_qmt_client_processes(PROCESS_NAMES) or []:
        started = item.get("started_at")
        rows.append({**item, "started_at": started.strftime("%Y-%m-%d %H:%M:%S") if started else ""})
    return rows


def client_version(root: Path) -> Any:
    exe = root / "bin.x64" / "XtItClient.exe"
    if not exe.exists():
        return {}
    quoted = str(exe).replace("'", "''")
    return _json_or_empty(_powershell(
        f"(Get-Item '{quoted}').VersionInfo | Select-Object FileVersion,ProductVersion | ConvertTo-Json -Compress",
        timeout=30,
    ))


def build_report(since: dt.date, until: dt.date) -> dict[str, Any]:
    secrets = _account_secrets()
    episodes: list[dict[str, Any]] = []
    if DAEMON_LOG.exists():
        with DAEMON_LOG.open("r", encoding="utf-8", errors="replace") as stream:
            episodes = extract_outage_episodes(stream, since)
    roots = find_qmt_roots()
    qmt = [block for block in (
        _safe(f"扫描{root}", lambda root=root: collect_qmt_files(root, since, until, secrets), None)
        for root in roots) if block]
    events = _safe("读取Windows事件日志", lambda: collect_windows_events(since, secrets),
                   {"application": [], "system": []})

    evidence: list[dict[str, str]] = []
    for block in qmt:
        for report in block["log_files"]:
            for kind in ("window_lines", "strong_lines"):
                for item in report.get(kind, []):
                    if item.get("time"):
                        evidence.append({"time": item["time"], "source": f"QMT日志 {report['path']}",
                                         "detail": item["line"]})
        for item in block["changed_programs"]:
            evidence.append({"time": item["modified"], "source": "程序文件变化", "detail": item["path"]})
        for item in block["changed_configs"]:
            evidence.append({"time": item["modified"], "source": "配置文件变化", "detail": item["path"]})
    for key, label in (("application", "应用崩溃/无响应事件"), ("system", "系统关机/重启事件")):
        for item in events[key]:
            evidence.append({"time": item["time"], "source": f"{label} {item['id']}",
                             "detail": item["message"][:300]})

    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "range": {"since": since.isoformat(), "until": until.isoformat()},
        "read_only": True,
        "qmt_roots": [str(root) for root in roots],
        "client_version": {str(root): _safe("读取客户端版本", lambda root=root: client_version(root), {})
                           for root in roots},
        "processes_now": _safe("读取客户端进程", collect_processes, []),
        "outage_episodes": correlate(episodes, evidence),
        "windows_events": events,
        "autostart": _json_or_empty(_powershell(_AUTOSTART_PS)),
        "qmt_files": qmt,
    }


def print_summary(report: dict[str, Any]) -> None:
    print(f"QMT安装目录：{'、'.join(report['qmt_roots']) or '未找到'}")
    for item in report["processes_now"]:
        print(f"当前进程：{item['exe']} pid={item['pid']} 启动={item['started_at']} 窗口={item['window_titles']}")
    crashes = report["windows_events"]["application"]
    print(f"QMT相关崩溃/无响应事件：{len(crashes)} 条")
    for item in crashes[:10]:
        print(f"  {item['time']} [{item['id']}] {item['message'][:120]}")
    reboots = report["windows_events"]["system"]
    print(f"系统关机/重启事件：{len(reboots)} 条")
    for block in report["qmt_files"]:
        print(f"[{block['root']}] 日志文件{len(block['log_files'])}个；含相关设置的配置文件{len(block['config_hits'])}个；"
              f"近期改动的程序文件{len(block['changed_programs'])}个")
        for item in block["changed_programs"][:8]:
            print(f"  程序文件 {item['modified']} {item['path']}")
    print(f"daemon日志里的失联：{len(report['outage_episodes'])} 次")
    for episode in report["outage_episodes"]:
        print(f"  {episode['start']}（{episode['minutes']}分钟，{'/'.join(episode['kinds'])}）前后证据{episode['evidence_count']}条")
        for item in episode["evidence"][:4]:
            print(f"    {item['time']} {item['source']}：{item['detail'][:100]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default=DEFAULT_SINCE.isoformat())
    parser.add_argument("--until", default=dt.date.today().isoformat())
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("请在 Windows 交易机上运行：cd C:\\A_System; py -3.11 scripts\\diagnose_qmt_client_restarts.py")
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    report = build_report(since, until)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print_summary(report)
    print(f"QMT_CLIENT_RESTART_DIAGNOSIS_OK {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
