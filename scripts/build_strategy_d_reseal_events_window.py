#!/usr/bin/env python3
"""从隔离的D分钟账本构建指定窗口全部14:55前回封事件。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from scripts.research_strategy_d_reseal_combinations import (  # noqa: E402
    attach_outcomes,
    extract_reseal_events,
)
from scripts.research_strategy_d_six_schools import OutcomeCache  # noqa: E402


LOGGER = logging.getLogger("build_strategy_d_reseal_events_window")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def portable_path(path: Path) -> str:
    """研究报告优先保存项目相对路径，便于跨机器核对哈希。"""

    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def load_ledger(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    required = {
        "trade_date", "ts_code", "name", "minute_status", "market_segment",
        "limit_price", "pre_close", "previous_trade_date", "daily_close",
        "closed_at_limit", "failed_to_close_at_limit",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"D隔离事件账本缺少字段：{missing}")
    frame["trade_date"] = frame["trade_date"].astype(str).str.replace(
        r"\.0$", "", regex=True
    )
    frame["ts_code"] = frame["ts_code"].astype(str)
    if frame.duplicated(["trade_date", "ts_code"]).any():
        raise ValueError("D隔离事件账本日期+代码重复")
    allowed_statuses = {
        "READY_1M_PATH_NO_QUEUE_DEPTH",
        "MISMATCH_DAILY_TOUCH_NOT_CONFIRMED",
        "MISSING_MINUTE_DATA",
    }
    invalid = ~frame["minute_status"].astype(str).isin(allowed_statuses)
    if invalid.any():
        counts = frame.loc[invalid, "minute_status"].value_counts().to_dict()
        raise RuntimeError(f"D隔离事件账本仍有未处理分钟状态：{counts}")
    fail_closed = ~frame["minute_status"].astype(str).eq(
        "READY_1M_PATH_NO_QUEUE_DEPTH"
    )
    if fail_closed.any():
        if "signal_rule_current" not in frame.columns or "execution_status" not in frame.columns:
            raise RuntimeError("D隔离事件账本缺少缺口fail-closed证明字段")
        leaked = frame.loc[fail_closed]
        signal_leaked = leaked["signal_rule_current"].astype(str).str.lower().isin(
            {"true", "1", "yes"}
        ).any()
        execution_leaked = ~leaked["execution_status"].astype(str).eq("NO_PATH_SIGNAL")
        if signal_leaked or execution_leaked.any():
            raise RuntimeError("D隔离分钟缺口未机械落为无信号")
    return frame.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


def build(
    *,
    ledger_path: Path,
    minute_path: Path,
    output_path: Path,
    summary_path: Path,
) -> dict[str, Any]:
    ledger = load_ledger(ledger_path)
    daily_data = strict.daily_data()
    events, extraction = extract_reseal_events(
        ledger,
        minute_path,
        daily_data,
        expected_event_count=None,
    )
    events = attach_outcomes(events, OutcomeCache(daily_data))
    events = events.sort_values(
        ["trade_date", "signal_hhmm", "open_times_at_signal", "ts_code"]
    ).reset_index(drop=True)
    events["event_id"] = range(len(events))
    if events["event_id"].duplicated().any():
        raise RuntimeError("D隔离回封事件event_id重复")
    atomic_write_csv(events, output_path)
    payload = {
        "schema_version": 1,
        "mode": "research_only",
        "formal_strategy_modified": False,
        "ledger_path": portable_path(ledger_path),
        "ledger_sha256": sha256(ledger_path),
        "minute_path": portable_path(minute_path),
        "minute_sha256": sha256(minute_path),
        "output_path": portable_path(output_path),
        "output_sha256": sha256(output_path),
        "first_trade_date": str(events["trade_date"].min()),
        "last_trade_date": str(events["trade_date"].max()),
        "ledger_target_count": int(len(ledger)),
        "event_count": int(len(events)),
        "event_stock_day_count": int(
            events[["trade_date", "ts_code"]].drop_duplicates().shape[0]
        ),
        "event_trade_day_count": int(events["trade_date"].nunique()),
        "queue_price_confirmed_count": int(
            events["queue_price_confirmed"].astype(str).str.lower().isin(
                {"true", "1", "yes"}
            ).sum()
        ),
        "extraction": extraction,
        "queue_depth_available": False,
        "certification_eligible": False,
        "status": "RESEARCH_EVENT_CACHE_READY_NO_QUEUE_DEPTH",
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建指定窗口D全部回封事件")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--minute-bars", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    return parser.parse_args()


def resolve(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    payload = build(
        ledger_path=resolve(args.ledger),
        minute_path=resolve(args.minute_bars),
        output_path=resolve(args.output),
        summary_path=resolve(args.summary),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
