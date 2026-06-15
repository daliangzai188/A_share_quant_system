import subprocess, sys, os, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

root = Path(__file__).parent
log = root / "logs" / "trading_daemon.log"
pid_file = root / ".daemon_pid"
daemon = root / "scripts" / "trading_daemon.py"
log.parent.mkdir(exist_ok=True)

if pid_file.exists():
    old_pid = pid_file.read_text().strip()
    subprocess.run(["taskkill", "/PID", old_pid, "/F"], capture_output=True)
    pid_file.unlink(missing_ok=True)
    print(f"Old process stopped (PID {old_pid})")

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
print("Showing live log (Ctrl+C to detach, daemon keeps running)...")

# 等日志文件出现
for _ in range(10):
    if log.exists():
        break
    time.sleep(0.5)

GREEN = "\033[92m"
RED   = "\033[91m"
RESET = "\033[0m"

with open(log, "r", encoding="utf-8", errors="replace") as f:
    f.seek(0, 2)
    while True:
        line = f.readline()
        if line:
            stripped = line.rstrip()
            if "✅" in stripped:
                print(GREEN + stripped + RESET, flush=True)
            elif "❌" in stripped or "| ERROR |" in stripped:
                print(RED + stripped + RESET, flush=True)
            else:
                print(stripped, flush=True)
        else:
            time.sleep(0.5)
