#!/usr/bin/env python3
"""研究D全窗口一分钟爆发/爆亏特征，并执行独立腿与ACDE双门禁。

本脚本固定使用2024-06-30~2026-06-30正式两年窗口。研究对象是完整首板触板
母池，不以最终收盘封板或收盘strong筛选入口。候选规则只能引用14:55前已经
知道的分钟路径、盘中首板情绪、市场板块和量价字段；最终封板、信号后价格穿透、
全天成交量及T+2收益只作结果标签或成交回放，不得参与候选选择。

成交采用两套边界：

1. ``PRICE_CONFIRMED_ONLY``：每日先按信号时点字段选唯一候选，再仅在信号后
   14:55前价格低于涨停价时确认成交；始终封板的未知队列机械记为未成交。
2. ``ASSUME_QUEUE_FILLED``：同一候选的乐观上界，未知队列也假设成交。

双门禁以保守的``PRICE_CONFIRMED_ONLY``为主，并与冻结正式D 39笔/2.0261239236倍、
当前A/E优化后ACDE 132笔/327.7267189755倍逐腿比较。脚本属于STRICT_DISCOVERY，
不会自行修改正式D；是否改动由双门禁结果和后续代码落地共同决定。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import sys
from typing import Any, Callable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from scripts.build_strategy_d_intraday_event_ledger import (  # noqa: E402
    iter_minute_groups,
    load_known_data_gap_keys,
    load_known_price_mismatch_keys,
)
from scripts.research_strategy_d_explosion_features import (  # noqa: E402
    build_current_other_legs,
    combo_replay,
    executed_metrics,
    replay_d_only,
)
from src.mechanical_compound import mechanical_compound  # noqa: E402
from src.strategy_d_intraday_ledger import normalize_minute_bars  # noqa: E402
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402


LOGGER = logging.getLogger("strategy_d_full_window_features_and_gates")

START = "20240630"
END = "20260630"
FIRST_12M_END = "20250630"
SECOND_12M_START = "20250701"
EXPECTED_TARGET_COUNT = 40_336
EXPECTED_READY_COUNT = 40_328
EXPECTED_GAP_COUNT = 4
EXPECTED_MISMATCH_COUNT = 4
EXPECTED_SIGNAL_COUNT = 2_167
EXPECTED_CONFIRMED_FILL_COUNT = 1_710
EXPECTED_QUEUE_UNKNOWN_COUNT = 457
EXPECTED_FAILED_CLOSE_SIGNAL_COUNT = 407
EXPECTED_UNRESOLVED_EXIT_COUNT = 4
EXPECTED_SOURCE = "TUSHARE_STK_MINS_1M_UNADJUSTED"

BASELINE_D_TRADE_COUNT = 39
BASELINE_D_MULTIPLE = 2.0261239235922566
BASELINE_ACDE_TRADE_COUNT = 132
BASELINE_ACDE_MULTIPLE = 327.72671897548867
BASELINE_ACDE_LEG_COUNTS = {"D": 22, "A": 44, "E": 49, "C": 17}
TOLERANCE = 1e-12

DEFAULT_LEDGER = ROOT / "data/research/strategy_d_intraday/event_ledger_full_window.csv"
DEFAULT_MINUTE = ROOT / "data/research/strategy_d_intraday/minute_1m_tushare.csv"
DEFAULT_OUTPUT_DIR = ROOT / "reports/strategy_d_full_window_features"
STOCK_BASIC_PATH = ROOT / "data/raw/stock_basic/stock_basic_all.csv"
DAILY_DIR = ROOT / "data/raw/daily"

SIGNAL_TIME_ALLOWED_FIELDS = frozenset(
    {
        "first_seal_hhmm",
        "eligible_signal_hhmm",
        "open_times_at_signal",
        "market_segment",
        "first_to_signal_minutes",
        "last_break_to_signal_minutes",
        "intraday_first_board_ever_sealed_count",
        "intraday_first_board_active_sealed_count",
        "intraday_first_board_seal_rate",
        "same_segment_ever_sealed_count",
        "same_segment_active_sealed_count",
        "same_segment_seal_rate",
        "prior_path_signal_count",
        "same_minute_path_signal_count",
        "signal_cumulative_amount_vs_prev_day",
        "signal_recent_5m_amount_vs_prev_day",
        "signal_recent_10m_amount_vs_prev_day",
        "signal_cumulative_volume_vs_prev_day",
        "signal_open_gap_pct",
        "pre_signal_min_return_from_preclose",
        "pre_signal_vwap_return_from_preclose",
        "pre_signal_intraday_range_pct",
        "limit_close_bar_share_before_signal",
        "consecutive_limit_close_bars_at_signal",
    }
)

FUTURE_OR_NON_ASOF_FIELDS = frozenset(
    {
        "confirmed_fill_by_price",
        "queue_fill_status",
        "fill_hhmm",
        "closed_at_limit",
        "failed_to_close_at_limit",
        "daily_close",
        "daily_amount",
        "daily_volume",
        "signal_day_amount_completion_fraction",
        "execution_status",
        "account_return",
        "stock_return_before_fees",
        "exit_date",
        "historical_market_sentiment",
        "historical_limit_up_count",
        "historical_opened_limit_count",
        "historical_is_current_final_close_strong_day",
        "industry_current_proxy",
    }
)


@dataclass(frozen=True)
class CandidateRule:
    name: str
    description: str
    fields: tuple[str, ...]
    predicate: Callable[[pd.DataFrame], pd.Series]


def date_text(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True)


def bool_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    return series.fillna("").astype(str).str.lower().isin({"true", "1", "yes"})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def trading_minute_index(hhmm: int) -> int:
    hour, minute = divmod(int(hhmm), 100)
    absolute = hour * 60 + minute
    if absolute <= 11 * 60 + 30:
        return absolute - (9 * 60 + 30)
    return 121 + absolute - (13 * 60)


def trading_minutes_between(left: int, right: int) -> int:
    if int(left) <= 0 or int(right) <= 0:
        return 0
    return max(trading_minute_index(int(right)) - trading_minute_index(int(left)), 0)


def parse_events(value: object) -> list[dict[str, Any]]:
    if pd.isna(value) or not str(value):
        return []
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise ValueError("D事件JSON必须为list")
    return parsed


def load_ledger(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"缺少D全窗口一分钟账本：{path}")
    frame = pd.read_csv(path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    required = {
        "trade_date",
        "ts_code",
        "name",
        "market_segment",
        "previous_trade_date",
        "pre_close",
        "limit_price",
        "daily_amount",
        "daily_volume",
        "minute_data_source",
        "minute_status",
        "bar_count",
        "first_seal_hhmm",
        "eligible_signal_hhmm",
        "open_times_at_signal",
        "signal_rule_current",
        "queue_fill_status",
        "confirmed_fill_by_price",
        "failed_to_close_at_limit",
        "path_ambiguous",
        "execution_status",
        "exit_date",
        "account_return",
        "events_json",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"D全窗口账本缺少字段：{missing}")
    frame["trade_date"] = date_text(frame["trade_date"])
    frame["previous_trade_date"] = date_text(frame["previous_trade_date"])
    for column in (
        "signal_rule_current",
        "confirmed_fill_by_price",
        "failed_to_close_at_limit",
        "path_ambiguous",
        "closed_at_limit",
    ):
        if column in frame:
            frame[column] = bool_series(frame[column])
    duplicate_count = int(frame.duplicated(["trade_date", "ts_code"]).sum())
    status_counts = frame["minute_status"].astype(str).value_counts().to_dict()
    source_values = sorted(frame["minute_data_source"].dropna().astype(str).unique())
    failures: list[str] = []
    if len(frame) != EXPECTED_TARGET_COUNT:
        failures.append(f"目标数={len(frame)}")
    if duplicate_count:
        failures.append(f"重复键={duplicate_count}")
    if source_values != [EXPECTED_SOURCE]:
        failures.append(f"数据源={source_values}")
    expected_status = {
        "READY_1M_PATH_NO_QUEUE_DEPTH": EXPECTED_READY_COUNT,
        "MISSING_MINUTE_DATA": EXPECTED_GAP_COUNT,
        "MISMATCH_DAILY_TOUCH_NOT_CONFIRMED": EXPECTED_MISMATCH_COUNT,
    }
    if status_counts != expected_status:
        failures.append(f"覆盖状态={status_counts}")
    ready = frame["minute_status"].eq("READY_1M_PATH_NO_QUEUE_DEPTH")
    if not pd.to_numeric(frame.loc[ready, "bar_count"], errors="coerce").eq(241).all():
        failures.append("完整路径并非全部241根")
    if failures:
        raise RuntimeError("D全窗口账本冻结校验失败：" + "；".join(failures))
    return frame.sort_values(["trade_date", "ts_code"]).reset_index(drop=True), {
        "ledger_path": str(path.relative_to(ROOT)),
        "ledger_sha256": sha256(path),
        "target_count": int(len(frame)),
        "duplicate_key_count": duplicate_count,
        "minute_status_counts": {str(key): int(value) for key, value in status_counts.items()},
        "source": EXPECTED_SOURCE,
    }


def signal_frame(ledger: pd.DataFrame) -> pd.DataFrame:
    signals = ledger[ledger["signal_rule_current"]].copy()
    actual = {
        "signal": int(len(signals)),
        "confirmed": int(signals["confirmed_fill_by_price"].sum()),
        "unknown": int((~signals["confirmed_fill_by_price"]).sum()),
        "failed_close": int(signals["failed_to_close_at_limit"].sum()),
    }
    expected = {
        "signal": EXPECTED_SIGNAL_COUNT,
        "confirmed": EXPECTED_CONFIRMED_FILL_COUNT,
        "unknown": EXPECTED_QUEUE_UNKNOWN_COUNT,
        "failed_close": EXPECTED_FAILED_CLOSE_SIGNAL_COUNT,
    }
    if actual != expected:
        raise RuntimeError(f"D全窗口路径信号漂移：expected={expected} actual={actual}")
    execution_counts = signals["execution_status"].astype(str).value_counts().to_dict()
    if execution_counts != {"OK": EXPECTED_SIGNAL_COUNT - EXPECTED_UNRESOLVED_EXIT_COUNT, "NO_PRICE": EXPECTED_UNRESOLVED_EXIT_COUNT}:
        raise RuntimeError(
            "D路径信号退出结果不完整："
            f"{execution_counts}"
        )
    if not pd.to_numeric(signals["eligible_signal_hhmm"], errors="raise").lt(1455).all():
        raise RuntimeError("D路径信号包含14:55及之后委托")
    signals["account_return"] = pd.to_numeric(signals["account_return"], errors="raise")
    return signals.reset_index(drop=True)


def sealed_state_at(events: list[dict[str, Any]], hhmm: int) -> tuple[bool, int]:
    sealed = False
    break_count = 0
    for event in events:
        event_hhmm = int(event.get("hhmm", 0) or 0)
        if event_hhmm > int(hhmm):
            continue
        event_type = str(event.get("event_type", ""))
        if event_type in {"FIRST_SEAL", "RESEAL"}:
            sealed = True
        elif event_type == "LIMIT_OPEN_BREAK":
            sealed = False
            break_count += 1
    return sealed, break_count


def last_event_hhmm(
    events: list[dict[str, Any]], event_type: str, *, before_or_at: int
) -> int:
    values = [
        int(event.get("hhmm", 0) or 0)
        for event in events
        if str(event.get("event_type", "")) == event_type
        and int(event.get("hhmm", 0) or 0) <= int(before_or_at)
    ]
    return max(values) if values else 0


def load_industry_proxy() -> dict[str, str]:
    basic = pd.read_csv(STOCK_BASIC_PATH, dtype=str, low_memory=False)
    basic = basic.drop_duplicates("ts_code", keep="last")
    return dict(
        zip(
            basic["ts_code"].astype(str),
            basic.get("industry", pd.Series("unknown", index=basic.index))
            .fillna("unknown")
            .astype(str),
        )
    )


def add_intraday_context(ledger: pd.DataFrame, signals: pd.DataFrame) -> pd.DataFrame:
    """用当时已经发生的FIRST_SEAL/RESEAL/BREAK重建首板盘中情绪。"""

    industry_map = load_industry_proxy()
    day_rows: dict[str, list[dict[str, Any]]] = {}
    for row in ledger.itertuples(index=False):
        day_rows.setdefault(str(row.trade_date), []).append(
            {
                "ts_code": str(row.ts_code),
                "market_segment": str(row.market_segment),
                "industry_current_proxy": industry_map.get(str(row.ts_code), "unknown"),
                "first_seal_hhmm": int(row.first_seal_hhmm or 0),
                "eligible_signal_hhmm": int(row.eligible_signal_hhmm or 0),
                "events": parse_events(row.events_json),
            }
        )
    context_rows: list[dict[str, Any]] = []
    for row in signals.itertuples(index=False):
        date = str(row.trade_date)
        code = str(row.ts_code)
        signal_hhmm = int(row.eligible_signal_hhmm)
        segment = str(row.market_segment)
        industry = industry_map.get(code, "unknown")
        own_events = parse_events(row.events_json)
        members = day_rows[date]
        ever = active = break_count = 0
        same_segment_ever = same_segment_active = 0
        same_industry_ever = same_industry_active = 0
        prior_signals = same_minute_signals = 0
        for member in members:
            first = int(member["first_seal_hhmm"])
            if first <= 0 or first > signal_hhmm:
                continue
            member_active, member_breaks = sealed_state_at(member["events"], signal_hhmm)
            ever += 1
            active += int(member_active)
            break_count += member_breaks
            if member["market_segment"] == segment:
                same_segment_ever += 1
                same_segment_active += int(member_active)
            if industry != "unknown" and member["industry_current_proxy"] == industry:
                same_industry_ever += 1
                same_industry_active += int(member_active)
            member_signal = int(member["eligible_signal_hhmm"])
            prior_signals += int(0 < member_signal < signal_hhmm)
            same_minute_signals += int(member_signal == signal_hhmm)
        last_break = last_event_hhmm(
            own_events, "LIMIT_OPEN_BREAK", before_or_at=signal_hhmm
        )
        context_rows.append(
            {
                "trade_date": date,
                "ts_code": code,
                "industry_current_proxy": industry,
                "industry_proxy_asof_certified": False,
                "first_to_signal_minutes": trading_minutes_between(
                    int(row.first_seal_hhmm), signal_hhmm
                ),
                "last_break_hhmm_before_signal": last_break,
                "last_break_to_signal_minutes": trading_minutes_between(
                    last_break, signal_hhmm
                ),
                "intraday_first_board_ever_sealed_count": ever,
                "intraday_first_board_active_sealed_count": active,
                "intraday_first_board_opened_count": ever - active,
                "intraday_first_board_break_event_count": break_count,
                "intraday_first_board_seal_rate": active / ever if ever else 0.0,
                "same_segment_ever_sealed_count": same_segment_ever,
                "same_segment_active_sealed_count": same_segment_active,
                "same_segment_seal_rate": (
                    same_segment_active / same_segment_ever
                    if same_segment_ever
                    else 0.0
                ),
                "same_industry_ever_sealed_count_current_proxy": same_industry_ever,
                "same_industry_active_sealed_count_current_proxy": same_industry_active,
                "same_industry_seal_rate_current_proxy": (
                    same_industry_active / same_industry_ever
                    if same_industry_ever
                    else 0.0
                ),
                "prior_path_signal_count": prior_signals,
                "same_minute_path_signal_count": same_minute_signals,
            }
        )
    context = pd.DataFrame(context_rows)
    return signals.merge(
        context, on=["trade_date", "ts_code"], how="left", validate="one_to_one"
    )


def prior_day_metrics(signals: pd.DataFrame) -> dict[tuple[str, str], tuple[float, float]]:
    result: dict[tuple[str, str], tuple[float, float]] = {}
    for date, sample in signals.groupby("previous_trade_date", sort=True):
        path = DAILY_DIR / f"{date}.csv"
        if not path.exists():
            raise FileNotFoundError(f"D信号量价研究缺少前一交易日日线：{path}")
        daily = pd.read_csv(path, dtype={"ts_code": str}, low_memory=False)
        daily = daily.drop_duplicates("ts_code", keep="last").set_index("ts_code")
        for code in sample["ts_code"].astype(str):
            if code not in daily.index:
                continue
            row = daily.loc[code]
            result[(str(date), code)] = (
                float(pd.to_numeric(row.get("amount", 0), errors="coerce") or 0),
                float(pd.to_numeric(row.get("vol", 0), errors="coerce") or 0),
            )
    return result


def consecutive_true_at_end(values: pd.Series) -> int:
    count = 0
    for value in reversed(values.astype(bool).tolist()):
        if not value:
            break
        count += 1
    return count


def extract_signal_minute_features(
    signals: pd.DataFrame, minute_path: Path
) -> pd.DataFrame:
    """再次流式扫描大文件，但只为2,167个信号计算时点量价字段。"""

    signal_index = {
        (str(row.trade_date), str(row.ts_code)): row
        for row in signals.itertuples(index=False)
    }
    prior_metrics = prior_day_metrics(signals)
    rows: list[dict[str, Any]] = []
    for key, group in iter_minute_groups(minute_path):
        signal = signal_index.get(key)
        if signal is None:
            continue
        bars = normalize_minute_bars(group)
        signal_hhmm = int(signal.eligible_signal_hhmm)
        before = bars[bars["hhmm"].le(signal_hhmm)].copy()
        if before.empty:
            raise RuntimeError(f"D信号没有信号前分钟K：{key}")
        prev_amount, prev_volume = prior_metrics.get(
            (str(signal.previous_trade_date), str(signal.ts_code)), (0.0, 0.0)
        )
        # stk_mins amount为元、volume为股；daily amount为千元、vol为手。
        cumulative_amount = float(before["amount"].fillna(0).sum()) / 1000.0
        cumulative_volume = float(before["volume"].fillna(0).sum()) / 100.0
        recent_5_amount = float(before.tail(5)["amount"].fillna(0).sum()) / 1000.0
        recent_10_amount = float(before.tail(10)["amount"].fillna(0).sum()) / 1000.0
        total_raw_amount = float(before["amount"].fillna(0).sum())
        total_raw_volume = float(before["volume"].fillna(0).sum())
        vwap = total_raw_amount / total_raw_volume if total_raw_volume > 0 else 0.0
        pre_close = float(signal.pre_close)
        limit_price = float(signal.limit_price)
        at_limit_close = before["close"].sub(limit_price).abs().le(0.001)
        rows.append(
            {
                "trade_date": key[0],
                "ts_code": key[1],
                "signal_cumulative_amount": cumulative_amount,
                "signal_cumulative_volume": cumulative_volume,
                "previous_day_amount": prev_amount,
                "previous_day_volume": prev_volume,
                "signal_cumulative_amount_vs_prev_day": (
                    cumulative_amount / prev_amount if prev_amount > 0 else np.nan
                ),
                "signal_recent_5m_amount_vs_prev_day": (
                    recent_5_amount / prev_amount if prev_amount > 0 else np.nan
                ),
                "signal_recent_10m_amount_vs_prev_day": (
                    recent_10_amount / prev_amount if prev_amount > 0 else np.nan
                ),
                "signal_cumulative_volume_vs_prev_day": (
                    cumulative_volume / prev_volume if prev_volume > 0 else np.nan
                ),
                "signal_open_gap_pct": (
                    float(before.iloc[0]["open"]) / pre_close - 1.0
                    if pre_close > 0
                    else np.nan
                ),
                "pre_signal_min_return_from_preclose": (
                    float(before["low"].min()) / pre_close - 1.0
                    if pre_close > 0
                    else np.nan
                ),
                "pre_signal_vwap_return_from_preclose": (
                    vwap / pre_close - 1.0 if pre_close > 0 and vwap > 0 else np.nan
                ),
                "pre_signal_intraday_range_pct": (
                    float(before["high"].max()) / float(before["low"].min()) - 1.0
                    if float(before["low"].min()) > 0
                    else np.nan
                ),
                "limit_close_bar_share_before_signal": float(at_limit_close.mean()),
                "consecutive_limit_close_bars_at_signal": consecutive_true_at_end(
                    at_limit_close
                ),
                "signal_day_amount_completion_fraction": (
                    cumulative_amount / float(signal.daily_amount)
                    if float(signal.daily_amount) > 0
                    else np.nan
                ),
            }
        )
    features = pd.DataFrame(rows)
    if len(features) != len(signals):
        missing = sorted(set(signal_index) - set(zip(features["trade_date"], features["ts_code"])))
        raise RuntimeError(
            f"D信号量价特征不完整：expected={len(signals)} actual={len(features)} "
            f"missing={missing[:10]}"
        )
    return signals.merge(
        features, on=["trade_date", "ts_code"], how="left", validate="one_to_one"
    )


def add_buckets(signals: pd.DataFrame) -> pd.DataFrame:
    frame = signals.copy()
    frame["signal_time_bucket"] = pd.cut(
        frame["eligible_signal_hhmm"],
        bins=[1399, 1414, 1429, 1444, 1454],
        labels=["14:00-14:14", "14:15-14:29", "14:30-14:44", "14:45-14:54"],
    ).astype(str)
    frame["first_to_signal_bucket"] = pd.cut(
        frame["first_to_signal_minutes"],
        bins=[-1, 15, 30, 60, 120, 241],
        labels=["0_15", "16_30", "31_60", "61_120", "121_plus"],
    ).astype(str)
    frame["last_break_to_signal_bucket"] = pd.cut(
        frame["last_break_to_signal_minutes"],
        bins=[-1, 1, 2, 5, 10, 241],
        labels=["same_or_1m", "2m", "3_5m", "6_10m", "11m_plus"],
    ).astype(str)
    frame["intraday_ever_sealed_bucket"] = pd.cut(
        frame["intraday_first_board_ever_sealed_count"],
        bins=[-1, 20, 40, 60, 100, 150, 10_000],
        labels=["0_20", "21_40", "41_60", "61_100", "101_150", "151_plus"],
    ).astype(str)
    frame["intraday_seal_rate_bucket"] = pd.cut(
        frame["intraday_first_board_seal_rate"],
        bins=[-0.001, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0],
        labels=["lt40", "40_50", "50_60", "60_70", "70_80", "80_100"],
    ).astype(str)
    frame["amount_vs_prev_bucket"] = pd.cut(
        frame["signal_cumulative_amount_vs_prev_day"],
        bins=[-np.inf, 0.5, 1.0, 1.5, 2.0, 3.0, np.inf],
        labels=["lt0.5", "0.5_1.0", "1.0_1.5", "1.5_2.0", "2.0_3.0", "3.0_plus"],
    ).astype(str)
    frame["fill_evidence_after_signal"] = np.where(
        frame["confirmed_fill_by_price"], "PRICE_CONFIRMED", "QUEUE_UNKNOWN"
    )
    frame["close_result_after_signal"] = np.where(
        frame["failed_to_close_at_limit"], "FAILED_CLOSE", "CLOSED_AT_LIMIT"
    )
    return frame


def max_consecutive_losses(values: np.ndarray) -> int:
    maximum = current = 0
    for value in values:
        current = current + 1 if float(value) <= 0 else 0
        maximum = max(maximum, current)
    return maximum


def event_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    values = pd.to_numeric(frame["account_return"], errors="coerce").dropna().to_numpy(float)
    if len(values) == 0:
        return {
            "sample_count": 0,
            "trading_day_count": 0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "diagnostic_event_multiple": 1.0,
            "max_drawdown": 0.0,
            "profit_loss_ratio": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "max_consecutive_losses": 0,
            "explosion_rate_gte_10pct": 0.0,
            "big_loss_rate_lte_minus_5pct": 0.0,
            "failed_close_rate": 0.0,
            "price_confirmed_fill_rate": 0.0,
        }
    positive = values[values > 0]
    negative = values[values < 0]
    compound = mechanical_compound(values)
    return {
        "sample_count": int(len(values)),
        "trading_day_count": int(frame.loc[frame["account_return"].notna(), "trade_date"].nunique()),
        "win_rate": float((values > 0).mean()),
        "avg_account_return": float(values.mean()),
        "median_account_return": float(np.median(values)),
        "diagnostic_event_multiple": float(compound.equity_multiple),
        "max_drawdown": float(compound.max_drawdown),
        "profit_loss_ratio": (
            float(positive.mean() / abs(negative.mean()))
            if len(positive) and len(negative)
            else 0.0
        ),
        "max_profit": float(values.max()),
        "max_loss": float(values.min()),
        "max_consecutive_losses": max_consecutive_losses(values),
        "explosion_rate_gte_10pct": float((values >= 0.10).mean()),
        "big_loss_rate_lte_minus_5pct": float((values <= -0.05).mean()),
        "failed_close_rate": float(bool_series(frame["failed_to_close_at_limit"]).mean()),
        "price_confirmed_fill_rate": float(bool_series(frame["confirmed_fill_by_price"]).mean()),
    }


def factor_group_metrics(signals: pd.DataFrame) -> pd.DataFrame:
    dimensions = [
        ("SIGNAL_TIME", "首次封板时段", "first_time_bucket", True),
        ("SIGNAL_TIME", "第一次可交易回封时段", "signal_time_bucket", True),
        ("SIGNAL_TIME", "信号时炸板次数", "open_times_at_signal", True),
        ("SIGNAL_TIME", "首次封板到信号节奏", "first_to_signal_bucket", True),
        ("SIGNAL_TIME", "最后炸板到回封速度", "last_break_to_signal_bucket", True),
        ("SIGNAL_TIME", "盘中首板触板情绪", "intraday_ever_sealed_bucket", True),
        ("SIGNAL_TIME", "盘中首板封住率", "intraday_seal_rate_bucket", True),
        ("SIGNAL_TIME", "市场板块", "market_segment", True),
        ("SIGNAL_TIME", "信号前量比前日", "amount_vs_prev_bucket", True),
        ("NON_ASOF_METADATA", "当前行业代理", "industry_current_proxy", False),
        ("AFTER_SIGNAL_OUTCOME", "成交证据", "fill_evidence_after_signal", False),
        ("AFTER_SIGNAL_OUTCOME", "最终收盘成败", "close_result_after_signal", False),
    ]
    rows: list[dict[str, Any]] = []
    for timing, dimension, column, eligible in dimensions:
        for group, sample in signals.groupby(column, dropna=False, sort=True):
            if len(sample) < 3:
                continue
            rows.append(
                {
                    "scope": "EVENT_POPULATION_DIAGNOSTIC_NOT_STANDALONE",
                    "field_timing": timing,
                    "dimension": dimension,
                    "feature_column": column,
                    "group": str(group),
                    "eligible_for_candidate_rule": eligible,
                    **event_metrics(sample),
                }
            )
    return pd.DataFrame(rows)


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def candidate_rules() -> list[CandidateRule]:
    rules = [
        CandidateRule("path_all", "全部第一次可交易回封", tuple(), lambda x: pd.Series(True, index=x.index)),
        CandidateRule("first_before_1400", "首次封板早于14:00", ("first_seal_hhmm",), lambda x: numeric(x["first_seal_hhmm"]).lt(1400)),
        CandidateRule("signal_before_1430", "回封信号早于14:30", ("eligible_signal_hhmm",), lambda x: numeric(x["eligible_signal_hhmm"]).lt(1430)),
        CandidateRule("signal_before_1445", "回封信号早于14:45", ("eligible_signal_hhmm",), lambda x: numeric(x["eligible_signal_hhmm"]).lt(1445)),
        CandidateRule("open_times_2", "信号时炸板2次", ("open_times_at_signal",), lambda x: numeric(x["open_times_at_signal"]).eq(2)),
        CandidateRule("open_times_3", "信号时炸板3次", ("open_times_at_signal",), lambda x: numeric(x["open_times_at_signal"]).eq(3)),
        CandidateRule("fast_reseal_le_2m", "最后炸板后2分钟内回封", ("last_break_to_signal_minutes",), lambda x: numeric(x["last_break_to_signal_minutes"]).le(2)),
        CandidateRule("fast_reseal_le_5m", "最后炸板后5分钟内回封", ("last_break_to_signal_minutes",), lambda x: numeric(x["last_break_to_signal_minutes"]).le(5)),
        CandidateRule("first_to_signal_le_60m", "首次封板后60分钟内形成信号", ("first_to_signal_minutes",), lambda x: numeric(x["first_to_signal_minutes"]).le(60)),
        CandidateRule("seal_rate_ge_60", "信号时首板封住率至少60%", ("intraday_first_board_seal_rate",), lambda x: numeric(x["intraday_first_board_seal_rate"]).ge(0.60)),
        CandidateRule("seal_rate_ge_70", "信号时首板封住率至少70%", ("intraday_first_board_seal_rate",), lambda x: numeric(x["intraday_first_board_seal_rate"]).ge(0.70)),
        CandidateRule("ever_sealed_40_150", "信号时首板触板数40~150", ("intraday_first_board_ever_sealed_count",), lambda x: numeric(x["intraday_first_board_ever_sealed_count"]).between(40, 150)),
        CandidateRule("active_sealed_30_100", "信号时仍封住首板30~100", ("intraday_first_board_active_sealed_count",), lambda x: numeric(x["intraday_first_board_active_sealed_count"]).between(30, 100)),
        CandidateRule("same_segment_active_ge_10", "同市场板块已封住首板至少10只", ("same_segment_active_sealed_count",), lambda x: numeric(x["same_segment_active_sealed_count"]).ge(10)),
        CandidateRule("amount_vs_prev_ge_1", "信号前成交额至少为前日1倍", ("signal_cumulative_amount_vs_prev_day",), lambda x: numeric(x["signal_cumulative_amount_vs_prev_day"]).ge(1.0)),
        CandidateRule("amount_vs_prev_ge_1_5", "信号前成交额至少为前日1.5倍", ("signal_cumulative_amount_vs_prev_day",), lambda x: numeric(x["signal_cumulative_amount_vs_prev_day"]).ge(1.5)),
        CandidateRule("recent_5m_amount_ge_5pct_prev", "回封前5分钟成交额至少为前日5%", ("signal_recent_5m_amount_vs_prev_day",), lambda x: numeric(x["signal_recent_5m_amount_vs_prev_day"]).ge(0.05)),
        CandidateRule("recent_5m_amount_ge_10pct_prev", "回封前5分钟成交额至少为前日10%", ("signal_recent_5m_amount_vs_prev_day",), lambda x: numeric(x["signal_recent_5m_amount_vs_prev_day"]).ge(0.10)),
        CandidateRule("pre_signal_low_ge_0", "信号前最低价不低于昨收", ("pre_signal_min_return_from_preclose",), lambda x: numeric(x["pre_signal_min_return_from_preclose"]).ge(0.0)),
        CandidateRule("main_board_only", "仅沪深主板", ("market_segment",), lambda x: x["market_segment"].astype(str).isin({"sh_main", "sz_main"})),
        CandidateRule("first_before_1400_signal_before_1445", "首次封板早于14:00且14:45前回封", ("first_seal_hhmm", "eligible_signal_hhmm"), lambda x: numeric(x["first_seal_hhmm"]).lt(1400) & numeric(x["eligible_signal_hhmm"]).lt(1445)),
        CandidateRule("open3_signal_before_1445", "炸板3次且14:45前回封", ("open_times_at_signal", "eligible_signal_hhmm"), lambda x: numeric(x["open_times_at_signal"]).eq(3) & numeric(x["eligible_signal_hhmm"]).lt(1445)),
        CandidateRule("fast_reseal_seal_rate_ge_60", "5分钟内回封且首板封住率至少60%", ("last_break_to_signal_minutes", "intraday_first_board_seal_rate"), lambda x: numeric(x["last_break_to_signal_minutes"]).le(5) & numeric(x["intraday_first_board_seal_rate"]).ge(0.60)),
        CandidateRule("first_before_1400_fast_reseal", "首次封板早于14:00且5分钟内回封", ("first_seal_hhmm", "last_break_to_signal_minutes"), lambda x: numeric(x["first_seal_hhmm"]).lt(1400) & numeric(x["last_break_to_signal_minutes"]).le(5)),
        CandidateRule("open2_amount_ge_1_5", "炸板2次且信号前成交额至少前日1.5倍", ("open_times_at_signal", "signal_cumulative_amount_vs_prev_day"), lambda x: numeric(x["open_times_at_signal"]).eq(2) & numeric(x["signal_cumulative_amount_vs_prev_day"]).ge(1.5)),
        CandidateRule("fast_reseal_recent_volume", "5分钟内回封且近5分钟成交额至少前日5%", ("last_break_to_signal_minutes", "signal_recent_5m_amount_vs_prev_day"), lambda x: numeric(x["last_break_to_signal_minutes"]).le(5) & numeric(x["signal_recent_5m_amount_vs_prev_day"]).ge(0.05)),
        CandidateRule("emotion_amount_combo", "触板40~150、封住率60%以上且量比前日1倍", ("intraday_first_board_ever_sealed_count", "intraday_first_board_seal_rate", "signal_cumulative_amount_vs_prev_day"), lambda x: numeric(x["intraday_first_board_ever_sealed_count"]).between(40, 150) & numeric(x["intraday_first_board_seal_rate"]).ge(0.60) & numeric(x["signal_cumulative_amount_vs_prev_day"]).ge(1.0)),
    ]
    # 第二批来自上面的固定分箱诊断。仍然只使用信号时点字段，并完整披露
    # 同窗口二次搜索和多重比较风险，不能把窄样本的高复利直接当成发布证据。
    rules.extend(
        [
            CandidateRule("first_before_1430", "排除14:30后才首次封板", ("first_seal_hhmm",), lambda x: numeric(x["first_seal_hhmm"]).lt(1430)),
            CandidateRule("exclude_first_to_signal_le_15m", "排除15分钟内急促完成多次炸回", ("first_to_signal_minutes",), lambda x: numeric(x["first_to_signal_minutes"]).gt(15)),
            CandidateRule("first_to_signal_31_60m", "首次封板后31~60分钟形成信号", ("first_to_signal_minutes",), lambda x: numeric(x["first_to_signal_minutes"]).between(31, 60)),
            CandidateRule("last_break_to_signal_2_5m", "最后炸板后2~5分钟回封", ("last_break_to_signal_minutes",), lambda x: numeric(x["last_break_to_signal_minutes"]).between(2, 5)),
            CandidateRule("signal_1445_1454", "14:45~14:54形成第一次可交易回封", ("eligible_signal_hhmm",), lambda x: numeric(x["eligible_signal_hhmm"]).between(1445, 1454)),
            CandidateRule("seal_rate_40_50", "信号时首板封住率40%~50%", ("intraday_first_board_seal_rate",), lambda x: numeric(x["intraday_first_board_seal_rate"]).ge(0.40) & numeric(x["intraday_first_board_seal_rate"]).lt(0.50)),
            CandidateRule("seal_rate_40_60", "信号时首板封住率40%~60%", ("intraday_first_board_seal_rate",), lambda x: numeric(x["intraday_first_board_seal_rate"]).ge(0.40) & numeric(x["intraday_first_board_seal_rate"]).lt(0.60)),
            CandidateRule("ever_sealed_101_150", "信号时首板触板数101~150", ("intraday_first_board_ever_sealed_count",), lambda x: numeric(x["intraday_first_board_ever_sealed_count"]).between(101, 150)),
            CandidateRule("amount_vs_prev_2_3", "信号前成交额为前日2~3倍", ("signal_cumulative_amount_vs_prev_day",), lambda x: numeric(x["signal_cumulative_amount_vs_prev_day"]).between(2.0, 3.0, inclusive="left")),
            CandidateRule("star_only", "仅科创板", ("market_segment",), lambda x: x["market_segment"].astype(str).eq("star")),
            CandidateRule("chi_next_only", "仅创业板", ("market_segment",), lambda x: x["market_segment"].astype(str).eq("chi_next")),
            CandidateRule("star_or_chi_next", "仅科创板或创业板", ("market_segment",), lambda x: x["market_segment"].astype(str).isin({"star", "chi_next"})),
            CandidateRule("star_seal_rate_40_60", "科创板且首板封住率40%~60%", ("market_segment", "intraday_first_board_seal_rate"), lambda x: x["market_segment"].astype(str).eq("star") & numeric(x["intraday_first_board_seal_rate"]).ge(0.40) & numeric(x["intraday_first_board_seal_rate"]).lt(0.60)),
            CandidateRule("chi_next_seal_rate_40_60", "创业板且首板封住率40%~60%", ("market_segment", "intraday_first_board_seal_rate"), lambda x: x["market_segment"].astype(str).eq("chi_next") & numeric(x["intraday_first_board_seal_rate"]).ge(0.40) & numeric(x["intraday_first_board_seal_rate"]).lt(0.60)),
            CandidateRule("seal_rate_40_50_first_31_60m", "首板封住率40%~50%且31~60分钟形成信号", ("intraday_first_board_seal_rate", "first_to_signal_minutes"), lambda x: numeric(x["intraday_first_board_seal_rate"]).ge(0.40) & numeric(x["intraday_first_board_seal_rate"]).lt(0.50) & numeric(x["first_to_signal_minutes"]).between(31, 60)),
            CandidateRule("seal_rate_40_60_amount_2_3", "首板封住率40%~60%且量比前日2~3倍", ("intraday_first_board_seal_rate", "signal_cumulative_amount_vs_prev_day"), lambda x: numeric(x["intraday_first_board_seal_rate"]).ge(0.40) & numeric(x["intraday_first_board_seal_rate"]).lt(0.60) & numeric(x["signal_cumulative_amount_vs_prev_day"]).between(2.0, 3.0, inclusive="left")),
            CandidateRule("star_first_31_60m", "科创板且31~60分钟形成信号", ("market_segment", "first_to_signal_minutes"), lambda x: x["market_segment"].astype(str).eq("star") & numeric(x["first_to_signal_minutes"]).between(31, 60)),
            CandidateRule("chi_next_first_31_60m", "创业板且31~60分钟形成信号", ("market_segment", "first_to_signal_minutes"), lambda x: x["market_segment"].astype(str).eq("chi_next") & numeric(x["first_to_signal_minutes"]).between(31, 60)),
            CandidateRule("star_signal_before_1430", "科创板且14:30前形成回封信号", ("market_segment", "eligible_signal_hhmm"), lambda x: x["market_segment"].astype(str).eq("star") & numeric(x["eligible_signal_hhmm"]).lt(1430)),
            CandidateRule("first_31_60m_last_break_2_5m", "首次到信号31~60分钟且最后炸板后2~5分钟回封", ("first_to_signal_minutes", "last_break_to_signal_minutes"), lambda x: numeric(x["first_to_signal_minutes"]).between(31, 60) & numeric(x["last_break_to_signal_minutes"]).between(2, 5)),
        ]
    )
    for rule in rules:
        unknown = set(rule.fields) - SIGNAL_TIME_ALLOWED_FIELDS
        forbidden = set(rule.fields) & FUTURE_OR_NON_ASOF_FIELDS
        if unknown or forbidden:
            raise ValueError(
                f"D候选规则{rule.name}字段非法：unknown={sorted(unknown)} "
                f"forbidden={sorted(forbidden)}"
            )
    return rules


def select_daily_first(signals: pd.DataFrame, rule: CandidateRule) -> pd.DataFrame:
    """先过滤再取当日最早信号；同一分钟仅用当时量价做稳定排序。"""

    selected = signals[rule.predicate(signals).fillna(False)].copy()
    if selected.empty:
        return selected
    selected["_open2_priority"] = numeric(selected["open_times_at_signal"]).eq(2).astype(int)
    selected["_same_minute_volume_rank"] = numeric(
        selected["signal_recent_5m_amount_vs_prev_day"]
    ).fillna(-np.inf)
    return (
        selected.sort_values(
            [
                "trade_date",
                "eligible_signal_hhmm",
                "_open2_priority",
                "_same_minute_volume_rank",
                "ts_code",
            ],
            ascending=[True, True, False, False, True],
        )
        .groupby("trade_date", as_index=False)
        .head(1)
        .reset_index(drop=True)
    )


def d_outcome_frame(picks: pd.DataFrame, *, confirmed_only: bool) -> pd.DataFrame:
    sample = picks.copy()
    if confirmed_only:
        sample = sample[sample["confirmed_fill_by_price"]].copy()
    if sample.empty:
        return pd.DataFrame(
            columns=[
                "signal_date", "strategy_leg", "ts_code", "name", "status",
                "exit_date", "account_return",
            ]
        )
    sample["signal_date"] = sample["trade_date"].astype(str)
    sample["strategy_leg"] = "D"
    sample["status"] = sample["execution_status"].astype(str)
    return sample.sort_values("signal_date").reset_index(drop=True)


def flatten(prefix: str, metrics: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "trade_count",
        "win_rate",
        "avg_account_return",
        "median_account_return",
        "equity_multiple",
        "max_drawdown",
        "max_profit",
        "max_loss",
        "profit_loss_ratio",
        "max_consecutive_losses",
    )
    return {f"{prefix}_{key}": metrics.get(key) for key in keys}


def assert_formal_baseline(
    d_metrics: dict[str, Any], combo_metrics: dict[str, Any]
) -> None:
    counts = {
        leg: int(combo_metrics["leg_counts"].get(leg, 0))
        for leg in ("D", "A", "E", "C")
    }
    failures: list[str] = []
    if int(d_metrics["trade_count"]) != BASELINE_D_TRADE_COUNT:
        failures.append(f"D笔数={d_metrics['trade_count']}")
    if abs(float(d_metrics["equity_multiple"]) - BASELINE_D_MULTIPLE) > TOLERANCE:
        failures.append(f"D复利={d_metrics['equity_multiple']}")
    if int(combo_metrics["trade_count"]) != BASELINE_ACDE_TRADE_COUNT:
        failures.append(f"ACDE笔数={combo_metrics['trade_count']}")
    if abs(float(combo_metrics["equity_multiple"]) - BASELINE_ACDE_MULTIPLE) > TOLERANCE:
        failures.append(f"ACDE复利={combo_metrics['equity_multiple']}")
    if counts != BASELINE_ACDE_LEG_COUNTS:
        failures.append(f"ACDE腿数={counts}")
    if failures:
        raise RuntimeError("D全窗口研究正式基线漂移：" + "；".join(failures))


def evaluate_gates(
    signals: pd.DataFrame,
    *,
    other_legs: dict[str, pd.DataFrame],
    baseline_d_metrics: dict[str, Any],
    baseline_combo_metrics: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    rows: list[dict[str, Any]] = []
    conservative_details: dict[str, pd.DataFrame] = {}
    combo_details: dict[str, pd.DataFrame] = {}
    for rule in candidate_rules():
        picks = select_daily_first(signals, rule)
        conservative = d_outcome_frame(picks, confirmed_only=True)
        optimistic = d_outcome_frame(picks, confirmed_only=False)
        conservative_standalone = replay_d_only(conservative, START, END)
        optimistic_standalone = replay_d_only(optimistic, START, END)
        conservative_d_metrics = executed_metrics(conservative_standalone)
        optimistic_d_metrics = executed_metrics(optimistic_standalone)
        conservative_combo, conservative_combo_metrics = combo_replay(
            conservative, other_legs
        )
        optimistic_combo, optimistic_combo_metrics = combo_replay(optimistic, other_legs)
        first_half = executed_metrics(
            conservative_standalone[
                conservative_standalone["signal_date"].between(START, FIRST_12M_END)
            ]
        )
        second_half = executed_metrics(
            conservative_standalone[
                conservative_standalone["signal_date"].between(SECOND_12M_START, END)
            ]
        )
        d_improved = float(conservative_d_metrics["equity_multiple"]) > float(
            baseline_d_metrics["equity_multiple"]
        ) + TOLERANCE
        combo_improved = float(conservative_combo_metrics["equity_multiple"]) > float(
            baseline_combo_metrics["equity_multiple"]
        ) + TOLERANCE
        rows.append(
            {
                "rule": rule.name,
                "description": rule.description,
                "signal_time_fields": ",".join(rule.fields),
                "uses_only_signal_time_known_fields": True,
                "raw_signal_count": int(rule.predicate(signals).fillna(False).sum()),
                "candidate_day_count": int(len(picks)),
                "selected_price_confirmed_count": int(picks["confirmed_fill_by_price"].sum()),
                "selected_queue_unknown_count": int((~picks["confirmed_fill_by_price"]).sum()),
                **flatten("d_conservative", conservative_d_metrics),
                **flatten("combo_conservative", conservative_combo_metrics),
                "combo_conservative_d_count": int(conservative_combo_metrics["leg_counts"].get("D", 0)),
                "combo_conservative_a_count": int(conservative_combo_metrics["leg_counts"].get("A", 0)),
                "combo_conservative_e_count": int(conservative_combo_metrics["leg_counts"].get("E", 0)),
                "combo_conservative_c_count": int(conservative_combo_metrics["leg_counts"].get("C", 0)),
                **flatten("d_optimistic", optimistic_d_metrics),
                **flatten("combo_optimistic", optimistic_combo_metrics),
                "d_first_12m_trade_count": int(first_half["trade_count"]),
                "d_first_12m_multiple": float(first_half["equity_multiple"]),
                "d_second_12m_trade_count": int(second_half["trade_count"]),
                "d_second_12m_multiple": float(second_half["equity_multiple"]),
                "d_compound_improved": d_improved,
                "acde_compound_improved": combo_improved,
                "dual_gate_passed": bool(d_improved and combo_improved),
                "formal_rule_modified": False,
            }
        )
        conservative_details[rule.name] = conservative_standalone
        combo_details[rule.name] = conservative_combo
        LOGGER.info(
            "%s：保守D=%d笔/%.6f倍，ACDE=%d笔/%.6f倍，双门禁=%s",
            rule.name,
            int(conservative_d_metrics["trade_count"]),
            float(conservative_d_metrics["equity_multiple"]),
            int(conservative_combo_metrics["trade_count"]),
            float(conservative_combo_metrics["equity_multiple"]),
            bool(d_improved and combo_improved),
        )
    return pd.DataFrame(rows), conservative_details, combo_details


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D全窗口一分钟特征与双门禁研究")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--minute-bars", type=Path, default=DEFAULT_MINUTE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ledger_path = args.ledger if args.ledger.is_absolute() else ROOT / args.ledger
    minute_path = args.minute_bars if args.minute_bars.is_absolute() else ROOT / args.minute_bars
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    LOGGER.info("加载并冻结D全窗口一分钟事件账本")
    ledger, input_audit = load_ledger(ledger_path)
    known_gaps = load_known_data_gap_keys(ledger)
    known_mismatches = load_known_price_mismatch_keys(ledger)
    if len(known_gaps) != EXPECTED_GAP_COUNT or len(known_mismatches) != EXPECTED_MISMATCH_COUNT:
        raise RuntimeError("D已备案数据问题数量漂移")
    signals = signal_frame(ledger)
    LOGGER.info("重建信号时点首板情绪和板块联动")
    signals = add_intraday_context(ledger, signals)
    LOGGER.info("流式提取2,167个信号的时点量价字段")
    signals = extract_signal_minute_features(signals, minute_path)
    signals = add_buckets(signals)
    groups = factor_group_metrics(signals)
    valid_outcomes = signals[signals["execution_status"].eq("OK")].copy()
    overall_event_metrics = event_metrics(valid_outcomes)
    confirmed_event_metrics = event_metrics(
        valid_outcomes[valid_outcomes["confirmed_fill_by_price"]]
    )
    queue_unknown_event_metrics = event_metrics(
        valid_outcomes[~valid_outcomes["confirmed_fill_by_price"]]
    )
    failed_close_event_metrics = event_metrics(
        valid_outcomes[valid_outcomes["failed_to_close_at_limit"]]
    )
    explosion_by_date = (
        valid_outcomes.assign(
            explosion=valid_outcomes["account_return"].ge(0.10),
            big_loss=valid_outcomes["account_return"].le(-0.05),
        )
        .groupby("trade_date", as_index=False)
        .agg(
            signal_count=("ts_code", "size"),
            explosion_count=("explosion", "sum"),
            big_loss_count=("big_loss", "sum"),
            mean_account_return=("account_return", "mean"),
        )
        .sort_values(["explosion_count", "mean_account_return"], ascending=False)
    )

    LOGGER.info("重建冻结正式D与当前A/E/C基线")
    source, source_audit = strict.source_audit()
    if not bool(source_audit.get("passed")):
        raise RuntimeError("严格as-of源审计失败，拒绝D双门禁")
    daily_data = strict.daily_data()
    baseline_d_outcomes = strict.build_d(source, daily_data)
    baseline_d_detail = replay_d_only(baseline_d_outcomes, START, END)
    baseline_d_metrics = executed_metrics(baseline_d_detail)
    other_legs = build_current_other_legs()
    baseline_combo_detail, baseline_combo_metrics = combo_replay(
        baseline_d_outcomes, other_legs
    )
    assert_formal_baseline(baseline_d_metrics, baseline_combo_metrics)

    LOGGER.info("逐条执行D独立腿与ACDE逐腿替换双门禁")
    search, d_details, combo_details = evaluate_gates(
        signals,
        other_legs=other_legs,
        baseline_d_metrics=baseline_d_metrics,
        baseline_combo_metrics=baseline_combo_metrics,
    )
    passed = search[search["dual_gate_passed"]].copy()
    if passed.empty:
        selected_rule = ""
        formal_decision = "KEEP_CURRENT_D_NO_CANDIDATE_PASSED_BOTH_COMPOUND_GATES"
    else:
        passed = passed.sort_values(
            ["combo_conservative_equity_multiple", "d_conservative_equity_multiple"],
            ascending=False,
        )
        selected_rule = str(passed.iloc[0]["rule"])
        formal_decision = "CANDIDATE_PASSED_REQUIRES_FORMAL_RULE_IMPLEMENTATION_AND_RECERTIFICATION"

    summary = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "research_protocol": STRICT_DISCOVERY,
        "strategy": "D",
        "window": f"{START}~{END}",
        "research_objective": (
            "全面识别盘中打板爆发点和爆亏路径；仅信号时点字段可形成候选，"
            "并以D独立复利与ACDE逐腿替换双门禁决定是否修改正式D"
        ),
        "formal_rule_modified": False,
        "release_eligible": False,
        "input_audit": {
            **input_audit,
            "minute_path": str(minute_path.relative_to(ROOT)),
            "minute_sha256": sha256(minute_path),
            "known_empty_vendor_gap_count": len(known_gaps),
            "known_price_mismatch_count": len(known_mismatches),
            "all_eight_data_issues_fail_closed_keep_denominator": True,
            "strict_source_audit_passed": True,
        },
        "path_population": {
            "mother_pool_count": int(len(ledger)),
            "signal_count": int(len(signals)),
            "signal_day_count": int(signals["trade_date"].nunique()),
            "price_confirmed_fill_count": int(signals["confirmed_fill_by_price"].sum()),
            "queue_unknown_count": int((~signals["confirmed_fill_by_price"]).sum()),
            "failed_close_signal_count": int(signals["failed_to_close_at_limit"].sum()),
            "unresolved_exit_count": int(signals["execution_status"].ne("OK").sum()),
            "explosion_count_gte_10pct": int(signals["account_return"].ge(0.10).sum()),
            "big_loss_count_lte_minus_5pct": int(signals["account_return"].le(-0.05).sum()),
        },
        "outcome_diagnostics": {
            "scope_warning": "事件总体允许同日多信号，只用于爆发/爆亏特征诊断，不是D独立复利。",
            "all_resolved_path_signals": overall_event_metrics,
            "price_confirmed_fill_after_daily_order_unknown": confirmed_event_metrics,
            "queue_unknown_after_signal": queue_unknown_event_metrics,
            "failed_close_after_signal": failed_close_event_metrics,
            "top_explosion_dates": explosion_by_date.head(10).to_dict("records"),
        },
        "field_governance": {
            "signal_time_allowed_fields": sorted(SIGNAL_TIME_ALLOWED_FIELDS),
            "future_or_non_asof_forbidden_fields": sorted(FUTURE_OR_NON_ASOF_FIELDS),
            "industry_note": "当前stock_basic行业只作板块诊断，未取得历史as-of行业映射，不进入候选规则。",
            "final_close_note": "最终封板/失败和收盘情绪只作结果标签，禁止选股。",
        },
        "execution_model": {
            "daily_selection": "规则过滤后取当日最早可交易回封；同一分钟优先炸板2次、近5分钟量比、代码",
            "conservative_primary": "先选唯一候选；14:55前价格穿透涨停价才成交，未知队列记未成交",
            "optimistic_upper_bound": "同一已选候选的未知队列也假设成交",
            "no_future_fill_used_for_selection": True,
        },
        "frozen_formal_baseline": {
            "d": baseline_d_metrics,
            "acde": baseline_combo_metrics,
            "priority": "D>A>E>C",
            "position_pct": 0.825,
            "fees_slippage_limit_rules_t1_unchanged": True,
        },
        "candidate_search": {
            "rule_count": int(len(search)),
            "dual_gate_pass_count": int(len(passed)),
            "selected_rule": selected_rule,
            "formal_decision": formal_decision,
            "formal_rule_modified": False,
            "multiple_testing_risk": "全部候选属于当前24个月样本内STRICT_DISCOVERY，已同时报告前后12个月稳定性。",
        },
        "formal_decision": formal_decision,
        "limitations": [
            "一分钟bar采用收盘封板状态机械重建，同一分钟内逐笔先后仍不可见。",
            "457个始终封板信号缺历史队列深度；主门禁已保守记为未成交，乐观上界单列。",
            "当前行业分类不是历史as-of板块映射，只作诊断，不参与任何候选。",
            "机械复利是同口径研究标尺，不代表真实容量或未来收益。",
        ],
    }

    signals.to_csv(output_dir / "signal_feature_outcomes.csv", index=False, encoding="utf-8-sig")
    groups.to_csv(output_dir / "factor_group_metrics.csv", index=False, encoding="utf-8-sig")
    search.to_csv(output_dir / "candidate_rule_dual_gates.csv", index=False, encoding="utf-8-sig")
    explosion_by_date.to_csv(output_dir / "explosion_by_date.csv", index=False, encoding="utf-8-sig")
    valid_outcomes.sort_values("account_return", ascending=False).head(50).to_csv(
        output_dir / "top_explosion_events.csv", index=False, encoding="utf-8-sig"
    )
    valid_outcomes.sort_values("account_return", ascending=True).head(50).to_csv(
        output_dir / "top_big_loss_events.csv", index=False, encoding="utf-8-sig"
    )
    baseline_d_detail.to_csv(output_dir / "baseline_d_standalone_detail.csv", index=False, encoding="utf-8-sig")
    baseline_combo_detail.to_csv(output_dir / "baseline_acde_detail.csv", index=False, encoding="utf-8-sig")
    if selected_rule:
        d_details[selected_rule].to_csv(
            output_dir / "selected_rule_d_standalone_detail.csv", index=False, encoding="utf-8-sig"
        )
        combo_details[selected_rule].to_csv(
            output_dir / "selected_rule_acde_detail.csv", index=False, encoding="utf-8-sig"
        )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
