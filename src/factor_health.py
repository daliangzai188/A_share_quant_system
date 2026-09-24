"""因子健康监控：用整个涨停池衡量各腿条件集是否还有优势。

为什么需要它：实盘每月只有约5笔成交，等成交攒够证据太慢；而满足条件的涨停股
每月有十几到几十只，一年一两百只。条件集的优势先垮，成交结果才会垮，所以它是
更快的前瞻指标。

口径与A腿一致：信号日=涨停日，T+1开盘买入、T+2收盘卖出，前复权。
指标=滚动12个月的"匹配均值 − 不匹配均值"（差额剔掉了行情好坏），
失败线在配置里冻结，连续N个月跌破才报警。本模块只读数据、只出数字，不参与任何门禁。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


FORWARD_HOLD = 2  # T+1开盘买、T+2收盘卖


@dataclass(frozen=True)
class FactorHealthSettings:
    enabled: bool
    window: int
    min_periods: int
    min_samples: int
    consecutive_months: int
    lines: dict[str, float]


def load_settings(config: Mapping[str, Any]) -> FactorHealthSettings:
    section = dict(config.get("factor_health", {}) or {})
    return FactorHealthSettings(
        enabled=bool(section.get("enabled", False)),
        window=int(section.get("window_months", 12)),
        min_periods=int(section.get("min_periods", 10)),
        min_samples=int(section.get("min_samples", 60)),
        consecutive_months=int(section.get("consecutive_months", 2)),
        lines={str(k).upper(): float(v) for k, v in (section.get("lines", {}) or {}).items()},
    )


def parse_condition_profiles(strategy_config: Mapping[str, Any]) -> dict[str, list[list[tuple[str, list[str]]]]]:
    """从正式策略配置读取A/C的条件分支；换规则后指标自动跟着换。"""

    out: dict[str, list[list[tuple[str, list[str]]]]] = {}
    a_profiles = (strategy_config.get("candidate_filters", {}) or {}).get("condition_profiles", [])
    c_profiles = (
        (strategy_config.get("paper_ab_filtered_strategy", {}) or {})
        .get("c_strategy", {})
        .get("condition_profiles", [])
    )
    for leg, profiles in (("A", a_profiles), ("C", c_profiles)):
        parsed: list[list[tuple[str, list[str]]]] = []
        for profile in profiles or []:
            branch: list[tuple[str, list[str]]] = []
            for condition in profile.get("conditions", []) or []:
                column = condition.get("column")
                value = condition.get("value")
                if column is None or value is None:
                    branch = []
                    break
                values = [str(v) for v in value] if isinstance(value, list) else [str(value)]
                branch.append((str(column), values))
            if branch:
                parsed.append(branch)
        out[leg] = parsed
    return out


def condition_mask(frame: pd.DataFrame, branches: Sequence[Sequence[tuple[str, Sequence[str]]]]) -> tuple[pd.Series, int]:
    """分支之间取并集、分支内部取交集；字段缺失的分支跳过并计数。"""

    mask = pd.Series(False, index=frame.index)
    used = 0
    for branch in branches:
        sub = pd.Series(True, index=frame.index)
        ok = True
        for column, values in branch:
            if column not in frame.columns:
                ok = False
                break
            sub &= frame[column].astype(str).isin([str(v) for v in values])
        if ok:
            mask |= sub
            used += 1
    return mask, used


def attach_forward_returns(
    pool: pd.DataFrame,
    *,
    calendar_dates: Sequence[str],
    daily_dir: Path,
    adj_dir: Path,
    hold: int = FORWARD_HOLD,
) -> pd.DataFrame:
    """给涨停池每行补上 T+1开盘→T+(1+hold-1)收盘 的前复权收益；缺行情的行丢弃。"""

    dates = [str(d) for d in calendar_dates]
    index = {d: i for i, d in enumerate(dates)}
    frame = pool.copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame["entry_date"] = [
        dates[index[d] + 1] if index.get(d, -1) >= 0 and index[d] + 1 < len(dates) else ""
        for d in frame["trade_date"]
    ]
    frame["exit_date"] = [
        dates[index[d] + hold] if index.get(d, -1) >= 0 and index[d] + hold < len(dates) else ""
        for d in frame["trade_date"]
    ]
    wanted = sorted({d for d in frame["entry_date"] if d} | {d for d in frame["exit_date"] if d})
    codes = set(frame["ts_code"])
    quotes: list[pd.DataFrame] = []
    for date in wanted:
        path = daily_dir / f"{date}.csv"
        if not path.exists():
            continue
        day = pd.read_csv(path, usecols=["ts_code", "trade_date", "open", "close"],
                          dtype={"ts_code": str, "trade_date": str})
        quotes.append(day[day["ts_code"].isin(codes)])
    if not quotes:
        return frame.iloc[0:0].assign(ret=pd.Series(dtype=float))
    daily = pd.concat(quotes, ignore_index=True)
    adjs: list[pd.DataFrame] = []
    for date in wanted:
        path = adj_dir / f"{date}.csv"
        if not path.exists():
            continue
        item = pd.read_csv(path, usecols=["ts_code", "trade_date", "adj_factor"],
                           dtype={"ts_code": str, "trade_date": str})
        adjs.append(item[item["ts_code"].isin(codes)])
    if adjs:
        daily = daily.merge(pd.concat(adjs, ignore_index=True), on=["ts_code", "trade_date"], how="left")
    else:
        daily["adj_factor"] = 1.0
    daily["adj_factor"] = pd.to_numeric(daily["adj_factor"], errors="coerce").fillna(1.0)
    entry = daily.rename(columns={"trade_date": "entry_date", "open": "entry_open", "adj_factor": "entry_adj"})
    exit_ = daily.rename(columns={"trade_date": "exit_date", "close": "exit_close", "adj_factor": "exit_adj"})
    frame = frame.merge(entry[["ts_code", "entry_date", "entry_open", "entry_adj"]], on=["ts_code", "entry_date"], how="left")
    frame = frame.merge(exit_[["ts_code", "exit_date", "exit_close", "exit_adj"]], on=["ts_code", "exit_date"], how="left")
    frame["ret"] = (frame["exit_close"] * frame["exit_adj"]) / (frame["entry_open"] * frame["entry_adj"]) - 1.0
    return frame.dropna(subset=["ret"]).copy()


def monthly_edge(frame: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    """每月：匹配样本数、匹配均值、不匹配均值、差额。"""

    month = frame["trade_date"].astype(str).str[:6]
    hit = frame[mask].groupby(month[mask]).ret.agg(["size", "mean"])
    miss = frame[~mask].groupby(month[~mask]).ret.mean()
    months = sorted(set(month))
    return pd.DataFrame(
        {
            "n": hit["size"].reindex(months).fillna(0).astype(int),
            "hit": hit["mean"].reindex(months),
            "miss": miss.reindex(months),
        },
        index=months,
    ).assign(edge=lambda x: x["hit"] - x["miss"])


def rolling_health(
    monthly: pd.DataFrame,
    *,
    window: int,
    min_periods: int,
    min_samples: int,
    line: float | None,
    consecutive_months: int,
) -> dict[str, Any]:
    """滚动窗口差额、当前值、连续跌破月数与是否触发。"""

    rolled = monthly["edge"].rolling(window, min_periods=min_periods).mean()
    counts = monthly["n"].rolling(window, min_periods=min_periods).sum()
    valid = pd.DataFrame({"edge": rolled, "n": counts}).dropna()
    valid = valid[valid["n"] >= int(min_samples)]
    if valid.empty:
        return {"value": None, "samples": 0, "months_below": 0, "triggered": False,
                "line": line, "months": []}
    below: list[str] = []
    if line is not None:
        for month in reversed(list(valid.index)):
            if float(valid.loc[month, "edge"]) <= float(line):
                below.append(str(month))
            else:
                break
        below.reverse()
    return {
        "value": float(valid["edge"].iloc[-1]),
        "samples": int(valid["n"].iloc[-1]),
        "as_of": str(valid.index[-1]),
        "line": line,
        "months_below": len(below),
        "months": below,
        "need_months": int(consecutive_months),
        "triggered": line is not None and len(below) >= int(consecutive_months),
    }
