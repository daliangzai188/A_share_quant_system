#!/usr/bin/env python3
"""重建实盘成交完成率明细和一笔交易一行汇总。"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.execution_completion_tracker import ExecutionCompletionTracker


def main() -> int:
    parser = argparse.ArgumentParser(description="更新真实成交完成率跟踪报表")
    parser.add_argument(
        "--rebuild-only",
        action="store_true",
        help="只根据已经持久化的数据重建汇总，不兼容回填旧日志",
    )
    args = parser.parse_args()

    tracker = ExecutionCompletionTracker(PROJECT_ROOT)
    summary_path = (
        tracker.rebuild_summary() if args.rebuild_only else tracker.backfill_existing()
    )
    event_store_audit = tracker.mirror_existing_events()
    event_store_audit_path = (
        PROJECT_ROOT / "reports" / "execution_tracking" / "event_store_audit.json"
    )
    event_store_audit_path.parent.mkdir(parents=True, exist_ok=True)
    event_store_audit_path.write_text(
        json.dumps(event_store_audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    row_count = 0
    if summary_path.exists():
        with summary_path.open("r", newline="", encoding="utf-8-sig") as handle:
            row_count = sum(1 for _ in csv.DictReader(handle))
    print(f"真实成交完成率汇总已更新：{summary_path}（{row_count}笔）")
    print(f"买入逐片明细：{tracker.buy_path}")
    print(f"卖出逐片明细：{tracker.sell_path}")
    print(
        "事务镜像账本："
        f"{tracker.event_store_path}（{event_store_audit['status']}，"
        f"最新事件{event_store_audit['event_head_count']}条，"
        f"历史修订{event_store_audit['event_revision_count']}条）"
    )
    print(f"镜像完整性报告：{event_store_audit_path}")
    if event_store_audit["status"] != "PASS":
        raise RuntimeError("SQLite执行事件镜像与CSV不一致；不影响权威账本，但必须排查审计链")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
