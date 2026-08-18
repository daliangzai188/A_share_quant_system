"""按当前实盘代码口径审计策略 L 的完整历史候选池。

本脚本只做离线审计，不连接券商、不生成实盘计划、不修改策略配置。

审计目标：
1. 从完整 ``limit_up_fill_scored`` 候选池逐日运行当前 L 选股逻辑；
2. 使用完整题材热度与市场情绪特征，不读取旧 ``scenario_executed`` 子集；
3. 按 T+1 开盘买入、T+2 收盘退出、涨跌停约束、费用和滑点回放；
4. 检查 L2 三个过滤条件、参数邻域、训练/测试和分年稳定性；
5. 把纠正后的 L 候选放回当前 D>L>A>M>E>C 单账户组合复算。

输出目录：``reports/strategy_l/full_universe_audit``。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import sys
from typing import Any, Iterable

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCORED_PATH = PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored.csv"
THEME_PATH = PROJECT_ROOT / "data" / "processed" / "theme_heat_features.csv"
EMOTION_PATH = PROJECT_ROOT / "data" / "processed" / "market_emotion_features.csv"
DAILY_PATH = PROJECT_ROOT / "data" / "processed" / "daily_merged.csv"
CALENDAR_PATH = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_l" / "full_universe_audit"

START_DATE = "20240520"
# 预留后续退出行情；当前完整日线止于 20260626。
END_DATE = "20260618"
SPLIT_DATE = "20250601"
INITIAL_EQUITY = 500_000.0
BUY_SLIPPAGE = 0.001
SELL_SLIPPAGE = 0.001
LIMIT_TOLERANCE = 0.002


@dataclass(frozen=True)
class Variant:
    name: str
    heat_rank_max: int = 3
    leader_rank_max: int = 1
    exclude_retreat_2day: bool = False
    exclude_down_3_8: bool = False
    excluded_theme_counts: tuple[int, ...] = ()


VARIANTS = (
    Variant("CORE_TOP3_LEADER1"),
    Variant("L2_CURRENT", exclude_retreat_2day=True, exclude_down_3_8=True, excluded_theme_counts=(30,)),
    Variant("L2_NO_RETREAT_FILTER", exclude_down_3_8=True, excluded_theme_counts=(30,)),
    Variant("L2_NO_DOWN_FILTER", exclude_retreat_2day=True, excluded_theme_counts=(30,)),
    Variant("L2_NO_COUNT30_FILTER", exclude_retreat_2day=True, exclude_down_3_8=True),
    Variant("NEIGHBOR_TOP1", heat_rank_max=1, exclude_retreat_2day=True, exclude_down_3_8=True, excluded_theme_counts=(30,)),
    Variant("NEIGHBOR_TOP2", heat_rank_max=2, exclude_retreat_2day=True, exclude_down_3_8=True, excluded_theme_counts=(30,)),
    Variant("NEIGHBOR_TOP5", heat_rank_max=5, exclude_retreat_2day=True, exclude_down_3_8=True, excluded_theme_counts=(30,)),
    Variant("NEIGHBOR_LEADER_TOP2", leader_rank_max=2, exclude_retreat_2day=True, exclude_down_3_8=True, excluded_theme_counts=(30,)),
    Variant("NEIGHBOR_EXCLUDE_COUNT29", exclude_retreat_2day=True, exclude_down_3_8=True, excluded_theme_counts=(29,)),
    Variant("NEIGHBOR_EXCLUDE_COUNT31", exclude_retreat_2day=True, exclude_down_3_8=True, excluded_theme_counts=(31,)),
)


def normalize_bool(values: pd.Series) -> pd.Series:
    return values.fillna(False).astype(str).str.strip().str.lower().isin({"true", "1", "yes"})


def parse_hhmmss_to_minutes(value: Any) -> float:
    """把 Tushare HHMMSS 数字正确转换为分钟，缺失时返回 NaN。"""

    try:
        digits = str(int(float(value))).zfill(6)
        hour = int(digits[:2])
        minute = int(digits[2:4])
        second = int(digits[4:6])
    except (TypeError, ValueError):
        return float("nan")
    if hour > 23 or minute > 59 or second > 59:
        return float("nan")
    return hour * 60.0 + minute + second / 60.0


def first_time_bucket(minutes: float) -> str:
    if pd.isna(minutes):
        return "unknown"
    if minutes <= 570:
        return "open_auction"
    if minutes <= 600:
        return "before_1000"
    if minutes <= 660:
        return "1000_1100"
    if minutes <= 810:
        return "1100_1330"
    if minutes <= 870:
        return "1330_1430"
    return "after_1430"


def bucket_segment_limit_down_count(value: Any) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return "unknown"
    if number < 1:
        return "lt_1"
    if number < 3:
        return "1_3"
    if number < 8:
        return "3_8"
    if number < 15:
        return "8_15"
    return "gte_15"


def classify_segment_retreat_state(current: Any, previous1: Any, previous2: Any) -> str:
    values = [pd.to_numeric(value, errors="coerce") for value in (current, previous1, previous2)]
    if any(pd.isna(value) for value in values):
        return "unknown"
    current_value, prev1_value, prev2_value = map(float, values)
    if current_value <= 3:
        return "weak_below_3"
    if current_value < prev1_value < prev2_value:
        return "retreat_2day"
    if current_value < prev1_value and current_value <= 5:
        return "retreat_weak"
    if current_value > prev1_value > prev2_value:
        return "warming_2day"
    return "neutral"


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_complete_pool() -> pd.DataFrame:
    scored_columns = [
        "trade_date", "ts_code", "name", "market_segment", "limit_data_quality",
        "strategy_compatible", "is_st", "allow_buy_reliable", "is_fill_score_reliable",
        "limit_times", "first_time", "fd_amount_to_circ_mv", "limit_close", "fill_probability",
    ]
    data = pd.read_csv(
        SCORED_PATH,
        usecols=scored_columns,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    data = data[data["trade_date"].between(START_DATE, END_DATE)].copy()

    theme_columns = [
        "trade_date", "ts_code", "theme_data_available", "theme_source_column", "theme_name",
        "theme_limit_count", "theme_heat_rank", "theme_leader_rank", "theme_height_rank",
        "theme_is_mainline",
    ]
    theme = pd.read_csv(
        THEME_PATH,
        usecols=theme_columns,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    theme = theme[theme["trade_date"].between(START_DATE, END_DATE)].copy()
    if theme.duplicated(["trade_date", "ts_code"]).any():
        raise RuntimeError("完整题材特征存在重复股票日")
    data = data.merge(theme, on=["trade_date", "ts_code"], how="left", validate="one_to_one")

    emotion_columns = [
        "trade_date", "market_segment", "segment_limit_up_count_emotion",
        "segment_limit_up_count_emotion_prev1", "segment_limit_up_count_emotion_prev2",
        "segment_limit_down_count", "market_chain_count",
    ]
    emotion = pd.read_csv(
        EMOTION_PATH,
        usecols=emotion_columns,
        dtype={"trade_date": str, "market_segment": str},
        low_memory=False,
    )
    emotion = emotion[emotion["trade_date"].between(START_DATE, END_DATE)].copy()
    if emotion.duplicated(["trade_date", "market_segment"]).any():
        raise RuntimeError("市场情绪特征存在重复日期/市场分段")
    data = data.merge(emotion, on=["trade_date", "market_segment"], how="left", validate="many_to_one")

    for column in (
        "strategy_compatible", "is_st", "allow_buy_reliable", "is_fill_score_reliable",
        "theme_data_available", "theme_is_mainline",
    ):
        data[column] = normalize_bool(data[column])
    for column in (
        "limit_times", "fd_amount_to_circ_mv", "limit_close", "fill_probability",
        "theme_limit_count", "theme_heat_rank", "theme_leader_rank", "theme_height_rank",
        "segment_limit_down_count", "market_chain_count",
    ):
        data[column] = pd.to_numeric(data[column], errors="coerce")

    data["first_time_minutes"] = data["first_time"].map(parse_hhmmss_to_minutes)
    data["first_time_detail_bucket_correct"] = data["first_time_minutes"].map(first_time_bucket)
    # 当前实盘代码的错误口径，仅用于审计差异，绝不能参与纠正后选股。
    raw_time = pd.to_numeric(data["first_time"], errors="coerce")
    data["first_time_detail_bucket_live_bug"] = pd.cut(
        raw_time,
        bins=[-float("inf"), 570, 600, 660, 810, 870, float("inf")],
        labels=["open_auction", "before_1000", "1000_1100", "1100_1330", "1330_1430", "after_1430"],
    ).astype(str)
    data["segment_retreat_state_bucket"] = data.apply(
        lambda row: classify_segment_retreat_state(
            row.get("segment_limit_up_count_emotion"),
            row.get("segment_limit_up_count_emotion_prev1"),
            row.get("segment_limit_up_count_emotion_prev2"),
        ),
        axis=1,
    )
    data["segment_limit_down_count_bucket"] = data["segment_limit_down_count"].map(
        bucket_segment_limit_down_count
    )
    data["market_chain_count_bucket"] = pd.cut(
        data["market_chain_count"],
        bins=[-float("inf"), 3, 8, 15, 30, float("inf")],
        labels=["lt_3", "3_8", "8_15", "15_30", "gte_30"],
    ).astype(str)
    return data


def safe_mask(data: pd.DataFrame) -> pd.Series:
    return (
        data["limit_data_quality"].astype(str).eq("full")
        & data["strategy_compatible"]
        & ~data["is_st"]
        & data["allow_buy_reliable"]
        & data["is_fill_score_reliable"]
        & data["theme_data_available"]
    )


def select_variant(data: pd.DataFrame, variant: Variant) -> pd.DataFrame:
    mask = (
        safe_mask(data)
        & data["theme_heat_rank"].le(variant.heat_rank_max)
        & data["theme_leader_rank"].le(variant.leader_rank_max)
    )
    if variant.exclude_retreat_2day:
        mask &= data["segment_retreat_state_bucket"].ne("retreat_2day")
    if variant.exclude_down_3_8:
        mask &= data["segment_limit_down_count_bucket"].ne("3_8")
    if variant.excluded_theme_counts:
        mask &= ~data["theme_limit_count"].isin(variant.excluded_theme_counts)

    sort_columns = [
        "trade_date", "theme_heat_rank", "theme_leader_rank", "theme_height_rank",
        "limit_times", "first_time_minutes", "fd_amount_to_circ_mv",
    ]
    ascending = [True, True, True, True, False, True, False]
    selected = (
        data[mask]
        .sort_values(sort_columns, ascending=ascending)
        .groupby("trade_date", as_index=False)
        .head(1)
        .copy()
    )

    # 实盘先确定每日第一名，再执行 model=3 基础门；第一名被拒绝时不回补第二名。
    base_pass = (
        selected["market_segment"].astype(str).ne("star")
        & selected["segment_retreat_state_bucket"].isin({"neutral", "warming_2day"})
        & selected["market_chain_count_bucket"].isin({"3_8", "8_15", "15_30", "gte_30"})
    )
    selected = selected[base_pass].copy()
    selected["variant"] = variant.name
    return selected.sort_values("trade_date").reset_index(drop=True)


def open_trade_dates() -> list[str]:
    calendar = pd.read_csv(CALENDAR_PATH, dtype={"cal_date": str})
    mask = calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})
    return sorted(calendar.loc[mask, "cal_date"].astype(str).unique().tolist())


def load_price_table(codes: Iterable[str]) -> pd.DataFrame:
    columns = ["trade_date", "ts_code", "open", "high", "low", "close", "pre_close"]
    prices = pd.read_csv(
        DAILY_PATH,
        usecols=columns,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    prices = prices[prices["ts_code"].astype(str).isin(set(codes))].copy()
    if prices.duplicated(["trade_date", "ts_code"]).any():
        raise RuntimeError("日线价格存在重复股票日")
    return prices.set_index(["trade_date", "ts_code"]).sort_index()


def limit_pct(ts_code: str, name: str) -> float:
    upper_name = str(name).upper()
    if "ST" in upper_name or "退" in upper_name:
        return 0.05
    if ts_code.endswith(".BJ") or ts_code.startswith(("4", "8", "9")):
        return 0.30
    if ts_code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def fee_rate(config: dict[str, Any]) -> float:
    analysis = config.get("analysis", {})
    commission = float(analysis.get("commission_rate", 0.0003))
    transfer = float(analysis.get("transfer_fee_rate", 0.00001))
    stamp = float(analysis.get("stamp_tax_rate", 0.001))
    return commission + transfer + commission + transfer + stamp


def build_trade_rows(
    selected: pd.DataFrame,
    prices: pd.DataFrame,
    dates: list[str],
    config: dict[str, Any],
    buy_block_mode: str,
) -> pd.DataFrame:
    date_index = {date: index for index, date in enumerate(dates)}
    position_pct = float(config.get("portfolio_certification", {}).get("position_pct", 0.825))
    costs = fee_rate(config)
    rows: list[dict[str, Any]] = []
    for _, signal in selected.iterrows():
        signal_date = str(signal["trade_date"])
        ts_code = str(signal["ts_code"])
        name = str(signal.get("name", ts_code))
        index = date_index.get(signal_date)
        base = {
            "variant": str(signal["variant"]),
            "buy_block_mode": buy_block_mode,
            "signal_date": signal_date,
            "ts_code": ts_code,
            "name": name,
            "market_segment": str(signal.get("market_segment", "")),
            "theme_name": str(signal.get("theme_name", "")),
            "theme_heat_rank": signal.get("theme_heat_rank"),
            "theme_leader_rank": signal.get("theme_leader_rank"),
            "theme_limit_count": signal.get("theme_limit_count"),
            "first_time": signal.get("first_time"),
            "first_time_detail_bucket_correct": signal.get("first_time_detail_bucket_correct"),
            "first_time_detail_bucket_live_bug": signal.get("first_time_detail_bucket_live_bug"),
            "segment_retreat_state_bucket": signal.get("segment_retreat_state_bucket"),
            "segment_limit_down_count_bucket": signal.get("segment_limit_down_count_bucket"),
            "market_chain_count_bucket": signal.get("market_chain_count_bucket"),
        }
        if index is None or index + 2 >= len(dates):
            rows.append({**base, "status": "MISSING_FORWARD_CALENDAR"})
            continue
        try:
            buy_day = prices.loc[(dates[index + 1], ts_code)]
        except KeyError:
            rows.append({**base, "status": "MISSING_BUY_PRICE"})
            continue
        up_limit = float(buy_day["pre_close"]) * (
            1.0 + limit_pct(ts_code, name) - LIMIT_TOLERANCE
        )
        open_limit = float(buy_day["open"]) >= up_limit
        one_word_limit = float(buy_day["low"]) >= up_limit
        buy_blocked = open_limit if buy_block_mode == "open_limit_unbuyable" else one_word_limit
        if buy_blocked:
            rows.append(
                {
                    **base,
                    "status": "BUY_BLOCKED",
                    "buy_date": dates[index + 1],
                    "buy_open_limit": open_limit,
                    "buy_one_word_limit": one_word_limit,
                }
            )
            continue

        buy_price = float(buy_day["open"]) * (1.0 + BUY_SLIPPAGE)
        exit_date = ""
        sell_price = 0.0
        delayed_days = 0
        forward_values: dict[str, Any] = {
            "d1_trade_date": dates[index + 1],
            "d1_open": float(buy_day["open"]),
            "d1_high": float(buy_day["high"]),
            "d1_low": float(buy_day["low"]),
            "d1_close": float(buy_day["close"]),
            "d1_pre_close": float(buy_day["pre_close"]),
        }
        missing_forward = False
        # 实盘不会在D5后凭空丢掉持仓，而会继续等待可卖日。这里最多向后检查20个
        # 交易日；停牌/缺行情日跳过，避免把缺行情误当成已经释放资金。
        for offset in range(2, 21):
            if index + offset >= len(dates):
                missing_forward = True
                break
            try:
                exit_day = prices.loc[(dates[index + offset], ts_code)]
            except KeyError:
                delayed_days += 1
                continue
            for field in ("open", "high", "low", "close", "pre_close"):
                forward_values[f"d{offset}_{field}"] = float(exit_day[field])
            forward_values[f"d{offset}_trade_date"] = dates[index + offset]
            down_limit = float(exit_day["pre_close"]) * (
                1.0 - limit_pct(ts_code, name) + LIMIT_TOLERANCE
            )
            if float(exit_day["open"]) <= down_limit or float(exit_day["close"]) <= down_limit:
                delayed_days += 1
                continue
            exit_date = dates[index + offset]
            sell_price = float(exit_day["close"]) * (1.0 - SELL_SLIPPAGE)
            break
        if missing_forward:
            rows.append({**base, **forward_values, "status": "MISSING_EXIT_PRICE"})
            continue
        if not exit_date:
            rows.append({**base, **forward_values, "status": "SELL_UNRESOLVED"})
            continue

        net_return = sell_price / buy_price - 1.0 - costs
        rows.append(
            {
                **base,
                **forward_values,
                "status": "EXECUTABLE",
                "buy_date": dates[index + 1],
                "exit_date": exit_date,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "sell_delay_days": delayed_days,
                "stock_net_return": net_return,
                "account_return": net_return * position_pct,
                "replay_rule": "fixed_t2_close",
            }
        )
    return pd.DataFrame(rows)


def apply_single_account(trades: pd.DataFrame) -> pd.DataFrame:
    ordered = trades.sort_values("signal_date").copy()
    kept: list[pd.Series] = []
    position_until = ""
    for _, row in ordered.iterrows():
        if position_until and str(row["signal_date"]) < position_until:
            continue
        if str(row.get("status", "")) == "EXECUTABLE":
            kept.append(row)
            position_until = str(row["exit_date"])
        elif str(row.get("status", "")) == "SELL_UNRESOLVED":
            position_until = "99991231"
    executable = ordered[ordered["status"].eq("EXECUTABLE")]
    return pd.DataFrame(kept).reset_index(drop=True) if kept else executable.iloc[0:0].copy()


def max_consecutive_losses(returns: pd.Series) -> int:
    current = 0
    maximum = 0
    for value in returns:
        if float(value) < 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def metrics(trades: pd.DataFrame) -> dict[str, Any]:
    returns = pd.to_numeric(trades.get("account_return", pd.Series(dtype=float)), errors="coerce").dropna()
    if returns.empty:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "max_consecutive_losses": 0,
        }
    equity = (1.0 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    profits = returns[returns > 0]
    losses = returns[returns < 0]
    loss_mean = abs(float(losses.mean())) if len(losses) else 0.0
    return {
        "trade_count": int(len(returns)),
        "win_rate": float((returns > 0).mean()),
        "avg_account_return": float(returns.mean()),
        "median_account_return": float(returns.median()),
        "equity_multiple": float(equity.iloc[-1]),
        "max_drawdown": float(drawdown.min()),
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "profit_loss_ratio": float(profits.mean() / loss_mean) if len(profits) and loss_mean > 0 else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def split_masks(trades: pd.DataFrame) -> dict[str, pd.Series]:
    dates = trades["signal_date"].astype(str)
    return {
        "ALL": pd.Series(True, index=trades.index),
        f"TRAIN_LT_{SPLIT_DATE}": dates.lt(SPLIT_DATE),
        f"TEST_GE_{SPLIT_DATE}": dates.ge(SPLIT_DATE),
        "YEAR_2024": dates.str.startswith("2024"),
        "YEAR_2025": dates.str.startswith("2025"),
        "YEAR_2026": dates.str.startswith("2026"),
    }


def summarize_standalone(
    selected_by_variant: dict[str, pd.DataFrame],
    prices: pd.DataFrame,
    dates: list[str],
    config: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    summaries: list[dict[str, Any]] = []
    all_trades: list[pd.DataFrame] = []
    for buy_mode in ("open_limit_unbuyable", "one_word_limit_unbuyable"):
        for variant_name, selected in selected_by_variant.items():
            raw = build_trade_rows(selected, prices, dates, config, buy_mode)
            trades = apply_single_account(raw)
            all_trades.append(trades)
            for split, mask in split_masks(trades).items():
                summaries.append(
                    {
                        "variant": variant_name,
                        "buy_block_mode": buy_mode,
                        "split": split,
                        "signal_count": int(len(selected)),
                        "buy_blocked_count": int(raw["status"].eq("BUY_BLOCKED").sum()),
                        "unresolved_count": int(raw["status"].eq("SELL_UNRESOLVED").sum()),
                        **metrics(trades[mask]),
                    }
                )
    return pd.DataFrame(summaries), pd.concat(all_trades, ignore_index=True)


def to_certification_rows(trades: pd.DataFrame, open_limit_mode: bool) -> pd.DataFrame:
    rows = trades[trades["status"].eq("EXECUTABLE")].drop_duplicates("signal_date").copy()
    rows = rows.rename(columns={"signal_date": "trade_date"})
    # 组合认证的旧L收益函数最多读取到d5。真实逻辑会继续等待，因此把D5之后的首个
    # 可卖日折叠到d5槽位，同时保留真实退出日期，避免组合资金被错误提前释放。
    for index, row in rows.iterrows():
        actual_offset = int(pd.to_numeric(row.get("sell_delay_days", 0), errors="coerce") or 0) + 2
        if actual_offset <= 5:
            continue
        for field in ("trade_date", "open", "high", "low", "close", "pre_close"):
            source = f"d{actual_offset}_{field}"
            if source in rows.columns and pd.notna(row.get(source)):
                rows.at[index, f"d5_{field}"] = row[source]
    if open_limit_mode and not rows.empty:
        # 当前组合认证函数以 d1_low 判断一字板。把 low 设为 open 后，可复现更严格的
        # “开盘即涨停视为不可买”压力口径，不改变非涨停开盘样本。
        rows["d1_low"] = rows["d1_open"]
    return rows


def summarize_portfolio(corrected_l_trades: dict[str, pd.DataFrame]) -> pd.DataFrame:
    import scripts.certify_current_executable_portfolio as certify
    from scripts.research_strategy_model3_switch import build_l_lookup

    sources = certify.load_sources()
    scenarios: list[tuple[str, dict[str, pd.Series]]] = [("NO_L", {})]
    for mode, trades in corrected_l_trades.items():
        rows = to_certification_rows(trades, open_limit_mode=mode == "open_limit_unbuyable")
        scenarios.append((f"CORRECTED_L_{mode}", build_l_lookup(rows)))

    output: list[dict[str, Any]] = []
    for scenario, lookup in scenarios:
        replaced_sources = replace(sources, l_lookup=lookup)
        replay = certify.replay(
            replaced_sources,
            entry_gate_enabled=True,
            l_chain_3_8_enabled=True,
            m_enabled=True,
        )
        masks = {
            "ALL": pd.Series(True, index=replay.index),
            f"TRAIN_LT_{SPLIT_DATE}": replay["signal_date"].astype(str).lt(SPLIT_DATE),
            f"TEST_GE_{SPLIT_DATE}": replay["signal_date"].astype(str).ge(SPLIT_DATE),
            "YEAR_2024": replay["signal_date"].astype(str).str.startswith("2024"),
            "YEAR_2025": replay["signal_date"].astype(str).str.startswith("2025"),
            "YEAR_2026": replay["signal_date"].astype(str).str.startswith("2026"),
        }
        for split, mask in masks.items():
            subset = replay[mask].copy()
            executed = subset[subset["status"].eq("EXECUTED")]
            split_metrics = metrics(executed)
            output.append(
                {
                    "scenario": scenario,
                    "split": split,
                    "executed_trade_count": int(len(executed)),
                    "l_trade_count": int(executed["strategy_leg"].astype(str).eq("L").sum()),
                    "win_rate": float(split_metrics["win_rate"]),
                    "avg_return": float(split_metrics["avg_account_return"]),
                    "median_return": float(split_metrics["median_account_return"]),
                    "equity_multiple": float(split_metrics["equity_multiple"]),
                    "max_drawdown": float(split_metrics["max_drawdown"]),
                    "max_profit": float(split_metrics["max_profit"]),
                    "max_loss": float(split_metrics["max_loss"]),
                    "max_consecutive_losses": int(split_metrics["max_consecutive_losses"]),
                }
            )
    return pd.DataFrame(output)


def markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "无数据。"
    view = frame.copy()
    for column in view.columns:
        if pd.api.types.is_float_dtype(view[column]):
            view[column] = view[column].map(lambda value: f"{float(value):.6f}" if pd.notna(value) else "")
        else:
            view[column] = view[column].fillna("").astype(str)
    headers = list(view.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(map(str, row)) + " |" for row in view.values.tolist())
    return "\n".join(lines)


def write_report(
    pool: pd.DataFrame,
    selected_by_variant: dict[str, pd.DataFrame],
    standalone: pd.DataFrame,
    portfolio: pd.DataFrame,
) -> None:
    current = standalone[
        standalone["variant"].eq("L2_CURRENT")
        & standalone["buy_block_mode"].eq("open_limit_unbuyable")
    ].copy()
    current_all = current[current["split"].eq("ALL")].iloc[0]
    old_source = pd.read_csv(
        PROJECT_ROOT / "reports" / "strategy_l" / "leader_strategy_trades.csv",
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    old_source = old_source[old_source["l_rule"].astype(str).eq("L_theme_mainline_leader")].copy()
    old_l2 = old_source[
        old_source["segment_retreat_state_bucket"].astype(str).ne("retreat_2day")
        & old_source["segment_limit_down_count_bucket"].astype(str).ne("3_8")
        & pd.to_numeric(old_source["theme_limit_count"], errors="coerce").ne(30)
    ].copy()
    full_pairs = pool[["trade_date", "ts_code"]].drop_duplicates()
    old_l2_in_full = old_l2.merge(
        pool[["trade_date", "ts_code", "theme_heat_rank", "theme_leader_rank"]],
        on=["trade_date", "ts_code"],
        how="left",
        suffixes=("_old", "_full"),
    )
    still_current_body = (
        pd.to_numeric(old_l2_in_full["theme_heat_rank_full"], errors="coerce").le(3)
        & pd.to_numeric(old_l2_in_full["theme_leader_rank_full"], errors="coerce").eq(1)
    )
    time_bug_count = int(
        (
            pool["first_time_detail_bucket_correct"].astype(str)
            != pool["first_time_detail_bucket_live_bug"].astype(str)
        ).sum()
    )

    verdict = "不可作为已验证策略继续实盘"
    lines = [
        "# 策略 L 完整候选池与过拟合审计",
        "",
        "## 最终判定",
        "",
        f"**{verdict}。**",
        "",
        "当前实盘代码能稳定生成信号，但旧历史认证使用了错误的候选子集；按完整候选池和当前规则重放后，",
        "L2 在训练段、测试段和三个自然年均没有正期望，参数邻域也没有形成稳定正收益平台。",
        "这不是三笔实盘造成的结论，而是完整历史样本的结果。",
        "",
        "## 数据质量与口径",
        "",
        f"- 审计区间：{START_DATE}~{END_DATE}。",
        f"- 完整候选股票日：{len(full_pairs)}，交易日：{pool['trade_date'].nunique()}。",
        f"- 当前 L2 通过每日第一名及 model=3 基础门的信号日：{len(selected_by_variant['L2_CURRENT'])}。",
        "- T+1开盘买入、T+2收盘退出；开盘涨停不可买；跌停/停牌最多继续检查20个交易日，未解除时锁定账户且不把持仓凭空删除。",
        "- 双边0.1%滑点、佣金、过户费和卖出印花税均已扣除；单账户持仓不重叠。",
        f"- 当前 first_time HHMMSS/分钟混用导致完整池中 {time_bug_count} 行时间桶错误。",
        "",
        "## 当前 L2 单策略结果",
        "",
        markdown_table(current[[
            "split", "signal_count", "buy_blocked_count", "trade_count", "win_rate",
            "avg_account_return", "median_account_return", "equity_multiple", "max_drawdown",
            "max_profit", "max_loss", "profit_loss_ratio", "max_consecutive_losses",
        ]]),
        "",
        "## 参数邻域与过滤稳健性（严格开盘涨停不可买，全样本）",
        "",
        markdown_table(
            standalone[
                standalone["buy_block_mode"].eq("open_limit_unbuyable")
                & standalone["split"].eq("ALL")
            ][[
                "variant", "signal_count", "trade_count", "win_rate", "avg_account_return",
                "median_account_return", "equity_multiple", "max_drawdown", "max_consecutive_losses",
            ]].sort_values("variant")
        ),
        "",
        "## 当前组合复算",
        "",
        "以下组合结果对L采用偏乐观处理：L若因开盘涨停未买到，允许同日后续策略递补。即使在这个更有利于L的口径下，加入纠正后的L仍显著破坏组合收益与回撤。",
        "",
        markdown_table(portfolio),
        "",
        "## 旧认证失效证据",
        "",
        f"- 旧L2共{len(old_l2)}个理论信号；放回完整候选池后，仅{int(still_current_body.sum())}个仍满足当前“题材热度前三且题材龙头第一”。",
        "- 旧回测在选股前读取scenario_executed子集，题材排名不是在完整涨停池中生成。",
        "- L2过滤来自同一样本上大规模组合搜索，没有独立盲测；精确排除theme_limit_count=30缺乏邻域稳定性。",
        "",
        "## 实盘结论",
        "",
        "1. 当前L不满足继续作为82.5%仓位、高优先级正式策略的统计条件。",
        "2. 不能通过修改一两个过滤条件继续上线；完整池中的基础L和L2均为负。",
        "3. 如果保留研究，只能转为影子信号，重新定义真正题材数据与龙头规则后再做独立样本外验证。",
        "4. 当前3笔真实成交只作为附加样本，不是本次否决的主要依据。",
        "",
        f"严格口径全样本核心值：{int(current_all['trade_count'])}笔，胜率{float(current_all['win_rate']):.2%}，",
        f"平均账户收益{float(current_all['avg_account_return']):.3%}，中位数{float(current_all['median_account_return']):.3%}，",
        f"复利{float(current_all['equity_multiple']):.6f}倍，最大回撤{float(current_all['max_drawdown']):.2%}。",
    ]
    (OUTPUT_DIR / "l_full_universe_audit_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()
    pool = load_complete_pool()
    selected_by_variant = {variant.name: select_variant(pool, variant) for variant in VARIANTS}
    codes = set(pd.concat([frame["ts_code"] for frame in selected_by_variant.values()]).astype(str))
    prices = load_price_table(codes)
    dates = open_trade_dates()
    standalone_summary, standalone_trades = summarize_standalone(
        selected_by_variant, prices, dates, config
    )

    current_selected = selected_by_variant["L2_CURRENT"]
    corrected_for_portfolio: dict[str, pd.DataFrame] = {}
    for mode in ("open_limit_unbuyable", "one_word_limit_unbuyable"):
        corrected_for_portfolio[mode] = build_trade_rows(
            current_selected, prices, dates, config, mode
        )
    portfolio_summary = summarize_portfolio(corrected_for_portfolio)

    keep_columns = [
        "variant", "trade_date", "ts_code", "name", "market_segment", "theme_name",
        "theme_heat_rank", "theme_leader_rank", "theme_height_rank", "theme_limit_count",
        "segment_retreat_state_bucket", "segment_limit_down_count_bucket",
        "market_chain_count_bucket", "first_time", "first_time_minutes",
        "first_time_detail_bucket_correct", "first_time_detail_bucket_live_bug",
        "limit_close", "fill_probability",
    ]
    pd.concat(selected_by_variant.values(), ignore_index=True)[keep_columns].to_csv(
        OUTPUT_DIR / "l_full_universe_candidates.csv", index=False, encoding="utf-8-sig"
    )
    standalone_trades.to_csv(
        OUTPUT_DIR / "l_full_universe_trades.csv", index=False, encoding="utf-8-sig"
    )
    standalone_summary.to_csv(
        OUTPUT_DIR / "l_full_universe_summary.csv", index=False, encoding="utf-8-sig"
    )
    portfolio_summary.to_csv(
        OUTPUT_DIR / "l_corrected_portfolio_summary.csv", index=False, encoding="utf-8-sig"
    )
    write_report(pool, selected_by_variant, standalone_summary, portfolio_summary)

    current = standalone_summary[
        standalone_summary["variant"].eq("L2_CURRENT")
        & standalone_summary["buy_block_mode"].eq("open_limit_unbuyable")
        & standalone_summary["split"].eq("ALL")
    ].iloc[0]
    print("策略L完整候选池审计完成")
    print(
        f"严格口径：{int(current['trade_count'])}笔，胜率{float(current['win_rate']):.2%}，"
        f"平均账户收益{float(current['avg_account_return']):.3%}，"
        f"复利{float(current['equity_multiple']):.6f}倍，"
        f"最大回撤{float(current['max_drawdown']):.2%}"
    )
    print(f"输出目录：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
