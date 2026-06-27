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


def _taskkill(pid: str) -> bool | None:
    """快速停止进程树。

    以前这里同步等待 taskkill 完整返回；当 QMT/xtquant 子线程卡住时，
    Windows taskkill 偶尔会拖很久，导致 stop_windows.py 看起来卡死。
    现在最多等 3 秒：确认成功就返回 True；taskkill 自身卡住则让它后台继续，
    stop 脚本立即返回，避免手工停止程序时被阻塞。
    """
    try:
        proc = subprocess.Popen(
            ["taskkill", "/PID", pid, "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            return proc.wait(timeout=3) == 0
        except subprocess.TimeoutExpired:
            print(
                YELLOW
                + f"停止 PID {pid} 请求已发送，taskkill 仍在后台清理；如稍后仍未退出再检查任务管理器。"
                + RESET,
                flush=True,
            )
            return None
    except Exception as exc:
        print(YELLOW + f"停止 PID {pid} 启动失败：{exc}" + RESET, flush=True)
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
    stopped = _taskkill(pid)
    if stopped is True:
        print(GREEN + f"Stopped PID {pid}" + RESET, flush=True)
        _notify_stopped_async()
    elif stopped is None:
        print(YELLOW + f"Stop requested for PID {pid}; returning immediately." + RESET, flush=True)
        _notify_stopped_async()
    else:
        print(YELLOW + f"PID {pid} not found or stop failed (already stopped)" + RESET, flush=True)
    pid_file.unlink(missing_ok=True)
else:
    print(YELLOW + "Not running" + RESET, flush=True)
