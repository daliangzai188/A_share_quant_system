import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).absolute().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runtime_stop_state import write_manual_stop

pid_file = ROOT / ".daemon_pid"
d_monitor_pid_file = ROOT / "logs" / "strategy_d_monitor.pid"
keeper_pid_file = ROOT / ".keeper_pid"   # 守护器（win_daemon_keeper.py），须先于 daemon 停止
# 按命令行特征识别相关进程；即使 pid 文件丢失/被覆盖也能扫到孤儿进程。
DAEMON_KEYWORD = "trading_daemon.py"
D_MONITOR_KEYWORD = "monitor_strategy_d_intraday.py"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
STOP_VERIFY_TIMEOUT_SEC = 60
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def _notify_stopped_async() -> None:
    """后台发停止通知，不能阻塞停止脚本。

    Bark/网络偶发慢响应时，同步 notify 会让 stop_windows.py 看起来卡住。
    停止动作已完成后，通知放到独立 Python 进程里发，失败也不影响停止流程。
    """
    try:
        creationflags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags |= subprocess.DETACHED_PROCESS
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts" / "send_notify.py"),
                "system_error",
                "🔌 守护进程已停止",
                "守护进程已手动停止，实盘自动交易已暂停。如需恢复请重新启动。",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:
        print(YELLOW + f"停止通知后台发送启动失败（不影响停止）：{exc}" + RESET, flush=True)


def _pid_exists(pid: str) -> bool:
    """用 Windows API 确认 PID 是否仍存在，避免 tasklist 反复启动导致停止脚本卡顿。"""
    try:
        import ctypes

        process_id = int(pid)
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:
        print(YELLOW + f"检查 PID {pid} 状态失败：{exc}" + RESET, flush=True)
        return True


def _terminate_process(pid: str) -> bool:
    """直接调用 Windows API 强制终止进程，避免 taskkill.exe 枚举进程树导致卡顿。"""
    try:
        import ctypes

        process_id = int(pid)
        kernel32 = ctypes.windll.kernel32
        access = PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(access, False, process_id)
        if not handle:
            return not _pid_exists(pid)
        try:
            if not kernel32.TerminateProcess(handle, 1):
                print(YELLOW + f"TerminateProcess PID {pid} 返回失败。" + RESET, flush=True)
                return False
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:
        print(YELLOW + f"强制停止 PID {pid} 失败：{exc}" + RESET, flush=True)
        return False


def _list_descendant_pids(root_pid: str) -> list[int]:
    """用进程快照枚举 root_pid 的全部后代进程。

    纯 Windows API（CreateToolhelp32Snapshot），不启动 taskkill/PowerShell，
    毫秒级完成，不会在慢IO机器上卡顿。
    必须在杀主进程之前调用：TerminateProcess 不级联杀子进程，
    收盘流水线子进程（collect/clean等）成孤儿后会继续狂读写共享盘，
    把下次启动的 import/读配置拖到分钟级（2026-07-03 实测2.5分钟+）。
    """
    try:
        import ctypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return []
        snap_handle = ctypes.c_void_p(snapshot)
        parent_map: dict[int, list[int]] = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if kernel32.Process32FirstW(snap_handle, ctypes.byref(entry)):
                while True:
                    parent_map.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
                    if not kernel32.Process32NextW(snap_handle, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snap_handle)
        descendants: list[int] = []
        seen: set[int] = set()
        queue = [int(root_pid)]
        while queue:
            pid = queue.pop()
            for child in parent_map.get(pid, []):
                if child not in seen:
                    seen.add(child)
                    descendants.append(child)
                    queue.append(child)
        return descendants
    except Exception as exc:
        print(YELLOW + f"枚举 PID {root_pid} 子进程失败（继续停止主进程）：{exc}" + RESET, flush=True)
        return []


def _wait_until_pid_gone(pid: str) -> bool:
    """等待到 PID 真正从进程表消失。

    成功标准不是等待了多少秒，而是 PID 不存在。超时只是防止脚本无限卡死；
    超时后不会删除 pid 文件，也不会提示可以重启。
    """
    start = time.monotonic()
    while True:
        if not _pid_exists(pid):
            return True
        if time.monotonic() - start >= STOP_VERIFY_TIMEOUT_SEC:
            print(
                YELLOW
                + f"PID {pid} 仍存在，尚未停干净；不要马上启动，先确认任务管理器或重新执行 stop。"
                + RESET,
                flush=True,
            )
            return False
        time.sleep(0.1)


def _stop_and_verify(pid: str) -> bool:
    if not _pid_exists(pid):
        return True
    # 杀主进程前先枚举后代：之后父子关系依然保留在快照结果里，逐个清理。
    descendants = _list_descendant_pids(pid)
    if not _terminate_process(pid):
        return False
    ok = _wait_until_pid_gone(pid)
    for child_pid in descendants:
        child = str(child_pid)
        if not _pid_exists(child):
            continue
        print(f"Stopping child process PID {child} ...", flush=True)
        if not _terminate_process(child) or not _wait_until_pid_gone(child):
            print(YELLOW + f"child PID {child} stop not confirmed." + RESET, flush=True)
            ok = False
    return ok


def _stop_pid_file(path: Path, label: str) -> bool:
    if not path.exists():
        return True
    pid = path.read_text().strip()
    if not pid:
        path.unlink(missing_ok=True)
        return True
    print(f"Stopping {label} PID {pid} ...", flush=True)
    stopped = _stop_and_verify(pid)
    if stopped:
        print(GREEN + f"Stopped {label} PID {pid}" + RESET, flush=True)
        path.unlink(missing_ok=True)
        return True
    print(YELLOW + f"{label} PID {pid} stop not confirmed; pid file kept." + RESET, flush=True)
    return False


def _run_capture(cmd: list[str], timeout: float) -> str:
    """执行命令并返回 stdout；失败/超时返回空串，不抛出。"""
    try:
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            creationflags=creationflags,
        )
        return proc.stdout or ""
    except Exception as exc:
        print(YELLOW + f"进程扫描命令失败（{cmd[0]}）：{exc}" + RESET, flush=True)
        return ""


def _scan_related_pids() -> dict[int, str]:
    """按命令行特征扫描所有相关 python 进程，返回 {pid: label}。

    只作为兜底：pid 文件里找不到任何存活进程时才调用（常规停止不跑）。
    只用 PowerShell CIM——wmic 已从新版 Windows 移除（2026-07-03 实测
    WinError 2），且全盘扫描+高IO压力曾把虚拟机卡到重启，不能每次都跑。
    只匹配脚本文件名，stop_windows.py / send_notify.py 等自身进程
    天然不含关键字，不会误杀。失败/超时返回空，不阻塞停止流程。
    """
    keywords = {DAEMON_KEYWORD: "daemon", D_MONITOR_KEYWORD: "D monitor"}
    found: dict[int, str] = {}

    def _classify(cmdline: str) -> str | None:
        low = cmdline.lower().replace("\\", "/")
        for kw, label in keywords.items():
            if kw in low:
                return label
        return None

    # PowerShell CIM：每行 "pid|commandline"
    out = _run_capture(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
         "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
        timeout=15.0,
    )
    for line in out.replace("\r\n", "\n").split("\n"):
        if "|" not in line:
            continue
        pid_text, _, cmdline = line.partition("|")
        pid_text = pid_text.strip()
        if not pid_text.isdigit():
            continue
        label = _classify(cmdline)
        if label:
            found[int(pid_text)] = label
    return found


print(GREEN + f"{timestamp()} | 停止 A_System：清理全部守护进程 / D 监控进程..." + RESET, flush=True)

# 必须先于停止keeper/daemon落盘。这样即使停止动作与keeper的30秒轮询、
# 登录触发或每日08:15计划任务发生竞态，所有自动恢复入口也会看到人工停机意图。
try:
    manual_stop_marker = write_manual_stop(
        ROOT,
        source="stop_windows.py",
        reason="用户显式执行Windows停止命令；仅人工start_windows.py允许恢复",
    )
    print(
        GREEN
        + f"{timestamp()} | 已登记人工停机：{manual_stop_marker.name}；自动恢复将保持暂停。"
        + RESET,
        flush=True,
    )
except Exception as exc:
    print(
        YELLOW
        + f"{timestamp()} | 无法写入人工停机标记，停止已中止，避免稍后被计划任务误启动：{exc}"
        + RESET,
        flush=True,
    )
    raise SystemExit(1)

# 目标进程集合：pid 文件（精确、毫秒级）优先；子进程由 _stop_and_verify 的
# 进程树清理带走。只有 pid 文件找不到任何存活进程时，才用命令行扫描兜底找孤儿——
# 全盘 CIM 扫描要十几秒且吃CPU/IO，每次都跑曾把停止拖到 62 秒、虚拟机卡到重启。
targets: dict[int, str] = {}
# keeper 必须排在最前：它的职责是"发现 daemon 不在就拉起"，若不先停它，
# 停掉 daemon 后 30 秒内会被自动拉回来，用户按老习惯 stop 会以为没停掉
# （2026-07-27 新增 keeper 后的可用性保护）。
for path, label in [(keeper_pid_file, "keeper"), (pid_file, "daemon"), (d_monitor_pid_file, "D monitor")]:
    if path.exists():
        pid_text = path.read_text().strip()
        if pid_text.isdigit() and _pid_exists(pid_text):
            targets[int(pid_text)] = label

if not targets:
    for pid, label in _scan_related_pids().items():
        targets.setdefault(pid, label)

if not targets:
    # 无存活进程：清掉可能残留的 pid 文件，明确告知“确实没在跑”。
    pid_file.unlink(missing_ok=True)
    d_monitor_pid_file.unlink(missing_ok=True)
    keeper_pid_file.unlink(missing_ok=True)
    print(YELLOW + f"{timestamp()} | 未发现运行中的守护/监控进程（已清理残留 pid 文件）。" + RESET, flush=True)
    sys.exit(0)

all_ok = True
cleared = 0
for pid in sorted(targets):
    pid_str = str(pid)
    if not _pid_exists(pid_str):
        continue  # 可能已被先前进程树清理带走
    print(f"{timestamp()} | Stopping {targets[pid]} PID {pid} ...", flush=True)
    if _stop_and_verify(pid_str):
        cleared += 1
        print(GREEN + f"{timestamp()} | Stopped {targets[pid]} PID {pid}" + RESET, flush=True)
    else:
        all_ok = False
        print(YELLOW + f"{timestamp()} | {targets[pid]} PID {pid} 未确认停止。" + RESET, flush=True)

# 停止动作已完成，统一清理 pid 文件。
pid_file.unlink(missing_ok=True)
d_monitor_pid_file.unlink(missing_ok=True)
keeper_pid_file.unlink(missing_ok=True)

if all_ok:
    print(GREEN + f"{timestamp()} | 已清理 {cleared} 个进程，全部停止确认，可安全重启。" + RESET, flush=True)
    if cleared > 0:
        _notify_stopped_async()
else:
    print(YELLOW + f"{timestamp()} | 部分进程未确认停止；先看任务管理器或再次执行 stop，勿立即启动。" + RESET, flush=True)
    raise SystemExit(1)
