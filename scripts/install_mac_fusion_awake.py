"""安装 Fusion 防空闲休眠守护；允许锁屏、息屏，不启动虚拟机。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import os


LABEL = "com.eass.a-system.fusion-awake"
DEFAULT_VMRUN = Path("/Applications/VMware Fusion.app/Contents/Library/vmrun")
VMX_EXECUTABLE = "/Applications/VMware Fusion.app/Contents/Library/vmware-vmx"
SOURCE = Path(__file__).with_name("guard_fusion_awake.sh")


def detect_vmx() -> Path:
    result = subprocess.run(
        [str(DEFAULT_VMRUN), "list"], capture_output=True, text=True,
        check=True, timeout=20,
    )
    paths = [Path(line.strip()) for line in result.stdout.splitlines()
             if line.strip().endswith(".vmx") and Path(line.strip()).is_file()]
    if len(paths) != 1:
        raise RuntimeError("请用 --vmx 指定唯一目标虚拟机的 .vmx 文件")
    return paths[0].resolve()


def process_pattern(vmx: Path) -> str:
    # pgrep uses extended regular expressions, so escape only ERE metacharacters.
    def escape(value: str) -> str:
        return re.sub(r"([\\.\[\]()*+?^$|{}])", r"\\\1", value)
    return "^" + escape(VMX_EXECUTABLE) + " .*" + escape(str(vmx)) + "$"


def install(vmx: Path, *, home: Path | None = None) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("本工具仅在 Mac 主机运行")
    vmx = vmx.expanduser().resolve()
    if not vmx.is_file() or vmx.suffix != ".vmx":
        raise RuntimeError("目标 .vmx 文件不存在")
    home = home or Path.home()
    support = home / "Library" / "Application Support" / "A_System" / "fusion_awake"
    support.mkdir(parents=True, exist_ok=True)
    helper = support / SOURCE.name
    helper.write_bytes(SOURCE.read_bytes())
    helper.chmod(0o700)
    plist = home / "Library" / "LaunchAgents" / (LABEL + ".plist")
    plist.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", str(helper), process_pattern(vmx)],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(support / "awake.log"),
        "StandardErrorPath": str(support / "awake_error.log"),
    }
    plist.write_bytes(plistlib.dumps(payload))
    domain = f"gui/{os.getuid()}"
    # Reuse the installed label, replacing the former machine-specific helper.
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"],
                   capture_output=True, check=False, timeout=20)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)],
                   capture_output=True, check=True, timeout=20)
    return {"status": "INSTALLED", "plist": str(plist), "vmx": str(vmx),
            "screen_lock_allowed": True, "display_sleep_allowed": True,
            "starts_vm": False, "closed_lid_supported": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vmx", type=Path, help="目标 .vmx 路径；省略时读取唯一运行中的虚拟机")
    args = parser.parse_args()
    try:
        if sys.platform != "darwin":
            raise RuntimeError("本工具仅在 Mac 主机运行")
        print(json.dumps(install(args.vmx or detect_vmx()), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
