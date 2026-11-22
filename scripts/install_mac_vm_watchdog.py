#!/usr/bin/env python3
"""安装不访问 Desktop 的 VMware 虚拟机独立哨兵。

运行（Mac 终端，项目目录）：/usr/bin/python3 scripts/install_mac_vm_watchdog.py --no-auto-start
--no-auto-start：只报警，不在工作日早上自动开虚拟机（开机后 Windows 仍要人登录，自己恢复不了交易）。

2026-11-23：心跳文件和过期线只由 scripts/mac_vm_watchdog.py 决定，这里不再写进配置；装完等哨兵第一次检查，
把结果一起打印出来（first_check），不再出现“装上了但从没检查成功”。
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "scripts" / "mac_vm_watchdog.py"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "A_System"
INSTALLED_SCRIPT = SUPPORT_DIR / "mac_vm_watchdog.py"
CONFIG_PATH = SUPPORT_DIR / "vm_watchdog.json"
STATE_PATH = SUPPORT_DIR / "vm_watchdog_state.json"   # 和 mac_vm_watchdog.STATE_PATH 相同（测试核对）
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.asystem.vm-watchdog.plist"
LABEL = "com.asystem.vm-watchdog"
LEGACY_LABEL = "com.asystem.watchdog"
DEFAULT_VMRUN = Path("/Applications/VMware Fusion.app/Contents/Library/vmrun")


def _read_bark_url() -> str:
    env_path = PROJECT_ROOT / ".env"
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("BARK_URL="):
                return line.split("=", 1)[1].strip().strip("\"'").rstrip("/")
    except OSError:
        pass  # 可以忽略：.env 读不了按没配置推送处理
    return ""


def _detect_vmx(vmrun: Path) -> Path | None:
    try:
        result = subprocess.run(
            [str(vmrun), "list"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
        )
        for line in result.stdout.splitlines()[1:]:
            candidate = Path(line.strip()).expanduser()
            if candidate.suffix.lower() == ".vmx" and candidate.exists():
                return candidate.resolve()
    except Exception:
        pass  # 可以忽略：自动探测虚拟机文件失败时走手工指定路径
    roots = [Path.home() / "Virtual Machines.localized", Path.home() / "Virtual Machines"]
    for root in roots:
        if root.exists():
            matches = list(root.glob("**/*.vmx"))
            if len(matches) == 1:
                return matches[0].resolve()
    return None


def _syncthing_heartbeat_config() -> dict:
    """发现已有本机同步服务；凭据只存于权限0600的本机配置，不进入仓库。"""
    source = Path.home() / "Library" / "Application Support" / "Syncthing" / "config.xml"
    root = ET.parse(source).getroot()
    gui = root.find("gui")
    if gui is None:
        raise RuntimeError("Syncthing缺少本机API配置")
    address = gui.findtext("address", "")
    if not address.startswith(("127.0.0.1:", "localhost:", "[::1]:")) or gui.get("tls") == "true":
        raise RuntimeError("心跳检查要求已有的本机HTTP Syncthing接口")
    matches = [folder for folder in root.findall("folder")
               if Path(folder.get("path", "")).expanduser().resolve() == PROJECT_ROOT]
    key = gui.findtext("apikey", "")
    if len(matches) != 1 or not key:
        raise RuntimeError("未能唯一识别项目同步目录或本机API密钥")
    # 心跳文件和过期线不写进配置，由 mac_vm_watchdog.py 的 HEARTBEAT_FILE、HEARTBEAT_STALE_SEC 决定（2026-11-23）
    return {"syncthing_url": "http://" + address, "syncthing_api_key": key,
            "syncthing_folder": matches[0].get("id")}


def first_check(started_at: float, *, timeout: float = 30.0, state_path: Path = STATE_PATH) -> dict:
    """等哨兵装完后的第一次检查（launchd 加载时立即跑一次），返回它记下的结果。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if state_path.stat().st_mtime >= started_at:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                beat = state.get("heartbeat") or {}
                status = state.get("runtime_status", "")
                if state.get("was_down"):
                    status = "VM_NOT_RUNNING"
                return {"runtime_status": status or "UNKNOWN", "heartbeat_file": beat.get("file"),
                        "heartbeat_age_sec": beat.get("age_sec")}
        except (OSError, ValueError):
            pass  # 可以忽略：哨兵还没写完状态文件，下一秒再读
        time.sleep(1)
    return {"runtime_status": "NO_CHECK_YET", "hint": "30秒内没等到第一次检查，看 /tmp/com.asystem.vm-watchdog.err"}


def install(*, vmx: Path | None, auto_start: bool) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("Mac独立哨兵只能在macOS安装")
    vmrun = DEFAULT_VMRUN.resolve()
    if not vmrun.exists():
        raise RuntimeError(f"未找到VMware vmrun：{vmrun}")
    resolved_vmx = vmx.expanduser().resolve() if vmx else _detect_vmx(vmrun)
    if not resolved_vmx or not resolved_vmx.exists():
        raise RuntimeError("未能唯一识别交易虚拟机.vmx，请用 --vmx 指定")
    bark_url = _read_bark_url()
    if not bark_url:
        raise RuntimeError(".env中未找到BARK_URL，无法安装独立告警")
    heartbeat_config = _syncthing_heartbeat_config()

    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, INSTALLED_SCRIPT)
    os.chmod(INSTALLED_SCRIPT, 0o700)
    config = {
        "vmrun": str(vmrun),
        "vmx": str(resolved_vmx),
        "bark_url": bark_url,
        "alert_gap_sec": 3600,
        "auto_start_weekday_morning": bool(auto_start),
        **heartbeat_config,
    }
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(INSTALLED_SCRIPT)],
        "StartInterval": 300,
        "RunAtLoad": True,
        "StandardOutPath": "/tmp/com.asystem.vm-watchdog.out",
        "StandardErrorPath": "/tmp/com.asystem.vm-watchdog.err",
    }
    with PLIST_PATH.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)

    uid = str(os.getuid())
    # 旧哨兵直接从Desktop执行Python，在现代macOS会被TCC拒绝并每5分钟退出码2；
    # 新哨兵启用后卸载旧job，保留旧plist文件便于审计，不删除用户数据。
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LEGACY_LABEL}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(PLIST_PATH)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started_at = time.time()
    loaded = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
        check=False,
        text=True,
        capture_output=True,
    )
    if loaded.returncode != 0:
        raise RuntimeError(loaded.stderr.strip() or "launchctl bootstrap失败")
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "status": "INSTALLED",
        "label": LABEL,
        "vmx": str(resolved_vmx),
        "interval_sec": 300,
        "auto_start_weekday_morning": bool(auto_start),
        "runtime_location": str(INSTALLED_SCRIPT),
        "desktop_tcc_dependency": False,
        "first_check": first_check(started_at),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vmx", type=Path)
    parser.add_argument(
        "--no-auto-start",
        action="store_true",
        help="只告警，不在工作日07:45-09:10自动启动VM",
    )
    args = parser.parse_args()
    try:
        result = install(vmx=args.vmx, auto_start=not args.no_auto_start)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
