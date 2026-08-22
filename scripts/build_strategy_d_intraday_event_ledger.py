#!/usr/bin/env python3
"""建立策略D完整首板触板母池和历史分钟事件账本。

该脚本从当前两年窗口内全部历史strong交易日出发，纳入所有允许板块、非ST、
昨日未涨停且当日最高价触及涨停的股票，包括收盘未封住的失败样本。它不会从
收盘涨停池反推母样本，因此不继承D旧回测最关键的幸存者偏差。

分钟数据可通过``--minute-bars``传入统一CSV。没有分钟数据时仍会生成完整母池、
采集目标和明确的缺失覆盖报告；绝不使用日线的最终first_time/last_time伪造盘中路径。

运行：

    # 第一步：生成6848只次完整采集目标
    python3 scripts/build_strategy_d_intraday_event_ledger.py --targets-only

    # 第二步：有分钟数据后重建事件账本
    python3 scripts/build_strategy_d_intraday_event_ledger.py \
      --minute-bars data/research/strategy_d_intraday/minute_5m_baostock.csv \
      --data-source BAOSTOCK_5M_APPROXIMATE

输出目录：``data/research/strategy_d_intraday``和
``reports/strategy_d_intraday_research``。

一分钟OHLCV可以确认开板后的涨停限价单必然可成交，但始终封板时没有历史队列
深度，仍必须标记未知；五分钟数据还存在同一bar内先后顺序歧义，只作覆盖预检。
"""
from __future__ import annotations

import argparse
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
from src.market_rules import (  # noqa: E402
    is_st_name,
    market_segment,
)
from src.strategy_d_intraday_ledger import (  # noqa: E402
    daily_consistency_status,
    event_rows,
    normalize_minute_bars,
    replay_intraday_path,
)
from src.strategy_d_spec import historical_candidate_mask  # noqa: E402
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402


LOGGER = logging.getLogger("strategy_d_intraday_event_ledger")

START = "20240630"
END = "20260630"
ALLOWED_SEGMENTS = {"sh_main", "sz_main", "chi_next", "star", "bj"}
EXPECTED_STRONG_DAY_COUNT = 56
EXPECTED_MOTHER_POOL_COUNT = 6848
EXPECTED_FAILED_CLOSE_COUNT = 1710
EXPECTED_STATIC_D_POOL_COUNT = 206

MARKET_SENTIMENT_PATH = ROOT / "data/research/five_year_strict/market_sentiment.csv"
STRICT_FEATURE_POOL_PATH = ROOT / "data/research/five_year_strict/strict_feature_pool.csv"
TRADE_CALENDAR_PATH = ROOT / "data/raw/trade_calendar.csv"
STOCK_BASIC_PATH = ROOT / "data/raw/stock_basic/stock_basic_all.csv"
STOCK_NAMECHANGE_PATH = ROOT / "data/raw/stock_namechange/stock_namechange.csv"
STK_LIMIT_DIR = ROOT / "data/raw/stk_limit_history"
DAILY_DIR = ROOT / "data/raw/daily"
LIMIT_LIST_DIR = ROOT / "data/raw/limit_list"
DATA_DIR = ROOT / "data/research/strategy_d_intraday"
REPORT_DIR = ROOT / "reports/strategy_d_intraday_research"
MOTHER_POOL_PATH = DATA_DIR / "mother_pool.csv"
TARGET_MANIFEST_PATH = DATA_DIR / "minute_target_manifest.csv"
EVENT_LEDGER_PATH = DATA_DIR / "event_ledger.csv"
EVENT_DETAIL_PATH = DATA_DIR / "event_detail.csv"
COVERAGE_PATH = DATA_DIR / "minute_coverage.csv"
SUMMARY_PATH = REPORT_DIR / "summary.json"


def date_text(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True)


def load_calendar() -> list[str]:
    calendar = pd.read_csv(TRADE_CALENDAR_PATH, dtype=str, low_memory=False)
    if "is_open" in calendar.columns:
        calendar = calendar[
            calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})
        ]
    return sorted(calendar["cal_date"].astype(str))


def load_stock_metadata() -> dict[str, dict[str, str]]:
    basic = pd.read_csv(STOCK_BASIC_PATH, dtype=str, low_memory=False)
    basic = basic.drop_duplicates("ts_code", keep="last")
    return {
        str(row.ts_code): {
            "name": str(row.name or ""),
            "list_date": str(row.list_date or "").replace(".0", ""),
            "delist_date": str(row.delist_date or "").replace(".0", ""),
            "list_status": str(row.list_status or ""),
        }
        for row in basic.itertuples(index=False)
    }


def load_historical_names() -> dict[str, list[dict[str, str]]]:
    """加载Tushare历史更名区间，解决后来变ST反向污染旧日期的问题。"""

    if not STOCK_NAMECHANGE_PATH.exists():
        raise FileNotFoundError(
            "D历史身份缺少stock_namechange：请先采集Tushare namechange，"
            "禁止用当前名称代替历史名称。"
        )
    frame = pd.read_csv(STOCK_NAMECHANGE_PATH, dtype=str, low_memory=False)
    required = {"ts_code", "name", "start_date", "end_date"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"stock_namechange缺少字段：{missing}")
    frame["start_date"] = date_text(frame["start_date"])
    frame["end_date"] = frame["end_date"].fillna("").map(
        lambda value: str(value).replace(".0", "")
    )
    result: dict[str, list[dict[str, str]]] = {}
    for code, group in frame.groupby("ts_code", sort=False):
        result[str(code)] = [
            {
                "name": str(row.name or ""),
                "start_date": str(row.start_date),
                "end_date": str(row.end_date or ""),
            }
            for row in group.sort_values("start_date").itertuples(index=False)
        ]
    return result


def historical_name(
    code: str,
    date: str,
    intervals: dict[str, list[dict[str, str]]],
    current_metadata: dict[str, dict[str, str]],
) -> tuple[str, str]:
    for item in intervals.get(code, []):
        end = item["end_date"] or "99991231"
        if item["start_date"] <= date <= end:
            return item["name"], "TUSHARE_NAMECHANGE_ASOF"
    return str(current_metadata.get(code, {}).get("name", "")), "CURRENT_NAME_FALLBACK"


def limit_list_names(date: str) -> dict[str, str]:
    """读取信号日收盘涨停池名称。

    ``stock_namechange``只记录发生过更名的区间，个别已退市股票既不在
    当前``stock_basic``中，也没有更名记录。若它当日收盘涨停，当日涨停池
    是最接近信号时点的名称证据。
    """

    path = LIMIT_LIST_DIR / f"{date}.csv"
    if not path.exists():
        return {}
    frame = pd.read_csv(path, dtype={"ts_code": str}, low_memory=False)
    if not {"ts_code", "name"}.issubset(frame.columns):
        return {}
    return dict(
        zip(frame["ts_code"].astype(str), frame["name"].fillna("").astype(str))
    )


def historical_st_status(
    *, name: str, name_source: str, segment: str, pre_close: float, cap: float
) -> tuple[bool, str]:
    """以as-of名称为主，仅在当前名称回退时用官方涨停幅反证。"""

    if name_source != "CURRENT_NAME_FALLBACK":
        return is_st_name(name), "ASOF_NAME"
    if segment in {"sh_main", "sz_main"} and pre_close > 0 and cap > 0:
        cap_ratio = cap / pre_close - 1.0
        if cap_ratio <= 0.065:
            return True, "OFFICIAL_LIMIT_PCT_MAIN_BOARD_ST"
        if cap_ratio >= 0.085:
            return False, "OFFICIAL_LIMIT_PCT_MAIN_BOARD_NON_ST"
    return is_st_name(name), "CURRENT_NAME_FALLBACK"


def historical_strong_days() -> pd.DataFrame:
    sentiment = pd.read_csv(
        MARKET_SENTIMENT_PATH, dtype={"trade_date": str}, low_memory=False
    )
    sentiment["trade_date"] = date_text(sentiment["trade_date"])
    compatible = (
        sentiment["strategy_compatible"]
        .astype(str)
        .str.lower()
        .isin({"true", "1", "yes"})
    )
    result = sentiment[
        sentiment["trade_date"].between(START, END)
        & sentiment["market_sentiment_level"].astype(str).eq("strong")
        & compatible
    ][
        [
            "trade_date",
            "market_sentiment_level",
            "limit_up_count",
            "opened_limit_count",
            "limit_up_max_height",
            "limit_data_source",
            "limit_data_quality",
        ]
    ].copy()
    result = result.sort_values("trade_date").reset_index(drop=True)
    if len(result) != EXPECTED_STRONG_DAY_COUNT:
        raise RuntimeError(
            f"D盘中母池strong日漂移：expected={EXPECTED_STRONG_DAY_COUNT} actual={len(result)}"
        )
    return result


def current_static_d_pool() -> pd.DataFrame:
    columns = [
        "trade_date",
        "ts_code",
        "first_time",
        "last_time",
        "open_times",
        "fd_amount_to_circ_mv",
        "fill_probability",
        "is_fill_score_reliable",
        "limit_times",
        "is_st",
        "market_sentiment_level",
        "board_type",
        "first_time_bucket",
        "market_segment",
    ]
    frame = pd.read_csv(
        STRICT_FEATURE_POOL_PATH, usecols=columns, low_memory=False
    )
    frame["trade_date"] = date_text(frame["trade_date"])
    mask = historical_candidate_mask(
        frame,
        min_fill_probability=0.80,
        allowed_segments=ALLOWED_SEGMENTS,
    )
    result = frame[mask & frame["trade_date"].between(START, END)].copy()
    if len(result) != EXPECTED_STATIC_D_POOL_COUNT:
        raise RuntimeError(
            "当前D收盘静态池漂移："
            f"expected={EXPECTED_STATIC_D_POOL_COUNT} actual={len(result)}"
        )
    return result.rename(
        columns={
            "first_time": "static_first_time",
            "last_time": "static_last_time",
            "open_times": "static_open_times",
            "fd_amount_to_circ_mv": "static_fd_amount_to_circ_mv",
            "fill_probability": "static_fill_probability",
        }
    )[
        [
            "trade_date",
            "ts_code",
            "static_first_time",
            "static_last_time",
            "static_open_times",
            "static_fd_amount_to_circ_mv",
            "static_fill_probability",
        ]
    ]


def build_mother_pool() -> pd.DataFrame:
    """从全部strong日的日线最高价建立完整首板触板母池。"""

    calendar = load_calendar()
    date_index = {date: index for index, date in enumerate(calendar)}
    metadata = load_stock_metadata()
    name_intervals = load_historical_names()
    strong = historical_strong_days()
    strong_map = strong.set_index("trade_date").to_dict("index")
    rows: list[dict[str, Any]] = []

    for date in strong["trade_date"]:
        index = date_index.get(date)
        daily_path = DAILY_DIR / f"{date}.csv"
        if index is None or index == 0 or not daily_path.exists():
            raise FileNotFoundError(f"D盘中母池缺少交易日或日线：{date}")
        previous_date = calendar[index - 1]
        previous_limit_path = LIMIT_LIST_DIR / f"{previous_date}.csv"
        if not previous_limit_path.exists():
            raise FileNotFoundError(f"D盘中母池缺少昨日涨停池：{previous_limit_path}")
        previous_limit = pd.read_csv(
            previous_limit_path, dtype={"ts_code": str}, low_memory=False
        )
        previous_codes = set(previous_limit["ts_code"].astype(str))
        stk_limit_path = STK_LIMIT_DIR / f"{date}.csv"
        if not stk_limit_path.exists():
            raise FileNotFoundError(
                f"D盘中母池缺少交易所涨跌停价：{stk_limit_path}；禁止自行估价。"
            )
        stk_limit = pd.read_csv(
            stk_limit_path, dtype={"ts_code": str}, low_memory=False
        )
        official_caps = dict(
            zip(
                stk_limit["ts_code"].astype(str),
                pd.to_numeric(stk_limit["up_limit"], errors="coerce"),
            )
        )
        daily = pd.read_csv(daily_path, dtype={"ts_code": str}, low_memory=False)
        market = strong_map[date]
        close_limit_names = limit_list_names(date)
        for row in daily.itertuples(index=False):
            code = str(row.ts_code)
            meta = metadata.get(code, {})
            name, name_source = historical_name(
                code, date, name_intervals, metadata
            )
            segment = market_segment(code)
            if (
                name_source == "CURRENT_NAME_FALLBACK"
                and close_limit_names.get(code)
            ):
                name = close_limit_names[code]
                name_source = "LIMIT_LIST_D_SIGNAL_DATE"
            if code in previous_codes or segment not in ALLOWED_SEGMENTS:
                continue
            pre_close = float(row.pre_close or 0.0)
            high = float(row.high or 0.0)
            close = float(row.close or 0.0)
            cap_value = official_caps.get(code)
            cap = float(cap_value) if pd.notna(cap_value) else 0.0
            if cap <= 0 or high < cap - 1e-9:
                continue
            is_st, st_evidence = historical_st_status(
                name=name,
                name_source=name_source,
                segment=segment,
                pre_close=pre_close,
                cap=cap,
            )
            if is_st:
                continue
            closed_at_limit = bool(close >= cap - 1e-9)
            rows.append(
                {
                    "trade_date": date,
                    "ts_code": code,
                    "name": name,
                    "name_metadata_missing": not bool(name),
                    "historical_name_source": name_source,
                    "historical_st": False,
                    "historical_st_evidence": st_evidence,
                    "market_segment": segment,
                    "list_date": str(meta.get("list_date", "")),
                    "delist_date": str(meta.get("delist_date", "")),
                    "list_status": str(meta.get("list_status", "")),
                    "previous_trade_date": previous_date,
                    "previous_day_limit_up": False,
                    "pre_close": pre_close,
                    "limit_price": float(cap),
                    "daily_open": float(row.open or 0.0),
                    "daily_high": high,
                    "daily_low": float(row.low or 0.0),
                    "daily_close": close,
                    "daily_volume": float(row.vol or 0.0),
                    "daily_amount": float(row.amount or 0.0),
                    "daily_high_touched_limit": True,
                    "closed_at_limit": closed_at_limit,
                    "failed_to_close_at_limit": not closed_at_limit,
                    "historical_market_sentiment": str(
                        market["market_sentiment_level"]
                    ),
                    "historical_limit_up_count": int(market["limit_up_count"]),
                    "historical_opened_limit_count": int(
                        market["opened_limit_count"]
                    ),
                    "historical_limit_up_max_height": int(
                        market["limit_up_max_height"]
                    ),
                    "sentiment_is_final_close_value": True,
                }
            )
    mother = pd.DataFrame(rows).sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    static = current_static_d_pool()
    mother = mother.merge(
        static,
        on=["trade_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    mother["in_current_static_d_pool"] = mother["static_first_time"].notna()
    failed = int(mother["failed_to_close_at_limit"].sum())
    if (
        len(mother) != EXPECTED_MOTHER_POOL_COUNT
        or failed != EXPECTED_FAILED_CLOSE_COUNT
    ):
        raise RuntimeError(
            "D完整首板触板母池漂移："
            f"rows expected={EXPECTED_MOTHER_POOL_COUNT} actual={len(mother)}；"
            f"failed expected={EXPECTED_FAILED_CLOSE_COUNT} actual={failed}"
        )
    static_count = int(mother["in_current_static_d_pool"].sum())
    if static_count != EXPECTED_STATIC_D_POOL_COUNT:
        raise RuntimeError(
            f"D母池与当前静态池合并不完整：expected={EXPECTED_STATIC_D_POOL_COUNT} actual={static_count}"
        )
    return mother


def write_mother_and_targets(mother: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    mother.to_csv(MOTHER_POOL_PATH, index=False, encoding="utf-8-sig")
    targets = mother[
        [
            "trade_date",
            "ts_code",
            "name",
            "market_segment",
            "limit_price",
            "closed_at_limit",
            "failed_to_close_at_limit",
            "in_current_static_d_pool",
        ]
    ].copy()
    targets["target_key"] = targets["trade_date"] + "|" + targets["ts_code"]
    targets["required_start_hhmm"] = 930
    targets["required_end_hhmm"] = 1500
    targets["required_frequency"] = "1m_for_path;tick_depth_for_queue"
    targets["acceptance_role"] = "D_COMPLETE_FIRST_BOARD_TOUCH_MOTHER_POOL"
    targets.to_csv(TARGET_MANIFEST_PATH, index=False, encoding="utf-8-sig")


def load_minute_source(path: Path | None) -> pd.DataFrame:
    if path is None or not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(
        path,
        dtype={"trade_date": str, "ts_code": str, "trade_time": str, "bar_time": str},
        low_memory=False,
    )
    normalized_parts: list[pd.DataFrame] = []
    for (date, code), group in frame.groupby(["trade_date", "ts_code"], sort=False):
        normalized_parts.append(
            normalize_minute_bars(group, ts_code=str(code), trade_date=str(date))
        )
    if not normalized_parts:
        return pd.DataFrame()
    return pd.concat(normalized_parts, ignore_index=True)


def replay_ledger(
    mother: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    data_source: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    groups = {
        (str(date), str(code)): group.copy()
        for (date, code), group in bars.groupby(["trade_date", "ts_code"], sort=False)
    } if not bars.empty else {}
    ledger_rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    daily_data = strict.daily_data()
    outcome_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for mother_row in mother.itertuples(index=False):
        date = str(mother_row.trade_date)
        code = str(mother_row.ts_code)
        key = (date, code)
        group = groups.get(key, pd.DataFrame())
        replay = replay_intraday_path(group, limit_price=float(mother_row.limit_price))
        consistency = daily_consistency_status(
            group,
            limit_price=float(mother_row.limit_price),
            daily_high=float(mother_row.daily_high),
            daily_close=float(mother_row.daily_close),
        )
        if not group.empty and not consistency["minute_confirms_daily_touch"]:
            replay["minute_status"] = "MISMATCH_DAILY_TOUCH_NOT_CONFIRMED"
            replay["path_complete"] = False
        events = replay.pop("events", [])
        path_signal = bool(replay["signal_rule_current"])
        if path_signal:
            if key not in outcome_cache:
                row = pd.Series(
                    {
                        "signal_date": date,
                        "ts_code": code,
                        "name": str(mother_row.name),
                        "limit_close": float(mother_row.limit_price),
                    }
                )
                outcome_cache[key] = strict.d_execution(row, daily_data)
            outcome = outcome_cache[key]
        else:
            outcome = {
                "status": "NO_PATH_SIGNAL",
                "buy_date": "",
                "exit_date": "",
                "stock_return_before_fees": None,
                "account_return": None,
            }
        queue_status = str(replay["queue_fill_status"])
        confirmed_fill = queue_status == "CONFIRMED_FILL_PRICE_TRADED_BELOW_LIMIT"
        ledger_rows.append(
            {
                **mother_row._asdict(),
                "minute_data_source": data_source,
                **replay,
                **consistency,
                "queue_depth_available": False,
                "confirmed_fill_by_price": confirmed_fill,
                "execution_status": outcome.get("status", ""),
                "exit_date": outcome.get("exit_date", ""),
                "stock_return_before_fees": outcome.get("stock_return_before_fees"),
                "account_return": outcome.get("account_return"),
                "events_json": json.dumps(events, ensure_ascii=False, separators=(",", ":")),
            }
        )
        detail_rows.extend(
            [
                {
                    **row,
                    "minute_data_source": data_source,
                    "bar_minutes": replay["bar_minutes"],
                }
                for row in event_rows(
                    {"events": events}, trade_date=date, ts_code=code
                )
            ]
        )
        coverage_rows.append(
            {
                "trade_date": date,
                "ts_code": code,
                "minute_data_source": data_source,
                "minute_status": replay["minute_status"],
                "bar_minutes": replay["bar_minutes"],
                "bar_count": replay["bar_count"],
                "first_hhmm": replay["first_hhmm"],
                "last_hhmm": replay["last_hhmm"],
                "path_complete": replay["path_complete"],
                "queue_depth_available": False,
                **consistency,
            }
        )
    return (
        pd.DataFrame(ledger_rows),
        pd.DataFrame(detail_rows),
        pd.DataFrame(coverage_rows),
    )


def summary_payload(
    mother: pd.DataFrame,
    ledger: pd.DataFrame,
    coverage: pd.DataFrame,
    *,
    minute_path: Path | None,
    data_source: str,
    targets_only: bool,
) -> dict[str, Any]:
    if ledger.empty:
        minute_status_counts: dict[str, int] = {}
        signal_count = confirmed_fill_count = queue_unknown_count = 0
        failed_close_signal_count = 0
        ambiguous_count = 0
        signal_ambiguous_count = 0
        first_before_1400_signal_count = 0
        static_pool_signal_overlap_count = 0
        signal_open_times_counts: dict[str, int] = {}
    else:
        minute_status_counts = {
            str(key): int(value)
            for key, value in ledger["minute_status"].value_counts().to_dict().items()
        }
        signal_count = int(ledger["signal_rule_current"].sum())
        confirmed_fill_count = int(ledger["confirmed_fill_by_price"].sum())
        queue_unknown_count = int(
            ledger["queue_fill_status"]
            .astype(str)
            .eq("QUEUE_FILL_UNKNOWN_NO_DEPTH_CANCEL_1455")
            .sum()
        )
        failed_close_signal_count = int(
            (ledger["signal_rule_current"] & ledger["failed_to_close_at_limit"]).sum()
        )
        ambiguous_count = int(ledger["path_ambiguous"].sum())
        signal_mask = ledger["signal_rule_current"].astype(bool)
        signal_ambiguous_count = int(
            (signal_mask & ledger["path_ambiguous"].astype(bool)).sum()
        )
        first_before_1400_signal_count = int(
            (signal_mask & ledger["signal_rule_first_before_1400"].astype(bool)).sum()
        )
        static_pool_signal_overlap_count = int(
            (signal_mask & ledger["in_current_static_d_pool"].astype(bool)).sum()
        )
        signal_open_times_counts = {
            str(int(key)): int(value)
            for key, value in ledger.loc[
                signal_mask, "open_times_at_signal"
            ].value_counts().sort_index().to_dict().items()
        }
    one_minute_ready = int(
        coverage["minute_status"].astype(str).eq("READY_1M_PATH_NO_QUEUE_DEPTH").sum()
    ) if not coverage.empty else 0
    approximate_5m_ready = int(
        coverage["minute_status"]
        .astype(str)
        .eq("APPROXIMATE_5M_PATH_NO_QUEUE_DEPTH")
        .sum()
    ) if not coverage.empty else 0
    return {
        "schema_version": 2,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "research_protocol": STRICT_DISCOVERY,
        "strategy": "D",
        "window": f"{START}~{END}",
        "formal_rule_modified": False,
        "release_eligible": False,
        "targets_only": targets_only,
        "mother_pool": {
            "historical_strong_day_count": EXPECTED_STRONG_DAY_COUNT,
            "first_board_touch_count": int(len(mother)),
            "closed_at_limit_count": int(mother["closed_at_limit"].sum()),
            "failed_to_close_at_limit_count": int(
                mother["failed_to_close_at_limit"].sum()
            ),
            "current_static_d_survivor_count": int(
                mother["in_current_static_d_pool"].sum()
            ),
            "all_strong_days_have_failed_close_touch": bool(
                mother.groupby("trade_date")["failed_to_close_at_limit"].any().all()
            ),
        },
        "minute_data": {
            "path": str(minute_path.relative_to(ROOT)) if minute_path and minute_path.is_absolute() and ROOT in minute_path.parents else str(minute_path or ""),
            "source": data_source,
            "status_counts": minute_status_counts,
            "one_minute_path_ready_count": one_minute_ready,
            "approximate_five_minute_path_ready_count": approximate_5m_ready,
            "target_count": int(len(mother)),
            "one_minute_coverage_complete": one_minute_ready == len(mother),
            "queue_depth_coverage_complete": False,
        },
        "path_replay": {
            "first_eligible_reseal_signal_count": signal_count,
            "failed_close_signal_count": failed_close_signal_count,
            "confirmed_fill_by_price_count": confirmed_fill_count,
            "queue_fill_unknown_no_depth_count": queue_unknown_count,
            "ambiguous_path_count": ambiguous_count,
            "ambiguous_signal_count": signal_ambiguous_count,
            "unambiguous_signal_count": signal_count - signal_ambiguous_count,
            "first_before_1400_signal_count": first_before_1400_signal_count,
            "current_static_pool_signal_overlap_count": static_pool_signal_overlap_count,
            "open_times_at_signal_counts": signal_open_times_counts,
            "first_eligible_signal_definition": (
                "09:30起连续跟踪；首次封板桶合规；炸板2~3次；14:00后第一次回封"
            ),
            "queue_fill_definition": (
                "信号后且14:55撤单前价格低于涨停价才确认涨停限价单可成交；"
                "始终封板因缺少队列深度而标记未知"
            ),
        },
        "certification": {
            "complete_mother_pool_passed": True,
            "complete_one_minute_path_passed": one_minute_ready == len(mother),
            "historical_queue_depth_passed": False,
            "daily_live_ranking_reconstructable": False,
            "d_standalone_replay_certifiable": False,
            "acde_one_leg_replacement_certifiable": False,
            "status": "BLOCKED_BY_MINUTE_AND_QUEUE_DEPTH" if not targets_only else "TARGET_MANIFEST_READY",
        },
        "limitations": [
            "历史市场strong门使用最终收盘情绪；精确复现实盘还需全市场盘中ever_sealed路径。",
            "OHLCV没有回封时买一封单和队列前方数量，始终封板期间不能证明排队成交。",
            "五分钟K无法确定同一bar内触板、炸板和回封先后，只能作近似覆盖预检。",
            "跨数据源分钟最高价未确认官方涨停价的目标单独标记不一致，禁止用于路径认证。",
            "在一分钟路径和历史队列深度补齐前，不得用本账本修改正式D规则或计算认证组合复利。",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="建立D完整首板触板分钟事件账本")
    parser.add_argument(
        "--minute-bars",
        type=Path,
        default=None,
        help="标准分钟K CSV；缺省时只生成母池并把分钟覆盖标记为缺失。",
    )
    parser.add_argument(
        "--data-source",
        default="MISSING",
        help="分钟数据源标签，例如TUSHARE_STK_MINS_1M或BAOSTOCK_5M_APPROXIMATE。",
    )
    parser.add_argument(
        "--targets-only", action="store_true", help="只生成完整母池和采集目标。"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LOGGER.info("从全部历史strong日构建D完整首板触板母池")
    mother = build_mother_pool()
    write_mother_and_targets(mother)
    LOGGER.info(
        "D母池完成：strong=%d日，触板=%d，失败收盘=%d，当前收盘静态池=%d",
        mother["trade_date"].nunique(),
        len(mother),
        int(mother["failed_to_close_at_limit"].sum()),
        int(mother["in_current_static_d_pool"].sum()),
    )

    if args.targets_only:
        ledger = pd.DataFrame()
        coverage = pd.DataFrame()
    else:
        minute_path = args.minute_bars
        if minute_path is not None and not minute_path.is_absolute():
            minute_path = ROOT / minute_path
        LOGGER.info("加载分钟数据：%s", minute_path or "缺失")
        bars = load_minute_source(minute_path)
        ledger, details, coverage = replay_ledger(
            mother, bars, data_source=args.data_source
        )
        ledger.to_csv(EVENT_LEDGER_PATH, index=False, encoding="utf-8-sig")
        details.to_csv(EVENT_DETAIL_PATH, index=False, encoding="utf-8-sig")
        coverage.to_csv(COVERAGE_PATH, index=False, encoding="utf-8-sig")
        LOGGER.info(
            "事件账本完成：分钟有数据=%d/%d，路径信号=%d，价格确认成交=%d",
            int(coverage["bar_count"].gt(0).sum()),
            len(coverage),
            int(ledger["signal_rule_current"].sum()),
            int(ledger["confirmed_fill_by_price"].sum()),
        )
    minute_path = args.minute_bars
    if minute_path is not None and not minute_path.is_absolute():
        minute_path = ROOT / minute_path
    summary = summary_payload(
        mother,
        ledger,
        coverage,
        minute_path=minute_path,
        data_source=args.data_source,
        targets_only=args.targets_only,
    )
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    LOGGER.info("D盘中事件研究摘要：%s", SUMMARY_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
