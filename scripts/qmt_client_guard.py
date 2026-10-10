#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QMT 客户端守护。仅拉起客户端和普通登录，不操作交易程序或委托。

Windows: python qmt_client_guard.py --install --project C:\\A_System
凭据由 --configure 弹出的本机窗口加密保存，绝不经过参数、日志或同步目录。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

TASK_NAME = "A_System_QmtClientGuard"
BEIJING = timezone(timedelta(hours=8))


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def private_dir():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise RuntimeError("Windows LOCALAPPDATA is required")
    return Path(base) / "A_System" / "qmt_client_recovery"


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def powershell(args, timeout=20):
    return subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", *args], capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
    )


@dataclass
class RecoveryState:
    failures: int = 0
    next_attempt: float = 0
    password_error: bool = False
    credential_version: int = 0


def choose_action(observed, state, now, settings):
    """纯状态机：身份/桌面/验证码不确定时不输入，不把进程或端口当成在线。"""
    if not settings.get("enabled", False):
        return "disabled"
    if observed.get("manual_stop"):
        return "manual_stop"
    if observed.get("kind") == "observation_error":
        return "observation_error"
    version = int(observed.get("credential_version", 0))
    if version != state.credential_version:
        state.password_error = False
        state.failures = 0
        state.next_attempt = 0
        state.credential_version = version
    if observed.get("kind") == "main":
        if observed.get("broker_online"):
            state.failures = 0
            state.next_attempt = 0
            state.password_error = False
            return "client_online"
        return "waiting_native_reconnect"
    if observed.get("password_error"):
        state.password_error = True
    if state.password_error:
        return "password_error"
    if now < state.next_attempt:
        return "backoff"
    if observed.get("kind") == "missing":
        return "launch"
    if observed.get("locked"):
        return "waiting_unlock"
    if observed.get("kind") != "login":
        return "window_unverified"
    if observed.get("captcha"):
        return "waiting_captcha"
    if not observed.get("credentials_ready"):
        return "waiting_credentials"
    if not observed.get("controls_supported"):
        return "controls_unsupported"
    return "login"


def record_attempt(state, now, settings):
    delays = settings["retry_delays_seconds"]
    state.next_attempt = now + float(delays[min(state.failures, len(delays) - 1)])
    state.failures += 1


def validate_settings(settings):
    exe = Path(settings["qmt_exe"])
    if exe.name.lower() != "xtitclient.exe":
        raise ValueError("Only the full QMT XtItClient.exe is supported")
    interval = float(settings.get("check_interval_seconds", 30))
    delays = settings.get("retry_delays_seconds", [])
    if not 10 <= interval <= 300 or not delays or any(float(x) < 60 for x in delays):
        raise ValueError("Invalid recovery intervals")
    if not settings.get("project_root"):
        raise ValueError("project_root is required")
    return settings


def broker_online_from_log(qmt_root, started_at):
    """读取最近会话状态，脱机成功和 TCP 连通不算登录。仅返回布尔值。"""
    latest = []
    current = datetime.now(BEIJING).date()
    for day in (current - timedelta(days=1), current):
        log = Path(qmt_root) / "userdata" / "log" / ("XtClient_" + day.strftime("%Y%m%d") + ".log")
        try:
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 262144))
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            stamp = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
            status = re.search(r"(?:onRelogin|onUserStatus)[^\r\n]*?status\s*[:=]\s*(\d+)", line)
            if not stamp or not status:
                continue
            try:
                at = datetime.fromisoformat(stamp.group(1)).replace(tzinfo=BEIJING).timestamp()
            except ValueError:
                continue
            if at >= float(started_at):
                latest.append((at, int(status.group(1))))
    # 12: 在线；22: 断开。没有正面证据时保持“未验证”。
    return bool(latest and max(enumerate(latest), key=lambda item: (item[1][0], item[0]))[1][1] == 12)


def helper(home, settings, action="inspect"):
    result = powershell([
        "-File", str(home / "qmt_login_controls.ps1"), "-Action", action,
        "-ExePath", str(settings["qmt_exe"]),
        "-CredentialPath", str(home / "credentials.json"),
    ], timeout=15)
    if result.returncode:
        # 不回显第三方错误文本，避免异常里带窗口值或凭据。
        raise RuntimeError("QMT_CONTROL_HELPER_FAILED")
    return json.loads(result.stdout.strip().splitlines()[-1])


def observe(home, settings):
    observed = helper(home, settings)
    observed["manual_stop"] = (Path(settings["project_root"]) / ".manual_stop.json").exists()
    credential = home / "credentials.json"
    observed["credentials_ready"] = credential.is_file()
    observed["credential_version"] = credential.stat().st_mtime_ns if credential.is_file() else 0
    observed["broker_online"] = bool(
        observed.get("kind") == "main" and broker_online_from_log(
            Path(settings["qmt_exe"]).parent.parent, observed.get("started_at", time.time()))
    )
    # 只检查当前会话中明确的密码错误文字，既不输出整条日志也不抓取密码。
    log = Path(settings["qmt_exe"]).parent.parent / "userdata" / "log" / ("XtClient_" + datetime.now(BEIJING).strftime("%Y%m%d") + ".log")
    try:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 65536))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        for line in lines:
            if not re.search(r"密码错误|密码不正确|密码.{0,12}锁定|账户.{0,12}锁定", line):
                continue
            stamp = re.search(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", line)
            if stamp and datetime.fromisoformat(stamp.group()).replace(tzinfo=BEIJING).timestamp() >= max(float(observed.get("started_at", time.time())), observed["credential_version"] / 1e9):
                observed["password_error"] = True
    except OSError:
        pass
    return observed


def acquire_mutex():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    mutex = kernel.CreateMutexW(None, False, "Local\\A_System_QmtClientGuard")
    if not mutex:
        raise RuntimeError("CLIENT_GUARD_MUTEX_FAILED")
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle(mutex)
        return None
    return mutex


def run_guard(home):
    mutex = acquire_mutex()
    if mutex is None:
        return 0
    logger = logging.getLogger("qmt_client_guard")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(home / "guard.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    try:
        saved = json.loads((home / "state.json").read_text(encoding="utf-8"))
        state = RecoveryState(**saved)
    except (OSError, ValueError, TypeError):
        state = RecoveryState()
    last_action = ""
    while True:
        settings = validate_settings(json.loads((home / "config.json").read_text(encoding="utf-8-sig")))
        try:
            observed = observe(home, settings)
        except Exception as exc:
            observed = {"kind": "observation_error", "error_type": type(exc).__name__}
        now = time.time()
        action = choose_action(observed, state, now, settings)
        if action in ("launch", "login"):
            # 先持久化节流，即使辅助程序超时或守护进程被重启也不密集撞登录。
            record_attempt(state, now, settings)
            atomic_json(home / "state.json", vars(state))
            try:
                if action == "launch":
                    exe = Path(settings["qmt_exe"])
                    if not exe.is_file():
                        raise FileNotFoundError("QMT_EXE_MISSING")
                    subprocess.Popen([str(exe)], cwd=str(exe.parent),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
                    action = "client_launched_unverified"
                else:
                    if observed.get("backend") == "native":
                        from qmt_win32_login import login_native
                        outcome = login_native(observed, settings["qmt_exe"], home / "credentials.json")
                    else:
                        outcome = helper(home, settings, "login")
                    action = outcome.get("result", "login_unverified")
            except Exception as exc:
                action = "attempt_failed"
                logger.warning("attempt_failed type=%s", type(exc).__name__)
        atomic_json(home / "state.json", vars(state))
        status = dict(time=datetime.now(BEIJING).isoformat(), pid=os.getpid(), enabled=settings["enabled"],
                      state=action, client_pid=observed.get("pid"),
                      broker_online=bool(observed.get("broker_online")),
                      next_attempt_at=state.next_attempt, attempts=state.failures,
                      manual_stop=bool(observed.get("manual_stop")), sends_orders=False)
        atomic_json(home / "status.json", status)
        if action != last_action:
            logger.info("state=%s client_pid=%s", action, observed.get("pid"))
            last_action = action
        if action == "disabled":
            return 0
        time.sleep(float(settings["check_interval_seconds"]))


def install(home, project, exe):
    home.mkdir(parents=True, exist_ok=True)
    # 只停止旧版客户端守护，避免替换文件后旧 Python 实例仍占单例锁。
    stopped = powershell(["-Command", "$ErrorActionPreference='Stop'; $task=Get-ScheduledTask -TaskName " + ps_quote(TASK_NAME) + " -ErrorAction SilentlyContinue; if($null -ne $task){ $task | Stop-ScheduledTask -ErrorAction Stop }; exit 0"], timeout=20)
    if stopped.returncode:
        raise RuntimeError("OLD_CLIENT_GUARD_STOP_FAILED")
    here = Path(__file__).resolve().parent
    for name in ("qmt_client_guard.py", "qmt_win32_login.py", "qmt_login_controls.ps1", "configure_qmt_credentials.ps1"):
        source, target = here / name, home / name
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
    cfg = {"enabled": True, "project_root": str(Path(project).resolve()), "qmt_exe": str(exe),
           "check_interval_seconds": 30, "retry_delays_seconds": [60, 180, 600, 1800]}
    validate_settings(cfg)
    if not Path(exe).is_file():
        raise FileNotFoundError("QMT executable was not found")
    atomic_json(home / "config.json", cfg)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise FileNotFoundError("pythonw.exe was not found")
    command = f"""
$ErrorActionPreference='Stop'
$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$action=New-ScheduledTaskAction -Execute {ps_quote(pythonw)} -Argument {ps_quote('"' + str(home / 'qmt_client_guard.py') + '" --run')}
$triggers=@((New-ScheduledTaskTrigger -AtLogOn -User $user),(New-ScheduledTaskTrigger -Daily -At '08:00'))
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'QMT client recovery only; no order or daemon controls' -Force | Out-Null
Start-ScheduledTask -TaskName {ps_quote(TASK_NAME)}
"""
    result = powershell(["-Command", command], timeout=30)
    if result.returncode:
        raise RuntimeError("QMT_GUARD_TASK_INSTALL_FAILED")
    print(json.dumps({"task": TASK_NAME, "installed": True, "status_file": str(home / "status.json")}, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ("install", "run", "status", "disable", "configure"):
        modes.add_argument("--" + name, action="store_true")
    parser.add_argument("--project", default=r"C:\A_System")
    parser.add_argument("--qmt-exe", default=r"C:\QMT\GJQMT\bin.x64\XtItClient.exe")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("This tool runs inside Windows only.")
        return 2
    for stream in (sys.stdout, sys.stderr):
        if callable(getattr(stream, "reconfigure", None)):
            stream.reconfigure(encoding="utf-8")
    home = private_dir()
    if args.install:
        return install(home, args.project, args.qmt_exe)
    if args.run:
        return run_guard(home)
    if args.configure:
        # 用户在该窗口内输入；本进程不读取、不打印密码。
        return subprocess.call(["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                                "-File", str(home / "configure_qmt_credentials.ps1"),
                                "-CredentialPath", str(home / "credentials.json")])
    if args.disable:
        cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
        cfg["enabled"] = False
        atomic_json(home / "config.json", cfg)
        result = powershell(["-Command", "Disable-ScheduledTask -TaskName " + ps_quote(TASK_NAME) + " | Out-Null"])
        return result.returncode
    print((home / "status.json").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("QMT_CLIENT_GUARD_ERROR: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
