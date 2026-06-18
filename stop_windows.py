import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).absolute().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pid_file = ROOT / ".daemon_pid"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def _notify_stopped() -> None:
    """发停止通知。Windows 用 taskkill /F 强杀守护进程，其信号处理器来不及运行，
    故由停止脚本本身推送，确保"程序已停止"一定能通知到。失败安全，绝不影响停止流程。"""
    try:
        from src.notify import notify
        notify("system_error", "🔌 守护进程已停止",
               "守护进程已手动停止，实盘自动交易已暂停。如需恢复请重新启动。")
    except Exception as exc:
        print(YELLOW + f"停止通知发送失败（不影响停止）：{exc}" + RESET)


if pid_file.exists():
    pid = pid_file.read_text().strip()
    result = subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, text=True)
    if result.returncode == 0:
        print(GREEN + f"Stopped PID {pid}" + RESET)
        _notify_stopped()
    else:
        print(YELLOW + f"PID {pid} not found (already stopped)" + RESET)
    pid_file.unlink(missing_ok=True)
else:
    print(YELLOW + "Not running" + RESET)
