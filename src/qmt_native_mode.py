"""QMT 原生版（2026-10-22 起）：A_System 整体运行在国金 QMT 的内置 Python 里。

券商 2026-10-23 起限制 QMT 内置 Python 读写文件和联网，旧架构（外部 trading_daemon + 文件桥接）
无法再用。启用原生版后（config/qmt_native.json 的 enabled=true）：

- QMT 里的 A_SYSTEM 随 QMT 加载，但只在“交易开关”为 run 时运行交易守护线程（查账户、跑流水线、
  下单、推送）；开关不在或为 pause 时只待命。开关在 Windows 注册表
  HKCU\\Software\\A_System\\QmtNative 的 Trading 值里（用户 10-27 的决定：QMT 内不能读文件，只能这样
  传递暂停/开始；属于外部程序控制 QMT 内策略，合规风险由用户确认承担）。
- start_windows.py：开关设为 run（一直有效，QMT 意外重开也继续，直到 stop）；QMT 没打开或没登录时
  提示先打开并登录；登录着就等 QMT 日志确认交易线程已启动。
- stop_windows.py：开关设为 pause，等 QMT 日志确认交易线程已结束；QMT 保持打开。
- 绝不再启动旧的外部守护进程；keeper/计划任务调用 start 时什么也不改。

本模块只读 QMT 日志、写/读这一个注册表值、查询进程/窗口标题/计划任务，不下单。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
import urllib.parse
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
    result: dict[str, Any] = {"log": None, "last_status_at": None, "age_seconds": None, "trading": None,
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
        trading = re.search(rb"trading=(\w+)", last.group(2))  # 有交易开关的版本才有
        releases = re.findall(rb"started: mode=live release=([0-9A-Za-z-]+)", data)
        result.update(log=str(path), last_status_at=at, age_seconds=max(0.0, (now - at).total_seconds()),
                      daemon_alive=(alive.group(1) == b"True") if alive else None,
                      trading=trading.group(1).decode("ascii") if trading else None,
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


SWITCH_KEY = r"Software\A_System\QmtNative"


def write_switch(value: str, by: str) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SWITCH_KEY) as key:
        winreg.SetValueEx(key, "Trading", 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "ChangedAt", 0, winreg.REG_SZ, dt.datetime.now().isoformat(sep=" ", timespec="seconds"))
        winreg.SetValueEx(key, "ChangedBy", 0, winreg.REG_SZ, by)


def read_switch() -> dict[str, str] | None:
    try:
        import winreg
    except ImportError:
        return None
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SWITCH_KEY) as key:
            for name in ("Trading", "ChangedAt", "ChangedBy"):
                try:
                    out[name] = str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    pass
    except OSError:
        return {}
    return out


def qmt_logged_in() -> bool | None:
    """QMT 是否已登录：有标题含“QMT交易端”的可见主窗口（按窗口标题判断）；不是 Windows 时为 None。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except (ImportError, AttributeError):
        return None
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if "QMT交易端" in buf.value:
                found.append(hwnd)
        return True
    user32.EnumWindows(callback, 0)
    return bool(found)


def status_lines(settings: dict[str, Any], ensure_forwarder: bool = False, now: dt.datetime | None = None,
                 qmt: Callable[[], bool | None] = qmt_running, task: Callable[[str], str | None] = task_state,
                 start: Callable[[str], bool] = start_task,
                 switch: Callable[[], dict[str, str] | None] = read_switch) -> tuple[bool, list[str]]:
    """(是否正常, 给人看的状态行)。正常 = QMT 在运行、A_SYSTEM 最近有状态行、推送转发在运行。"""
    lines: list[str] = []
    healthy = True
    stale = float(settings.get("stale_minutes") or 10) * 60
    sw = switch()
    if sw is not None:
        value = sw.get("Trading") or "（未设置 = 暂停）"
        lines.append("交易开关：%s%s" % (value, "（%s 由 %s 设置）" % (sw.get("ChangedAt", "?"), sw.get("ChangedBy", "?"))
                                         if sw.get("Trading") else ""))
    running = qmt()
    if running is False:
        healthy = False
        lines.append("❌ QMT（XtItClient.exe）没有运行：请先打开并登录国金 QMT。")
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
                         "说明 QMT 内置 Python 卡住了，请退出 QMT 再登录。" % (when, live["age_seconds"] // 60))
        elif live["trading"] == "pause":
            lines.append("⏸ QMT 里的 A_SYSTEM 处于暂停：最后状态 %s（%d 秒前），交易线程未运行（不查账户、不跑流水线、"
                         "不下单），版本 %s。" % (when, live["age_seconds"], live["release"] or "?"))
        else:
            lines.append("✅ QMT 里的 A_SYSTEM 在交易：最后状态 %s（%d 秒前），交易线程%s，版本 %s。" % (
                when, live["age_seconds"], "正常" if live["daemon_alive"] else "未运行",
                live["release"] or "（本段日志里未见启动行）"))
            if live["daemon_alive"] is False:
                healthy = False
        if live["account"]:
            lines.append("   日志里最近一次账户检查：%s" % live["account"])
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
    lines.append("暂停：py -3.11 stop_windows.py；开始：py -3.11 start_windows.py（QMT 保持打开）。")
    return healthy, lines


def _wait(settings: dict[str, Any], since: dt.datetime, seconds: float, done: Callable[[dict[str, Any]], bool],
          clock: Callable[[], dt.datetime], sleep: Callable[[float], None]) -> dict[str, Any] | None:
    """等 since 之后的一行状态行满足 done（状态行每 20 秒一行）。"""
    since = since.replace(microsecond=0)
    while (clock() - since).total_seconds() < seconds:
        sleep(5)
        live = live_status(settings, clock())
        if live["last_status_at"] is not None and live["last_status_at"] >= since and done(live):
            return live
    return None


def native_start(settings: dict[str, Any], root: str | Path, automatic: bool, out: Callable[[str], None] = print,
                 switch: Callable[[str, str], None] = write_switch, logged_in: Callable[[], bool | None] = qmt_logged_in,
                 clock: Callable[[], dt.datetime] = dt.datetime.now, sleep: Callable[[float], None] = time.sleep,
                 **status_kwargs: Any) -> int:
    """start_windows.py 在原生版下：交易开关设为 run，确认 QMT 里的交易线程已启动；返回退出码。"""
    if automatic:
        out("A_System 运行在 QMT 内置 Python 中：keeper/计划任务不改变交易开关，也不启动外部守护进程。")
        return 0
    from src.runtime_stop_state import clear_manual_stop

    try:
        switch("run", "start_windows.py")
    except Exception as exc:  # noqa: BLE001
        out("❌ 无法写入交易开关（注册表 HKCU\\%s）：%s: %s" % (SWITCH_KEY, type(exc).__name__, exc))
        return 1
    clear_manual_stop(root)  # QMT 客户端守护恢复自动拉起 QMT，推送转发恢复停摆报警
    out("✅ 已允许开始交易（交易开关 = run，一直有效，直到运行 stop_windows.py）。")
    if not logged_in():
        out("QMT 还没有打开或还没有登录：请先打开并登录国金 QMT，登录后 A_SYSTEM 会自动开始交易；"
            "登录后可再运行一次 start_windows.py 查看状态。")
        return 1
    out("等待 QMT 里的 A_SYSTEM 启动交易线程（最多 90 秒）……")
    started = clock()
    live = _wait(settings, started, 90, lambda l: l["trading"] == "run" and l["daemon_alive"] is True, clock, sleep)
    if live is None:
        last = live_status(settings, clock())
        if last["last_status_at"] is None or last["age_seconds"] > 60:
            out("❌ QMT 已登录，但 QMT 里的 A_SYSTEM 策略没有输出：在 QMT“模型交易”里看 A_SYSTEM 是否“运行中”"
                "（没有就点 ▶）；显示运行中却没有输出，就退出 QMT 重新登录。")
        elif last["trading"] is None:
            out("❌ QMT 里运行的 A_SYSTEM 还是旧版本，不认识交易开关：需要先部署新版本。")
        else:
            out("❌ 90 秒内没有看到交易线程启动（交易开关仍为 run，A_SYSTEM 读到后会自动启动）：稍后再运行一次 "
                "start_windows.py 查看。")
        return 1
    healthy, lines = status_lines(settings, ensure_forwarder=True, now=clock(), **status_kwargs)
    for line in lines:
        out(line)
    out("✅ A_System 已开始交易：交易线程运行中，刚启动时会自动补跑数据；手机会收到“✅ 程序与账户已恢复正常”。")
    return 0 if healthy else 1


def native_stop(settings: dict[str, Any], out: Callable[[str], None] = print,
                switch: Callable[[str, str], None] = write_switch, logged_in: Callable[[], bool | None] = qmt_logged_in,
                notify: Callable[[], None] | None = None, clock: Callable[[], dt.datetime] = dt.datetime.now,
                sleep: Callable[[float], None] = time.sleep) -> int:
    """stop_windows.py 在原生版下：交易开关设为 pause，确认 QMT 里的交易线程已结束；QMT 保持打开。"""
    try:
        switch("pause", "stop_windows.py")
    except Exception as exc:  # noqa: BLE001
        out("❌ 无法写入交易开关（注册表 HKCU\\%s）：%s: %s。请在 QMT“模型交易”里点 A_SYSTEM 的 ■ 停止。"
            % (SWITCH_KEY, type(exc).__name__, exc))
        return 1
    out("已暂停交易（交易开关 = pause）：QMT 保持打开，运行 start_windows.py 才会再开始。")
    if not logged_in():
        out("QMT 没有打开或没有登录：之后打开 QMT 也保持暂停，直到运行 start_windows.py。")
        if notify is not None:
            notify()
        return 0
    out("等待 QMT 里的 A_SYSTEM 结束交易线程（最多 60 秒）……")
    started = clock()
    live = _wait(settings, started, 60, lambda l: l["trading"] == "pause" and l["daemon_alive"] is False, clock, sleep)
    if live is None:
        last = live_status(settings, clock())
        if last["last_status_at"] is None or last["age_seconds"] > 60:
            out("QMT 里的 A_SYSTEM 策略没有在输出（本来就没在运行）；开关已是 pause，之后启动也保持暂停。")
            if notify is not None:
                notify()
            return 0
        out("❌ 60 秒内没有确认暂停%s：请在 QMT“模型交易”里点 A_SYSTEM 的 ■ 停止。" % (
            "（QMT 里运行的 A_SYSTEM 还是旧版本，不认识交易开关）" if last["trading"] is None else ""))
        return 1
    if notify is not None:
        notify()
    out("✅ QMT 里的 A_SYSTEM 已暂停：交易线程已结束（不查账户、不跑流水线、不下单），QMT 保持登录。")
    if live["account"]:
        out("暂停前最后一次账户检查：%s" % live["account"])
    return 0


QMT_LINE = re.compile(rb"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \[\w+\] ")


def _text(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding).rstrip("\r")
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace").rstrip("\r")


class LogFollower:
    """像以前 start_windows.py 跟随 logs/trading_daemon.log 一样，在终端显示 QMT 里 A_SYSTEM 的日志：
    交易程序的每一行（QMT 日志里的 “[A_SYSTEM_LOG] …”）、宿主消息、推送标题、暂停/交易切换。"""

    def __init__(self, settings: dict[str, Any], out: Callable[[str], None] = print):
        self.log_dir = Path(settings.get("qmt_log_dir") or DEFAULT_LOG_DIR)
        self.name = str(settings.get("strategy_file") or "A_SYSTEM_QMT_INNER")
        self.out = out
        self.path: Path | None = None
        self.offset = 0
        self.in_block = False
        self.trading: str | None = None
        self.last = ""
        self.echo_seen = False
        self.other = False

    def feed(self, raw: bytes) -> None:
        text = _text(raw)
        if not QMT_LINE.match(raw):
            if not text.strip() or self.other:
                return
            if text.startswith("[A_SYSTEM"):   # QMT 把同一次刷新里的几次打印放在一个标题下
                self._payload(text)
            elif self.in_block:                # 多行记录（如开仓计划的方框）或被折行的长行
                self.out(text)
            return
        self.in_block = False
        self.other = self.name not in text
        if not self.other:
            self._payload(text.split("output = ", 1)[1] if "output = " in text else text)

    def _payload(self, payload: str) -> None:
        self.in_block = False
        if payload.startswith("[A_SYSTEM_LOG] "):
            self.echo_seen = self.in_block = True
            self.out(payload[len("[A_SYSTEM_LOG] "):])
        elif payload.startswith("[A_SYSTEM_NOTIFY"):
            parts = payload.split(" ", 3)
            title = urllib.parse.unquote(parts[3].split("/")[1]) if len(parts) > 3 and "/" in parts[3] else payload
            self.out("📣 推送：%s" % title)
        elif payload.startswith("[A_SYSTEM_EXPORT"):
            return
        elif " status: " in payload:
            m = re.search(r"trading=(\w+)", payload)
            if m and m.group(1) != self.trading:
                self.trading = m.group(1)
                self.out("⏸ 交易已暂停（交易开关 = pause）" if self.trading == "pause" else "▶ 交易中（交易开关 = run）")
            last = re.search(r'last="([^"]*)', payload)
            if not self.echo_seen and last and last.group(1) != self.last:   # 旧版本没有逐行日志：显示状态里的最新一行
                self.last = last.group(1)
                self.out(self.last)
        elif payload.startswith("[A_SYSTEM] "):
            self.out(payload)

    def poll(self, today: dt.date) -> None:
        path = self.log_dir / ("XtClient_FormulaOutput_%s.log" % today.strftime("%Y%m%d"))
        if path != self.path:
            self.path, self.offset = path, 0
        try:
            with open(path, "rb") as handle:
                size = handle.seek(0, 2)
                if size < self.offset:
                    self.offset = 0
                handle.seek(self.offset)
                chunk = handle.read(size - self.offset)
        except OSError:
            return
        end = chunk.rfind(b"\n") + 1     # 只处理完整的行
        self.offset += end
        for raw in chunk[:end].split(b"\n")[:-1]:
            self.feed(raw)

    def start_at_end(self, today: dt.date, history: int = 30) -> None:
        """从文件末尾开始跟随，先显示最近 history 行交易日志作为上下文。"""
        lines: list[str] = []
        keep, self.out = self.out, lines.append
        self.poll(today)
        self.out = keep
        for line in lines[-history:]:
            self.out(line)


def follow_log(settings: dict[str, Any], out: Callable[[str], None] = print,
               clock: Callable[[], dt.datetime] = dt.datetime.now, sleep: Callable[[float], None] = time.sleep,
               stop: Callable[[], bool] = lambda: False) -> None:
    follower = LogFollower(settings, out)
    follower.start_at_end(clock().date())
    while not stop():
        sleep(1)
        follower.poll(clock().date())

