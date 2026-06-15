import subprocess
from pathlib import Path

pid_file = Path(__file__).absolute().parent / ".daemon_pid"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

if pid_file.exists():
    pid = pid_file.read_text().strip()
    result = subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, text=True)
    if result.returncode == 0:
        print(GREEN + f"Stopped PID {pid}" + RESET)
    else:
        print(YELLOW + f"PID {pid} not found (already stopped)" + RESET)
    pid_file.unlink(missing_ok=True)
else:
    print(YELLOW + "Not running" + RESET)
