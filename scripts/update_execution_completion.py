#!/usr/bin/env python3
"""重建实盘成交完成率明细和一笔交易一行汇总。"""
from __future__ import annotations

import argparse
import csv
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
    row_count = 0
    if summary_path.exists():
        with summary_path.open("r", newline="", encoding="utf-8-sig") as handle:
            row_count = sum(1 for _ in csv.DictReader(handle))
    print(f"真实成交完成率汇总已更新：{summary_path}（{row_count}笔）")
    print(f"买入逐片明细：{tracker.buy_path}")
    print(f"卖出逐片明细：{tracker.sell_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
