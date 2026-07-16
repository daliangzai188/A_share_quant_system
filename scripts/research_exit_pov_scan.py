#!/usr/bin/env python3
"""回放 1000 万级尾盘 POV 平仓，并扫描执行参数。

回放与实盘时序保持一致：

* 13:00 只使用上午已经完成的成交额，倒推 5 分钟 POV 的动态启动时间；
* 14:30 对13:00未触发标的按已实现午后流速复核，容量不足则从下一片启动；
* 5 分钟信号最多到 14:40，信号只能使用刚结束的 bar，成交落在下一根 bar；
* 14:45 用最近 15 分钟流速复查余量；
* 14:46~14:52 用上一根已完成的 1 分钟 bar 决定下一根 bar 的委托量；
* 14:53 停止新 POV，14:55 主单只卖实际余量；因实盘14:56:20起撤单交接，
  保守只计完整可用的14:55~14:56连续竞价容量，不把14:56~14:57整分钟算满；
* 15:00 收盘集合竞价只承接最后余量。

分钟总成交额不等于买盘深度。capacity_haircut 默认取 50%，即每根 bar
最多只把总成交额的一半视为可供主动卖单使用的压力容量。价格同时输出成交
VWAP 代理（中性）与 bar 最低价代理（压力）；它们都不是 Level-2 逐笔成交承诺。

数据限制必须保留在报告中：当前抓取脚本把策略 C 错按 T+2 下载，而策略 C
实际是 T+3 平仓，因此 C 全部排除；抓取来源是 ABCE2 审计，不包含策略 L，
因此 L 没有样本，也不能用本报告替代 L 的验证。
"""

from __future__ import annotations

import argparse
import bisect
import itertools
import math
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_5M = ROOT / "data" / "processed" / "research_exit_5m_vol.csv"
DEFAULT_1M = ROOT / "data" / "processed" / "research_exit_1m_tail.csv"
DEFAULT_OUT = ROOT / "reports" / "exit_pov_optimization"
DEFAULT_AUDIT = ROOT / "reports" / "current_live_abce2_audit" / "current_live_abce2_detail.csv"
DEFAULT_CALENDAR = ROOT / "data" / "raw" / "trade_calendar.csv"
DEFAULT_DAILY_AMOUNT = ROOT / "data" / "processed" / "daily_amount_lookup.csv"

VALID_LEGS = ("A", "B", "E2")
INVALID_EXIT_DATE_LEGS = ("C",)
MISSING_LEGS = ("L",)
DEFAULT_BACKTEST_SELL_SLIPPAGE = {"A": 0.001, "B": 0.001, "E2": 0.001}
BASE_SIGNAL_TIMES = tuple(
    f"{hour:02d}{minute:02d}"
    for hour, minute in (
        (13, 5), (13, 10), (13, 15), (13, 20), (13, 25), (13, 30),
        (13, 35), (13, 40), (13, 45), (13, 50), (13, 55), (14, 0),
        (14, 5), (14, 10), (14, 15), (14, 20), (14, 25), (14, 30),
        (14, 35), (14, 40),
    )
)
LATE_SIGNAL_TIMES = ("1446", "1447", "1448", "1449", "1450", "1451", "1452")
MAIN_FILL_TIMES = ("1456",)


@dataclass(frozen=True)
class Scenario:
    base_participation: float
    late_participation: float
    runway_buffer: float
    capacity_haircut: float

    @property
    def scenario_id(self) -> str:
        return (
            f"base_{self.base_participation:.3f}__late_{self.late_participation:.3f}"
            f"__buffer_{self.runway_buffer:.2f}__haircut_{self.capacity_haircut:.2f}"
        )


@dataclass(frozen=True)
class TrendPolicy:
    name: str
    up_threshold: float | None
    up_participation_multiplier: float


@dataclass(frozen=True)
class PositionPolicy:
    name: str
    mode: str
    signal_amount_cap_pct: float | None
    use_tail_q10_cap: bool = False


def _parse_grid(raw: str) -> tuple[float, ...]:
    values = tuple(float(item.strip()) for item in str(raw).split(",") if item.strip())
    if not values:
        raise ValueError("参数网格不能为空")
    if any(value <= 0 for value in values):
        raise ValueError(f"参数网格必须全部大于 0：{raw}")
    return values


def _load_bars(path: Path, *, require_leg: bool) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    dtype = {"ts_code": str, "exit_date": str, "bar_time": str, "leg": str}
    raw = pd.read_csv(path, dtype=dtype, low_memory=False)
    required = {"ts_code", "exit_date", "bar_time", "open", "close", "high", "low", "volume", "amount"}
    if require_leg:
        required.add("leg")
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"{path} 缺少字段：{missing}")

    frame = raw.copy()
    frame["ts_code"] = frame["ts_code"].astype(str).str.strip()
    frame["exit_date"] = (
        frame["exit_date"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(8)
    )
    digits = frame["bar_time"].astype(str).str.replace(r"\D", "", regex=True)
    frame["hhmm"] = digits.str[-6:-2]
    frame["key"] = frame["ts_code"] + "|" + frame["exit_date"]
    if require_leg:
        frame["leg"] = frame["leg"].astype(str).str.strip().str.upper()
    for column in ("open", "close", "high", "low", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    duplicate_key_time = int(frame.duplicated(["key", "hhmm"], keep=False).sum())
    invalid_time = int((~frame["hhmm"].str.fullmatch(r"\d{4}", na=False)).sum())
    invalid_price = int(
        ((frame[["open", "close", "high", "low"]] <= 0).any(axis=1)
         | frame[["open", "close", "high", "low"]].isna().any(axis=1)).sum()
    )
    invalid_amount = int((frame["amount"].isna() | (frame["amount"] < 0)).sum())
    stats = {
        "rows_raw": int(len(frame)),
        "samples_raw": int(frame["key"].nunique()),
        "date_min": frame["exit_date"].min() if not frame.empty else "",
        "date_max": frame["exit_date"].max() if not frame.empty else "",
        "duplicate_key_time_rows": duplicate_key_time,
        "invalid_time_rows": invalid_time,
        "invalid_price_rows": invalid_price,
        "invalid_amount_rows": invalid_amount,
        "zero_amount_rows": int((frame["amount"].fillna(0) == 0).sum()),
        "null_numeric_cells": int(
            frame[["open", "close", "high", "low", "volume", "amount"]].isna().sum().sum()
        ),
    }
    if invalid_time or invalid_price or invalid_amount:
        raise ValueError(
            f"{path} 存在无法安全回放的数据：invalid_time={invalid_time}, "
            f"invalid_price={invalid_price}, invalid_amount={invalid_amount}"
        )
    # 重复 bar 会重复计算容量；报告保留问题数量，回放只保留最后一行。
    frame = frame.drop_duplicates(["key", "hhmm"], keep="last").copy()
    return frame, stats


def _bar_vwap(bar: Any) -> float:
    """兼容 QMT volume 为股或手两种口径，返回落在 OHLC 内的成交均价代理。"""
    amount = max(float(getattr(bar, "amount", 0.0) or 0.0), 0.0)
    volume = max(float(getattr(bar, "volume", 0.0) or 0.0), 0.0)
    close = float(getattr(bar, "close"))
    low = float(getattr(bar, "low"))
    high = float(getattr(bar, "high"))
    if amount <= 0 or volume <= 0:
        return close
    candidates = (amount / volume, amount / volume / 100.0)
    for candidate in candidates:
        if low * 0.995 <= candidate <= high * 1.005:
            return float(min(max(candidate, low), high))
    return close


def _floor_lot(quantity: float, lot_size: int = 100) -> int:
    if not np.isfinite(quantity) or quantity <= 0:
        return 0
    return int(quantity // lot_size) * lot_size


def _add_minutes(hhmm: str, minutes: int) -> str:
    total = int(hhmm[:2]) * 60 + int(hhmm[2:]) + minutes
    return f"{total // 60:02d}{total % 60:02d}"


def _as_bar_map(frame: pd.DataFrame) -> dict[str, Any]:
    return {str(row.hhmm): row for row in frame.sort_values("hhmm").itertuples(index=False)}


def _append_quality(
    rows: list[dict[str, Any]], check: str, status: str, value: Any, note: str
) -> None:
    rows.append({"check": check, "status": status, "value": value, "note": note})


def _build_quality(
    five: pd.DataFrame,
    one: pd.DataFrame,
    five_stats: dict[str, Any],
    one_stats: dict[str, Any],
) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict[str, Any]] = []
    for name, stats, expected in (("5m", five_stats, 48), ("1m_tail", one_stats, 16)):
        for field, value in stats.items():
            status = "WARN" if field in {
                "duplicate_key_time_rows", "invalid_time_rows", "invalid_price_rows",
                "invalid_amount_rows", "null_numeric_cells",
            } and int(value or 0) > 0 else "PASS"
            if field == "zero_amount_rows":
                status = "INFO"
            _append_quality(rows, f"{name}.{field}", status, value, f"数据集={name}")
        sizes = (five if name == "5m" else one).groupby("key").size()
        incomplete = int((sizes != expected).sum())
        _append_quality(
            rows, f"{name}.incomplete_samples", "PASS" if incomplete == 0 else "WARN",
            incomplete, f"每个样本预期 {expected} 根 bar",
        )

    five_leg = five[["key", "leg"]].drop_duplicates("key")
    five_keys = set(five["key"])
    one_keys = set(one["key"])
    common = five_keys & one_keys
    valid_five = set(five_leg[five_leg["leg"].isin(VALID_LEGS)]["key"])
    eligible = sorted(common & valid_five)
    missing_tail = sorted(valid_five - one_keys)
    c_keys = sorted(set(five_leg[five_leg["leg"].isin(INVALID_EXIT_DATE_LEGS)]["key"]))
    l_count = int((five_leg["leg"] == "L").sum())

    _append_quality(rows, "join.common_samples", "INFO", len(common), "5m 与尾盘 1m 可连接样本")
    _append_quality(
        rows, "exclude.C_wrong_exit_date", "EXCLUDED", len(c_keys),
        "抓取脚本统一用了 next_trade(signal, 2)，但 C 实盘/回测为 T+3 平仓；当前 C 日期错误，全部排除",
    )
    _append_quality(
        rows, "exclude.L_missing", "EXCLUDED", l_count,
        "抓取来源 current_live_abce2_detail 不含 L；L 样本为 0，不能据此验证 L",
    )
    _append_quality(
        rows, "exclude.valid_leg_missing_1m", "EXCLUDED" if missing_tail else "PASS", len(missing_tail),
        "缺少尾盘 1m，无法同构回放；keys=" + (";".join(missing_tail) if missing_tail else "无"),
    )
    _append_quality(
        rows, "eligible.A_B_E2_common", "PASS" if eligible else "FAIL", len(eligible),
        "只包含退出日正确且同时具备完整 5m/1m 的 A、B、E2",
    )
    for leg in (*VALID_LEGS, *INVALID_EXIT_DATE_LEGS, *MISSING_LEGS):
        count = int(sum(1 for key in eligible if five_leg.set_index("key").at[key, "leg"] == leg)) if leg in VALID_LEGS else int((five_leg["leg"] == leg).sum())
        _append_quality(
            rows, f"cohort.{leg}", "PASS" if leg in VALID_LEGS and count > 0 else "EXCLUDED",
            count, "有效共同样本" if leg in VALID_LEGS else "不进入参数统计",
        )

    # 15:00 收盘价应由两套粒度独立落在同一价格，用于识别错日/错标的。
    f_close = five[five["hhmm"] == "1500"][["key", "close"]].rename(columns={"close": "close_5m"})
    o_close = one[one["hhmm"] == "1500"][["key", "close"]].rename(columns={"close": "close_1m"})
    close_check = f_close.merge(o_close, on="key", how="inner")
    mismatch = int((~np.isclose(close_check["close_5m"], close_check["close_1m"], rtol=0, atol=0.011)).sum())
    _append_quality(
        rows, "join.close_1500_mismatch", "PASS" if mismatch == 0 else "WARN", mismatch,
        "5m 与 1m 的 15:00 close 允许 0.01 元舍入差",
    )
    _append_quality(
        rows, "scope.warning", "INFO", len(eligible),
        "样本集中于 QMT 可取历史区间且无 Level-2；参数扫描属于样本内容量压力测试，存在过拟合风险",
    )
    return pd.DataFrame(rows), eligible


def _normalize_date(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(8)


def _load_signal_metadata(
    *,
    audit_path: Path,
    calendar_path: Path,
    daily_amount_path: Path,
    eligible: list[str],
) -> pd.DataFrame:
    """把退出样本映射回信号日，并只读取所需的信号日成交额。

    A/B/E2 均为信号日后的第 2 个交易日退出。C 不进入此函数，因为其正确
    退出日是第 3 个交易日；使用同一映射会再次制造错日。
    """
    for path in (audit_path, calendar_path, daily_amount_path):
        if not path.exists():
            raise FileNotFoundError(path)
    audit = pd.read_csv(audit_path, dtype=str, low_memory=False)
    required_audit = {"operation_status", "strategy_leg", "signal_date", "ts_code"}
    missing_audit = sorted(required_audit - set(audit.columns))
    if missing_audit:
        raise ValueError(f"{audit_path} 缺少字段：{missing_audit}")
    audit = audit[
        audit["operation_status"].eq("HISTORICAL_SIM_FILLED")
        & audit["strategy_leg"].astype(str).str.upper().isin(VALID_LEGS)
    ].copy()
    audit["signal_date"] = _normalize_date(audit["signal_date"])
    audit["strategy_leg"] = audit["strategy_leg"].astype(str).str.upper()

    calendar = pd.read_csv(calendar_path, dtype={"cal_date": str, "is_open": str}, low_memory=False)
    required_calendar = {"cal_date", "is_open"}
    missing_calendar = sorted(required_calendar - set(calendar.columns))
    if missing_calendar:
        raise ValueError(f"{calendar_path} 缺少字段：{missing_calendar}")
    open_days = sorted(
        set(_normalize_date(calendar[calendar["is_open"].astype(str).eq("1")]["cal_date"]))
    )

    def next_trade_day(signal_date: str, offset: int = 2) -> str:
        index = bisect.bisect_right(open_days, signal_date)
        target_index = index + offset - 1
        return open_days[target_index] if target_index < len(open_days) else ""

    audit["exit_date_expected"] = audit["signal_date"].map(next_trade_day)
    audit["key"] = audit["ts_code"].astype(str) + "|" + audit["exit_date_expected"]
    wanted_keys = set(eligible)
    meta = audit[audit["key"].isin(wanted_keys)][
        ["key", "ts_code", "signal_date", "exit_date_expected", "strategy_leg"]
    ].drop_duplicates("key", keep="last")
    missing_keys = sorted(wanted_keys - set(meta["key"]))
    if missing_keys:
        raise ValueError(f"审计无法映射 {len(missing_keys)} 个有效样本：{missing_keys[:5]}")

    wanted_pairs = set(zip(meta["ts_code"], meta["signal_date"]))
    amount_parts: list[pd.DataFrame] = []
    for chunk in pd.read_csv(
        daily_amount_path,
        dtype={"trade_date": str, "ts_code": str},
        usecols=["trade_date", "ts_code", "amount_yuan"],
        chunksize=500_000,
        low_memory=False,
    ):
        chunk["trade_date"] = _normalize_date(chunk["trade_date"])
        pairs = pd.Series(list(zip(chunk["ts_code"], chunk["trade_date"])), index=chunk.index)
        selected = chunk[pairs.isin(wanted_pairs)]
        if not selected.empty:
            amount_parts.append(selected)
    if not amount_parts:
        raise ValueError(f"{daily_amount_path} 没有命中任何信号日成交额")
    amounts = pd.concat(amount_parts, ignore_index=True)
    amounts["amount_yuan"] = pd.to_numeric(amounts["amount_yuan"], errors="coerce")
    amounts = amounts.drop_duplicates(["ts_code", "trade_date"], keep="last")
    meta = meta.merge(
        amounts,
        left_on=["ts_code", "signal_date"],
        right_on=["ts_code", "trade_date"],
        how="left",
        validate="one_to_one",
    ).rename(columns={"amount_yuan": "signal_day_amount"})
    invalid_amount = meta["signal_day_amount"].isna() | (meta["signal_day_amount"] <= 0)
    if invalid_amount.any():
        bad = meta.loc[invalid_amount, "key"].tolist()
        raise ValueError(f"{len(bad)} 个样本缺少有效信号日成交额：{bad[:5]}")
    return meta[
        ["key", "signal_date", "exit_date_expected", "strategy_leg", "signal_day_amount"]
    ].sort_values(["exit_date_expected", "key"]).reset_index(drop=True)


def _attach_tail_calibration(
    metadata: pd.DataFrame,
    five: pd.DataFrame,
    one: pd.DataFrame,
    *,
    development_fraction: float = 2.0 / 3.0,
) -> tuple[pd.DataFrame, float]:
    """用时间前 2/3 样本校准信号日成交额到退出尾段容量的保守比例。"""
    tail_5m_times = {"1420", "1425", "1430", "1435", "1440", "1445"}
    tail_1m_times = {
        "1446", "1447", "1448", "1449", "1450", "1451", "1452", "1453",
        "1454", "1455", "1456", "1457", "1500",
    }
    tail_five = five[five["hhmm"].isin(tail_5m_times)].groupby("key")["amount"].sum()
    tail_one = one[one["hhmm"].isin(tail_1m_times)].groupby("key")["amount"].sum()
    result = metadata.copy()
    result["raw_tail_amount_1415_1500"] = (
        result["key"].map(tail_five).fillna(0.0) + result["key"].map(tail_one).fillna(0.0)
    )
    result["raw_tail_to_signal_amount_ratio"] = (
        result["raw_tail_amount_1415_1500"] / result["signal_day_amount"]
    )
    split_index = max(1, min(len(result) - 1, int(math.floor(len(result) * development_fraction))))
    result["sample_split"] = "validation"
    result.loc[result.index < split_index, "sample_split"] = "development"
    development = result[result["sample_split"] == "development"]
    tail_q10 = float(development["raw_tail_to_signal_amount_ratio"].quantile(0.10))
    if not np.isfinite(tail_q10) or tail_q10 <= 0:
        raise ValueError(f"开发集尾段容量 10 分位无效：{tail_q10}")
    return result, tail_q10


def _trend_adjusted_participation(
    bar_map: dict[str, Any],
    signal_hhmm: str,
    base_participation: float,
    policy: TrendPolicy,
    *,
    lookback_bars: int = 3,
) -> tuple[float, float, bool]:
    """只用当前及更早 bar 判断上涨；返回参与率、趋势收益和是否降速。"""
    if policy.up_threshold is None or policy.up_participation_multiplier >= 1.0:
        return base_participation, 0.0, False
    available = sorted(hhmm for hhmm in bar_map if hhmm <= signal_hhmm)
    if not available or signal_hhmm not in bar_map:
        return base_participation, 0.0, False
    index = available.index(signal_hhmm)
    reference_hhmm = available[max(0, index - lookback_bars)]
    reference = float(bar_map[reference_hhmm].close)
    current = float(bar_map[signal_hhmm].close)
    trend_return = current / reference - 1.0 if reference > 0 else 0.0
    reduced = bool(trend_return > float(policy.up_threshold))
    participation = (
        base_participation * policy.up_participation_multiplier if reduced else base_participation
    )
    return participation, trend_return, reduced


def _sell_from_bar(
    state: dict[str, Any],
    *,
    signal_bar: Any | None,
    fill_bar: Any,
    participation: float | None,
    haircut: float,
    leg_slippage: float,
    stage: str,
    fill_hhmm: str,
    auction: bool = False,
) -> None:
    remain = int(state["remain_qty"])
    if remain <= 0:
        return
    neutral_raw = float(fill_bar.close) if auction else _bar_vwap(fill_bar)
    stress_raw = float(fill_bar.close) if auction else float(fill_bar.low)
    if participation is None:
        order_qty = remain
    else:
        if signal_bar is None:
            return
        budget = max(float(signal_bar.amount), 0.0) * participation
        signal_price = max(float(signal_bar.close), 0.01)
        order_qty = min(remain, _floor_lot(budget / signal_price))
    capacity_price = max(neutral_raw, 0.01)
    capacity_qty = _floor_lot(max(float(fill_bar.amount), 0.0) * haircut / capacity_price)
    quantity = min(remain, order_qty, capacity_qty)
    if quantity <= 0:
        return

    neutral_price = neutral_raw if auction else neutral_raw * (1.0 - leg_slippage)
    stress_price = stress_raw if auction else stress_raw * (1.0 - leg_slippage)
    state["remain_qty"] = remain - quantity
    state["neutral_proceeds"] += quantity * neutral_price
    state["stress_proceeds"] += quantity * stress_price
    state["stage_qty"][stage] += quantity
    state["slice_qty"].append(quantity)
    state["fill_events"] += 1
    if not state["completed_hhmm"] and state["remain_qty"] <= 0:
        state["completed_hhmm"] = fill_hhmm


def _replay_one(
    five: pd.DataFrame,
    one: pd.DataFrame,
    *,
    target_amount: float,
    scenario: Scenario,
    trigger_pct: float,
    pm_extrapolate: float,
    backtest_slippage: dict[str, float],
    trend_policy: TrendPolicy | None = None,
    start_floor_hhmm: str = "",
    force_start_hhmm: str = "",
    start_ceiling_hhmm: str = "",
    disable_pov: bool = False,
) -> dict[str, Any]:
    five_map = _as_bar_map(five)
    one_map = _as_bar_map(one)
    leg = str(five.iloc[0]["leg"])
    required_5m = {"1130", "1430", "1435", "1440", "1445", "1500"}
    required_1m = {*LATE_SIGNAL_TIMES, "1453", *MAIN_FILL_TIMES, "1500"}
    if not required_5m.issubset(five_map) or not required_1m.issubset(one_map):
        missing = sorted((required_5m - set(five_map)) | (required_1m - set(one_map)))
        raise ValueError(f"{five.iloc[0]['key']} 缺少回放必需 bar：{missing}")

    reference_price = float(five_map["1130"].close)
    target_qty = _floor_lot(target_amount / reference_price)
    if target_qty <= 0:
        raise ValueError(f"target_amount={target_amount} 无法形成整手仓位")
    initial_mark = target_qty * reference_price
    leg_slippage = float(backtest_slippage[leg])
    trend_policy = trend_policy or TrendPolicy("none", None, 1.0)
    state: dict[str, Any] = {
        "remain_qty": target_qty,
        "neutral_proceeds": 0.0,
        "stress_proceeds": 0.0,
        "stage_qty": {"base_5m": 0, "late_1m": 0, "main_continuous": 0, "close_auction": 0},
        "slice_qty": [],
        "fill_events": 0,
        "completed_hhmm": "",
        "trend_signal_count": 0,
        "trend_reduced_count": 0,
        "trend_return_sum": 0.0,
    }

    # 13:00 时只能看见上午 09:30~11:30 的累计成交额。
    morning_amount = float(five[five["hhmm"] <= "1130"]["amount"].sum())
    trigger_1300 = bool(initial_mark > morning_amount * trigger_pct)
    base_activated = bool(
        (trigger_1300 or force_start_hhmm or start_ceiling_hhmm) and not disable_pov
    )
    trigger_1430 = False
    projected_order_capacity_1430 = 0.0
    if not base_activated and not disable_pov:
        observed_pm_1430 = float(
            five[(five["hhmm"] >= "1305") & (five["hhmm"] <= "1430")]["amount"].sum()
        )
        projected_order_capacity_1430 = observed_pm_1430 / 90.0 * (
            15.0 * scenario.base_participation
            + len(LATE_SIGNAL_TIMES) * scenario.late_participation
        )
        mark_1430 = target_qty * float(five_map["1430"].close)
        trigger_1430 = bool(
            projected_order_capacity_1430 <= 0
            or mark_1430 * scenario.runway_buffer > projected_order_capacity_1430
        )
        base_activated = trigger_1430
    dynamic_start = ""
    planned_slots = 0
    if base_activated:
        estimated_slice_amount = morning_amount * pm_extrapolate / len(BASE_SIGNAL_TIMES)
        estimated_order_per_slice = estimated_slice_amount * scenario.base_participation
        if estimated_order_per_slice > 0:
            planned_slots = int(math.ceil(initial_mark * scenario.runway_buffer / estimated_order_per_slice))
        else:
            planned_slots = len(BASE_SIGNAL_TIMES)
        planned_slots = max(1, min(planned_slots, len(BASE_SIGNAL_TIMES)))
        dynamic_start = BASE_SIGNAL_TIMES[-planned_slots]
        if force_start_hhmm:
            dynamic_start = force_start_hhmm
            planned_slots = sum(hhmm >= dynamic_start for hhmm in BASE_SIGNAL_TIMES)
        else:
            if start_ceiling_hhmm:
                # “最晚于”门禁：容量需要更早时保留动态早启动，否则到上限时点
                # 强制开始；区别于force_start的所有样本固定同一时点。
                dynamic_start = min(dynamic_start, start_ceiling_hhmm)
            if trigger_1430:
                # 实盘14:30创建计划时先刷新流量基线，最早用14:30已完成bar发出
                # 下一片委托并在14:35 bar成交，因此不能回填更早的历史片。
                dynamic_start = max(dynamic_start, "1430")
            if start_floor_hhmm:
                dynamic_start = max(dynamic_start, start_floor_hhmm)
        # 正确时序：signal_bar 在 hh:mm 刚结束后才可见，委托成交使用下一根 5m bar。
        for signal_hhmm in BASE_SIGNAL_TIMES:
            if signal_hhmm < dynamic_start or state["remain_qty"] <= 0:
                continue
            fill_hhmm = _add_minutes(signal_hhmm, 5)
            participation, trend_return, reduced = _trend_adjusted_participation(
                five_map, signal_hhmm, scenario.base_participation, trend_policy
            )
            state["trend_signal_count"] += 1
            state["trend_return_sum"] += trend_return
            state["trend_reduced_count"] += int(reduced)
            _sell_from_bar(
                state,
                signal_bar=five_map.get(signal_hhmm),
                fill_bar=five_map[fill_hhmm],
                participation=participation,
                haircut=scenario.capacity_haircut,
                leg_slippage=leg_slippage,
                stage="base_5m",
                fill_hhmm=fill_hhmm,
            )

    qty_after_base = int(state["remain_qty"])
    # 14:45 复查：最近 15 分钟真实流量只用于决定是否开启尾段，不能用未来流量。
    recent_15m_amount = sum(float(five_map[hhmm].amount) for hhmm in ("1435", "1440", "1445"))
    projected_late_order = recent_15m_amount / 15.0 * len(LATE_SIGNAL_TIMES) * scenario.late_participation
    remain_mark_1445 = qty_after_base * float(five_map["1445"].close)
    trigger_1445 = bool(
        not disable_pov
        and qty_after_base > 0
        and (base_activated or remain_mark_1445 * scenario.runway_buffer > projected_late_order)
    )
    if trigger_1445:
        # 14:46 的 bar 在 14:46 后才可知，因此最早成交代理是 14:47；14:52
        # 最后一片落到 14:53，随后停止新单并交接。
        for signal_hhmm in LATE_SIGNAL_TIMES:
            if state["remain_qty"] <= 0:
                break
            fill_hhmm = _add_minutes(signal_hhmm, 1)
            participation, trend_return, reduced = _trend_adjusted_participation(
                one_map, signal_hhmm, scenario.late_participation, trend_policy
            )
            state["trend_signal_count"] += 1
            state["trend_return_sum"] += trend_return
            state["trend_reduced_count"] += int(reduced)
            _sell_from_bar(
                state,
                signal_bar=one_map[signal_hhmm],
                fill_bar=one_map[fill_hhmm],
                participation=participation,
                haircut=scenario.capacity_haircut,
                leg_slippage=leg_slippage,
                stage="late_1m",
                fill_hhmm=fill_hhmm,
            )

    qty_at_handoff_1453 = int(state["remain_qty"])
    # 14:53~14:55 为撤单/核仓交接窗，不虚构成交。实盘14:56:20起撤单，
    # 14:56~14:57这根bar只能用到一小段且没有tick可精确切分；为避免高估，
    # 主单只使用完整可见的14:55~14:56一根连续竞价bar容量。
    for fill_hhmm in MAIN_FILL_TIMES:
        if state["remain_qty"] <= 0:
            break
        _sell_from_bar(
            state,
            signal_bar=None,
            fill_bar=one_map[fill_hhmm],
            participation=None,
            haircut=scenario.capacity_haircut,
            leg_slippage=leg_slippage,
            stage="main_continuous",
            fill_hhmm=fill_hhmm,
        )
    qty_before_auction = int(state["remain_qty"])

    # 15:00 bar 是 14:57~15:00 收盘集合竞价；匹配成功部分按收盘价成交。
    if state["remain_qty"] > 0:
        _sell_from_bar(
            state,
            signal_bar=None,
            fill_bar=one_map["1500"],
            participation=None,
            haircut=scenario.capacity_haircut,
            leg_slippage=leg_slippage,
            stage="close_auction",
            fill_hhmm="1500",
            auction=True,
        )
    final_residual_qty = int(state["remain_qty"])
    close_1500 = float(one_map["1500"].close)
    backtest_exit_price = close_1500 * (1.0 - leg_slippage)
    # 未成交余量不是已实现收益。close_mark 仅用于隔离“提前执行的价格损益”，
    # 完成率和实际现金覆盖率另外输出，绝不把余量伪装成已成交。
    neutral_close_mark = (state["neutral_proceeds"] + final_residual_qty * close_1500) / target_qty
    stress_close_mark = (state["stress_proceeds"] + final_residual_qty * close_1500) / target_qty
    sold_qty = target_qty - final_residual_qty
    max_slice_qty = max(state["slice_qty"], default=0)
    result: dict[str, Any] = {
        "key": str(five.iloc[0]["key"]),
        "ts_code": str(five.iloc[0]["ts_code"]),
        "exit_date": str(five.iloc[0]["exit_date"]),
        "leg": leg,
        "target_amount_requested": target_amount,
        "target_qty": target_qty,
        "initial_mark_amount": initial_mark,
        "reference_price_1300": reference_price,
        "morning_amount_1300": morning_amount,
        "trigger_1300": trigger_1300,
        "trigger_1430": trigger_1430,
        "projected_order_capacity_1430": projected_order_capacity_1430,
        "base_activated": base_activated,
        "force_start_hhmm": force_start_hhmm,
        "start_ceiling_hhmm": start_ceiling_hhmm,
        "disable_pov": disable_pov,
        "dynamic_start_hhmm": dynamic_start,
        "planned_base_slots": planned_slots,
        "start_floor_hhmm": start_floor_hhmm,
        "recent_15m_amount_1445": recent_15m_amount,
        "projected_late_order_1445": projected_late_order,
        "trigger_1445": trigger_1445,
        "completed_hhmm": state["completed_hhmm"],
        "fill_events": state["fill_events"],
        "trend_policy": trend_policy.name,
        "trend_up_threshold": trend_policy.up_threshold,
        "trend_up_participation_multiplier": trend_policy.up_participation_multiplier,
        "trend_signal_count": state["trend_signal_count"],
        "trend_reduced_count": state["trend_reduced_count"],
        "trend_reduced_rate": (
            state["trend_reduced_count"] / state["trend_signal_count"]
            if state["trend_signal_count"] > 0 else 0.0
        ),
        "base_5m_qty": state["stage_qty"]["base_5m"],
        "late_1m_qty": state["stage_qty"]["late_1m"],
        "main_continuous_qty": state["stage_qty"]["main_continuous"],
        "close_auction_qty": state["stage_qty"]["close_auction"],
        "qty_at_handoff_1453": qty_at_handoff_1453,
        "qty_before_auction_1457": qty_before_auction,
        "final_residual_qty": final_residual_qty,
        "final_sold_qty": sold_qty,
        "neutral_realized_proceeds": state["neutral_proceeds"],
        "stress_realized_proceeds": state["stress_proceeds"],
        "neutral_realized_avg_price": state["neutral_proceeds"] / sold_qty if sold_qty > 0 else np.nan,
        "stress_realized_avg_price": state["stress_proceeds"] / sold_qty if sold_qty > 0 else np.nan,
        "handoff_residual_amount_close_mark": qty_at_handoff_1453 * close_1500,
        "preauction_residual_amount_close_mark": qty_before_auction * close_1500,
        "final_residual_amount_close_mark": final_residual_qty * close_1500,
        "fill_ratio_handoff_1453": 1.0 - qty_at_handoff_1453 / target_qty,
        "fill_ratio_preauction_1457": 1.0 - qty_before_auction / target_qty,
        "final_fill_ratio": sold_qty / target_qty,
        "complete_by_handoff_1453": qty_at_handoff_1453 == 0,
        "complete_before_auction_1457": qty_before_auction == 0,
        "complete_final_1500": final_residual_qty == 0,
        "uses_close_auction": state["stage_qty"]["close_auction"] > 0,
        "max_slice_qty_pct": max_slice_qty / target_qty,
        "main_continuous_qty_pct": state["stage_qty"]["main_continuous"] / target_qty,
        "close_auction_qty_pct": state["stage_qty"]["close_auction"] / target_qty,
        "backtest_sell_slippage": leg_slippage,
        "close_1500": close_1500,
        "backtest_exit_price": backtest_exit_price,
        "neutral_close_mark_price": neutral_close_mark,
        "stress_close_mark_price": stress_close_mark,
        "neutral_close_mark_vs_close_pct": (neutral_close_mark / close_1500 - 1.0) * 100.0,
        "stress_close_mark_vs_close_pct": (stress_close_mark / close_1500 - 1.0) * 100.0,
        "neutral_close_mark_vs_backtest_pct": (neutral_close_mark / backtest_exit_price - 1.0) * 100.0,
        "stress_close_mark_vs_backtest_pct": (stress_close_mark / backtest_exit_price - 1.0) * 100.0,
        "neutral_not_below_backtest": neutral_close_mark >= backtest_exit_price,
        "stress_not_below_backtest": stress_close_mark >= backtest_exit_price,
        "neutral_cash_coverage_vs_backtest": state["neutral_proceeds"] / (target_qty * backtest_exit_price),
        "stress_cash_coverage_vs_backtest": state["stress_proceeds"] / (target_qty * backtest_exit_price),
    }
    return result


def _q(series: pd.Series, value: float) -> float:
    return float(series.quantile(value)) if not series.empty else np.nan


def _summarize(group: pd.DataFrame, scenario: Scenario, scope: str) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "scope": scope,
        "base_participation": scenario.base_participation,
        "late_participation": scenario.late_participation,
        "runway_buffer": scenario.runway_buffer,
        "capacity_haircut": scenario.capacity_haircut,
        "samples": len(group),
        "backtest_sell_slippage_min": float(group["backtest_sell_slippage"].min()),
        "backtest_sell_slippage_max": float(group["backtest_sell_slippage"].max()),
        "trigger_1300_rate": float(group["trigger_1300"].mean()),
        "trigger_1445_rate": float(group["trigger_1445"].mean()),
        "complete_by_handoff_1453_rate": float(group["complete_by_handoff_1453"].mean()),
        "complete_before_auction_1457_rate": float(group["complete_before_auction_1457"].mean()),
        "complete_final_1500_rate": float(group["complete_final_1500"].mean()),
        "close_auction_usage_rate": float(group["uses_close_auction"].mean()),
        "mean_fill_ratio_handoff_1453": float(group["fill_ratio_handoff_1453"].mean()),
        "mean_fill_ratio_preauction_1457": float(group["fill_ratio_preauction_1457"].mean()),
        "mean_final_fill_ratio": float(group["final_fill_ratio"].mean()),
        "median_handoff_residual_amount": float(group["handoff_residual_amount_close_mark"].median()),
        "p90_handoff_residual_amount": _q(group["handoff_residual_amount_close_mark"], 0.90),
        "median_preauction_residual_amount": float(group["preauction_residual_amount_close_mark"].median()),
        "p90_preauction_residual_amount": _q(group["preauction_residual_amount_close_mark"], 0.90),
        "median_final_residual_amount": float(group["final_residual_amount_close_mark"].median()),
        "p90_final_residual_amount": _q(group["final_residual_amount_close_mark"], 0.90),
        "max_final_residual_amount": float(group["final_residual_amount_close_mark"].max()),
        "mean_max_slice_qty_pct": float(group["max_slice_qty_pct"].mean()),
        "p90_max_slice_qty_pct": _q(group["max_slice_qty_pct"], 0.90),
        "mean_main_continuous_qty_pct": float(group["main_continuous_qty_pct"].mean()),
        "mean_close_auction_qty_pct": float(group["close_auction_qty_pct"].mean()),
        "mean_neutral_vs_close_pct": float(group["neutral_close_mark_vs_close_pct"].mean()),
        "median_neutral_vs_close_pct": float(group["neutral_close_mark_vs_close_pct"].median()),
        "mean_stress_vs_close_pct": float(group["stress_close_mark_vs_close_pct"].mean()),
        "worst_stress_vs_close_pct": float(group["stress_close_mark_vs_close_pct"].min()),
        "mean_neutral_vs_backtest_pct": float(group["neutral_close_mark_vs_backtest_pct"].mean()),
        "median_neutral_vs_backtest_pct": float(group["neutral_close_mark_vs_backtest_pct"].median()),
        "neutral_not_below_backtest_rate": float(group["neutral_not_below_backtest"].mean()),
        "mean_stress_vs_backtest_pct": float(group["stress_close_mark_vs_backtest_pct"].mean()),
        "stress_not_below_backtest_rate": float(group["stress_not_below_backtest"].mean()),
        "mean_neutral_cash_coverage_vs_backtest": float(group["neutral_cash_coverage_vs_backtest"].mean()),
        "mean_stress_cash_coverage_vs_backtest": float(group["stress_cash_coverage_vs_backtest"].mean()),
    }


def _scenario_grid(
    base_parts: Iterable[float],
    late_parts: Iterable[float],
    runway_buffers: Iterable[float],
    capacity_haircut: float,
) -> Iterable[Scenario]:
    for base, late, buffer_value in itertools.product(base_parts, late_parts, runway_buffers):
        yield Scenario(base, late, buffer_value, capacity_haircut)


def _position_target(
    policy: PositionPolicy,
    *,
    max_target: float,
    signal_day_amount: float,
    tail_cap_pct: float,
) -> tuple[bool, float, str, float]:
    caps = [max_target]
    if policy.signal_amount_cap_pct is not None:
        caps.append(signal_day_amount * policy.signal_amount_cap_pct)
    if policy.use_tail_q10_cap:
        caps.append(signal_day_amount * tail_cap_pct)
    liquidity_cap = float(min(caps))
    if policy.mode == "fixed":
        return True, max_target, "fixed_target", liquidity_cap
    if policy.mode == "scale":
        return True, max(0.0, liquidity_cap), "scaled_to_known_signal_liquidity", liquidity_cap
    if policy.mode == "admit_full":
        admitted = liquidity_cap >= max_target
        return admitted, max_target if admitted else 0.0, (
            "full_target_admitted" if admitted else "rejected_by_signal_liquidity"
        ), liquidity_cap
    raise ValueError(f"未知仓位策略：{policy.mode}")


def _summarize_pareto(
    frame: pd.DataFrame,
    *,
    strategy_id: str,
    split: str,
    scope: str,
    max_target: float,
) -> dict[str, Any]:
    candidates = frame if scope == "ALL" else frame[frame["leg"] == scope]
    executed = candidates[candidates["admitted"]].copy()
    base = {
        "strategy_id": strategy_id,
        "sample_split": split,
        "scope": scope,
        "position_policy": str(candidates["position_policy"].iloc[0]) if not candidates.empty else "",
        "trend_policy": str(candidates["trend_policy"].dropna().iloc[0]) if candidates["trend_policy"].notna().any() else "",
        "signal_amount_cap_pct": candidates["signal_amount_cap_pct"].iloc[0] if not candidates.empty else np.nan,
        "tail_q10_cap_pct": candidates["tail_q10_cap_pct"].iloc[0] if not candidates.empty else np.nan,
        "trend_up_threshold": candidates["trend_up_threshold"].dropna().iloc[0] if candidates["trend_up_threshold"].notna().any() else np.nan,
        "trend_up_participation_multiplier": candidates["trend_up_participation_multiplier"].dropna().iloc[0] if candidates["trend_up_participation_multiplier"].notna().any() else np.nan,
        "start_rule": str(candidates["start_rule"].iloc[0]) if not candidates.empty else "",
        "candidate_samples": len(candidates),
        "admitted_samples": len(executed),
        "admission_rate": float(candidates["admitted"].mean()) if not candidates.empty else np.nan,
        "capital_deployment_ratio": float(candidates["target_amount_effective"].sum() / (len(candidates) * max_target)) if len(candidates) else np.nan,
        "mean_target_amount_all_candidates": float(candidates["target_amount_effective"].mean()) if len(candidates) else np.nan,
        "mean_target_amount_admitted": float(executed["target_amount_effective"].mean()) if len(executed) else np.nan,
    }
    if executed.empty:
        base.update(
            {
                "complete_by_handoff_1453_rate": np.nan,
                "complete_before_auction_1457_rate": np.nan,
                "complete_final_1500_rate": np.nan,
                "mean_fill_ratio_preauction_1457": np.nan,
                "p90_preauction_residual_amount": np.nan,
                "p90_final_residual_amount": np.nan,
                "mean_neutral_vs_backtest_pct": np.nan,
                "median_neutral_vs_backtest_pct": np.nan,
                "neutral_not_below_backtest_rate": np.nan,
                "mean_stress_vs_backtest_pct": np.nan,
                "stress_not_below_backtest_rate": np.nan,
                "mean_trend_reduced_rate": np.nan,
                "mean_close_auction_qty_pct": np.nan,
            }
        )
        return base
    base.update(
        {
            "complete_by_handoff_1453_rate": float(executed["complete_by_handoff_1453"].mean()),
            "complete_before_auction_1457_rate": float(executed["complete_before_auction_1457"].mean()),
            "complete_final_1500_rate": float(executed["complete_final_1500"].mean()),
            "mean_fill_ratio_preauction_1457": float(executed["fill_ratio_preauction_1457"].mean()),
            "p90_preauction_residual_amount": _q(executed["preauction_residual_amount_close_mark"], 0.90),
            "p90_final_residual_amount": _q(executed["final_residual_amount_close_mark"], 0.90),
            "mean_neutral_vs_backtest_pct": float(executed["neutral_close_mark_vs_backtest_pct"].mean()),
            "median_neutral_vs_backtest_pct": float(executed["neutral_close_mark_vs_backtest_pct"].median()),
            "neutral_not_below_backtest_rate": float(executed["neutral_not_below_backtest"].mean()),
            "mean_stress_vs_backtest_pct": float(executed["stress_close_mark_vs_backtest_pct"].mean()),
            "stress_not_below_backtest_rate": float(executed["stress_not_below_backtest"].mean()),
            "mean_trend_reduced_rate": float(executed["trend_reduced_rate"].mean()),
            "mean_close_auction_qty_pct": float(executed["close_auction_qty_pct"].mean()),
        }
    )
    return base


def _pareto_mask(frame: pd.DataFrame, objectives: list[str]) -> pd.Series:
    """所有目标均为越大越好；相同点都保留，不偷偷挑一个样本内冠军。"""
    values = frame[objectives].to_numpy(dtype=float)
    keep = np.ones(len(frame), dtype=bool)
    for index, point in enumerate(values):
        if not np.isfinite(point).all():
            keep[index] = False
            continue
        dominates = np.all(values >= point, axis=1) & np.any(values > point, axis=1)
        dominates[index] = False
        if dominates.any():
            keep[index] = False
    return pd.Series(keep, index=frame.index)


def _run_pareto_exploration(
    *,
    five_groups: dict[str, pd.DataFrame],
    one_groups: dict[str, pd.DataFrame],
    metadata: pd.DataFrame,
    max_target: float,
    capacity_haircut: float,
    trigger_pct: float,
    pm_extrapolate: float,
    backtest_slippage: dict[str, float],
    tail_q10: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    # q10 仅由时间靠前的开发集估计；再乘 50% 容量折扣，作为入场时可知的
    # 保守尾段容量代理。绝不使用单笔未来退出日的实际尾段量缩小该笔仓位。
    tail_cap_pct = tail_q10 * capacity_haircut
    position_policies = (
        PositionPolicy("fixed_10m", "fixed", None),
        PositionPolicy("scale_signal_1pct", "scale", 0.010),
        PositionPolicy("scale_signal_0p5pct", "scale", 0.005),
        # 0.25%/0.10% 仅作为“95%完成率需要缩到多小”的容量边界压力，
        # 不参与默认建议；避免看到结果后再连续微调阈值。
        PositionPolicy("scale_signal_0p25pct_boundary", "scale", 0.0025),
        PositionPolicy("scale_signal_0p1pct_boundary", "scale", 0.0010),
        PositionPolicy("scale_tail_q10", "scale", None, True),
        PositionPolicy("admit_signal_1pct_full10m", "admit_full", 0.010),
        PositionPolicy("admit_signal_0p5pct_full10m", "admit_full", 0.005),
        PositionPolicy("admit_tail_q10_full10m", "admit_full", None, True),
    )
    trend_policies = (
        TrendPolicy("none", None, 1.0),
        TrendPolicy("rise_gt_0_hold_half", 0.0, 0.50),
        TrendPolicy("rise_gt_0_hold_quarter", 0.0, 0.25),
        TrendPolicy("rise_gt_0p1_hold_half", 0.001, 0.50),
        TrendPolicy("rise_gt_0p1_hold_quarter", 0.001, 0.25),
        TrendPolicy("rise_gt_0p1_pause", 0.001, 0.0),
    )
    execution_scenario = Scenario(0.25, 0.35, 1.5, capacity_haircut)
    start_rules = (
        # 当前实盘代码的精确口径：按容量倒推启动时间，最早可到13:00。
        ("dynamic_live_no_floor", "", "", ""),
        ("dynamic_not_before_1415", "1415", "", ""),
        ("force_no_later_1415", "", "", "1415"),
        ("force_1415", "", "1415", ""),
    )
    all_details: list[pd.DataFrame] = []
    summary_rows: list[dict[str, Any]] = []
    meta_by_key = metadata.set_index("key")

    for position_policy, trend_policy, start_rule in itertools.product(
        position_policies, trend_policies, start_rules
    ):
        start_rule_name, start_floor_hhmm, force_start_hhmm, start_ceiling_hhmm = start_rule
        strategy_id = f"{position_policy.name}__{trend_policy.name}__{start_rule_name}"
        rows: list[dict[str, Any]] = []
        for key, meta in meta_by_key.iterrows():
            admitted, target, sizing_reason, liquidity_cap = _position_target(
                position_policy,
                max_target=max_target,
                signal_day_amount=float(meta["signal_day_amount"]),
                tail_cap_pct=tail_cap_pct,
            )
            common = {
                "strategy_id": strategy_id,
                "key": key,
                "signal_date": str(meta["signal_date"]),
                "exit_date": str(meta["exit_date_expected"]),
                "leg": str(meta["strategy_leg"]),
                "sample_split": str(meta["sample_split"]),
                "position_policy": position_policy.name,
                "signal_amount_cap_pct": position_policy.signal_amount_cap_pct,
                "uses_tail_q10_cap": position_policy.use_tail_q10_cap,
                "tail_q10_raw_ratio": tail_q10,
                "tail_q10_cap_pct": tail_cap_pct,
                "signal_day_amount": float(meta["signal_day_amount"]),
                "liquidity_cap_amount": liquidity_cap,
                "admitted": admitted,
                "sizing_reason": sizing_reason,
                "target_amount_effective": target,
                "target_to_max_ratio": target / max_target,
                "start_rule": start_rule_name,
                "trend_policy": trend_policy.name,
                "trend_up_threshold": trend_policy.up_threshold,
                "trend_up_participation_multiplier": trend_policy.up_participation_multiplier,
            }
            if not admitted or target <= 0:
                rows.append(common)
                continue
            result = _replay_one(
                five_groups[key], one_groups[key],
                target_amount=target,
                scenario=execution_scenario,
                trigger_pct=trigger_pct,
                pm_extrapolate=pm_extrapolate,
                backtest_slippage=backtest_slippage,
                trend_policy=trend_policy,
                start_floor_hhmm=start_floor_hhmm,
                force_start_hhmm=force_start_hhmm,
                start_ceiling_hhmm=start_ceiling_hhmm,
            )
            common.update(result)
            # result 中的同名字段属于实际回放，应保持策略元数据字段不被覆盖。
            common["signal_date"] = str(meta["signal_date"])
            common["sample_split"] = str(meta["sample_split"])
            common["position_policy"] = position_policy.name
            common["target_amount_effective"] = target
            common["admitted"] = True
            rows.append(common)
        case = pd.DataFrame(rows)
        all_details.append(case)
        for split in ("development", "validation", "ALL"):
            split_case = case if split == "ALL" else case[case["sample_split"] == split]
            for scope in ("ALL", *VALID_LEGS):
                summary_rows.append(
                    _summarize_pareto(
                        split_case,
                        strategy_id=strategy_id,
                        split=split,
                        scope=scope,
                        max_target=max_target,
                    )
                )

    # admission 策略的拒绝行天然包含全空成交列；pandas 对这类 concat 发出的
    # FutureWarning 不影响口径，这里局部屏蔽，避免研究脚本正常运行时制造噪声。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        details = pd.concat(all_details, ignore_index=True, sort=False)
    summary = pd.DataFrame(summary_rows)
    development = summary[(summary["sample_split"] == "development") & (summary["scope"] == "ALL")].copy()
    validation_counts = summary[
        (summary["sample_split"] == "validation") & (summary["scope"] == "ALL")
    ].set_index("strategy_id")["admitted_samples"]
    development["validation_admitted_samples"] = development["strategy_id"].map(validation_counts).fillna(0)
    # 少于开发20笔或验证10笔的策略仍保留在汇总，但不准进入“可比较Pareto”。
    enough = (development["admitted_samples"] >= 20) & (development["validation_admitted_samples"] >= 10)
    objectives = [
        "complete_before_auction_1457_rate",
        "mean_neutral_vs_backtest_pct",
        "capital_deployment_ratio",
    ]
    development["pareto_eligible_sample"] = enough
    development["is_development_pareto"] = False
    if enough.any():
        eligible_dev = development[enough]
        development.loc[eligible_dev.index, "is_development_pareto"] = _pareto_mask(
            eligible_dev, objectives
        )
    front_dev = development[development["is_development_pareto"]].copy()
    validation = summary[
        (summary["sample_split"] == "validation") & (summary["scope"] == "ALL")
    ].copy()
    metric_columns = [
        "admitted_samples", "admission_rate", "capital_deployment_ratio",
        "mean_target_amount_all_candidates", "complete_before_auction_1457_rate",
        "complete_final_1500_rate", "p90_preauction_residual_amount",
        "mean_neutral_vs_backtest_pct", "neutral_not_below_backtest_rate",
        "mean_stress_vs_backtest_pct", "stress_not_below_backtest_rate",
    ]
    front = front_dev[["strategy_id", *metric_columns]].merge(
        validation[["strategy_id", *metric_columns]], on="strategy_id", suffixes=("_development", "_validation")
    )
    if not front.empty:
        front["completion_validation_minus_development"] = (
            front["complete_before_auction_1457_rate_validation"]
            - front["complete_before_auction_1457_rate_development"]
        )
        front["return_validation_minus_development_pct"] = (
            front["mean_neutral_vs_backtest_pct_validation"]
            - front["mean_neutral_vs_backtest_pct_development"]
        )
        front = front.sort_values(
            ["complete_before_auction_1457_rate_validation", "mean_neutral_vs_backtest_pct_validation", "capital_deployment_ratio_validation"],
            ascending=[False, False, False],
        )
    summary = summary.merge(
        development[["strategy_id", "pareto_eligible_sample", "is_development_pareto"]],
        on="strategy_id", how="left",
    )
    return details, summary, front


def _run_leg_rule_check(
    *,
    five_groups: dict[str, pd.DataFrame],
    one_groups: dict[str, pd.DataFrame],
    metadata: pd.DataFrame,
    target: float,
    capacity_haircut: float,
    trigger_pct: float,
    pm_extrapolate: float,
    backtest_slippage: dict[str, float],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """预先定义三个简单 leg 规则；不按结果搜索阈值。"""
    rules = {
        "uniform_current_pov": {"A": "current", "B": "current", "E2": "current"},
        "A_main_1455_only__BE2_current": {"A": "main_only", "B": "current", "E2": "current"},
        "A_not_before_1440__BE2_current": {"A": "late_1440", "B": "current", "E2": "current"},
    }
    scenario = Scenario(0.25, 0.35, 1.5, capacity_haircut)
    meta_by_key = metadata.set_index("key")
    detail_frames: list[pd.DataFrame] = []
    summary_rows: list[dict[str, Any]] = []
    for rule_name, by_leg in rules.items():
        rows = []
        for key, meta in meta_by_key.iterrows():
            leg = str(meta["strategy_leg"])
            action = by_leg[leg]
            result = _replay_one(
                five_groups[key], one_groups[key],
                target_amount=target,
                scenario=scenario,
                trigger_pct=trigger_pct,
                pm_extrapolate=pm_extrapolate,
                backtest_slippage=backtest_slippage,
                start_floor_hhmm="1440" if action == "late_1440" else "",
                disable_pov=action == "main_only",
            )
            result.update(
                {
                    "leg_rule": rule_name,
                    "leg_action": action,
                    "signal_date": str(meta["signal_date"]),
                    "sample_split": str(meta["sample_split"]),
                }
            )
            rows.append(result)
        case = pd.DataFrame(rows)
        detail_frames.append(case)
        for split in ("development", "validation", "ALL"):
            split_case = case if split == "ALL" else case[case["sample_split"] == split]
            for scope in ("ALL", *VALID_LEGS):
                group = split_case if scope == "ALL" else split_case[split_case["leg"] == scope]
                summary_rows.append(
                    {
                        "leg_rule": rule_name,
                        "sample_split": split,
                        "scope": scope,
                        "samples": len(group),
                        "complete_before_auction_1457_rate": float(group["complete_before_auction_1457"].mean()),
                        "complete_final_1500_rate": float(group["complete_final_1500"].mean()),
                        "p90_preauction_residual_amount": _q(group["preauction_residual_amount_close_mark"], 0.90),
                        "mean_neutral_vs_backtest_pct": float(group["neutral_close_mark_vs_backtest_pct"].mean()),
                        "neutral_not_below_backtest_rate": float(group["neutral_not_below_backtest"].mean()),
                        "mean_stress_vs_backtest_pct": float(group["stress_close_mark_vs_backtest_pct"].mean()),
                    }
                )
    details = pd.concat(detail_frames, ignore_index=True)
    summary = pd.DataFrame(summary_rows)
    baseline = summary[summary["leg_rule"] == "uniform_current_pov"].set_index(
        ["sample_split", "scope"]
    )
    for metric in ("complete_before_auction_1457_rate", "mean_neutral_vs_backtest_pct"):
        lookup = baseline[metric]
        summary[f"{metric}_vs_uniform"] = [
            float(row[metric] - lookup.get((row["sample_split"], row["scope"]), np.nan))
            for _, row in summary.iterrows()
        ]
    return details, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="同构扫描 1000 万级尾盘 POV 平仓参数")
    parser.add_argument("--five-minute", type=Path, default=DEFAULT_5M)
    parser.add_argument("--one-minute", type=Path, default=DEFAULT_1M)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--trade-calendar", type=Path, default=DEFAULT_CALENDAR)
    parser.add_argument("--daily-amount", type=Path, default=DEFAULT_DAILY_AMOUNT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--target", type=float, default=10_000_000.0)
    parser.add_argument("--base-parts", default="0.15,0.20,0.25")
    parser.add_argument("--late-parts", default="0.25,0.30,0.35")
    parser.add_argument("--runway-buffers", default="1.0,1.25,1.5")
    parser.add_argument("--capacity-haircut", type=float, default=0.50)
    parser.add_argument("--trigger-pct", type=float, default=0.01)
    parser.add_argument("--pm-extrapolate", type=float, default=0.44)
    parser.add_argument("--backtest-slippage-a", type=float, default=0.001)
    parser.add_argument("--backtest-slippage-b", type=float, default=0.001)
    parser.add_argument("--backtest-slippage-e2", type=float, default=0.001)
    args = parser.parse_args()

    if args.target <= 0:
        raise ValueError("--target 必须大于 0")
    if not 0 < args.capacity_haircut <= 1:
        raise ValueError("--capacity-haircut 必须在 (0, 1] 内")
    if args.trigger_pct <= 0 or args.pm_extrapolate <= 0:
        raise ValueError("--trigger-pct 与 --pm-extrapolate 必须大于 0")

    five, five_stats = _load_bars(args.five_minute, require_leg=True)
    one, one_stats = _load_bars(args.one_minute, require_leg=False)
    quality, eligible = _build_quality(five, one, five_stats, one_stats)
    if not eligible:
        raise RuntimeError("没有退出日口径正确、且同时具备完整 5m/1m 的 A/B/E2 样本")

    five_groups = {key: group for key, group in five[five["key"].isin(eligible)].groupby("key")}
    one_groups = {key: group for key, group in one[one["key"].isin(eligible)].groupby("key")}
    metadata = _load_signal_metadata(
        audit_path=args.audit,
        calendar_path=args.trade_calendar,
        daily_amount_path=args.daily_amount,
        eligible=eligible,
    )
    metadata, tail_q10 = _attach_tail_calibration(metadata, five, one)
    quality_extra = pd.DataFrame(
        [
            {
                "check": "signal_metadata.mapped_samples",
                "status": "PASS",
                "value": len(metadata),
                "note": "81笔有效退出样本均映射到信号日；A/B/E2 使用信号后第2个交易日退出",
            },
            {
                "check": "signal_metadata.amount_missing",
                "status": "PASS",
                "value": int(metadata["signal_day_amount"].isna().sum()),
                "note": "信号日成交额来自 daily_amount_lookup.amount_yuan",
            },
            {
                "check": "validation.chronological_split",
                "status": "INFO",
                "value": f"development={int((metadata['sample_split'] == 'development').sum())};validation={int((metadata['sample_split'] == 'validation').sum())}",
                "note": "按退出日期排序，前2/3开发、后1/3验证；Pareto只由开发集确定",
            },
            {
                "check": "tail_capacity.development_q10_raw_ratio",
                "status": "INFO",
                "value": tail_q10,
                "note": "14:15~15:00原始成交额/信号日成交额的开发集10分位；入场容量代理还会乘50%压力折扣",
            },
        ]
    )
    quality = pd.concat([quality, quality_extra], ignore_index=True)
    backtest_slippage = {
        "A": float(args.backtest_slippage_a),
        "B": float(args.backtest_slippage_b),
        "E2": float(args.backtest_slippage_e2),
    }
    base_parts = _parse_grid(args.base_parts)
    late_parts = _parse_grid(args.late_parts)
    buffers = _parse_grid(args.runway_buffers)

    detail_frames: list[pd.DataFrame] = []
    summary_rows: list[dict[str, Any]] = []
    scenarios = list(_scenario_grid(base_parts, late_parts, buffers, args.capacity_haircut))
    for scenario in scenarios:
        rows = []
        for key in eligible:
            row = _replay_one(
                five_groups[key], one_groups[key],
                target_amount=args.target,
                scenario=scenario,
                trigger_pct=args.trigger_pct,
                pm_extrapolate=args.pm_extrapolate,
                backtest_slippage=backtest_slippage,
            )
            row.update(
                {
                    "scenario_id": scenario.scenario_id,
                    "base_participation": scenario.base_participation,
                    "late_participation": scenario.late_participation,
                    "runway_buffer": scenario.runway_buffer,
                    "capacity_haircut": scenario.capacity_haircut,
                }
            )
            rows.append(row)
        case = pd.DataFrame(rows)
        detail_frames.append(case)
        summary_rows.append(_summarize(case, scenario, "ALL"))
        for leg, group in case.groupby("leg", sort=True):
            summary_rows.append(_summarize(group, scenario, str(leg)))

    detail = pd.concat(detail_frames, ignore_index=True)
    summary = pd.DataFrame(summary_rows)
    summary = summary.sort_values(
        ["scope", "complete_before_auction_1457_rate", "mean_neutral_vs_backtest_pct", "p90_preauction_residual_amount"],
        ascending=[True, False, False, True],
    ).reset_index(drop=True)

    pareto_detail, pareto_summary, pareto_front = _run_pareto_exploration(
        five_groups=five_groups,
        one_groups=one_groups,
        metadata=metadata,
        max_target=args.target,
        capacity_haircut=args.capacity_haircut,
        trigger_pct=args.trigger_pct,
        pm_extrapolate=args.pm_extrapolate,
        backtest_slippage=backtest_slippage,
        tail_q10=tail_q10,
    )
    leg_detail, leg_summary = _run_leg_rule_check(
        five_groups=five_groups,
        one_groups=one_groups,
        metadata=metadata,
        target=args.target,
        capacity_haircut=args.capacity_haircut,
        trigger_pct=args.trigger_pct,
        pm_extrapolate=args.pm_extrapolate,
        backtest_slippage=backtest_slippage,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    quality_path = args.output_dir / "data_quality.csv"
    summary_path = args.output_dir / "parameter_summary.csv"
    detail_path = args.output_dir / "parameter_detail.csv"
    pareto_summary_path = args.output_dir / "pareto_summary.csv"
    pareto_detail_path = args.output_dir / "pareto_detail.csv"
    pareto_front_path = args.output_dir / "pareto_front.csv"
    leg_summary_path = args.output_dir / "leg_rule_summary.csv"
    leg_detail_path = args.output_dir / "leg_rule_detail.csv"
    quality.to_csv(quality_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    pareto_summary.to_csv(pareto_summary_path, index=False, encoding="utf-8-sig")
    pareto_detail.to_csv(pareto_detail_path, index=False, encoding="utf-8-sig")
    pareto_front.to_csv(pareto_front_path, index=False, encoding="utf-8-sig")
    leg_summary.to_csv(leg_summary_path, index=False, encoding="utf-8-sig")
    leg_detail.to_csv(leg_detail_path, index=False, encoding="utf-8-sig")

    current = summary[
        (summary["scope"] == "ALL")
        & np.isclose(summary["base_participation"], 0.25)
        & np.isclose(summary["late_participation"], 0.35)
        & np.isclose(summary["runway_buffer"], 1.5)
    ]
    print(quality.to_string(index=False))
    print(
        f"\n有效样本={len(eligible)}（仅 A/B/E2）；C 因退出日抓错被排除；L 无样本。"
        f"容量压力折扣={args.capacity_haircut:.0%}，场景数={len(scenarios)}。"
    )
    if not current.empty:
        fields = [
            "samples", "complete_by_handoff_1453_rate", "complete_before_auction_1457_rate",
            "complete_final_1500_rate", "mean_fill_ratio_preauction_1457",
            "p90_preauction_residual_amount", "p90_final_residual_amount",
            "mean_neutral_vs_backtest_pct", "neutral_not_below_backtest_rate",
            "mean_stress_vs_backtest_pct", "stress_not_below_backtest_rate",
        ]
        print("\n当前候选参数（25% / 35% / buffer=1.5）的 50% 容量压力结果：")
        print(current.iloc[0][fields].to_string())
    print(f"\n数据质量：{quality_path}")
    print(f"参数汇总：{summary_path}")
    print(f"逐笔明细：{detail_path}")
    print(f"\n趋势/流动性探索汇总：{pareto_summary_path}")
    print(f"趋势/流动性探索逐笔：{pareto_detail_path}")
    print(f"开发集Pareto及独立验证：{pareto_front_path}")
    print(f"分腿规则汇总：{leg_summary_path}")
    print(f"分腿规则逐笔：{leg_detail_path}")
    if not pareto_front.empty:
        show = [
            "strategy_id", "admitted_samples_validation", "capital_deployment_ratio_validation",
            "complete_before_auction_1457_rate_validation",
            "mean_neutral_vs_backtest_pct_validation",
            "neutral_not_below_backtest_rate_validation",
        ]
        print("\n开发集Pareto方案在后1/3验证集的表现（不据此反向调参）：")
        print(pareto_front[show].to_string(index=False))
    leg_validation = leg_summary[
        (leg_summary["sample_split"] == "validation")
        & leg_summary["scope"].isin(["ALL", "A"])
    ]
    print("\nA延后与统一POV的验证集对照（A验证仅4笔，不能据此上线）：")
    print(leg_validation.to_string(index=False))
    print("注意：close_mark 只用于比较提前卖出与回测价格，不代表未成交余量已卖出。")


if __name__ == "__main__":
    main()
