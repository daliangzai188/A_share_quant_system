#!/usr/bin/env python3
"""建立D3半路买入的完整7%触发母池和新增一分钟采集目标。

D3不能只看最终触及涨停的股票。母池必须包含所有盘中最高价达到昨收7%以上、
但随后可能没有到涨停的失败样本。信号研究只能在分钟路径中寻找第一次穿越
7%/8%/9%的时点；日线最高价只负责确定需要采集哪些股票日，不能进入候选排序。

现有D分钟账本已覆盖所有首板触板股票日。本脚本同时输出完整D3母池和扣除现有
触板母池后的新增采集清单，避免重复下载约4万只次。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_strategy_d_intraday_event_ledger import (  # noqa: E402
    ALLOWED_SEGMENTS,
    DAILY_DIR,
    LIMIT_LIST_DIR,
    STK_LIMIT_DIR,
    build_mother_pool as build_d_touch_mother_pool,
    historical_name,
    historical_st_status,
    historical_window_days,
    limit_list_names,
    load_calendar,
    load_historical_identity_overrides,
    load_historical_names,
    load_stock_metadata,
)
from src.market_rules import market_segment  # noqa: E402
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402


OUTPUT_DIR = ROOT / "data/research/strategy_d3_halfway"
REPORT_DIR = ROOT / "reports/strategy_d3_halfway"
MOTHER_PATH = OUTPUT_DIR / "mother_pool_high_ge_7pct_full_window.csv"
ALL_TARGET_PATH = OUTPUT_DIR / "minute_target_manifest_all.csv"
NEW_TARGET_PATH = OUTPUT_DIR / "minute_target_manifest_new_non_touch.csv"
SUMMARY_PATH = REPORT_DIR / "mother_pool_summary.json"


def build_mother_pool() -> pd.DataFrame:
    calendar = load_calendar()
    date_index = {date: index for index, date in enumerate(calendar)}
    metadata = load_stock_metadata()
    names = load_historical_names()
    overrides = load_historical_identity_overrides()
    window = historical_window_days()
    market_map = window.set_index("trade_date").to_dict("index")
    rows: list[dict[str, Any]] = []

    for date in window["trade_date"].astype(str):
        calendar_index = date_index.get(date)
        daily_path = DAILY_DIR / f"{date}.csv"
        limit_path = STK_LIMIT_DIR / f"{date}.csv"
        if calendar_index is None or calendar_index == 0:
            raise RuntimeError(f"D3日期不在交易日历：{date}")
        if not daily_path.exists() or not limit_path.exists():
            raise FileNotFoundError(f"D3缺少日线或官方涨跌停价：{date}")
        previous_date = calendar[calendar_index - 1]
        previous_limit_path = LIMIT_LIST_DIR / f"{previous_date}.csv"
        if not previous_limit_path.exists():
            raise FileNotFoundError(f"D3缺少昨日涨停池：{previous_limit_path}")
        previous_limit = pd.read_csv(
            previous_limit_path, dtype={"ts_code": str}, usecols=["ts_code"]
        )
        previous_codes = set(previous_limit["ts_code"].astype(str))
        official = pd.read_csv(
            limit_path, dtype={"ts_code": str}, usecols=["ts_code", "up_limit"]
        )
        caps = dict(
            zip(
                official["ts_code"].astype(str),
                pd.to_numeric(official["up_limit"], errors="coerce"),
            )
        )
        daily = pd.read_csv(daily_path, dtype={"ts_code": str}, low_memory=False)
        close_names = limit_list_names(date)
        market = market_map[date]
        for row in daily.itertuples(index=False):
            code = str(row.ts_code)
            segment = market_segment(code)
            if code in previous_codes or segment not in ALLOWED_SEGMENTS:
                continue
            pre_close = float(row.pre_close or 0.0)
            high = float(row.high or 0.0)
            cap_value = caps.get(code)
            cap = float(cap_value) if pd.notna(cap_value) else 0.0
            # 7%阈值只用于建立采集分母。未来研究不得读取当天最终high做排序。
            if pre_close <= 0 or cap <= 0 or high < pre_close * 1.07 - 1e-9:
                continue
            name, name_source = historical_name(
                code, date, names, metadata, overrides
            )
            if name_source == "CURRENT_NAME_FALLBACK" and close_names.get(code):
                name = close_names[code]
                name_source = "LIMIT_LIST_D_SIGNAL_DATE"
            is_st, st_evidence = historical_st_status(
                name=name,
                name_source=name_source,
                segment=segment,
                pre_close=pre_close,
                cap=cap,
            )
            if is_st:
                continue
            touched_limit = bool(high >= cap - 1e-9)
            meta = metadata.get(code, {})
            rows.append(
                {
                    "trade_date": date,
                    "ts_code": code,
                    "name": name,
                    "historical_name_source": name_source,
                    "historical_st": False,
                    "historical_st_evidence": st_evidence,
                    "market_segment": segment,
                    "list_date": str(meta.get("list_date", "")),
                    "delist_date": str(meta.get("delist_date", "")),
                    "previous_trade_date": previous_date,
                    "previous_day_limit_up": False,
                    "pre_close": pre_close,
                    "limit_price": cap,
                    "daily_open": float(row.open or 0.0),
                    "daily_high": high,
                    "daily_low": float(row.low or 0.0),
                    "daily_close": float(row.close or 0.0),
                    "daily_volume": float(row.vol or 0.0),
                    "daily_amount": float(row.amount or 0.0),
                    "daily_high_return": high / pre_close - 1.0,
                    "daily_high_touched_limit": touched_limit,
                    "failed_to_touch_limit_after_reaching_7pct": not touched_limit,
                    "historical_market_sentiment_final_close_label": str(
                        market["market_sentiment_level"]
                    ),
                    "historical_limit_up_count_final_close_label": int(
                        market["limit_up_count"]
                    ),
                    "daily_high_used_only_for_collection_denominator": True,
                }
            )
    mother = pd.DataFrame(rows).sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    if mother.duplicated(["trade_date", "ts_code"]).any():
        raise RuntimeError("D3完整母池日期+代码重复")
    return mother


def target_manifest(mother: pd.DataFrame, *, role: str) -> pd.DataFrame:
    result = mother[
        [
            "trade_date",
            "ts_code",
            "name",
            "market_segment",
            "pre_close",
            "limit_price",
            "daily_high_touched_limit",
            "failed_to_touch_limit_after_reaching_7pct",
        ]
    ].copy()
    result["target_key"] = result["trade_date"] + "|" + result["ts_code"]
    result["required_start_hhmm"] = 930
    result["required_end_hhmm"] = 1500
    result["required_frequency"] = "1m"
    result["acceptance_role"] = role
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="建立D3全7%失败分母母池")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    mother = build_mother_pool()
    touch_mother = build_d_touch_mother_pool()[["trade_date", "ts_code"]].copy()
    touch_keys = set(
        touch_mother["trade_date"].astype(str) + "|" + touch_mother["ts_code"].astype(str)
    )
    mother["covered_by_existing_d_touch_minute_ledger"] = (
        mother["trade_date"].astype(str) + "|" + mother["ts_code"].astype(str)
    ).isin(touch_keys)
    touch = mother[mother["daily_high_touched_limit"]]
    # D触板母池有极少数上市初期/特殊涨停幅股票，官方涨停本身不足昨收7%，
    # 它们不属于D3“先到7%再半路”的定义。这里只要求D3触板子集全部能在
    # 现有D分钟账本找到，不反向要求两个母池数量相等。
    if not touch["covered_by_existing_d_touch_minute_ledger"].all():
        missing = sorted(
            set(touch["trade_date"] + "|" + touch["ts_code"]) - touch_keys
        )
        raise RuntimeError(
            "D3母池的触板子集必须与现有D完整触板母池一一一致："
            f"d3_touch={len(touch)} d_touch={len(touch_mother)} missing={missing[:5]}"
        )
    all_targets = target_manifest(mother, role="D3_ALL_REACHED_7PCT_DENOMINATOR")
    new_targets = target_manifest(
        mother[~mother["covered_by_existing_d_touch_minute_ledger"]].copy(),
        role="D3_REACHED_7PCT_BUT_NOT_LIMIT_NEW_MINUTE_TARGET",
    )
    mother.to_csv(output_dir / MOTHER_PATH.name, index=False, encoding="utf-8-sig")
    all_targets.to_csv(output_dir / ALL_TARGET_PATH.name, index=False, encoding="utf-8-sig")
    new_targets.to_csv(output_dir / NEW_TARGET_PATH.name, index=False, encoding="utf-8-sig")
    summary = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "protocol": STRICT_DISCOVERY,
        "window": "20240630~20260630",
        "strategy_school": "D3_HALF_WAY_7_TO_9_PERCENT",
        "formal_strategy_modified": False,
        "mother_count": int(len(mother)),
        "touch_limit_count_reusing_existing_minute": int(mother["daily_high_touched_limit"].sum()),
        "failed_to_touch_limit_count_new_minute_targets": int(len(new_targets)),
        "failed_denominator_rate": float(
            mother["failed_to_touch_limit_after_reaching_7pct"].mean()
        ),
        "duplicate_key_count": int(mother.duplicated(["trade_date", "ts_code"]).sum()),
        "daily_high_role": "COLLECTION_DENOMINATOR_ONLY_NOT_SIGNAL_OR_RANKING_FIELD",
        "all_target_path": str((output_dir / ALL_TARGET_PATH.name).relative_to(ROOT)),
        "new_target_path": str((output_dir / NEW_TARGET_PATH.name).relative_to(ROOT)),
        "release_eligible": False,
    }
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
