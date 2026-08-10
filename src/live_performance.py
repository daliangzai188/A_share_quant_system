"""真实成交滚动绩效口径。

只使用成交完成汇总中的真实买卖金额。缺少卖出金额、数量不闭合或仍有隔夜残量的
交易不进入收益统计，并在数据质量指标中单独暴露，避免用0元卖出价污染策略结论。
"""
from __future__ import annotations

from typing import Any, Mapping

import pandas as pd


ACTIVE_LEGS = {"A", "C", "D", "E2", "L", "M"}


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
        str(value).upper()
        for value in config.get("active_legs", sorted(ACTIVE_LEGS))
    }
    frame = raw.copy()
    frame["strategy_leg"] = frame["strategy_leg"].fillna("").astype(str).str.upper()
    frame = frame[frame["strategy_leg"].isin(active_legs)].copy()
    for column in (
        "entry_filled_qty",
        "entry_fill_amount",
        "exit_filled_qty",
        "exit_fill_amount",
        "total_slippage_bps",
    ):
        frame[column] = pd.to_numeric(frame.get(column, 0.0), errors="coerce").fillna(0.0)
    frame["data_complete"] = (
        frame["entry_filled_qty"].gt(0)
        & frame["entry_fill_amount"].gt(0)
        & frame["exit_filled_qty"].eq(frame["entry_filled_qty"])
        & frame["exit_fill_amount"].gt(0)
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
    trades["exit_date"] = trades["exit_date"].astype(str).str.replace("-", "", regex=False).str[:8]
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
