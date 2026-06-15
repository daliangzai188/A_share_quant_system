import subprocess, sys, os, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

root = Path(__file__).absolute().parent
log = root / "logs" / "trading_daemon.log"
pid_file = root / ".daemon_pid"
daemon = root / "scripts" / "trading_daemon.py"
log.parent.mkdir(exist_ok=True)

if pid_file.exists():
    old_pid = pid_file.read_text().strip()
    subprocess.run(["taskkill", "/PID", old_pid, "/F"], capture_output=True)
    pid_file.unlink(missing_ok=True)
    print(f"Old process stopped (PID {old_pid}), waiting for QMT session to release...")
    time.sleep(15)  # 等 QMT session 完全释放，避免新进程启动时全部连接 -1

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
