"""只读核验具名策略复核快照；不连接券商或行情服务。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(root: Path, verify_source_inputs: bool = False) -> dict:
    """核对原件字节、声明范围及三笔假设交易与统计的一致性。"""
    root = root.resolve()
    manifest = json.loads((root / "snapshot_manifest.json").read_text(encoding="utf-8"))
    records = manifest["snapshot_files"]
    assert len({item["path"] for item in records}) == len(records)
    for item in records:
        path = (root / item["path"]).resolve()
        assert path.is_relative_to(root), "快照文件路径越界"
        assert path.is_file() and path.stat().st_size == item["bytes"], item["path"]
        assert sha256(path) == item["sha256"], item["path"]

    review = json.loads((root / "strategy_review.json").read_text(encoding="utf-8"))
    assert review["status"] == "COMPLETE_STRATEGY_ONLY_RECENT_REVIEW"
    assert review["base_commit"] == manifest["source_base_commit"]
    assert review["as_of"] == manifest["captured_as_of"]
    assert review["requires_actual_account_trades"] is False
    assert review["production_writes"] is False and review["broker_calls_sent"] is False
    assert review["independent_ma_flag_differences"] == 0
    assert review["source_and_research_pool_verified"] is True
    assert review["factor_diagnostics"]["adjustment_fallbacks_in_retained_samples"] == 0
    for item in review["factor_diagnostics"]["health"].values():
        assert item["branches_used"] == item["branches_total"]

    forward = review["formal_forward"]
    assert forward["trading_days"] == forward["stop_days"]
    assert forward["assumed_initial_positions"] == forward["hypothetical_new_buys"] == 0
    assert forward["hypothetical_trade_return"] == forward["hypothetical_drawdown"] == 0
    assert forward["blocked_new_entry_legs"] == ["A", "C", "E", "D"]
    statistics = next(period["shadow"] for period in review["period_statistics"]
                      if period["shadow"]["start"] == forward["start"])
    with (root / "hypothetical_shadow_forward_trades.csv").open(encoding="utf-8-sig", newline="") as source:
        trades = list(csv.DictReader(source))
    assert len(trades) == statistics["sample_count"] == review["forward_fill_probability"]["sample_count"]
    assert all(row["status"] == "EXECUTED" and row["exit_date"] <= review["as_of"] for row in trades)
    assert all(0 <= float(row["fill_probability"]) <= 1 for row in trades)
    assert math.isclose(math.prod(1 + float(row["account_return"]) for row in trades) - 1,
                        statistics["compound_return"], rel_tol=1e-11, abs_tol=1e-11)

    excluded = review["unpriced_exclusion_classification"]
    assert excluded["unexplained_stock_days"] == 0
    assert sum(excluded["counts"].values()) == excluded["checked_stock_days"] == excluded["samples"] * 2
    assert excluded["counts"]["BSE_NOTICE_AND_CALENDAR_SUSPENSION_INFERENCE"] == excluded[
        "notice_inference_rows_explicitly_not_api_returned"]

    source_count = 0
    if verify_source_inputs:
        for name, digest in review["inputs_sha256"].items():
            path = Path(name)
            assert path.is_file() and sha256(path) == digest, name
            source_count += 1
    return {"status": "PASS_STRATEGY_REVIEW_SNAPSHOT", "snapshot_files_verified": len(records),
            "source_inputs_verified": source_count, "hypothetical_trades_verified": len(trades),
            "strategy_changed": False, "network_calls": False, "production_writes": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--verify-source-inputs", action="store_true",
                        help="本机保留原隔离输入时，额外核验报告记录的18份输入哈希")
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.verify_source_inputs), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
