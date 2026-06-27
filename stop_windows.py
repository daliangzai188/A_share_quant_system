import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).absolute().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pid_file = ROOT / ".daemon_pid"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"


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


def _taskkill(pid: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["taskkill", "/PID", pid, "/F"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        print(YELLOW + f"停止 PID {pid} 超时，请用任务管理器确认进程是否已退出。" + RESET, flush=True)
        return None


def _cleanup_children_async() -> None:
    """后台清理本项目残留子进程，不阻塞 stop_windows.py 返回。"""
    try:
        creationflags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags |= subprocess.DETACHED_PROCESS
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        root_text = str(ROOT).replace("\\", "\\\\")
        command = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -and $_.CommandLine -match '{root_text}' -and "
            "$_.CommandLine -match 'scripts\\\\(trading_daemon|collect_all_data|clean_collected_data|"
            "build_dynamic_features|score_limit_up_fill_probability|run_paper_ab_filtered_daily_ops|"
            "run_strategy_e2_signal|run_strategy_l_signal|monitor_strategy_d_intraday)\\.py' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:
        print(YELLOW + f"后台残留进程清理启动失败（不影响停止）：{exc}" + RESET, flush=True)


if pid_file.exists():
    pid = pid_file.read_text().strip()
    print(f"Stopping PID {pid} ...", flush=True)
    result = _taskkill(pid)
    if result is not None and result.returncode == 0:
        print(GREEN + f"Stopped PID {pid}" + RESET, flush=True)
        _cleanup_children_async()
        _notify_stopped_async()
    else:
        print(YELLOW + f"PID {pid} not found or stop failed (already stopped)" + RESET, flush=True)
    pid_file.unlink(missing_ok=True)
else:
    print(YELLOW + "Not running" + RESET, flush=True)
