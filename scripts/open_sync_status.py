"""Windows desktop entry: ensure local Syncthing runs, then open its GUI."""
import argparse
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import time

GUI_HOST = "127.0.0.1"
GUI_PORT = 8384
GUI_URL = "http://127.0.0.1:8384/"


def gui_listening():
    try:
        with socket.create_connection((GUI_HOST, GUI_PORT), timeout=1):
            return True
    except OSError:
        return False


def ensure_running(timeout=60):
    if gui_listening():
        return "already_running"
    executable = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Syncthing" / "syncthing.exe"
    if not executable.is_file():
        located = shutil.which("syncthing.exe")
        if not located:
            raise FileNotFoundError(f"Syncthing executable missing: {executable}")
        executable = Path(located)
    subprocess.Popen(
        [str(executable), "serve", "--no-console", "--no-browser"],
        cwd=str(executable.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gui_listening():
            return "started"
        time.sleep(0.5)
    raise TimeoutError("Syncthing did not open its local GUI port within 60 seconds")


def install_shortcuts():
    """把入口放到私有目录，创建桌面网页入口及登录自启；不触碰同步设备身份。"""
    if sys.platform != "win32":
        raise RuntimeError("Shortcuts must be installed on Windows")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError("pythonw.exe missing from this Python environment")
    helper = Path(os.environ["LOCALAPPDATA"]) / "A_System" / "sync_tools" / "open_sync_status.py"
    helper.parent.mkdir(parents=True, exist_ok=True)
    staged = helper.with_suffix(".install.tmp")
    staged.write_bytes(Path(__file__).read_bytes())
    staged.replace(helper)
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = f"""
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$ErrorActionPreference='Stop'
$shell=New-Object -ComObject WScript.Shell
$desktop=[Environment]::GetFolderPath('Desktop')
$startup=[Environment]::GetFolderPath('Startup')
foreach ($entry in @(@{{dir=$desktop;name='A_System 同步状态.lnk';args={quote('"' + str(helper) + '"')}}},
                    @{{dir=$startup;name='Syncthing.lnk';args={quote('"' + str(helper) + '" --start-only')}}})) {{
    $shortcut=$shell.CreateShortcut((Join-Path $entry.dir $entry.name))
    $shortcut.TargetPath={quote(pythonw)}
    $shortcut.Arguments=$entry.args
    $shortcut.WorkingDirectory={quote(helper.parent)}
    $shortcut.Save()
}}
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Shortcut installation failed")
    return {"status": "INSTALLED", "helper": str(helper), "device_identity_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-only", action="store_true")
    parser.add_argument("--install-shortcuts", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("Run this entry on Windows")
    if args.install_shortcuts:
        print(json.dumps(install_shortcuts(), ensure_ascii=True, indent=2))
        return 0
    report_dir = Path(os.environ["LOCALAPPDATA"]) / "A_System" / "sync_tools"
    report_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = ensure_running()
        if not args.start_only:
            os.startfile(GUI_URL)
        report = {"status": "PASS", "action": result, "gui": GUI_URL, "opened_browser": not args.start_only}
        exit_code = 0
    except Exception as exc:
        report = {"status": "ERROR", "reason": str(exc), "gui": GUI_URL}
        exit_code = 1
    (report_dir / "last_launch.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
