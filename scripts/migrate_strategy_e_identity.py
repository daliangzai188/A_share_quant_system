#!/usr/bin/env python3
"""把旧 E2 运行产物迁移为唯一策略腿 E；不删除旧审计文件。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy_identity import (  # noqa: E402
    ACTIVE_E_VARIANT,
    normalize_e_variant,
    normalize_strategy_frame,
    normalize_strategy_record,
)


LEGACY_SIGNAL_DIR = PROJECT_ROOT / "reports" / "strategy_e2"
SIGNAL_DIR = PROJECT_ROOT / "reports" / "strategy_e"
LEGACY_CAPACITY_PATH = PROJECT_ROOT / "data" / "processed" / "e2_capacity_history.json"
CAPACITY_PATH = PROJECT_ROOT / "data" / "processed" / "e_capacity_history.json"


def _atomic_json(payload: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _normalize_rows(payload: dict[str, Any], key: str) -> dict[str, Any]:
    result = dict(payload)
    rows = []
    for row in payload.get(key, []):
        if not isinstance(row, dict):
            continue
        normalized = normalize_strategy_record(row)
        if normalized.get("strategy_leg") != "E":
            normalized["strategy_leg"] = "E"
            normalized["strategy_family"] = "E"
            date_value = normalized.get("signal_date") or normalized.get("trade_date")
            normalized["strategy_variant"] = normalize_e_variant(
                normalized.get("strategy_variant"), signal_date=date_value
            )
        rows.append(normalized)
    result[key] = rows
    result["strategy_family"] = "E"
    result["active_strategy_variant"] = ACTIVE_E_VARIANT
    result["legacy_source"] = str(LEGACY_SIGNAL_DIR.relative_to(PROJECT_ROOT))
    return result


def migrate_json(source_name: str, target_name: str, row_key: str) -> int:
    source = LEGACY_SIGNAL_DIR / source_name
    if not source.exists():
        return 0
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{source}根节点不是对象")
    normalized = _normalize_rows(payload, row_key)
    _atomic_json(normalized, SIGNAL_DIR / target_name)
    return len(normalized.get(row_key, []))


def migrate_candidate_csvs() -> int:
    count = 0
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    for source in sorted(LEGACY_SIGNAL_DIR.glob("e2_signal_*_candidates.csv")):
        try:
            frame = pd.read_csv(source, low_memory=False)
        except EmptyDataError:
            # 旧脚本用0字节文件表示当日无候选；运行状态已在runs账本中保留，
            # 不把这种无表头文件复制进新的正式目录。
            continue
        if "strategy_leg" not in frame.columns:
            frame.insert(0, "strategy_leg", "E")
        frame = normalize_strategy_frame(frame)
        if "strategy_family" not in frame.columns:
            frame.insert(1, "strategy_family", "E")
        date_text = source.name.removeprefix("e2_signal_").removesuffix("_candidates.csv")
        frame["strategy_variant"] = normalize_e_variant("", signal_date=date_text)
        target = SIGNAL_DIR / source.name.replace("e2_signal_", "e_signal_", 1)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        frame.to_csv(temporary, index=False, encoding="utf-8-sig")
        os.replace(temporary, target)
        count += 1
    return count


def migrate_capacity() -> int:
    if not LEGACY_CAPACITY_PATH.exists() or CAPACITY_PATH.exists():
        return 0
    payload = json.loads(LEGACY_CAPACITY_PATH.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        payload["records"] = [
            normalize_strategy_record(row, default_e_variant=ACTIVE_E_VARIANT)
            if isinstance(row, dict)
            else row
            for row in payload["records"]
        ]
        payload["strategy_family"] = "E"
        payload["strategy_variant"] = ACTIVE_E_VARIANT
        payload["legacy_source"] = str(LEGACY_CAPACITY_PATH.relative_to(PROJECT_ROOT))
    _atomic_json(payload, CAPACITY_PATH)
    return 1


def main() -> None:
    signal_count = migrate_json("e2_signals_recent.json", "e_signals_recent.json", "signals")
    run_count = migrate_json("e2_signal_runs_recent.json", "e_signal_runs_recent.json", "runs")
    csv_count = migrate_candidate_csvs()
    capacity_count = migrate_capacity()
    print(
        "策略E身份迁移完成："
        f"signals={signal_count}，runs={run_count}，candidate_csv={csv_count}，"
        f"capacity_file={capacity_count}。旧文件只读保留，未删除。"
    )


if __name__ == "__main__":
    main()
