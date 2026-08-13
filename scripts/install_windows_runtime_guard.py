# -*- coding: utf-8 -*-
"""安装/检查 Windows 计划任务，确保登录后及每天08:15启动 A_System。"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENSURE_SCRIPT = PROJECT_ROOT / "scripts" / "ensure_windows_runtime.py"
TASK_NAME = "A_System_RuntimeGuard"
REPORT_PATH = PROJECT_ROOT / "reports" / "runtime" / "windows_runtime_guard.json"


def _powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=60,
    )


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def install() -> dict:
    if sys.platform != "win32":
        raise RuntimeError("安装命令必须在 Windows 虚拟机中运行")
    py = str(Path(sys.executable).resolve())
    ensure = str(ENSURE_SCRIPT.resolve())
    name = _ps_quote(TASK_NAME)
    command = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute {_ps_quote(py)} -Argument {_ps_quote('"' + ensure + '"')}
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Daily -At '08:15')
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName {name} -Action $action -Trigger $triggers -Settings $settings `
    -Description 'A_System登录/每日08:15运行状态兜底；只启动缺失进程，不重启健康daemon。' `
    -Force | Out-Null
$task = Get-ScheduledTask -TaskName {name}
$info = Get-ScheduledTaskInfo -TaskName {name}
[ordered]@{{
    task_name = $task.TaskName
    state = [string]$task.State
    next_run_time = if ($info.NextRunTime) {{ $info.NextRunTime.ToString('s') }} else {{ '' }}
    last_run_time = if ($info.LastRunTime) {{ $info.LastRunTime.ToString('s') }} else {{ '' }}
    last_task_result = $info.LastTaskResult
}} | ConvertTo-Json -Compress
"""
    result = _powershell(command)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "计划任务安装失败")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else {}
    payload.update(
        {
            "status": "INSTALLED",
            "python": py,
            "ensure_script": ensure,
            "triggers": ["AT_LOGON", "DAILY_08:15"],
            "start_when_available": True,
            "changes_live_orders": False,
        }
    )
    return payload


def status() -> dict:
    if sys.platform != "win32":
        raise RuntimeError("检查命令必须在 Windows 虚拟机中运行")
    name = _ps_quote(TASK_NAME)
    command = f"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName {name}
$info = Get-ScheduledTaskInfo -TaskName {name}
[ordered]@{{
    task_name = $task.TaskName
    state = [string]$task.State
    next_run_time = if ($info.NextRunTime) {{ $info.NextRunTime.ToString('s') }} else {{ '' }}
    last_run_time = if ($info.LastRunTime) {{ $info.LastRunTime.ToString('s') }} else {{ '' }}
    last_task_result = $info.LastTaskResult
}} | ConvertTo-Json -Compress
"""
    result = _powershell(command)
    if result.returncode != 0:
        return {
            "status": "NOT_INSTALLED",
            "task_name": TASK_NAME,
            "reason": result.stderr.strip() or result.stdout.strip(),
        }
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else {}
    payload["status"] = "INSTALLED"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="只检查，不安装或修改")
    args = parser.parse_args()
    try:
        payload = status() if args.status else install()
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("status") == "INSTALLED" else 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
