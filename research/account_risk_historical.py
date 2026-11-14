"""账户风险暂停规则的无前视历史叠加回放。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class RiskOverlaySpec:
    daily_loss_pct: float | None
    drawdown_pct: float | None
    consecutive_losses: int | None
    cooldown_trade_days: int

    @property
    def candidate_id(self) -> str:
        def value(item: float | int | None) -> str:
            return "off" if item is None else str(item).replace(".", "p")

        return (
            f"daily_{value(self.daily_loss_pct)}__dd_{value(self.drawdown_pct)}__"
            f"streak_{value(self.consecutive_losses)}__cooldown_{self.cooldown_trade_days}"
        )


def normalize_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "")[:8]
    return text if len(text) == 8 and text.isdigit() else ""


def validate_inputs(
    trades: pd.DataFrame, daily: pd.DataFrame
) -> tuple[pd.DataFrame, list[str]]:
    required = {"signal_date", "exit_date", "account_return", "strategy_leg", "ts_code"}
    missing = sorted(required - set(trades.columns))
    if missing:
        raise ValueError("组合交易缺少字段：" + "、".join(missing))
    if "signal_date" not in daily.columns:
        raise ValueError("组合逐日表缺少signal_date")
    frame = trades.copy()
    for column in ("signal_date", "exit_date"):
        frame[column] = frame[column].map(normalize_date)
    frame["account_return"] = pd.to_numeric(frame["account_return"], errors="coerce")
    if frame["signal_date"].eq("").any() or frame["exit_date"].eq("").any():
        raise ValueError("组合交易存在无效信号日或退出日")
    if frame["account_return"].isna().any() or frame["account_return"].le(-1).any():
        raise ValueError("组合交易收益为空或不大于-100%")
    if frame["signal_date"].duplicated().any():
        raise ValueError("组合交易signal_date重复，不符合串行单仓口径")
    frame = frame.sort_values(["signal_date", "exit_date", "ts_code"]).reset_index(drop=True)

    calendar = sorted(
        {
            normalize_date(value)
            for value in daily["signal_date"].tolist()
            if normalize_date(value)
        }
    )
    if not calendar:
        raise ValueError("组合逐日表没有有效交易日")
    missing_dates = sorted(
        (set(frame["signal_date"]) | set(frame["exit_date"])) - set(calendar)
    )
    if missing_dates:
        raise ValueError("交易日期不在逐日交易日历：" + "、".join(missing_dates[:10]))
    if frame["exit_date"].lt(frame["signal_date"]).any():
        raise ValueError("组合交易退出日早于信号日")
    return frame, calendar


def _valid_spec(spec: RiskOverlaySpec) -> None:
    if spec.daily_loss_pct is not None and not 0 < spec.daily_loss_pct < 1:
        raise ValueError("daily_loss_pct必须为空或在(0,1)内")
    if spec.drawdown_pct is not None and not 0 < spec.drawdown_pct < 1:
        raise ValueError("drawdown_pct必须为空或在(0,1)内")
    if spec.consecutive_losses is not None and spec.consecutive_losses < 1:
        raise ValueError("consecutive_losses必须为空或为正整数")
    if spec.cooldown_trade_days < 1:
        raise ValueError("cooldown_trade_days必须为正整数")


def replay_risk_overlay(
    trades: pd.DataFrame,
    calendar: list[str],
    spec: RiskOverlaySpec,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """只用已退出交易结果决定后续信号是否暂停，不读取未来收益。"""

    _valid_spec(spec)
    index = {date: position for position, date in enumerate(calendar)}
    pending: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    triggers: list[dict[str, Any]] = []
    gate_equity = 1.0
    gate_peak = 1.0
    gate_streak = 0
    blocked_through = -1
    reset_at: int | None = None

    def settle(signal_date: str) -> None:
        nonlocal gate_equity, gate_peak, gate_streak, blocked_through, reset_at
        for trade in list(pending):
            if trade["exit_date"] > signal_date:
                continue
            pending.remove(trade)
            trade_return = float(trade["account_return"])
            gate_equity *= 1.0 + trade_return
            gate_peak = max(gate_peak, gate_equity)
            gate_streak = gate_streak + 1 if trade_return < 0 else 0
            gate_drawdown = gate_equity / gate_peak - 1.0
            reasons: list[str] = []
            if spec.daily_loss_pct is not None and trade_return <= -spec.daily_loss_pct:
                reasons.append("DAILY_REALIZED_LOSS")
            if spec.drawdown_pct is not None and gate_drawdown <= -spec.drawdown_pct:
                reasons.append("ACCOUNT_DRAWDOWN")
            if (
                spec.consecutive_losses is not None
                and gate_streak >= spec.consecutive_losses
            ):
                reasons.append("CONSECUTIVE_LOSSES")
            if not reasons:
                continue
            long_pause = any(
                reason in {"ACCOUNT_DRAWDOWN", "CONSECUTIVE_LOSSES"}
                for reason in reasons
            )
            cooldown = spec.cooldown_trade_days if long_pause else 1
            exit_index = index[trade["exit_date"]]
            blocked_through = max(blocked_through, exit_index + cooldown - 1)
            if long_pause:
                reset_at = blocked_through + 1
            triggers.append(
                {
                    "candidate_id": spec.candidate_id,
                    "trigger_exit_date": trade["exit_date"],
                    "trigger_signal_date": trade["signal_date"],
                    "trigger_leg": trade["strategy_leg"],
                    "trigger_code": trade["ts_code"],
                    "trigger_return": trade_return,
                    "gate_drawdown": gate_drawdown,
                    "gate_consecutive_losses": gate_streak,
                    "trigger_reasons": ",".join(reasons),
                    "cooldown_trade_days": cooldown,
                    "blocked_through_date": calendar[
                        min(blocked_through, len(calendar) - 1)
                    ],
                }
            )

    for trade in trades.to_dict("records"):
        signal_date = str(trade["signal_date"])
        signal_index = index[signal_date]
        settle(signal_date)
        if reset_at is not None and signal_index >= reset_at:
            gate_peak = gate_equity
            gate_streak = 0
            reset_at = None
        if signal_index <= blocked_through:
            decisions.append(
                {
                    **trade,
                    "risk_decision": "SKIP_RISK_COOLDOWN",
                    "blocked_through_date": calendar[
                        min(blocked_through, len(calendar) - 1)
                    ],
                }
            )
            continue
        decisions.append(
            {**trade, "risk_decision": "EXECUTED", "blocked_through_date": ""}
        )
        selected.append(trade)
        pending.append(trade)
    settle("99999999")
    return pd.DataFrame(selected), pd.DataFrame(decisions), pd.DataFrame(triggers)


def maximum_consecutive_losses(returns: pd.Series) -> int:
    current = maximum = 0
    for value in pd.to_numeric(returns, errors="coerce").dropna():
        current = current + 1 if float(value) < 0 else 0
        maximum = max(maximum, current)
    return maximum


def performance_metrics(trades: pd.DataFrame) -> dict[str, Any]:
    returns = pd.to_numeric(
        trades.get("account_return", pd.Series(dtype=float)), errors="coerce"
    ).dropna()
    if returns.empty:
        return {
            "sample_count": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "equity_multiple": 1.0,
            "total_compound_return": 0.0,
            "max_drawdown": 0.0,
            "profit_loss_ratio": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "max_consecutive_losses": 0,
        }
    curve = (1.0 + returns).cumprod()
    peak = curve.cummax().clip(lower=1.0)
    gains = returns[returns > 0]
    losses = returns[returns < 0]
    multiple = float(curve.iloc[-1])
    return {
        "sample_count": int(len(returns)),
        "win_rate": float(returns.gt(0).mean()),
        "avg_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "equity_multiple": multiple,
        "total_compound_return": multiple - 1.0,
        "max_drawdown": float((curve / peak - 1.0).min()),
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean()))
        if len(gains) and len(losses)
        else 0.0,
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "max_consecutive_losses": maximum_consecutive_losses(returns),
    }


def segment_masks(trades: pd.DataFrame, split_date: str) -> dict[str, pd.Series]:
    dates = trades["signal_date"].astype(str)
    return {
        "全部": pd.Series(True, index=trades.index),
        f"训练半段<{split_date}": dates.lt(split_date),
        f"检验半段>={split_date}": dates.ge(split_date),
        "自然年2024": dates.str[:4].eq("2024"),
        "自然年2025": dates.str[:4].eq("2025"),
        "自然年2026": dates.str[:4].eq("2026"),
    }
