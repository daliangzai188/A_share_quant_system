"""真实成交滚动绩效口径。

只使用成交完成汇总中的真实买卖金额。缺少卖出金额、数量不闭合或仍有隔夜残量的
交易不进入收益统计，并在数据质量指标中单独暴露，避免用0元卖出价污染策略结论。
"""
from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

from src.strategy_identity import normalize_strategy_frame, normalize_strategy_leg


ACTIVE_LEGS = {"A", "C", "D", "E", "L", "M"}


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    values = frame.get(column)
    if values is None:
        return pd.Series(0.0, index=frame.index, dtype=float)
    return pd.to_numeric(values, errors="coerce").fillna(0.0)


def _plan_source(frame: pd.DataFrame) -> pd.Series:
    """兼容旧汇总：历史回填目标不能用于证明真实容量。"""

    if "entry_plan_source" in frame.columns:
        return frame["entry_plan_source"].fillna("").astype(str).str.upper()
    notes = frame.get("data_quality_note", pd.Series("", index=frame.index))
    notes = notes.fillna("").astype(str)
    return pd.Series(
        [
            "BACKFILLED"
            if "回填" in note
            else "MISSING"
            if "缺少原始计划" in note
            else "LIVE_FROZEN"
            for note in notes
        ],
        index=frame.index,
        dtype=str,
    )


def _maximum_consecutive_losses(values: pd.Series) -> int:
    current = maximum = 0
    for value in values:
        if float(value) < 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def completed_live_trades(
    raw: pd.DataFrame,
    config: Mapping[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """清洗真实成交，并按佣金、印花税、过户费估算净收益。"""

    required = {
        "trade_key",
        "entry_date",
        "exit_date",
        "ts_code",
        "strategy_leg",
        "entry_filled_qty",
        "entry_fill_amount",
        "exit_filled_qty",
        "exit_fill_amount",
    }
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError("真实成交汇总缺少字段：" + "、".join(missing))
    active_legs = {
        normalize_strategy_leg(value)
        for value in config.get("active_legs", sorted(ACTIVE_LEGS))
    }
    frame = normalize_strategy_frame(raw)
    frame = frame[frame["strategy_leg"].isin(active_legs)].copy()
    for column in (
        "entry_filled_qty",
        "entry_fill_amount",
        "exit_filled_qty",
        "exit_fill_amount",
        "total_slippage_bps",
    ):
        frame[column] = pd.to_numeric(
            frame.get(column, pd.Series(0.0, index=frame.index)), errors="coerce"
        ).fillna(0.0)
    frame["exit_date"] = (
        frame["exit_date"].fillna("").astype(str).str.replace("-", "", regex=False).str[:8]
    )
    frame["data_complete"] = (
        frame["entry_filled_qty"].gt(0)
        & frame["entry_fill_amount"].gt(0)
        & frame["exit_filled_qty"].eq(frame["entry_filled_qty"])
        & frame["exit_fill_amount"].gt(0)
        & frame["exit_date"].str.fullmatch(r"\d{8}")
    )
    quality = {
        "active_trade_rows": int(len(frame)),
        "complete_trade_rows": int(frame["data_complete"].sum()),
        "incomplete_trade_rows": int((~frame["data_complete"]).sum()),
        "data_complete_rate": float(frame["data_complete"].mean()) if len(frame) else 0.0,
    }
    trades = frame[frame["data_complete"]].copy()
    if trades.empty:
        return trades, quality

    commission = float(config.get("commission_rate", 0.0003))
    stamp_tax = float(config.get("stamp_tax_rate", 0.001))
    transfer_fee = float(config.get("transfer_fee_rate", 0.00001))
    minimum_commission = float(config.get("minimum_commission", 5.0))
    buy_commission = (trades["entry_fill_amount"] * commission).clip(lower=minimum_commission)
    sell_commission = (trades["exit_fill_amount"] * commission).clip(lower=minimum_commission)
    trades["estimated_fees"] = (
        buy_commission
        + sell_commission
        + (trades["entry_fill_amount"] + trades["exit_fill_amount"]) * transfer_fee
        + trades["exit_fill_amount"] * stamp_tax
    )
    trades["net_pnl"] = (
        trades["exit_fill_amount"] - trades["entry_fill_amount"] - trades["estimated_fees"]
    )
    trades["net_return"] = trades["net_pnl"] / trades["entry_fill_amount"]
    return trades.sort_values(["exit_date", "trade_key"]).reset_index(drop=True), quality


def performance_metrics(trades: pd.DataFrame, segment: str) -> dict[str, Any]:
    returns = pd.to_numeric(trades.get("net_return", pd.Series(dtype=float)), errors="coerce").dropna()
    if returns.empty:
        return {
            "segment": segment,
            "sample_count": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "total_net_pnl": 0.0,
            "return_on_invested_capital": 0.0,
            "hypothetical_full_notional_multiple": 1.0,
            "hypothetical_max_drawdown": 0.0,
            "profit_loss_ratio": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "max_consecutive_losses": 0,
            "avg_total_slippage_bps": 0.0,
        }
    curve = (1.0 + returns).cumprod()
    peak = curve.cummax().clip(lower=1.0)
    gains, losses = returns[returns > 0], returns[returns < 0]
    invested = float(pd.to_numeric(trades["entry_fill_amount"], errors="coerce").sum())
    slippage = pd.to_numeric(trades.get("total_slippage_bps", 0.0), errors="coerce")
    return {
        "segment": segment,
        "sample_count": int(len(returns)),
        "win_rate": float((returns > 0).mean()),
        "avg_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "total_net_pnl": float(trades["net_pnl"].sum()),
        "return_on_invested_capital": float(trades["net_pnl"].sum() / invested) if invested else 0.0,
        "hypothetical_full_notional_multiple": float(curve.iloc[-1]),
        "hypothetical_max_drawdown": float((curve / peak - 1.0).min()),
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "max_consecutive_losses": _maximum_consecutive_losses(returns),
        "avg_total_slippage_bps": float(slippage[slippage.ne(0)].mean()) if slippage.ne(0).any() else 0.0,
    }


def rolling_metrics(trades: pd.DataFrame, windows: list[int]) -> pd.DataFrame:
    rows = [performance_metrics(trades, "全部真实成交")]
    rows.extend(performance_metrics(trades.tail(size), f"最近{size}笔") for size in windows)
    rows.extend(
        performance_metrics(group, f"策略{leg}")
        for leg, group in trades.groupby("strategy_leg", sort=True)
    )
    return pd.DataFrame(rows)


def _capacity_segment_metrics(
    frame: pd.DataFrame,
    segment: str,
    *,
    full_fill_threshold: float,
) -> dict[str, Any]:
    trustworthy = frame[frame["entry_plan_source"].eq("LIVE_FROZEN")].copy()
    target_qty = _numeric(trustworthy, "entry_target_qty")
    entry_qty = _numeric(trustworthy, "entry_filled_qty")
    target_amount = _numeric(trustworthy, "entry_target_amount")
    entry_amount = _numeric(trustworthy, "entry_fill_amount")
    entry_ratio = (entry_qty / target_qty.where(target_qty.gt(0))).fillna(0.0)
    entry_amount_ratio = (
        entry_amount / target_amount.where(target_amount.gt(0))
    ).fillna(0.0)
    entry_ratio_capped = entry_ratio.clip(lower=0.0, upper=1.0)
    exit_qty = _numeric(trustworthy, "exit_filled_qty")
    exit_target = _numeric(trustworthy, "exit_target_qty")
    exit_ratio = (exit_qty / exit_target.where(exit_target.gt(0))).fillna(0.0)
    overnight = _numeric(trustworthy, "overnight_residual_qty")
    status = trustworthy.get(
        "execution_status", pd.Series("", index=trustworthy.index)
    ).fillna("").astype(str)
    exit_eligible = status.isin({"已平仓", "平仓中"}) | overnight.gt(0)
    entry_benchmark = _numeric(trustworthy, "benchmark_open")
    exit_benchmark = _numeric(trustworthy, "benchmark_close")
    entry_filled = entry_qty.gt(0)
    exit_filled = exit_qty.gt(0)
    buy_slippage = _numeric(trustworthy, "buy_slippage_bps")[
        entry_filled & entry_benchmark.gt(0)
    ]
    sell_slippage = _numeric(trustworthy, "sell_slippage_bps")[
        exit_filled & exit_benchmark.gt(0)
    ]
    total_slippage = (
        _numeric(trustworthy, "buy_slippage_bps")
        + _numeric(trustworthy, "sell_slippage_bps")
    )[
        entry_filled
        & entry_benchmark.gt(0)
        & exit_filled
        & exit_benchmark.gt(0)
    ]
    eligible_count = int(exit_eligible.sum())
    planned_amount = float(target_amount.sum())
    return {
        "segment": segment,
        "all_plan_rows": int(len(frame)),
        "trustworthy_plan_count": int(len(trustworthy)),
        "backfilled_plan_count": int(frame["entry_plan_source"].eq("BACKFILLED").sum()),
        "missing_plan_count": int(frame["entry_plan_source"].eq("MISSING").sum()),
        "zero_fill_count": int(entry_qty.eq(0).sum()),
        # 金额型补单会在成交价低于计划参考价时多买一手，但只要总成交金额仍在
        # 冻结预算内，就不是资金暴露超额。目标金额缺失时才退回纯股数口径。
        "overfill_count": int(
            (
                entry_ratio.gt(1.01)
                & (target_amount.le(0) | entry_amount_ratio.gt(1.01))
            ).sum()
        ),
        "entry_full_fill_rate": float(entry_ratio.ge(full_fill_threshold).mean())
        if len(trustworthy)
        else 0.0,
        "avg_entry_qty_completion": float(entry_ratio_capped.mean())
        if len(trustworthy)
        else 0.0,
        "p10_entry_qty_completion": float(entry_ratio_capped.quantile(0.10))
        if len(trustworthy)
        else 0.0,
        "planned_entry_amount": planned_amount,
        "filled_entry_amount": float(entry_amount.sum()),
        "entry_notional_completion": float(
            min(float(entry_amount.sum()) / planned_amount, 1.0)
        )
        if planned_amount > 0
        else 0.0,
        "exit_eligible_count": eligible_count,
        "exit_full_completion_rate": float(
            exit_ratio[exit_eligible].ge(full_fill_threshold).mean()
        )
        if eligible_count
        else 0.0,
        "overnight_residual_count": int(overnight.gt(0).sum()),
        "buy_benchmark_coverage": float(entry_benchmark[entry_filled].gt(0).mean())
        if entry_filled.any()
        else 0.0,
        "sell_benchmark_coverage": float(exit_benchmark[exit_filled].gt(0).mean())
        if exit_filled.any()
        else 0.0,
        "avg_buy_slippage_bps": float(buy_slippage.mean()) if len(buy_slippage) else 0.0,
        "p90_buy_slippage_bps": float(buy_slippage.quantile(0.90))
        if len(buy_slippage)
        else 0.0,
        "avg_sell_slippage_bps": float(sell_slippage.mean())
        if len(sell_slippage)
        else 0.0,
        "p90_sell_slippage_bps": float(sell_slippage.quantile(0.90))
        if len(sell_slippage)
        else 0.0,
        "avg_total_slippage_bps": float(total_slippage.mean())
        if len(total_slippage)
        else 0.0,
    }


def execution_capacity_metrics(
    raw: pd.DataFrame,
    config: Mapping[str, Any],
) -> pd.DataFrame:
    """按真实冻结计划评估成交率和TCA；历史反推目标只披露、不参与认证。"""

    required = {
        "strategy_leg",
        "entry_target_qty",
        "entry_filled_qty",
        "entry_target_amount",
        "entry_fill_amount",
        "exit_target_qty",
        "exit_filled_qty",
    }
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError("容量/TCA汇总缺少字段：" + "、".join(missing))
    active_legs = {
        normalize_strategy_leg(value)
        for value in config.get("active_legs", sorted(ACTIVE_LEGS))
    }
    frame = normalize_strategy_frame(raw)
    frame = frame[frame["strategy_leg"].isin(active_legs)].copy()
    frame["entry_plan_source"] = _plan_source(frame)
    review = dict(config.get("capacity_review", {}))
    threshold = float(review.get("full_fill_threshold", 0.98))
    rows = [_capacity_segment_metrics(frame, "全部当前策略", full_fill_threshold=threshold)]
    rows.extend(
        _capacity_segment_metrics(group, f"策略{leg}", full_fill_threshold=threshold)
        for leg, group in frame.groupby("strategy_leg", sort=True)
    )
    return pd.DataFrame(rows)


def capacity_monitor_status(
    capacity_metrics: pd.DataFrame,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """容量状态只进入报告，除非未来另行评审，绝不自动改变实盘下单。"""

    if capacity_metrics.empty:
        return {
            "status": "INSUFFICIENT_SAMPLE",
            "reason": "没有可用的容量/TCA记录。",
            "capacity_certified": False,
        }
    row = capacity_metrics.iloc[0]
    review = dict(config.get("capacity_review", {}))
    minimum = int(review.get("minimum_trustworthy_plans", 20))
    minimum_fill_rate = float(review.get("minimum_full_fill_rate", 0.90))
    minimum_completion = float(review.get("minimum_avg_entry_completion", 0.95))
    minimum_benchmark = float(review.get("minimum_benchmark_coverage", 0.90))
    count = int(row["trustworthy_plan_count"])
    if count < minimum:
        status = "INSUFFICIENT_SAMPLE"
        reason = f"真实冻结计划仅{count}笔，少于容量复核门槛{minimum}笔。"
    elif (
        float(row["buy_benchmark_coverage"]) < minimum_benchmark
        or float(row["sell_benchmark_coverage"]) < minimum_benchmark
    ):
        status = "DATA_GAP"
        reason = "开盘或收盘基准价覆盖不足，不能认证滑点。"
    elif (
        float(row["entry_full_fill_rate"]) < minimum_fill_rate
        or float(row["avg_entry_qty_completion"]) < minimum_completion
        or (
            int(row["exit_eligible_count"]) > 0
            and float(row["exit_full_completion_rate"]) < minimum_fill_rate
        )
        or int(row["overnight_residual_count"]) > 0
        or int(row["overfill_count"]) > 0
    ):
        status = "WATCH"
        reason = "成交完成率、超额成交或隔夜残量未达到容量认证标准。"
    else:
        status = "PASS"
        reason = "容量样本、成交完成率、基准覆盖和退出完整率均达到当前复核标准。"
    return {
        "status": status,
        "reason": reason,
        "capacity_certified": status == "PASS",
        "minimum_trustworthy_plans": minimum,
        "trustworthy_plan_count": count,
        "entry_full_fill_rate": float(row["entry_full_fill_rate"]),
        "avg_entry_qty_completion": float(row["avg_entry_qty_completion"]),
        "exit_full_completion_rate": float(row["exit_full_completion_rate"]),
        "buy_benchmark_coverage": float(row["buy_benchmark_coverage"]),
        "sell_benchmark_coverage": float(row["sell_benchmark_coverage"]),
        "overnight_residual_count": int(row["overnight_residual_count"]),
        "overfill_count": int(row["overfill_count"]),
        "enforce_live_gate": bool(review.get("enforce_live_gate", False)),
    }
