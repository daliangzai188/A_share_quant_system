import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).absolute().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pid_file = ROOT / ".daemon_pid"
d_monitor_pid_file = ROOT / "logs" / "strategy_d_monitor.pid"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"
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
    if not _terminate_process(pid):
        return False
    return _wait_until_pid_gone(pid)


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


if pid_file.exists():
    daemon_ok = _stop_pid_file(pid_file, "daemon")
    d_ok = _stop_pid_file(d_monitor_pid_file, "D monitor")
    if daemon_ok and d_ok:
        _notify_stopped_async()
    else:
        raise SystemExit(1)
else:
    print(YELLOW + "Not running" + RESET, flush=True)
