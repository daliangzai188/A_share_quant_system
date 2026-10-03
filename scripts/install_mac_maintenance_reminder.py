#!/usr/bin/env python3
"""安装不访问 Desktop 的每月维护/方案甲复核提醒，并在 launchd 环境里实测能运行。

旧任务 com.asystem.maintenance 直接执行桌面上的脚本，macOS 隐私保护拒绝 launchd
后台进程读取 Desktop，每次退出码2，提醒从未发出（2026-10-03 季度体检提醒漏发）。
本安装器与 install_mac_vm_watchdog.py 同一做法：脚本复制到
~/Library/Application Support/A_System/，Bark 地址存权限0600的本机配置，不进仓库。
安装后用一次性 launchd 任务执行 --check，确认后台环境确实能读到脚本和配置；
不通过就报错，不再出现“装上了但从没跑成功”。

运行（Mac 终端）：/usr/bin/python3 scripts/install_mac_maintenance_reminder.py
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "scripts" / "mac_monthly_maintenance_reminder.py"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "A_System"
INSTALLED_SCRIPT = SUPPORT_DIR / "mac_monthly_maintenance_reminder.py"
CONFIG_PATH = SUPPORT_DIR / "maintenance_reminder.json"
LOG_PATH = SUPPORT_DIR / "maintenance_reminder.log"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.asystem.maintenance.plist"
LABEL = "com.asystem.maintenance"
CHECK_LABEL = "com.asystem.maintenance.check"


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


def plist_payload() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(INSTALLED_SCRIPT)],
        "StartCalendarInterval": {"Weekday": 6, "Hour": 10, "Minute": 0},
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }


def _launchd_check(uid: str) -> str:
    """在真实 launchd 后台环境执行 --check（不推送），返回输出。"""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "check.out"
        subprocess.run(["launchctl", "remove", CHECK_LABEL], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ["launchctl", "submit", "-l", CHECK_LABEL, "-o", str(out), "-e", str(out), "--",
             "/usr/bin/python3", str(INSTALLED_SCRIPT), "--check"],
            check=True,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
                if "MAINTENANCE_REMINDER_" in text or "Error" in text:
                    return text.strip()
                time.sleep(0.5)
            return out.read_text(encoding="utf-8", errors="replace").strip() if out.exists() else ""
        finally:
            subprocess.run(["launchctl", "remove", CHECK_LABEL], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install() -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("提醒任务只能在macOS安装")
    bark_url = _read_bark_url()
    if not bark_url:
        raise RuntimeError(".env中未找到BARK_URL，无法安装提醒")

    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, INSTALLED_SCRIPT)
    os.chmod(INSTALLED_SCRIPT, 0o700)
    CONFIG_PATH.write_text(json.dumps({"bark_url": bark_url}, ensure_ascii=False), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)

    with PLIST_PATH.open("wb") as handle:
        plistlib.dump(plist_payload(), handle, sort_keys=False)
    uid = str(os.getuid())
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    loaded = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
                            check=False, text=True, capture_output=True)
    if loaded.returncode != 0:
        raise RuntimeError(loaded.stderr.strip() or "launchctl bootstrap失败")

    check_output = _launchd_check(uid)
    if "MAINTENANCE_REMINDER_READY" not in check_output:
        raise RuntimeError(f"launchd后台实测未通过：{check_output or '无输出'}")
    return {
        "status": "INSTALLED",
        "label": LABEL,
        "schedule": "每周六10:00（Mac本地时间），仅每月第一个周六推送；1/4/7/10月加推方案甲复核",
        "runtime_location": str(INSTALLED_SCRIPT),
        "log": str(LOG_PATH),
        "desktop_tcc_dependency": False,
        "launchd_check": check_output,
    }


def main() -> int:
    try:
        print(json.dumps(install(), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
