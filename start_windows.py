import subprocess, sys, os, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

root = Path(__file__).absolute().parent
log = root / "logs" / "trading_daemon.log"
pid_file = root / ".daemon_pid"
d_monitor_pid_file = root / "logs" / "strategy_d_monitor.pid"
daemon = root / "scripts" / "trading_daemon.py"
log.parent.mkdir(exist_ok=True)

def pid_exists(pid: str) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return pid in result.stdout
    except Exception:
        return True

def wait_pid_gone(pid: str) -> bool:
    """启动前只按进程状态判断旧进程是否释放，不用固定 sleep 猜时间。"""
    start = time.monotonic()
    while True:
        if not pid_exists(pid):
            return True
        if time.monotonic() - start >= 60:
            return False
        time.sleep(0.5)

def stop_pid_file(path: Path, label: str) -> bool:
    if not path.exists():
        return False
    old_pid = path.read_text().strip()
    if not old_pid:
        path.unlink(missing_ok=True)
        return False
    if not pid_exists(old_pid):
        path.unlink(missing_ok=True)
        print(f"Old {label} pid file cleaned (PID {old_pid} not running)")
        return False
    subprocess.Popen(
        ["taskkill", "/PID", old_pid, "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not wait_pid_gone(old_pid):
        print(f"Old {label} still running (PID {old_pid}); start aborted to avoid duplicate daemon/QMT session.")
        raise SystemExit(1)
    path.unlink(missing_ok=True)
    print(f"Old {label} stopped (PID {old_pid})")
    return True

stopped_d = stop_pid_file(d_monitor_pid_file, "D monitor")
# 不再每次启动都用 PowerShell 全进程扫描孤儿 D 监控。那一步很慢，且不是状态确认。
# D 监控的正常生命周期由 pid 文件和 taskkill /T 进程树停止保证；真出现孤儿进程时再单独排查。
stopped_orphan_d = False
stopped_daemon = stop_pid_file(pid_file, "daemon process")
if stopped_d or stopped_orphan_d or stopped_daemon:
    print("Old process state verified; starting new daemon.")

# 让 daemon 自己的 RotatingFileHandler 写日志，stdout/stderr 丢弃
proc = subprocess.Popen(
    [sys.executable, str(daemon)],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
)

pid_file.write_text(str(proc.pid))
print(f"Started PID {proc.pid}")
print(f"Log: {log}")

# 等日志文件出现
for _ in range(10):
    if log.exists():
        break
    time.sleep(0.5)

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED   = "\033[91m"
BOLD  = "\033[1m"
RESET = "\033[0m"

print(GREEN + BOLD + "A_System 已启动，下面只需要盯住绿色成功行和红色失败行。" + RESET)
print("Showing live log (Ctrl+C to detach, daemon keeps running)...")


def color_for_line(text: str) -> str:
    success_words = [
        "✅",
        "QMT连接成功",
        "程序正常",
        "当日涨停池模拟观察计划已生成",
        "收盘流水线完成",
        "计划单",
    ]
    warning_words = ["⚠️", "| WARNING |", "WARNING", "仅供参考", "暂不开仓"]
    error_words = ["❌", "| ERROR |", "ERROR", "失败", "异常"]
    if any(word in text for word in error_words):
        return RED
    if any(word in text for word in success_words):
        return GREEN
    if any(word in text for word in warning_words):
        return YELLOW
    return ""

with open(log, "r", encoding="utf-8", errors="replace") as f:
    f.seek(0, 2)
    while True:
        line = f.readline()
        if line:
            stripped = line.rstrip()
            color = color_for_line(stripped)
            if color:
                print(color + stripped + RESET, flush=True)
            else:
                print(stripped, flush=True)
        else:
            time.sleep(0.5)
