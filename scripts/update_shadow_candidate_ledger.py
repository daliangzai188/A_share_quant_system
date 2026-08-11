#!/usr/bin/env python3
"""更新发布版本影子候选与反事实收益账本（只读交易系统）。"""
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
    parser = argparse.ArgumentParser(description="更新全策略影子候选与反事实收益账本")
    parser.add_argument("--signal-date", help="信号日YYYYMMDD；不填时只刷新已有候选的未来收益")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="项目根目录（测试/迁移使用）")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    release = load_release(root)
    rows = collect_signal_date(root, release, args.signal_date) if args.signal_date else []
    frame = upsert_ledger(root, rows)
    signal_rows = frame[frame["signal_date"].astype(str).eq(str(args.signal_date))] if args.signal_date else frame
    payload = {
        "status": "UPDATED" if rows or not args.signal_date else "PRE_OOS_SKIPPED",
        "release_id": release["release_id"],
        "oos_start_date": release["oos_start_date"],
        "signal_date": args.signal_date or "",
        "ledger_rows": len(frame),
        "signal_rows": len(signal_rows),
        "candidate_count": int(signal_rows["candidate_status"].astype(str).eq("CANDIDATE").sum()) if not signal_rows.empty else 0,
        "resolved_count": int(frame["counterfactual_status"].astype(str).eq("RESOLVED").sum()) if not frame.empty else 0,
        "output": str(root / "reports" / "oos_shadow" / "shadow_candidates.csv"),
        "trading_side_effects": False,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
