"""冻结发布版本的随机连续历史区间伪实盘压力测试。

本模块只消费已经由当前组合认证逻辑生成的逐日明细。它不会重新搜索参数，也
不会改变实盘门禁。随机窗口保持原始时间顺序和持仓占用路径，并且只统计窗口
结束日前已经退出的交易，防止用到区间右边界之后的收益。
"""
from __future__ import annotations

import random
from typing import Any, Mapping, Sequence

import pandas as pd


REQUIRED_DETAIL_COLUMNS = {
    "signal_date",
    "status",
    "strategy_leg",
    "ts_code",
    "exit_date",
    "account_return",
}


def _date(value: Any) -> str:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _maximum_consecutive_losses(values: pd.Series) -> int:
    current = maximum = 0
    for value in values:
        if float(value) < 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def validate_replay_detail(detail: pd.DataFrame) -> pd.DataFrame:
    """校验并规范认证逐日明细，拒绝日期重复、乱序和非法收益。"""

    missing = sorted(REQUIRED_DETAIL_COLUMNS - set(detail.columns))
    if missing:
        raise ValueError("随机回放缺少逐日字段：" + "、".join(missing))
    normalized = detail.copy()
    normalized["signal_date"] = normalized["signal_date"].map(_date)
    normalized["exit_date"] = normalized["exit_date"].map(_date)
    if normalized.empty:
        raise ValueError("随机回放逐日明细为空")
    if normalized["signal_date"].eq("").any():
        raise ValueError("随机回放存在无效信号日")
    if normalized["signal_date"].duplicated().any():
        raise ValueError("随机回放信号日重复")
    if normalized["signal_date"].tolist() != sorted(normalized["signal_date"].tolist()):
        raise ValueError("随机回放信号日未按时间升序排列")
    normalized["account_return"] = pd.to_numeric(
        normalized["account_return"], errors="raise"
    )
    executed = normalized["status"].astype(str).eq("EXECUTED")
    if normalized.loc[executed, "exit_date"].eq("").any():
        raise ValueError("随机回放已执行交易缺少退出日")
    if normalized.loc[executed, "account_return"].le(-1.0).any():
        raise ValueError("随机回放存在小于等于-100%的账户收益")
    return normalized.reset_index(drop=True)


def build_market_context(features: pd.DataFrame) -> dict[str, float]:
    """从分市场情绪表提取逐日全市场涨停数，并校验同日全局口径一致。"""

    required = {"trade_date", "market_limit_up_count"}
    missing = sorted(required - set(features.columns))
    if missing:
        raise ValueError("市场情绪数据缺少字段：" + "、".join(missing))
    frame = features.copy()
    frame["trade_date"] = frame["trade_date"].map(_date)
    frame["market_limit_up_count"] = pd.to_numeric(
        frame["market_limit_up_count"], errors="coerce"
    )
    frame = frame[
        frame["trade_date"].ne("") & frame["market_limit_up_count"].notna()
    ]
    inconsistent = frame.groupby("trade_date")["market_limit_up_count"].nunique()
    if bool(inconsistent.gt(1).any()):
        bad = inconsistent[inconsistent.gt(1)].index[0]
        raise ValueError(f"市场情绪同日全市场涨停数不一致：{bad}")
    return (
        frame.drop_duplicates("trade_date", keep="last")
        .set_index("trade_date")["market_limit_up_count"]
        .astype(float)
        .to_dict()
    )


def market_regime(
    median_limit_up_count: float,
    breakpoints: Sequence[float],
    labels: Sequence[str],
) -> str:
    """按窗口内全市场涨停数中位数划分行情环境。"""

    points = [float(value) for value in breakpoints]
    if points != sorted(points) or len(labels) != len(points) + 1:
        raise ValueError("行情分层必须是升序断点，且标签数=断点数+1")
    for index, point in enumerate(points):
        if median_limit_up_count < point:
            return str(labels[index])
    return str(labels[-1])


def _sample_start_indices(
    dates: Sequence[str],
    window_length: int,
    sample_count: int,
    rng: random.Random,
    sampling_mode: str,
) -> tuple[list[int], int]:
    possible = list(range(0, len(dates) - window_length + 1))
    target = min(max(int(sample_count), 0), len(possible))
    if target == 0:
        return [], len(possible)
    if sampling_mode == "uniform":
        return sorted(rng.sample(possible, target)), len(possible)
    if sampling_mode != "balanced_start_year":
        raise ValueError(f"不支持的随机窗口抽样方式：{sampling_mode}")

    by_year: dict[str, list[int]] = {}
    for index in possible:
        by_year.setdefault(str(dates[index])[:4], []).append(index)
    years = sorted(by_year)
    base, remainder = divmod(target, len(years))
    selected: list[int] = []
    for position, year in enumerate(years):
        quota = base + (1 if position < remainder else 0)
        group = by_year[year]
        selected.extend(rng.sample(group, min(quota, len(group))))
    if len(selected) < target:
        selected_set = set(selected)
        remainder_pool = [index for index in possible if index not in selected_set]
        selected.extend(rng.sample(remainder_pool, target - len(selected)))
    return sorted(selected), len(possible)


def _window_metrics(
    detail: pd.DataFrame,
    *,
    start_index: int,
    window_length: int,
    window_id: str,
    possible_window_count: int,
    market_context: Mapping[str, float],
    regime_breakpoints: Sequence[float],
    regime_labels: Sequence[str],
) -> tuple[dict[str, Any], pd.DataFrame]:
    window = detail.iloc[start_index : start_index + window_length].copy()
    start_date = str(window.iloc[0]["signal_date"])
    end_date = str(window.iloc[-1]["signal_date"])
    executed = window[window["status"].astype(str).eq("EXECUTED")].copy()
    completed = executed[executed["exit_date"].astype(str).le(end_date)].copy()
    right_boundary = executed[executed["exit_date"].astype(str).gt(end_date)].copy()
    returns = pd.to_numeric(completed["account_return"], errors="raise")
    curve = (1.0 + returns).cumprod()
    gains = returns[returns > 0]
    losses = returns[returns < 0]
    multiple = float(curve.iloc[-1]) if len(curve) else 1.0
    drawdown = (
        float((curve / curve.cummax().clip(lower=1.0) - 1.0).min())
        if len(curve)
        else 0.0
    )
    limit_counts = pd.Series(
        [market_context.get(str(value), float("nan")) for value in window["signal_date"]],
        dtype=float,
    ).dropna()
    coverage = float(len(limit_counts) / len(window)) if len(window) else 0.0
    median_limit_count = float(limit_counts.median()) if len(limit_counts) else float("nan")
    regime = (
        market_regime(median_limit_count, regime_breakpoints, regime_labels)
        if len(limit_counts)
        else "unknown"
    )
    initial_occupied_days = 0
    for status in window["status"].astype(str):
        if status != "SKIP_OCCUPIED":
            break
        initial_occupied_days += 1

    metrics = {
        "window_id": window_id,
        "window_length": int(window_length),
        "possible_window_count": int(possible_window_count),
        "start_index": int(start_index),
        "start_date": start_date,
        "end_date": end_date,
        "start_year": start_date[:4],
        "market_regime": regime,
        "market_data_coverage": coverage,
        "median_market_limit_up_count": median_limit_count,
        "initial_occupied_days": int(initial_occupied_days),
        "executed_signal_count": int(len(executed)),
        "complete_trade_count": int(len(completed)),
        "right_boundary_open_trade_count": int(len(right_boundary)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_return": float(returns.mean()) if len(returns) else 0.0,
        "median_return": float(returns.median()) if len(returns) else 0.0,
        "compound_multiple": multiple,
        "fixed_initial_notional_multiple": float(1.0 + returns.sum()),
        "max_drawdown": drawdown,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": (
            float(gains.mean() / abs(losses.mean()))
            if len(gains) and len(losses)
            else 0.0
        ),
        "max_consecutive_losses": _maximum_consecutive_losses(returns),
        "positive_window": bool(multiple > 1.0),
    }
    completed.insert(0, "window_id", window_id)
    completed.insert(1, "window_start_date", start_date)
    completed.insert(2, "window_end_date", end_date)
    return metrics, completed


def _quantile(series: pd.Series, value: float) -> float:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    return float(numeric.quantile(value)) if len(numeric) else 0.0


def summarize_windows(windows: pd.DataFrame) -> pd.DataFrame:
    """按窗口长度汇总路径分布；P10回撤代表较差尾部。"""

    rows: list[dict[str, Any]] = []
    for length, group in windows.groupby("window_length", sort=True):
        rows.append(
            {
                "window_length": int(length),
                "sampled_window_count": int(len(group)),
                "possible_window_count": int(group["possible_window_count"].max()),
                "positive_window_rate": float(group["positive_window"].mean()),
                "trade_count_p10": _quantile(group["complete_trade_count"], 0.10),
                "trade_count_p50": _quantile(group["complete_trade_count"], 0.50),
                "trade_count_p90": _quantile(group["complete_trade_count"], 0.90),
                "compound_multiple_p10": _quantile(group["compound_multiple"], 0.10),
                "compound_multiple_p50": _quantile(group["compound_multiple"], 0.50),
                "compound_multiple_p90": _quantile(group["compound_multiple"], 0.90),
                "fixed_notional_multiple_p10": _quantile(
                    group["fixed_initial_notional_multiple"], 0.10
                ),
                "fixed_notional_multiple_p50": _quantile(
                    group["fixed_initial_notional_multiple"], 0.50
                ),
                "max_drawdown_p10": _quantile(group["max_drawdown"], 0.10),
                "max_drawdown_p50": _quantile(group["max_drawdown"], 0.50),
                "max_consecutive_losses_p90": _quantile(
                    group["max_consecutive_losses"], 0.90
                ),
                "market_data_coverage": float(group["market_data_coverage"].mean()),
            }
        )
    return pd.DataFrame(rows)


def summarize_regimes(windows: pd.DataFrame) -> pd.DataFrame:
    """按窗口长度和行情环境汇总，不把重叠窗口伪装成独立样本。"""

    rows: list[dict[str, Any]] = []
    for (length, regime), group in windows.groupby(
        ["window_length", "market_regime"], sort=True
    ):
        rows.append(
            {
                "window_length": int(length),
                "market_regime": str(regime),
                "window_count": int(len(group)),
                "positive_window_rate": float(group["positive_window"].mean()),
                "compound_multiple_p10": _quantile(group["compound_multiple"], 0.10),
                "compound_multiple_p50": _quantile(group["compound_multiple"], 0.50),
                "max_drawdown_p10": _quantile(group["max_drawdown"], 0.10),
                "max_drawdown_p50": _quantile(group["max_drawdown"], 0.50),
                "complete_trade_count_p50": _quantile(
                    group["complete_trade_count"], 0.50
                ),
            }
        )
    return pd.DataFrame(rows)


def summarize_legs(detail: pd.DataFrame, sampled_trades: pd.DataFrame) -> pd.DataFrame:
    """同时给出全历史唯一交易与随机窗口重复出现次数，避免重复样本误导。"""

    unique = detail[detail["status"].astype(str).eq("EXECUTED")].copy()
    legs = sorted(set(unique["strategy_leg"].astype(str)) - {""})
    rows: list[dict[str, Any]] = []
    for leg in legs:
        trades = unique[unique["strategy_leg"].astype(str).eq(leg)]
        returns = pd.to_numeric(trades["account_return"], errors="raise")
        occurrences = (
            sampled_trades[
                sampled_trades["strategy_leg"].astype(str).eq(leg)
            ]
            if not sampled_trades.empty
            else sampled_trades
        )
        rows.append(
            {
                "strategy_leg": leg,
                "unique_trade_count": int(len(trades)),
                "unique_win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "unique_avg_return": float(returns.mean()) if len(returns) else 0.0,
                "unique_median_return": float(returns.median()) if len(returns) else 0.0,
                "unique_compound_multiple": (
                    float((1.0 + returns).prod()) if len(returns) else 1.0
                ),
                "sampled_occurrence_count": int(len(occurrences)),
                "sampled_window_count": (
                    int(occurrences["window_id"].nunique())
                    if not occurrences.empty
                    else 0
                ),
            }
        )
    return pd.DataFrame(rows)


def run_random_windows(
    detail: pd.DataFrame,
    *,
    window_lengths: Sequence[int],
    samples_per_length: int,
    random_seed: int,
    sampling_mode: str,
    market_context: Mapping[str, float],
    regime_breakpoints: Sequence[float],
    regime_labels: Sequence[str],
) -> dict[str, pd.DataFrame]:
    """运行可复现的分层随机连续窗口回放。"""

    normalized = validate_replay_detail(detail)
    dates = normalized["signal_date"].astype(str).tolist()
    rng = random.Random(int(random_seed))
    window_rows: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    for length_value in window_lengths:
        length = int(length_value)
        if length <= 0:
            raise ValueError("随机窗口长度必须大于0")
        if length > len(normalized):
            raise ValueError(f"随机窗口{length}日超过历史长度{len(normalized)}日")
        starts, possible_count = _sample_start_indices(
            dates, length, samples_per_length, rng, sampling_mode
        )
        for ordinal, start_index in enumerate(starts, start=1):
            window_id = f"W{length:03d}-{ordinal:04d}"
            metrics, trades = _window_metrics(
                normalized,
                start_index=start_index,
                window_length=length,
                window_id=window_id,
                possible_window_count=possible_count,
                market_context=market_context,
                regime_breakpoints=regime_breakpoints,
                regime_labels=regime_labels,
            )
            window_rows.append(metrics)
            if not trades.empty:
                trade_frames.append(trades)
    windows = pd.DataFrame(window_rows)
    sampled_trades = (
        pd.concat(trade_frames, ignore_index=True)
        if trade_frames
        else pd.DataFrame(columns=["window_id"] + list(normalized.columns))
    )
    return {
        "windows": windows,
        "trades": sampled_trades,
        "summary": summarize_windows(windows),
        "regimes": summarize_regimes(windows),
        "legs": summarize_legs(normalized, sampled_trades),
    }
