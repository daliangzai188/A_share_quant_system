#!/usr/bin/env python3
"""逐笔审计N双分支上线前后的单账户占用路径与复利变化。"""
from __future__ import annotations

import argparse
from io import BytesIO
import json
from pathlib import Path
import subprocess
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CURRENT_PATH = ROOT / "reports/current_portfolio_alignment/portfolio_trades.csv"
OUTPUT_DIR = ROOT / "reports/strategy_n_v2_research"
KEY_COLUMNS = ["signal_date", "strategy_leg", "ts_code", "exit_date"]
BASELINE_COMMIT = "16cabfe"


def compound(frame: pd.DataFrame) -> float:
    returns = pd.to_numeric(frame["account_return"], errors="raise")
    return float((1.0 + returns).prod())


def load_git_csv(commit: str, relative_path: str) -> pd.DataFrame:
    result = subprocess.run(
        ["git", "show", f"{commit}:{relative_path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return pd.read_csv(BytesIO(result.stdout), low_memory=False)


def keyed(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in KEY_COLUMNS:
        result[column] = result[column].astype(str)
    result["path_key"] = result[KEY_COLUMNS].agg("|".join, axis=1)
    if result["path_key"].duplicated().any():
        raise RuntimeError("组合逐笔键重复，无法做一对一路径审计")
    return result


def leg_counts(frame: pd.DataFrame) -> dict[str, int]:
    counts = frame["strategy_leg"].astype(str).value_counts().sort_index()
    return {str(key): int(value) for key, value in counts.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-commit", default=BASELINE_COMMIT)
    args = parser.parse_args()

    relative = str(CURRENT_PATH.relative_to(ROOT))
    old = keyed(load_git_csv(args.baseline_commit, relative))
    new = keyed(pd.read_csv(CURRENT_PATH, low_memory=False))

    old_keys = set(old["path_key"])
    new_keys = set(new["path_key"])
    retained_keys = old_keys & new_keys
    lost_keys = old_keys - new_keys
    added_keys = new_keys - old_keys

    old_retained = old[old["path_key"].isin(retained_keys)].copy()
    new_retained = new[new["path_key"].isin(retained_keys)].copy()
    retained = old_retained[["path_key", "account_return"]].merge(
        new_retained[["path_key", "account_return"]],
        on="path_key",
        suffixes=("_old", "_new"),
        validate="one_to_one",
    )
    max_retained_return_diff = float(
        (
            pd.to_numeric(retained["account_return_old"], errors="raise")
            - pd.to_numeric(retained["account_return_new"], errors="raise")
        ).abs().max()
    )
    if max_retained_return_diff > 1e-12:
        raise RuntimeError(f"保留交易收益口径发生变化：最大差={max_retained_return_diff}")

    lost = old[old["path_key"].isin(lost_keys)].copy()
    added = new[new["path_key"].isin(added_keys)].copy()
    lost_multiple = compound(lost)
    added_multiple = compound(added)
    old_multiple = compound(old)
    new_multiple = compound(new)
    opportunity_ratio = added_multiple / lost_multiple
    reconstructed_new = old_multiple * opportunity_ratio
    if abs(new_multiple - reconstructed_new) > 1e-8:
        raise RuntimeError(
            f"逐笔复利恒等式不成立：正式={new_multiple}, 重建={reconstructed_new}"
        )

    details: list[pd.DataFrame] = []
    for label, frame in (("LOST", lost), ("ADDED", added)):
        part = frame[
            KEY_COLUMNS + ["name", "buy_date", "account_return", "return_source"]
        ].copy()
        part.insert(0, "path_change", label)
        details.append(part)
    detail = pd.concat(details, ignore_index=True).sort_values(
        ["signal_date", "path_change", "strategy_leg", "ts_code"]
    )

    summary: dict[str, Any] = {
        "baseline_commit": args.baseline_commit,
        "priority": "D>A>E>C>N",
        "old_trade_count": int(len(old)),
        "new_trade_count": int(len(new)),
        "retained_trade_count": int(len(retained_keys)),
        "lost_trade_count": int(len(lost_keys)),
        "added_trade_count": int(len(added_keys)),
        "net_trade_change": int(len(new) - len(old)),
        "old_leg_counts": leg_counts(old),
        "new_leg_counts": leg_counts(new),
        "lost_leg_counts": leg_counts(lost),
        "added_leg_counts": leg_counts(added),
        "old_equity_multiple": old_multiple,
        "new_equity_multiple": new_multiple,
        "lost_trade_multiple": lost_multiple,
        "added_trade_multiple": added_multiple,
        "added_over_lost_multiple_ratio": opportunity_ratio,
        "reconstructed_new_multiple": reconstructed_new,
        "max_retained_return_diff": max_retained_return_diff,
        "identity": "old_multiple * added_multiple / lost_multiple == new_multiple",
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    detail.to_csv(OUTPUT_DIR / "production_path_opportunity_cost.csv", index=False)
    (OUTPUT_DIR / "production_path_opportunity_cost.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
