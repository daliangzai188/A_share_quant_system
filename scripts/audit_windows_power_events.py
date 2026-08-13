# -*- coding: utf-8 -*-
"""只读导出 Windows 关机/重启/断电事件，判断虚拟机停止原因。"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "runtime" / "windows_power_events_latest.json"
POWER_EVENT_IDS = (41, 1074, 1076, 6005, 6006, 6008)


def classify_event(event_id: int) -> str:
    if event_id == 1074:
        return "PLANNED_SHUTDOWN_OR_RESTART"
    if event_id == 6006:
        return "CLEAN_EVENT_LOG_STOP"
    if event_id in {41, 6008}:
        return "UNEXPECTED_POWER_LOSS_OR_CRASH"
    if event_id == 6005:
        return "SYSTEM_BOOT_EVENT_LOG_START"
    if event_id == 1076:
        return "POST_UNEXPECTED_SHUTDOWN_REASON"
    return "OTHER"


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = []
    for event in events:
        event_id = int(event.get("Id", event.get("id", 0)) or 0)
        normalized.append(
            {
                "time_created": str(event.get("TimeCreated", event.get("time_created", "")) or ""),
                "event_id": event_id,
                "classification": classify_event(event_id),
                "provider": str(event.get("ProviderName", event.get("provider", "")) or ""),
                "level": str(event.get("LevelDisplayName", event.get("level", "")) or ""),
                "message": str(event.get("Message", event.get("message", "")) or ""),
            }
        )
    counts = Counter(item["classification"] for item in normalized)
    if counts["UNEXPECTED_POWER_LOSS_OR_CRASH"]:
        conclusion = "DETECTED_UNEXPECTED_POWER_LOSS_OR_CRASH"
    elif counts["PLANNED_SHUTDOWN_OR_RESTART"] or counts["CLEAN_EVENT_LOG_STOP"]:
        conclusion = "DETECTED_PLANNED_OR_CLEAN_SHUTDOWN"
    else:
        conclusion = "NO_DECISIVE_SHUTDOWN_EVENT_IN_RANGE"
    return {
        "status": "PASS",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event_ids": list(POWER_EVENT_IDS),
        "event_count": len(normalized),
        "classification_counts": dict(counts),
        "conclusion": conclusion,
        "events": normalized,
        "note": (
            "1074通常能看到发起关机/重启的进程；41/6008表示非正常断电、宿主强停或崩溃，"
            "仅凭项目日志无法进一步区分；6006表示事件日志正常停止。"
        ),
    }


def query_windows_events(days: int) -> list[dict[str, Any]]:
    ids = ",".join(str(value) for value in POWER_EVENT_IDS)
    command = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$events = Get-WinEvent -FilterHashtable @{{
    LogName='System';
    Id=@({ids});
    StartTime=(Get-Date).AddDays(-{max(int(days), 1)})
}} -ErrorAction SilentlyContinue | Sort-Object TimeCreated -Descending | Select-Object `
    @{{n='TimeCreated';e={{$_.TimeCreated.ToString('o')}}}}, Id, ProviderName, LevelDisplayName, Message
@($events) | ConvertTo-Json -Depth 4 -Compress
"""
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "读取System事件日志失败")
    raw = result.stdout.strip()
    if not raw:
        return []
    payload = json.loads(raw)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return [payload] if isinstance(payload, dict) else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="回看天数，默认7天")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if sys.platform != "win32":
        print("本脚本需要在 Windows 虚拟机 PowerShell 中运行。")
        return 2
    try:
        report = summarize(query_windows_events(args.days))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        public = {key: value for key, value in report.items() if key != "events"}
        public["report_path"] = str(args.output.resolve())
        print(json.dumps(public, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "ERROR", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
