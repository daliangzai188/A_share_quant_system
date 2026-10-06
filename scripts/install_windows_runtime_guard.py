# -*- coding: utf-8 -*-
"""安装/检查 Windows 运行兜底与防空闲休眠任务；允许锁屏、息屏。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENSURE_SCRIPT = PROJECT_ROOT / "scripts" / "ensure_windows_runtime.py"
SESSION_AWAKE_SOURCE = PROJECT_ROOT / "scripts" / "windows_session_awake_guard.py"
TASK_NAME = "A_System_RuntimeGuard"
SESSION_TASK_NAME = "A_System_SessionStabilityGuard"
REPORT_PATH = PROJECT_ROOT / "reports" / "runtime" / "windows_runtime_guard.json"


def _powershell(script: str) -> subprocess.CompletedProcess[str]:
    # Windows PowerShell 5.1 uses the console code page for redirected output.
    # Match the UTF-8 decoder below before emitting Chinese errors or JSON.
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "$OutputEncoding = [Console]::OutputEncoding\n"
        + script
    )
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


def _session_paths() -> tuple[Path, Path]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("未找到 LOCALAPPDATA，无法安装私有防休眠工具")
    helper = Path(local) / "A_System" / "runtime_tools" / "session_awake_guard.py"
    pythonw = Path(sys.executable).resolve().with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError("未找到同一 Python 环境的 pythonw.exe")
    return helper, pythonw


def install() -> dict:
    if sys.platform != "win32":
        raise RuntimeError("安装命令必须在 Windows 虚拟机中运行")
    py = str(Path(sys.executable).resolve())
    ensure = str(ENSURE_SCRIPT.resolve())
    helper, pythonw = _session_paths()
    helper.parent.mkdir(parents=True, exist_ok=True)
    temporary = helper.with_suffix(".install.tmp")
    temporary.write_bytes(SESSION_AWAKE_SOURCE.read_bytes())
    temporary.replace(helper)
    name = _ps_quote(TASK_NAME)
    session_name = _ps_quote(SESSION_TASK_NAME)
    session_arguments = _ps_quote(f'"{helper}"')
    command = f"""
$ErrorActionPreference = 'Stop'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$action = New-ScheduledTaskAction -Execute {_ps_quote(py)} -Argument {_ps_quote('"' + ensure + '"')}
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Daily -At '08:15')
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName {name} -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal `
    -Description 'A_System登录/每日08:15运行状态兜底；只启动缺失进程，不重启健康daemon。' `
    -Force | Out-Null
$sessionAction = New-ScheduledTaskAction -Execute {_ps_quote(str(pythonw))} -Argument {session_arguments}
$sessionTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Daily -At '07:50')
)
$sessionSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName {session_name} -Action $sessionAction -Trigger $sessionTriggers `
    -Settings $sessionSettings -Principal $principal `
    -Description '保持Windows不空闲休眠；允许锁屏、息屏，不修改认证或更新策略，不启停交易程序。' `
    -Force | Out-Null
Stop-ScheduledTask -TaskName {session_name}
Start-ScheduledTask -TaskName {session_name}
$task = Get-ScheduledTask -TaskName {name}
$info = Get-ScheduledTaskInfo -TaskName {name}
$sessionTask = Get-ScheduledTask -TaskName {session_name}
$sessionInfo = Get-ScheduledTaskInfo -TaskName {session_name}
[ordered]@{{
    task_name = $task.TaskName
    state = [string]$task.State
    next_run_time = if ($info.NextRunTime) {{ $info.NextRunTime.ToString('s') }} else {{ '' }}
    last_run_time = if ($info.LastRunTime) {{ $info.LastRunTime.ToString('s') }} else {{ '' }}
    last_task_result = $info.LastTaskResult
    session_task_name = $sessionTask.TaskName
    session_task_state = [string]$sessionTask.State
    session_next_run_time = if ($sessionInfo.NextRunTime) {{ $sessionInfo.NextRunTime.ToString('s') }} else {{ '' }}
    session_last_run_time = if ($sessionInfo.LastRunTime) {{ $sessionInfo.LastRunTime.ToString('s') }} else {{ '' }}
    session_last_task_result = $sessionInfo.LastTaskResult
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
            "session_stability_triggers": ["AT_LOGON", "DAILY_07:50"],
            "start_when_available": True,
            "changes_live_orders": False,
            "session_awake_script": str(helper),
            "screen_lock_allowed": True,
            "display_sleep_allowed": True,
            "authentication_or_update_policy_changed": False,
        }
    )
    return payload


def status() -> dict:
    if sys.platform != "win32":
        raise RuntimeError("检查命令必须在 Windows 虚拟机中运行")
    name = _ps_quote(TASK_NAME)
    session_name = _ps_quote(SESSION_TASK_NAME)
    helper, pythonw = _session_paths()
    command = f"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName {name}
$info = Get-ScheduledTaskInfo -TaskName {name}
$sessionTask = Get-ScheduledTask -TaskName {session_name}
$sessionInfo = Get-ScheduledTaskInfo -TaskName {session_name}
[ordered]@{{
    task_name = $task.TaskName
    state = [string]$task.State
    next_run_time = if ($info.NextRunTime) {{ $info.NextRunTime.ToString('s') }} else {{ '' }}
    last_run_time = if ($info.LastRunTime) {{ $info.LastRunTime.ToString('s') }} else {{ '' }}
    last_task_result = $info.LastTaskResult
    session_task_name = $sessionTask.TaskName
    session_task_state = [string]$sessionTask.State
    session_next_run_time = if ($sessionInfo.NextRunTime) {{ $sessionInfo.NextRunTime.ToString('s') }} else {{ '' }}
    session_last_run_time = if ($sessionInfo.LastRunTime) {{ $sessionInfo.LastRunTime.ToString('s') }} else {{ '' }}
    session_last_task_result = $sessionInfo.LastTaskResult
    session_lock_friendly = (
        $sessionTask.Actions.Count -eq 1 -and
        $sessionTask.Actions[0].Execute -eq {_ps_quote(str(pythonw))} -and
        $sessionTask.Actions[0].Arguments -eq {_ps_quote('"' + str(helper) + '"')} -and
        [string]$sessionTask.Settings.ExecutionTimeLimit -eq 'PT0S' -and
        [string]$sessionTask.Principal.RunLevel -eq 'Limited'
    )
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
    source_current = helper.is_file() and helper.read_bytes() == SESSION_AWAKE_SOURCE.read_bytes()
    payload["status"] = "INSTALLED" if payload.get("session_lock_friendly") and source_current else "OUTDATED"
    return payload


def main() -> int:
    # The parent launcher captures this process with a UTF-8 decoder. Without
    # this, a GBK stdout pipe can raise while printing the original exception.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
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
