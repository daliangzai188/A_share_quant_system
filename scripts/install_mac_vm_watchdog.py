#!/usr/bin/env python3
"""安装不访问 Desktop 的 VMware 虚拟机独立哨兵。"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "scripts" / "mac_vm_watchdog.py"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "A_System"
INSTALLED_SCRIPT = SUPPORT_DIR / "mac_vm_watchdog.py"
CONFIG_PATH = SUPPORT_DIR / "vm_watchdog.json"
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
        pass
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
        pass
    roots = [Path.home() / "Virtual Machines.localized", Path.home() / "Virtual Machines"]
    for root in roots:
        if root.exists():
            matches = list(root.glob("**/*.vmx"))
            if len(matches) == 1:
                return matches[0].resolve()
    return None


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

    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, INSTALLED_SCRIPT)
    os.chmod(INSTALLED_SCRIPT, 0o700)
    config = {
        "vmrun": str(vmrun),
        "vmx": str(resolved_vmx),
        "bark_url": bark_url,
        "alert_gap_sec": 3600,
        "auto_start_weekday_morning": bool(auto_start),
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
