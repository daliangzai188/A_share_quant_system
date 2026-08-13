#!/usr/bin/env python3
"""独立于项目目录的 VMware 虚拟机哨兵。

安装后本文件会被复制到 ``~/Library/Application Support/A_System``。运行时只读
该目录中的配置并调用 VMware ``vmrun list``，不访问 macOS 受保护的 Desktop；
从而避免旧版 launchd 哨兵因 TCC ``Operation not permitted`` 永久退出。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo


SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "A_System"
CONFIG_PATH = SUPPORT_DIR / "vm_watchdog.json"
STATE_PATH = SUPPORT_DIR / "vm_watchdog_state.json"
BEIJING = ZoneInfo("Asia/Shanghai")


def _load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_state(payload: dict) -> None:
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(STATE_PATH, 0o600)


def _notify(config: dict, title: str, body: str, *, critical: bool = True) -> bool:
    base = str(config.get("bark_url", "") or "").strip().rstrip("/")
    if not base:
        return False
    query = "group=A股实盘&sound=alarm&level=critical" if critical else "group=A股实盘&level=active"
    url = f"{base}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}?{query}"
    try:
        urllib.request.urlopen(url, timeout=20).read(1)
        return True
    except Exception:
        return False


def _running_vms(vmrun: str) -> set[str]:
    result = subprocess.run(
        [vmrun, "list"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "vmrun list失败")
    return {
        str(Path(line.strip()).expanduser().resolve())
        for line in result.stdout.splitlines()[1:]
        if line.strip().lower().endswith(".vmx")
    }


def _in_morning_recovery_window(now: datetime) -> bool:
    # 无法从受保护Desktop读交易日历，按周一至周五兜底；节假日多启动一次VM
    # 没有交易风险，daemon自身仍以真实交易日历决定是否下单。
    return now.weekday() < 5 and clock_time(7, 45) <= now.time() <= clock_time(9, 10)


def main() -> int:
    config = _load_json(CONFIG_PATH)
    if not config:
        return 2
    state = _load_json(STATE_PATH)
    now = datetime.now(BEIJING)
    now_ts = time.time()
    vmrun = str(config.get("vmrun", "") or "")
    vmx = str(Path(str(config.get("vmx", "") or "")).expanduser().resolve())
    alert_gap = max(int(config.get("alert_gap_sec", 3600) or 3600), 300)

    try:
        running = vmx in _running_vms(vmrun)
    except Exception as exc:
        last_error = float(state.get("last_vmrun_error_alert", 0.0) or 0.0)
        if now_ts - last_error >= alert_gap and _notify(
            config,
            "🛑 Mac无法检查Windows虚拟机",
            f"VMware vmrun检查失败：{exc}。请确认VMware Fusion可用。",
        ):
            state["last_vmrun_error_alert"] = now_ts
            _save_state(state)
        return 1

    if running:
        if state.get("was_down"):
            _notify(
                config,
                "✅ Windows虚拟机已恢复运行",
                "Mac独立哨兵已确认目标VM重新运行。仍请确认QMT已登录、A_System已收到“程序与账户恢复正常”通知。",
                critical=False,
            )
        _save_state({"was_down": False, "last_seen_running": now.isoformat(timespec="seconds")})
        return 0

    state["was_down"] = True
    last_alert = float(state.get("last_down_alert", 0.0) or 0.0)
    if now_ts - last_alert >= alert_gap and _notify(
        config,
        "🛑 Windows交易虚拟机未运行",
        "Mac独立哨兵确认VMware中的交易虚拟机已关闭/停止。工作日上午会尝试自动启动；"
        "启动后仍需确认QMT登录和A_System恢复通知。",
    ):
        state["last_down_alert"] = now_ts

    auto_start = bool(config.get("auto_start_weekday_morning", True))
    last_start = float(state.get("last_auto_start_attempt", 0.0) or 0.0)
    if auto_start and _in_morning_recovery_window(now) and now_ts - last_start >= 900:
        state["last_auto_start_attempt"] = now_ts
        try:
            result = subprocess.run(
                [vmrun, "start", vmx, "nogui"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=120,
            )
            if result.returncode == 0:
                _notify(
                    config,
                    "🔄 已自动启动Windows交易虚拟机",
                    "工作日早盘兜底已执行VMware启动。请等待QMT和A_System恢复通知；若10分钟内没有，请人工登录检查。",
                )
            else:
                _notify(
                    config,
                    "🛑 自动启动交易虚拟机失败",
                    (result.stderr.strip() or result.stdout.strip() or "vmrun返回失败")[:500],
                )
        except Exception as exc:
            _notify(config, "🛑 自动启动交易虚拟机异常", str(exc)[:500])
    _save_state(state)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
