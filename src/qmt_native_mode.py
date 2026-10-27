"""QMT 原生版（2026-10-22 起）：A_System 整体运行在国金 QMT 的内置 Python 里。

券商 2026-10-23 起限制 QMT 内置 Python 读写文件和联网，旧架构（外部 trading_daemon
+ 文件桥接 qmt_inner）无法再用。启用原生版后（config/qmt_native.json 的 enabled=true），
Windows 上的旧入口只做三件事：

1. 绝不再启动外部守护进程：两套系统同时管理同一个账户是禁止的；
2. 读 QMT 的策略输出日志，报告 QMT 里的 A_System 是否在运行；
3. 确认 QMT 外的推送转发计划任务在运行（start_windows.py 必要时启动它）。

本模块只读 QMT 日志、查询进程和计划任务，不连接 QMT、不下单、不发推送。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

CONFIG = Path("config") / "qmt_native.json"
DEFAULT_LOG_DIR = r"C:\QMT\GJQMT\userdata\log"
TAIL_BYTES = 3_000_000
ACCOUNT_MARK = "[账户]".encode("utf-8")


def native_settings(root: str | Path) -> dict[str, Any] | None:
    """启用原生版时返回其设置；未启用、文件缺失或损坏时返回 None（保持旧行为）。"""
    try:
        data = json.loads((Path(root) / CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("enabled") is True else None


def _tail(path: Path, limit: int = TAIL_BYTES) -> bytes:
    # QMT 一直开着这个文件写入：目录里的大小有滞后，按打开的句柄读真实长度
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - limit))
            return handle.read()
    except OSError:
        return b""


def live_status(settings: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    """QMT 里实盘副本最后一次状态行（每 20 秒一行）的时间、版本、守护线程和最近的账户检查。"""
    now = now or dt.datetime.now()
    log_dir = Path(settings.get("qmt_log_dir") or DEFAULT_LOG_DIR)
    name = str(settings.get("strategy_file") or "A_SYSTEM_QMT_INNER").encode("ascii")
    status = re.compile(
        rb"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ [^\n]*" + re.escape(name)
        + rb"[^\n]*\[A_SYSTEM\] \d\d:\d\d:\d\d status: ([^\n]*)", re.M)
    result: dict[str, Any] = {"log": None, "last_status_at": None, "age_seconds": None,
                              "daemon_alive": None, "release": None, "account": None}
    for day in (now.date(), now.date() - dt.timedelta(days=1)):
        path = log_dir / ("XtClient_FormulaOutput_%s.log" % day.strftime("%Y%m%d"))
        data = _tail(path)
        found = list(status.finditer(data))
        if not found:
            continue
        last = found[-1]
        at = dt.datetime.strptime(last.group(1).decode("ascii"), "%Y-%m-%d %H:%M:%S")
        alive = re.search(rb"daemon_alive=(\w+)", last.group(2))
        releases = re.findall(rb"started: mode=live release=([0-9A-Za-z-]+)", data)
        result.update(log=str(path), last_status_at=at, age_seconds=max(0.0, (now - at).total_seconds()),
                      daemon_alive=(alive.group(1) == b"True") if alive else None,
                      release=releases[-1].decode("ascii") if releases else None)
        # 守护进程每分钟一行，如 “✅ [账户] 2026-10-27 02:31:28 | 账户***03 总资产6.89万 | 无持仓”
        mark = data.rfind(ACCOUNT_MARK)
        if mark >= 0:
            prefix = data[max(0, mark - 8):mark]  # 行首的 ✅/❌ 标记
            cut = 0
            for sep in (b"| ", b'"'):
                if prefix.rfind(sep) >= 0:
                    cut = max(cut, prefix.rfind(sep) + len(sep))
            ends = [i for i in (data.find(b'"', mark), data.find(b"\n", mark)) if i >= 0]
            text = data[max(0, mark - 8) + cut:min(ends) if ends else len(data)]
            result["account"] = text.decode("utf-8", "replace").strip()
        break
    return result


def _run(command: list[str]) -> str | None:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=60,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def qmt_running() -> bool | None:
    out = _run(["tasklist", "/FI", "IMAGENAME eq XtItClient.exe", "/FO", "CSV", "/NH"])
    return None if out is None else "xtitclient.exe" in out.lower()


def task_state(name: str) -> str | None:
    # Get-ScheduledTask 的 State 是英文枚举（Running/Ready/Disabled），不随系统语言变化
    out = _run(["powershell", "-NoProfile", "-Command", "(Get-ScheduledTask -TaskName '%s').State" % name])
    return out.strip() if out and out.strip() else None


def start_task(name: str) -> bool:
    return _run(["powershell", "-NoProfile", "-Command", "Start-ScheduledTask -TaskName '%s'" % name]) is not None


def status_lines(settings: dict[str, Any], ensure_forwarder: bool = False, now: dt.datetime | None = None,
                 qmt: Callable[[], bool | None] = qmt_running, task: Callable[[str], str | None] = task_state,
                 start: Callable[[str], bool] = start_task) -> tuple[bool, list[str]]:
    """(是否正常, 给人看的状态行)。正常 = QMT 在运行、实盘副本最近有状态行、推送转发在运行。"""
    lines: list[str] = []
    healthy = True
    stale = float(settings.get("stale_minutes") or 10) * 60
    running = qmt()
    if running is False:
        healthy = False
        lines.append("❌ QMT（XtItClient.exe）没有运行：请打开国金 QMT 并登录，A_SYSTEM 会随 QMT 自动启动。")
    elif running:
        lines.append("✅ QMT 正在运行。")
    live = live_status(settings, now)
    if live["last_status_at"] is None:
        healthy = False
        lines.append("❌ 今天和昨天的 QMT 策略日志里都没有 A_SYSTEM 的状态行：在 QMT“模型交易”里查看 A_SYSTEM 是否“运行中”。")
    else:
        when = live["last_status_at"].strftime("%Y-%m-%d %H:%M:%S")
        if live["age_seconds"] > stale:
            healthy = False
            lines.append("❌ QMT 里的 A_SYSTEM 最后一次输出在 %s（%d 分钟前）：若“模型交易”里仍显示运行中，"
                         "说明 QMT 内置 Python 卡住了，请退出 QMT 再登录（策略随 QMT 自动启动）。"
                         % (when, live["age_seconds"] // 60))
        else:
            lines.append("✅ QMT 里的 A_SYSTEM 在运行：最后状态 %s（%d 秒前），守护线程%s，版本 %s。" % (
                when, live["age_seconds"], "正常" if live["daemon_alive"] else "异常",
                live["release"] or "（本段日志里未见启动行）"))
            if live["daemon_alive"] is False:
                healthy = False
        if live["account"]:
            lines.append("   最近的账户检查：%s" % live["account"])
    name = str(settings.get("forwarder_task") or "A_System_QmtNotifyForwarder")
    state = task(name)
    if state == "Running":
        lines.append("✅ 推送转发（计划任务 %s）在运行。" % name)
    elif ensure_forwarder and state is not None and start(name) and task(name) == "Running":
        lines.append("✅ 推送转发（计划任务 %s）原先为 %s，已启动。" % (name, state))
    else:
        healthy = False
        lines.append("❌ 推送转发（计划任务 %s）状态：%s。手机将收不到推送，请运行 "
                     "powershell -ep bypass -f C:\\A_System\\qmt_native_phase0\\install_notify_forwarder.ps1"
                     % (name, state or "未找到"))
    lines.append("停止交易：QMT →“模型交易”→ A_SYSTEM 行点 ■；恢复：点 ▶。stop_windows.py 只清理旧系统进程，不影响 QMT 里的 A_System。")
    return healthy, lines


def _clear_manual_stop(root: str | Path) -> None:
    # 人工停机标记：stop 写下后，QMT 客户端守护不再拉起 QMT、推送转发不报“策略停止输出”
    from src.runtime_stop_state import clear_manual_stop

    clear_manual_stop(root)


def native_start(settings: dict[str, Any], root: str | Path, automatic: bool, out: Callable[[str], None] = print,
                 control: Any = None, clock: Callable[[], dt.datetime] = dt.datetime.now,
                 sleep: Callable[[float], None] = time.sleep, **status_kwargs: Any) -> int:
    """start_windows.py 在原生版下：启动 QMT（A_SYSTEM 随 QMT 自动运行），等它真的开始输出；返回退出码。"""
    if automatic:
        out("A_System 运行在 QMT 内置 Python 中：keeper/计划任务不启动外部守护进程。")
        return 0
    if control is None:
        from src.qmt_process_control import ProcessControl

        control = ProcessControl(settings.get("qmt_exe") or r"C:\QMT\GJQMT\bin.x64\XtItClient.exe")
    out("启动 A_System：A_System 运行在 QMT 内置 Python 中，随 QMT 自动运行。")
    pids = control.pids()
    if pids is None:
        out("❌ 无法查询 QMT 进程（tasklist 失败）。")
        return 1
    last = live_status(settings, clock())
    fresh = last["last_status_at"] is not None and last["age_seconds"] <= 60
    if pids and fresh:
        _clear_manual_stop(root)
        healthy, lines = status_lines(settings, ensure_forwarder=True, **status_kwargs)
        for line in lines:
            out(line)
        out("A_System 本来就在运行，无需启动。")
        return 0 if healthy else 1
    if pids and control.main_window():
        when = last["last_status_at"].strftime("%m-%d %H:%M:%S") if last["last_status_at"] else "（今天没有）"
        out("QMT 已登录，但 A_SYSTEM 没有在运行（最后一次输出 %s）：先关闭 QMT，再重新打开。" % when)
        ok, message = control.close()
        out(("✅ " if ok else "❌ ") + message)
        if not ok:
            return 1
        pids = []
    if not pids:
        ok, message = control.launch()
        out(("✅ " if ok else "❌ ") + message)
        if not ok:
            return 1
    started = clock()
    _clear_manual_stop(root)
    wait = float(settings.get("start_wait_minutes") or 15) * 60
    hinted = False
    out("等待 QMT 登录、A_SYSTEM 开始输出（最多 %d 分钟）……" % (wait // 60))
    while (clock() - started).total_seconds() < wait:
        sleep(5)
        now = clock()
        at = live_status(settings, now)["last_status_at"]
        if at is not None and at >= started.replace(microsecond=0):
            healthy, lines = status_lines(settings, ensure_forwarder=True, now=now, **status_kwargs)
            for line in lines:
                out(line)
            out("✅ A_System 已开始运行：启动后自动补跑数据，手机会收到“✅ 程序与账户已恢复正常”。")
            return 0 if healthy else 1
        if not hinted and (now - started).total_seconds() >= 15 and not control.main_window():
            hinted = True
            out("请在 QMT 登录窗口登录（QMT 没有开启自动登录）；登录后 A_SYSTEM 会自动运行。")
    out("❌ %d 分钟内 A_SYSTEM 没有开始输出：确认 QMT 已登录、“模型交易”里 A_SYSTEM 为运行中；"
        "若 QMT 已登录仍无输出，再运行一次 start_windows.py（会重启 QMT）。" % (wait // 60))
    return 1


def native_stop(settings: dict[str, Any], out: Callable[[str], None] = print, control: Any = None,
                notify: Callable[[], None] | None = None, clock: Callable[[], dt.datetime] = dt.datetime.now) -> int:
    """stop_windows.py 在原生版下：关闭 QMT，A_SYSTEM 和全部自动交易随之停止；返回退出码。
    调用前必须已写好人工停机标记（否则 QMT 客户端守护会把 QMT 重新拉起来）。"""
    if control is None:
        from src.qmt_process_control import ProcessControl

        control = ProcessControl(settings.get("qmt_exe") or r"C:\QMT\GJQMT\bin.x64\XtItClient.exe")
    last = live_status(settings, clock())
    pids = control.pids()
    if pids is None:
        out("❌ 无法查询 QMT 进程（tasklist 失败）。")
        return 1
    if not pids:
        out("QMT 没有运行：A_SYSTEM 本来就是停止状态。")
        return 0
    out("停止 A_System：关闭 QMT（A_SYSTEM 和全部自动交易随之停止）……")
    ok, message = control.close()
    out(("✅ " if ok else "❌ ") + message)
    if not ok:
        return 1
    if notify is not None:
        notify()
    if last["account"]:
        out("停止前最后一次账户检查：%s" % last["account"])
    out("A_System 已全部停止。恢复：py -3.11 start_windows.py（会打开 QMT，需要你登录）。")
    return 0
