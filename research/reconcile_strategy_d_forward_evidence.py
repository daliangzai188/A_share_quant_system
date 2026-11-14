#!/usr/bin/env python3
"""用事务意图账本修复策略D信号文件中的最终委托事实。

只处理D信号CSV，事务意图数据库是委托状态权威来源。脚本不连接QMT、不撤单、
不下单；可重复执行，已一致的记录不会改写。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data_cleaner import DataCleaner


DEFAULT_SIGNAL_DIR = ROOT / "reports/strategy_d"
DEFAULT_INTENT_DB = ROOT / "data/state/execution_events.sqlite3"
DEFAULT_FILL_FALLBACK = ROOT / "data/processed/fill_rate_fallback.csv"
DEFAULT_CONFIG = ROOT / "config/config.json"


def _load_intents(path: Path, start_date: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT broker_order_id, business_date, status, filled_qty,
                   filled_amount, avg_fill_price, error_message, updated_at
            FROM trade_intents
            WHERE strategy_leg = 'D'
              AND business_date >= ?
              AND broker_order_id IS NOT NULL
              AND broker_order_id != ''
            """,
            (start_date,),
        ).fetchall()
    finally:
        connection.close()
    return {str(row["broker_order_id"]): dict(row) for row in rows}


def _final_signal_status(intent: dict[str, Any]) -> str:
    status = str(intent.get("status", "")).upper()
    filled_qty = int(intent.get("filled_qty", 0) or 0)
    if status == "CANCELLED":
        return "PARTIAL_FILLED_CANCELLED" if filled_qty > 0 else "CANCELLED_NO_FILL"
    if status == "FILLED":
        return "FILLED"
    if status == "REJECTED":
        return "REJECTED_BY_QMT"
    if status == "FAILED":
        return "ORDER_EXCEPTION"
    return status


def _fallback_sample_lookup(path: Path) -> dict[tuple[str, str, str, str], tuple[int, bool]]:
    if not path.exists():
        return {}
    table = pd.read_csv(path, dtype={"limit_times_bucket": str}, low_memory=False)
    lookup: dict[tuple[str, str, str, str], tuple[int, bool]] = {}
    for _, row in table.iterrows():
        key = (
            str(row.get("market_segment", "")),
            str(row.get("limit_times_bucket", "")),
            str(row.get("board_type", "")),
            str(row.get("first_time_bucket", "")),
        )
        enough = str(row.get("is_sample_enough", False)).strip().lower() in {
            "true", "1", "yes"
        }
        lookup[key] = (int(row.get("sample_count", 0) or 0), enough)
    return lookup


def reconcile(
    *,
    start_date: str,
    signal_dir: Path = DEFAULT_SIGNAL_DIR,
    intent_db: Path = DEFAULT_INTENT_DB,
    fill_fallback_path: Path = DEFAULT_FILL_FALLBACK,
    config_path: Path = DEFAULT_CONFIG,
) -> dict[str, Any]:
    intents = _load_intents(intent_db, start_date)
    fallback = _fallback_sample_lookup(fill_fallback_path)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    min_group_samples = int(config.get("fill_model", {}).get("min_group_samples", 0))
    files_seen = rows_seen = rows_changed = statuses_reconciled = samples_reconciled = 0

    for path in sorted(signal_dir.glob("intraday_signals_*.csv")):
        trade_date = path.stem.rsplit("_", 1)[-1]
        if len(trade_date) != 8 or trade_date < start_date:
            continue
        files_seen += 1
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        if frame.empty:
            continue
        changed = False
        for index, row in frame.iterrows():
            if str(row.get("signal_type", "")) != "BUY":
                continue
            rows_seen += 1
            order_id = str(row.get("order_id", "")).strip()
            intent = intents.get(order_id)
            if intent and str(intent.get("business_date", "")) == trade_date:
                updates = {
                    "order_status": _final_signal_status(intent),
                    "order_status_text": f"TRADE_INTENT:{intent.get('status', '')}",
                    "filled_qty": int(intent.get("filled_qty", 0) or 0),
                    "filled_amount": float(intent.get("filled_amount", 0.0) or 0.0),
                    "avg_fill_price": float(intent.get("avg_fill_price", 0.0) or 0.0),
                    "order_status_updated_at": str(intent.get("updated_at", "")),
                }
                error = str(intent.get("error_message", "") or "").strip()
                if error:
                    updates["failure_reason"] = error
                status_changed = False
                for column, value in updates.items():
                    if column not in frame.columns:
                        frame[column] = ""
                    if str(frame.at[index, column]) != str(value):
                        frame.at[index, column] = value
                        changed = True
                        status_changed = True
                statuses_reconciled += int(status_changed)

            source = str(row.get("fill_matched_source", ""))
            if source.startswith("fallback"):
                segment = DataCleaner.classify_market_segment(row.get("ts_code", ""))
                key = (
                    segment,
                    "1",
                    "multi_open",
                    str(row.get("first_time_bucket", "")),
                )
                sample = fallback.get(key)
                if sample:
                    sample_count, sample_enough = sample
                    sample_changed = False
                    for column, value in {
                        "fill_sample_count": sample_count,
                        "fill_min_group_samples": min_group_samples,
                        "fill_sample_evidence_source": "fill_rate_fallback",
                        "fill_sample_enough_reconciled": sample_enough,
                    }.items():
                        if column not in frame.columns:
                            frame[column] = ""
                        if str(frame.at[index, column]) != str(value):
                            frame.at[index, column] = value
                            changed = True
                            sample_changed = True
                    samples_reconciled += int(sample_changed)

        if changed:
            temp_path = path.with_suffix(path.suffix + ".tmp")
            frame.to_csv(temp_path, index=False)
            temp_path.replace(path)
            rows_changed += 1

    return {
        "start_date": start_date,
        "files_seen": files_seen,
        "buy_rows_seen": rows_seen,
        "files_changed": rows_changed,
        "order_status_rows_reconciled": statuses_reconciled,
        "sample_evidence_rows_reconciled": samples_reconciled,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="修复策略D前向执行证据")
    parser.add_argument("--start-date", default="20260903")
    args = parser.parse_args()
    result = reconcile(start_date=str(args.start_date).replace("-", "")[:8])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
