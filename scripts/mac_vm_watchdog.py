#!/usr/bin/env python3
"""独立于项目目录的 VMware 虚拟机哨兵。

安装后本文件会被复制到 ``~/Library/Application Support/A_System``。运行时只读
该目录中的配置并调用 VMware ``vmrun list``，不访问 macOS 受保护的 Desktop；
从而避免旧版 launchd 哨兵因 TCC ``Operation not permitted`` 永久退出。
通过本机 Syncthing 索引核对交易心跳，避免把“VM开着但Windows未登录”判为恢复。
"""
from __future__ import annotations

import json
import os
import re
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
    params = {"group": "A股实盘", "level": "critical" if critical else "active"}
    if critical:
        params["sound"] = "alarm"
    # urllib不能发送含未转义中文的请求路径；否则告警会在本机编码阶段失败。
    query = urllib.parse.urlencode(params)
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


def _heartbeat_status(config: dict, now_ts: float) -> dict:
    """只读本机同步索引；旧文件、断同步和无法检查都不能冒充交易程序健康。

    使用 local 而非 global 记录：Windows刚发布但Mac尚未收到的心跳不算验收。
    不直接读取Desktop，也不保存Windows密码或尝试绕过登录。
    """
    base = str(config.get("syncthing_url", "") or "").rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("未配置本机Syncthing心跳检查地址")
    key = str(config.get("syncthing_api_key", "") or "")
    folder = str(config.get("syncthing_folder", "") or "")
    if not key or not folder:
        raise ValueError("Syncthing心跳检查配置不完整")
    query = urllib.parse.urlencode({"folder": folder, "file": "logs/daemon_heartbeat.txt"})
    request = urllib.request.Request(base + "/rest/db/file?" + query, headers={"X-API-Key": key})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    local = payload.get("local") or {}
    if not local or any(local.get(k) for k in ("deleted", "invalid", "ignored", "mustRescan")):
        raise ValueError("本机同步索引中没有有效的交易心跳")
    timestamp = str(local.get("modified", "")).replace("Z", "+00:00")
    # Syncthing/Windows输出7~9位小数，macOS自带Python 3.9仅接受3或6位。
    timestamp = re.sub(r"\.(\d+)(?=[+-])", lambda m: "." + m.group(1)[:6].ljust(6, "0"), timestamp)
    modified = datetime.fromisoformat(timestamp)
    if modified.tzinfo is None:
        raise ValueError("交易心跳时间缺少时区")
    age = now_ts - modified.timestamp()
    if age < -120:
        raise ValueError("交易心跳时间超前，请检查Windows和Mac时钟")
    stale_sec = max(int(config.get("heartbeat_stale_sec", 900)), 120)
    return {"status": "HEALTHY" if age <= stale_sec else "STALE",
            "age_sec": max(0, int(age)), "last_modified": modified.isoformat()}


def _check_runtime(config: dict, state: dict, now: datetime, now_ts: float, alert_gap: int) -> int:
    """VM运行与交易运行分开记录；失败只告警，不重置VM或重启交易会话。"""
    try:
        runtime = _heartbeat_status(config, now_ts)
    except Exception as exc:
        # 不把HTTP请求对象/配置写入日志，避免泄露本机API密钥。
        runtime = {"status": "UNKNOWN", "reason": type(exc).__name__}
    previous = state.get("runtime_status")
    state.update({"was_down": False, "last_seen_running": now.isoformat(timespec="seconds"),
                  "runtime_status": runtime["status"], "runtime_checked_at": now.isoformat(timespec="seconds"),
                  "heartbeat": runtime})
    if runtime["status"] == "HEALTHY":
        if previous in {"STALE", "UNKNOWN"}:
            _notify(config, "✅ 交易程序心跳已恢复", "Mac独立哨兵已确认交易心跳重新更新。", critical=False)
        state.pop("last_runtime_alert", None)
        _save_state(state)
        return 0
    last_alert = float(state.get("last_runtime_alert", 0.0) or 0.0)
    if now_ts - last_alert >= alert_gap:
        detail = (f"交易心跳已有{runtime['age_sec'] // 60}分钟未更新。"
                  if runtime["status"] == "STALE" else "无法读取本机同步心跳索引，交易程序状态未知。")
        if _notify(config, "🛑 虚拟机开着，但交易程序未确认运行",
                   detail + "请检查Windows是否停在登录界面，以及daemon/keeper和同步是否运行。"):
            state["last_runtime_alert"] = now_ts
    _save_state(state)
    return 1


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
                "Mac独立哨兵已确认目标VM重新运行。仍请确认A_System已恢复心跳。",
                critical=False,
            )
        # 2026-09-15：Windows夜间重启后VM始终在list中，旧版在此直接返回成功，
        # 导致登录界面上的整夜停机既未恢复也未告警。必须继续核对交易心跳。
        return _check_runtime(config, state, now, now_ts, alert_gap)

    state["was_down"] = True
    last_alert = float(state.get("last_down_alert", 0.0) or 0.0)
    if now_ts - last_alert >= alert_gap and _notify(
        config,
        "🛑 Windows交易虚拟机未运行",
        "Mac独立哨兵确认VMware中的交易虚拟机已关闭/停止。工作日上午会尝试自动启动；"
        "启动后仍需确认A_System心跳恢复。",
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
                    "工作日早盘兜底已执行VMware启动。请等待A_System心跳恢复；若10分钟内没有，请人工登录检查。",
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
