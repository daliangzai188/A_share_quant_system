#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QMT 客户端守护。仅拉起客户端和普通登录，不操作交易程序或委托。

Windows: python qmt_client_guard.py --install --project C:\\A_System
默认沿用 QMT 原生登录，不额外保存密码。旧凭据后端必须明确选择才启用。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import hashlib
import math
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
    connection: dict = field(default_factory=dict)


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
    if settings.get("login_mode", "native_only") == "native_only":
        return "waiting_native_login"
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
    if (not isinstance(settings.get("enabled"), bool) or not 10 <= interval <= 300
            or not isinstance(delays, list) or not delays
            or any(not math.isfinite(float(x)) or not 60 <= float(x) <= 86400 for x in delays)):
        raise ValueError("Invalid recovery intervals")
    if not settings.get("project_root"):
        raise ValueError("project_root is required")
    if settings.get("login_mode", "native_only") not in ("native_only", "local_credentials"):
        raise ValueError("Invalid login mode")
    budget = int(settings.get("log_scan_bytes_per_check", 8 * 1024 * 1024))
    if not 65536 <= budget <= 64 * 1024 * 1024:
        raise ValueError("Invalid log scan budget")
    preferred = settings.get("preferred_login_server", {})
    if preferred and (not isinstance(preferred, dict)
                      or not re.fullmatch(r"[a-zA-Z0-9.:[\]-]+", str(preferred.get("host", "")))
                      or not 1 <= int(preferred.get("port", 0)) <= 65535):
        raise ValueError("Invalid preferred login server")
    return settings


_LOG_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d{1,6})?)")


def decode_log(raw):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


def parse_connection_event(line, started_at, now, login_port=56001):
    """只返回公开连接字段；Qt statusNow 与旧 status 兼容，不保存日志正文。"""
    stamp = _LOG_STAMP.search(line)
    if not stamp:
        return None
    try:
        at = datetime.fromisoformat(stamp.group(1).replace(",", ".")).replace(tzinfo=BEIJING).timestamp()
    except ValueError:
        return None
    if at < float(started_at) or at > now + 5:
        return None
    status = re.search(r"\bCProxyClient::slot_loginStatus\][^\r\n]*?\bstatusNow\s*=\s*(\d+)", line)
    if not status:
        status = re.search(r"\b(?:onRelogin|onUserStatus)\b[^\r\n]*?\bstatus\s*[:=]\s*(\d+)", line)
    if status:
        return dict(at=at, status=int(status.group(1)), source="session_status")
    remote = re.search(r"\bremote[ _]addr\s*[:=]\s*(\[[0-9a-fA-F:]+\]|[a-zA-Z0-9.-]+):(\d+)\b", line)
    if remote and int(remote.group(2)) == int(login_port):
        endpoint = dict(host=remote.group(1).strip("[]"), port=int(remote.group(2)))
        if re.search(r"\[(?:handleRecvData|(?:\w+::)*onDisconnected)\]", line):
            return dict(at=at, status=22, source="login_connection_lost", endpoint=endpoint)
        if re.search(r"\[(?:\w+::)*onConnected\]", line):
            # TCP 成功只确认线路，不得把账户标记在线。
            return dict(at=at, status=None, source="login_tcp_connected", endpoint=endpoint)
    return None


def connection_from_log(qmt_root, started_at, cache, *, session_id="", now=None,
                        scan_budget=8 * 1024 * 1024, login_port=56001):
    """增量读取，持久化游标；重启、截断、轮转均重新验证，预算未扫完时不报在线。

    日志的十六进制字段是线程字段，不用它冒充 PID。会话绑定来自独立窗口观察。
    """
    now = time.time() if now is None else float(now)
    start = float(started_at)
    if not math.isfinite(start) or start <= 0 or start > now + 5:
        cache.clear()
        return dict(online=False, state="unverified", caught_up=False)
    key = [str(session_id), start]
    if cache.get("session") != key:
        cache.clear()
        cache.update(session=key, files={}, latest=None, endpoint=None)
    current = datetime.fromtimestamp(now, BEIJING).date()
    paths = [Path(qmt_root) / "userdata" / "log" / ("XtClient_" + day.strftime("%Y%m%d") + ".log")
             for day in (current - timedelta(days=1), current)]
    caught_up = True
    budget = int(scan_budget)
    for path in paths:
        name = path.name
        previous = cache["files"].get(name, {})
        try:
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = [stat.st_dev, stat.st_ino]
                offset = int(previous.get("offset", 0))
                rotated = bool(previous and (previous.get("identity") != identity or stat.st_size < offset))
                if offset and not rotated:
                    stream.seek(max(0, offset - 64))
                    anchor = hashlib.sha256(stream.read(min(64, offset))).hexdigest()
                    rotated = anchor != previous.get("anchor")
                if rotated:
                    # 老文件里的成功不能盖住新文件尚未读取的断线。
                    cache["latest"] = None
                    cache["endpoint"] = None
                    offset = 0
                if budget <= 0 and stat.st_size > offset:
                    caught_up = False
                    continue
                stream.seek(offset)
                chunk = stream.read(max(0, budget))
                if offset + len(chunk) < stat.st_size or (chunk and not chunk.endswith(b"\n")):
                    end = chunk.rfind(b"\n") + 1
                    chunk = chunk[:end]
                budget -= len(chunk)
                for line in decode_log(chunk).splitlines():
                    event = parse_connection_event(line, start, now, login_port)
                    if event is None:
                        continue
                    if "endpoint" in event:
                        cache["endpoint"] = event["endpoint"]
                    if "status" in event and (not cache["latest"] or event["at"] >= cache["latest"]["at"]):
                        cache["latest"] = {k: event[k] for k in ("at", "status", "source")}
                offset += len(chunk)
                stream.seek(max(0, offset - 64))
                cache["files"][name] = dict(identity=identity, offset=offset,
                    anchor=hashlib.sha256(stream.read(min(64, offset))).hexdigest())
                if offset < os.fstat(stream.fileno()).st_size:
                    caught_up = False
        except FileNotFoundError:
            if previous:
                cache["latest"] = None
                cache["endpoint"] = None
                cache["files"].pop(name, None)
        except OSError:
            caught_up = False
    cache["files"] = {p.name: cache["files"][p.name] for p in paths if p.name in cache["files"]}
    latest = cache.get("latest") or {}
    online = bool(caught_up and latest.get("status") == 12)
    return dict(online=online, state="online" if online else
                "offline" if caught_up and latest.get("status") is not None else "unverified",
                caught_up=caught_up, last_status=latest.get("status"),
                last_status_at=latest.get("at"), source=latest.get("source"),
                endpoint=cache.get("endpoint"))


def broker_online_from_log(qmt_root, started_at):
    """兼容旧调用者；没有本次会话的明确在线事件就返回 False。"""
    return connection_from_log(qmt_root, started_at, {})["online"]


def inner_bridge_health(settings):
    """只读验签心跳，不连接柜台、不写 RPC、不启动模型或业务进程。"""
    if not settings.get("monitor_inner_bridge", True):
        return dict(state="not_monitored", healthy=False)
    try:
        project = str(Path(settings["project_root"]))
        if project not in sys.path:
            sys.path.insert(0, project)
        from qmt_inner.protocol import FileClient, load_settings
        from qmt_inner.engine import ENGINE_REVISION
        hb = FileClient(load_settings()).heartbeat()
        if hb.get("engine_revision") != ENGINE_REVISION or not hb.get("instance"):
            return dict(state="revision_unverified", healthy=False)
        return dict(state="heartbeat_verified", healthy=True, mode=hb.get("mode", ""))
    except FileNotFoundError:
        return dict(state="not_configured_or_not_running", healthy=False)
    except Exception as exc:
        # 不回显配置、token、账户或任意第三方异常正文。
        return dict(state="heartbeat_unavailable", healthy=False, error_type=type(exc).__name__)


def status_payload(observed, state, settings, action, now=None):
    connection = observed.get("connection", {})
    preferred = settings.get("preferred_login_server", {})
    actual = connection.get("endpoint") or {}
    endpoint_match = (actual.get("host") == preferred.get("host")
                      and actual.get("port") == preferred.get("port")) if actual and preferred else None
    return dict(time=datetime.fromtimestamp(time.time() if now is None else now, BEIJING).isoformat(),
                pid=os.getpid(), enabled=settings["enabled"], state=action,
                client_pid=observed.get("pid"), broker_online=bool(observed.get("broker_online")),
                next_attempt_at=state.next_attempt, attempts=state.failures,
                manual_stop=bool(observed.get("manual_stop")), sends_orders=False,
                login_mode=settings.get("login_mode", "native_only"),
                preferred_login_server=preferred, connection=connection,
                endpoint_matches_preferred=endpoint_match,
                inner_bridge=observed.get("inner_bridge", {}), starts_trading_daemon=False,
                requires_business_verification=True)


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


def observe(home, settings, state=None):
    observed = helper(home, settings)
    observed["manual_stop"] = (Path(settings["project_root"]) / ".manual_stop.json").exists()
    credential = home / "credentials.json"
    local_login = settings.get("login_mode", "native_only") == "local_credentials"
    observed["credentials_ready"] = local_login and credential.is_file()
    observed["credential_version"] = credential.stat().st_mtime_ns if observed["credentials_ready"] else 0
    state = state if state is not None else RecoveryState()
    observed["connection"] = connection_from_log(
        Path(settings["qmt_exe"]).parent.parent, observed.get("started_at", time.time()),
        state.connection, session_id=str(observed.get("pid", "")),
        scan_budget=settings.get("log_scan_bytes_per_check", 8 * 1024 * 1024),
        login_port=settings.get("preferred_login_server", {}).get("port", 56001))
    observed["broker_online"] = bool(observed.get("kind") == "main" and observed["connection"]["online"])
    observed["inner_bridge"] = inner_bridge_health(settings)
    # 只检查当前会话中明确的密码错误文字，既不输出整条日志也不抓取密码。
    log = Path(settings["qmt_exe"]).parent.parent / "userdata" / "log" / ("XtClient_" + datetime.now(BEIJING).strftime("%Y%m%d") + ".log")
    try:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 65536))
            lines = decode_log(stream.read()).splitlines()
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
            observed = observe(home, settings, state)
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
        status = status_payload(observed, state, settings, action, now)
        atomic_json(home / "status.json", status)
        if action != last_action:
            logger.info("state=%s client_pid=%s", action, observed.get("pid"))
            last_action = action
        if action == "disabled":
            return 0
        time.sleep(float(settings["check_interval_seconds"]))


def build_install_settings(project, exe, existing=None):
    """重装保留用户的启停、退避、登录方式和线路；公开项目配置作为首次安装默认。"""
    cfg = dict(enabled=True, check_interval_seconds=30,
               retry_delays_seconds=[60, 180, 600, 1800], login_mode="native_only",
               log_scan_bytes_per_check=8 * 1024 * 1024, monitor_inner_bridge=True)
    profile_path = Path(project) / "config" / "qmt_client_recovery.json"
    if profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        cfg.update(profile)
    if existing:
        cfg.update(existing)
    # 显式安装参数决定本机路径；不得沿用其他机器的旧目录。
    cfg.update(project_root=str(Path(project).resolve()), qmt_exe=str(exe))
    return validate_settings(cfg)


def public_connection_profile(settings):
    """只备份恢复参数和公开线路；不复制 QMT 账号配置、密码缓存或凭据。"""
    names = ("preferred_login_server", "native_server_failover", "login_mode",
             "check_interval_seconds", "retry_delays_seconds", "monitor_inner_bridge")
    return {name: settings[name] for name in names if name in settings}


def install(home, project, exe):
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.json"
    existing = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else None
    cfg = build_install_settings(project, exe, existing)
    if not Path(exe).is_file():
        raise FileNotFoundError("QMT executable was not found")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise FileNotFoundError("pythonw.exe was not found")
    # 只停止旧版客户端守护，避免替换文件后旧 Python 实例仍占单例锁。
    stopped = powershell(["-Command", "$ErrorActionPreference='Stop'; $task=Get-ScheduledTask -TaskName " + ps_quote(TASK_NAME) + " -ErrorAction SilentlyContinue; if($null -ne $task){ $task | Stop-ScheduledTask -ErrorAction Stop }; exit 0"], timeout=20)
    if stopped.returncode:
        raise RuntimeError("OLD_CLIENT_GUARD_STOP_FAILED")
    here = Path(__file__).resolve().parent
    for name in ("qmt_client_guard.py", "qmt_win32_login.py", "qmt_login_controls.ps1", "configure_qmt_credentials.ps1"):
        source, target = here / name, home / name
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
    if config_path.exists():
        shutil.copyfile(config_path, home / "config.before_install.json")
    atomic_json(home / "config.json", cfg)
    atomic_json(home / "connection_profile.json", public_connection_profile(cfg))
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
