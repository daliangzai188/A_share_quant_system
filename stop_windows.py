import subprocess
from pathlib import Path

pid_file = Path(__file__).parent / ".daemon_pid"

if pid_file.exists():
    pid = pid_file.read_text().strip()
    result = subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Stopped PID {pid}")
    else:
        print(f"PID {pid} not found (already stopped)")
    pid_file.unlink(missing_ok=True)
else:
    print("Not running")
