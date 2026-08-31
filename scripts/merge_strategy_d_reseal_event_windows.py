#!/usr/bin/env python3
"""只读拼接两个不重叠D回封事件窗口，生成三年研究事件账本。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def portable_path(path: Path) -> str:
    """项目内文件使用相对路径写入审计报告，项目外文件才保留绝对路径。"""

    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def read_events(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    required = {
        "event_id", "trade_date", "ts_code", "signal_hhmm",
        "open_times_at_signal", "queue_price_confirmed", "execution_status",
        "exit_date", "account_return",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"D回封窗口缺少字段：{path} {missing}")
    frame["trade_date"] = frame["trade_date"].astype(str).str.replace(
        r"\.0$", "", regex=True
    )
    return frame


def atomic_write(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


def expected_open_dates(
    calendar_path: Path,
    *,
    start: str,
    end: str,
) -> list[str]:
    """读取目标窗口交易日；D事件合并必须逐日完整覆盖该集合。"""

    if str(start) > str(end):
        raise ValueError(f"D三年目标窗口无效：{start}>{end}")
    calendar = pd.read_csv(calendar_path, dtype={"cal_date": str}, low_memory=False)
    missing = sorted({"cal_date", "is_open"}.difference(calendar.columns))
    if missing:
        raise ValueError(f"交易日历缺少字段：{missing}")
    calendar["cal_date"] = calendar["cal_date"].astype(str).str.replace(
        r"\.0$", "", regex=True
    )
    opened = calendar[
        pd.to_numeric(calendar["is_open"], errors="coerce").eq(1)
        & calendar["cal_date"].between(str(start), str(end))
    ]
    dates = sorted(opened["cal_date"].unique())
    if not dates:
        raise RuntimeError(f"D三年目标窗口没有交易日：{start}~{end}")
    return dates


def merge(
    first_path: Path,
    second_path: Path,
    output_path: Path,
    *,
    calendar_path: Path,
    expected_start: str,
    expected_end: str,
) -> dict[str, object]:
    """合并两段按时间先后排列的事件，并对目标交易日做等值覆盖校验。"""

    first = read_events(first_path)
    second = read_events(second_path)
    overlap_dates = sorted(set(first["trade_date"]) & set(second["trade_date"]))
    if overlap_dates:
        raise RuntimeError(f"D回封窗口交易日重叠：{overlap_dates[:5]}")
    if set(first.columns) != set(second.columns):
        missing_first = sorted(set(second.columns) - set(first.columns))
        missing_second = sorted(set(first.columns) - set(second.columns))
        raise RuntimeError(
            f"D回封窗口字段不一致：first_missing={missing_first} second_missing={missing_second}"
        )
    first_dates = set(first["trade_date"].astype(str))
    second_dates = set(second["trade_date"].astype(str))
    if not first_dates or not second_dates:
        raise RuntimeError("D三年合并的两个源窗口都必须非空")
    if max(first_dates) >= min(second_dates):
        raise RuntimeError(
            "D回封源窗口必须按时间先后且不得交错："
            f"first_last={max(first_dates)} second_first={min(second_dates)}"
        )
    target_dates = expected_open_dates(
        calendar_path,
        start=str(expected_start),
        end=str(expected_end),
    )
    actual_dates = first_dates | second_dates
    missing_dates = sorted(set(target_dates) - actual_dates)
    unexpected_dates = sorted(actual_dates - set(target_dates))
    if missing_dates or unexpected_dates:
        raise RuntimeError(
            "D回封合并未完整覆盖目标交易日："
            f"missing={missing_dates[:5]} unexpected={unexpected_dates[:5]}"
        )
    combined = pd.concat([first[second.columns], second], ignore_index=True)
    combined = combined.sort_values(
        ["trade_date", "signal_hhmm", "open_times_at_signal", "ts_code"]
    ).reset_index(drop=True)
    duplicate_path_count = int(
        combined.duplicated(
            ["trade_date", "ts_code", "signal_hhmm", "open_times_at_signal"]
        ).sum()
    )
    if duplicate_path_count:
        raise RuntimeError(f"D三年回封路径重复：{duplicate_path_count}")
    combined["event_id"] = range(len(combined))
    atomic_write(combined, output_path)
    return {
        "schema_version": 1,
        "mode": "research_only",
        "formal_strategy_modified": False,
        "window": {
            "expected_start": str(expected_start),
            "expected_end": str(expected_end),
            "expected_trade_day_count": len(target_dates),
            "coverage_passed": True,
        },
        "sources": [
            {
                "path": portable_path(first_path),
                "sha256": sha256(first_path),
                "first_trade_date": str(first["trade_date"].min()),
                "last_trade_date": str(first["trade_date"].max()),
                "event_count": int(len(first)),
            },
            {
                "path": portable_path(second_path),
                "sha256": sha256(second_path),
                "first_trade_date": str(second["trade_date"].min()),
                "last_trade_date": str(second["trade_date"].max()),
                "event_count": int(len(second)),
            },
        ],
        "output_path": portable_path(output_path),
        "output_sha256": sha256(output_path),
        "first_trade_date": str(combined["trade_date"].min()),
        "last_trade_date": str(combined["trade_date"].max()),
        "event_count": int(len(combined)),
        "event_trade_day_count": int(combined["trade_date"].nunique()),
        "duplicate_path_count": duplicate_path_count,
        "queue_depth_available": False,
        "certification_eligible": False,
        "status": "THREE_YEAR_RESEARCH_EVENT_CACHE_READY",
    }


def resolve(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拼接D回封事件研究窗口")
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument(
        "--calendar",
        type=Path,
        default=ROOT / "data/raw/trade_calendar.csv",
    )
    parser.add_argument("--expected-start", required=True, help="目标窗口左边界（含）")
    parser.add_argument("--expected-end", required=True, help="目标窗口右边界（含）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = resolve(args.output)
    payload = merge(
        resolve(args.first),
        resolve(args.second),
        output,
        calendar_path=resolve(args.calendar),
        expected_start=str(args.expected_start).replace("-", ""),
        expected_end=str(args.expected_end).replace("-", ""),
    )
    summary = resolve(args.summary)
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
