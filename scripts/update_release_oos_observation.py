#!/usr/bin/env python3
"""收盘后更新发布版本旁路观察；永不参与交易决策。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.shadow_candidate_ledger import collect_signal_date, load_release, upsert_ledger  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="更新发布版本样本外旁路观察")
    parser.add_argument("--signal-date", required=True, help="信号日YYYYMMDD")
    args = parser.parse_args()

    release = load_release(PROJECT_ROOT)
    rows = collect_signal_date(PROJECT_ROOT, release, args.signal_date)
    ledger = upsert_ledger(PROJECT_ROOT, rows)
    report_status = "REPORT_NOT_INSTALLED"
    # 第二阶段报告模块部署后由同一个旁路步骤自动更新，交易主流程无需再次改动。
    try:
        from src.release_oos_robustness import write_release_oos_report  # type: ignore
        from src.release_oos_monitor import record_and_maybe_remind

        report = write_release_oos_report(PROJECT_ROOT)
        report_status = str(report.get("status", "UPDATED"))
        monitor = record_and_maybe_remind(PROJECT_ROOT, args.signal_date, report)
        for line in monitor["log_lines"]:
            print(line)
        print(
            f"[OOS提醒] 周报日={monitor['is_weekly_report_day']} "
            f"本周已推送={monitor['weekly_notification_sent']}"
        )
    except ImportError:
        pass
    payload = {
        "status": "UPDATED" if rows else "PRE_OOS_SKIPPED",
        "release_id": release["release_id"],
        "signal_date": args.signal_date,
        "signal_rows": len(rows),
        "ledger_rows": len(ledger),
        "report_status": report_status,
        "trading_side_effects": False,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
