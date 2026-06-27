import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).absolute().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pid_file = ROOT / ".daemon_pid"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"
STOP_VERIFY_TIMEOUT_SEC = 60


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
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, process_id)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            still_active = 259
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:
        print(YELLOW + f"检查 PID {pid} 状态失败：{exc}" + RESET, flush=True)
        return True


def _taskkill(pid: str) -> bool:
    """发送强制停止请求；是否停干净由 _pid_exists 再确认。"""
    try:
        subprocess.Popen(
            ["taskkill", "/PID", pid, "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as exc:
        print(YELLOW + f"停止 PID {pid} 请求发送失败：{exc}" + RESET, flush=True)
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
    if not _taskkill(pid):
        return False
    return _wait_until_pid_gone(pid)


if pid_file.exists():
    pid = pid_file.read_text().strip()
    print(f"Stopping PID {pid} ...", flush=True)
    stopped = _stop_and_verify(pid)
    if stopped:
        print(GREEN + f"Stopped PID {pid}" + RESET, flush=True)
        _notify_stopped_async()
        pid_file.unlink(missing_ok=True)
    else:
        print(YELLOW + f"PID {pid} stop not confirmed; .daemon_pid kept." + RESET, flush=True)
        raise SystemExit(1)
else:
    print(YELLOW + "Not running" + RESET, flush=True)
