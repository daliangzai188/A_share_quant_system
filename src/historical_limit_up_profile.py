"""个股历史涨停后路径的严格时点画像。

本模块只构造研究特征，不改变任何正式策略。历史事件只有在其T+3收盘已经
发生后，才允许进入后续信号日的画像，防止把尚未发生的路径泄漏给候选筛选。
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


PROFILE_MEASURES = (
    "d2_return",
    "d3_return",
    "d23_return",
    "fade",
    "both_down",
    "entry_to_t2",
    "entry_to_t3",
)


def normalize_date(value: Any) -> str:
    """把CSV日期统一成YYYYMMDD；缺失值返回空字符串。"""

    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def open_trade_dates(calendar: pd.DataFrame) -> list[str]:
    """从项目交易日历提取已开市日期并去重排序。"""

    date_column = "cal_date" if "cal_date" in calendar.columns else calendar.columns[0]
    opened = calendar.copy()
    if "is_open" in opened.columns:
        opened = opened[
            opened["is_open"]
            .fillna("")
            .astype(str)
            .str.lower()
            .isin({"1", "1.0", "true", "yes"})
        ]
    return sorted({normalize_date(value) for value in opened[date_column] if normalize_date(value)})


def prepare_event_schedule(
    limit_events: pd.DataFrame,
    trade_dates: Sequence[str],
    *,
    start_date: str,
    cutoff: str,
) -> tuple[pd.DataFrame, dict[str, set[str]], dict[str, Any]]:
    """为每次涨停映射T+1、T+2、T+3，并汇总逐日需要读取的股票代码。"""

    required = {"trade_date", "ts_code"}
    missing = sorted(required.difference(limit_events.columns))
    if missing:
        raise ValueError(f"涨停事件缺少字段: {missing}")
    events = limit_events.copy()
    events["trade_date"] = events["trade_date"].map(normalize_date)
    events["ts_code"] = events["ts_code"].astype(str)
    events = events[
        events["trade_date"].between(str(start_date), str(cutoff), inclusive="both")
    ].copy()
    duplicate_count = int(events.duplicated(["trade_date", "ts_code"]).sum())
    events = events.drop_duplicates(["trade_date", "ts_code"], keep="last")

    dates = [str(value) for value in trade_dates]
    positions = {date: index for index, date in enumerate(dates)}
    rows: list[dict[str, str]] = []
    requests: dict[str, set[str]] = defaultdict(set)
    no_calendar = 0
    beyond_cutoff = 0
    for row in events.itertuples(index=False):
        event_date = str(row.trade_date)
        code = str(row.ts_code)
        index = positions.get(event_date)
        if index is None or index + 3 >= len(dates):
            no_calendar += 1
            continue
        d1_date, d2_date, d3_date = dates[index + 1 : index + 4]
        if d3_date > str(cutoff):
            beyond_cutoff += 1
            continue
        rows.append(
            {
                "event_date": event_date,
                "ts_code": code,
                "d1_date": d1_date,
                "d2_date": d2_date,
                "d3_date": d3_date,
                "outcome_available_date": d3_date,
            }
        )
        for date in (d1_date, d2_date, d3_date):
            requests[date].add(code)
    schedule = pd.DataFrame(rows)
    audit = {
        "source_event_count": int(len(events)),
        "source_duplicate_key_count": duplicate_count,
        "scheduled_event_count": int(len(schedule)),
        "no_calendar_event_count": int(no_calendar),
        "outcome_after_cutoff_count": int(beyond_cutoff),
        "requested_price_date_count": int(len(requests)),
    }
    return schedule, dict(requests), audit


def load_requested_quotes(
    daily_dir: Path,
    requests: Mapping[str, set[str]],
) -> tuple[dict[tuple[str, str], tuple[float, float, float]], dict[str, Any]]:
    """只保留事件路径涉及的日线行，避免把数GB全市场行情常驻内存。"""

    quotes: dict[tuple[str, str], tuple[float, float, float]] = {}
    missing_files: list[str] = []
    invalid_price_rows = 0
    for trade_date in sorted(requests):
        path = daily_dir / f"{trade_date}.csv"
        if not path.exists():
            missing_files.append(trade_date)
            continue
        frame = pd.read_csv(
            path,
            usecols=["ts_code", "open", "close", "pre_close"],
            dtype={"ts_code": str},
            low_memory=False,
        )
        frame = frame[frame["ts_code"].isin(requests[trade_date])].copy()
        for row in frame.itertuples(index=False):
            values = pd.to_numeric(
                pd.Series([row.open, row.close, row.pre_close]), errors="coerce"
            ).to_numpy(float)
            if not np.isfinite(values).all() or np.any(values <= 0):
                invalid_price_rows += 1
                continue
            quotes[(trade_date, str(row.ts_code))] = tuple(values.tolist())
    return quotes, {
        "quote_row_count": int(len(quotes)),
        "missing_daily_file_count": int(len(missing_files)),
        "missing_daily_files": missing_files,
        "invalid_price_row_count": int(invalid_price_rows),
    }


def build_event_outcomes(
    schedule: pd.DataFrame,
    quote_lookup: Mapping[tuple[str, str], tuple[float, float, float]],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """计算涨停后第2/3日路径；用pre_close链接处理除权除息。"""

    rows: list[dict[str, Any]] = []
    missing_quote_count = 0
    missing_by_stage = {"d1": 0, "d2": 0, "d3": 0}
    for raw in schedule.to_dict("records"):
        code = str(raw["ts_code"])
        d1 = quote_lookup.get((str(raw["d1_date"]), code))
        d2 = quote_lookup.get((str(raw["d2_date"]), code))
        d3 = quote_lookup.get((str(raw["d3_date"]), code))
        if d1 is None or d2 is None or d3 is None:
            missing_quote_count += 1
            missing_by_stage["d1"] += int(d1 is None)
            missing_by_stage["d2"] += int(d2 is None)
            missing_by_stage["d3"] += int(d3 is None)
            continue
        d1_open, d1_close, _d1_pre_close = d1
        _d2_open, d2_close, d2_pre_close = d2
        _d3_open, d3_close, d3_pre_close = d3
        d2_return = d2_close / d2_pre_close - 1.0
        d3_return = d3_close / d3_pre_close - 1.0
        d23_return = (1.0 + d2_return) * (1.0 + d3_return) - 1.0
        d1_factor = d1_close / d1_open
        rows.append(
            {
                **raw,
                "d1_intraday_return": d1_factor - 1.0,
                "d2_return": d2_return,
                "d3_return": d3_return,
                "d23_return": d23_return,
                "fade": float(d23_return < 0.0),
                "both_down": float(d2_return < 0.0 and d3_return < 0.0),
                "entry_to_t2": d1_factor * (1.0 + d2_return) - 1.0,
                "entry_to_t3": d1_factor
                * (1.0 + d2_return)
                * (1.0 + d3_return)
                - 1.0,
            }
        )
    outcomes = pd.DataFrame(rows)
    audit = {
        "valid_outcome_count": int(len(outcomes)),
        "missing_quote_event_count": int(missing_quote_count),
        "missing_quote_by_stage": missing_by_stage,
        "valid_outcome_ratio": (
            float(len(outcomes) / len(schedule)) if len(schedule) else 0.0
        ),
    }
    return outcomes, audit


def attach_point_in_time_profiles(
    rows: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    date_column: str = "trade_date",
    code_column: str = "ts_code",
    recent_windows: Iterable[int] = (2, 3, 5),
) -> pd.DataFrame:
    """附加历史画像，且仅使用T+3已在当前信号日收盘前完成的旧事件。"""

    if date_column not in rows.columns or code_column not in rows.columns:
        raise ValueError("待画像数据缺少日期或股票代码")
    if outcomes.empty:
        result = rows.copy()
        result["hist_limit_event_count"] = 0
        return result
    required = {"event_date", "outcome_available_date", "ts_code", *PROFILE_MEASURES}
    missing = sorted(required.difference(outcomes.columns))
    if missing:
        raise ValueError(f"历史事件结果缺少字段: {missing}")

    history_by_code: dict[str, pd.DataFrame] = {}
    for code, group in outcomes.groupby("ts_code", sort=False):
        ordered = group.copy()
        ordered["outcome_available_date"] = ordered["outcome_available_date"].map(
            normalize_date
        )
        history_by_code[str(code)] = ordered.sort_values(
            ["outcome_available_date", "event_date"]
        ).reset_index(drop=True)

    windows = sorted({int(value) for value in recent_windows if int(value) > 0})
    feature_rows: list[dict[str, Any]] = []
    for raw_date, raw_code in zip(rows[date_column], rows[code_column]):
        signal_date = normalize_date(raw_date)
        code = str(raw_code)
        history = history_by_code.get(code)
        features: dict[str, Any] = {"hist_limit_event_count": 0}
        if history is None:
            feature_rows.append(features)
            continue
        available_dates = history["outcome_available_date"].tolist()
        count = bisect_right(available_dates, signal_date)
        known = history.iloc[:count]
        features["hist_limit_event_count"] = int(count)
        if known.empty:
            feature_rows.append(features)
            continue
        latest = known.iloc[-1]
        features["hist_last_event_date"] = str(latest["event_date"])
        features["hist_last_outcome_available_date"] = str(
            latest["outcome_available_date"]
        )
        for measure in PROFILE_MEASURES:
            features[f"hist_last_{measure}"] = float(latest[measure])
        for window in windows:
            recent = known.tail(window)
            features[f"hist_recent{window}_count"] = int(len(recent))
            for measure in PROFILE_MEASURES:
                suffix = "rate" if measure in {"fade", "both_down"} else "mean"
                features[f"hist_recent{window}_{measure}_{suffix}"] = float(
                    pd.to_numeric(recent[measure], errors="coerce").mean()
                )
        feature_rows.append(features)
    return pd.concat(
        [rows.reset_index(drop=True), pd.DataFrame(feature_rows)], axis=1
    )


def rule_mask(frame: pd.DataFrame, rule: Mapping[str, Any]) -> pd.Series:
    """执行配置化研究规则；历史不足或字段缺失一律不判为坏样本。"""

    minimum = int(rule.get("minimum_history_count", 0))
    mask = pd.to_numeric(
        frame.get("hist_limit_event_count", pd.Series(0, index=frame.index)),
        errors="coerce",
    ).fillna(0).ge(minimum)
    conditions = rule.get("all") or [rule]
    for condition in conditions:
        metric = str(condition.get("metric", ""))
        if not metric or metric not in frame.columns:
            return pd.Series(False, index=frame.index, dtype="bool")
        values = pd.to_numeric(frame[metric], errors="coerce")
        threshold = float(condition["threshold"])
        operator = str(condition["operator"])
        if operator == "<=":
            mask &= values.le(threshold)
        elif operator == ">=":
            mask &= values.ge(threshold)
        elif operator == "<":
            mask &= values.lt(threshold)
        elif operator == ">":
            mask &= values.gt(threshold)
        else:
            raise ValueError(f"不支持的历史涨停规则运算符: {operator}")
    return mask.fillna(False).astype(bool)


def wilson_interval(successes: int, samples: int, z: float = 1.96) -> tuple[float, float]:
    """返回二项比例的Wilson 95%区间，避免小样本正态近似失真。"""

    if samples <= 0:
        return (float("nan"), float("nan"))
    probability = successes / samples
    denominator = 1.0 + z * z / samples
    centre = (probability + z * z / (2.0 * samples)) / denominator
    margin = (
        z
        * np.sqrt(
            probability * (1.0 - probability) / samples
            + z * z / (4.0 * samples * samples)
        )
        / denominator
    )
    return (float(centre - margin), float(centre + margin))
