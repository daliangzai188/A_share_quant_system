"""固定账户风险候选的随机连续交易窗口复核。"""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

from src.account_risk_historical import (
    RiskOverlaySpec,
    performance_metrics,
    replay_risk_overlay,
)


def random_contiguous_window_results(
    trades: pd.DataFrame,
    calendar: list[str],
    spec: RiskOverlaySpec,
    *,
    window_trade_counts: Iterable[int],
    samples_per_window: int,
    seed: int,
    retained_floor: float,
) -> pd.DataFrame:
    """固定随机种子抽取连续交易窗口；不做参数选择。"""

    if samples_per_window < 1:
        raise ValueError("samples_per_window必须为正整数")
    if not 0 < retained_floor <= 1:
        raise ValueError("retained_floor必须在(0,1]内")
    sizes = [int(value) for value in window_trade_counts]
    if not sizes or any(size < 2 or size > len(trades) for size in sizes):
        raise ValueError("窗口交易笔数必须在[2,总交易数]内")
    rng = np.random.default_rng(int(seed))
    rows: list[dict] = []
    for size in sizes:
        starts = rng.integers(0, len(trades) - size + 1, size=samples_per_window)
        for sample_index, start_raw in enumerate(starts):
            start = int(start_raw)
            window = trades.iloc[start : start + size].copy()
            selected, decisions, triggers = replay_risk_overlay(window, calendar, spec)
            baseline = performance_metrics(window)
            candidate = performance_metrics(selected)
            retained = candidate["equity_multiple"] / max(
                baseline["equity_multiple"], 1e-12
            )
            dd_improvement = candidate["max_drawdown"] - baseline["max_drawdown"]
            rows.append(
                {
                    "candidate_id": spec.candidate_id,
                    "window_trade_count": size,
                    "sample_index": sample_index,
                    "start_trade_index": start,
                    "start_signal_date": str(window.iloc[0]["signal_date"]),
                    "end_signal_date": str(window.iloc[-1]["signal_date"]),
                    "baseline_sample_count": baseline["sample_count"],
                    "candidate_sample_count": candidate["sample_count"],
                    "trigger_count": int(len(triggers)),
                    "skipped_trade_count": int(
                        decisions["risk_decision"].eq("SKIP_RISK_COOLDOWN").sum()
                    ),
                    "baseline_equity_multiple": baseline["equity_multiple"],
                    "candidate_equity_multiple": candidate["equity_multiple"],
                    "retained_ratio": retained,
                    "baseline_max_drawdown": baseline["max_drawdown"],
                    "candidate_max_drawdown": candidate["max_drawdown"],
                    "drawdown_improvement": dd_improvement,
                    "retained_floor_passed": retained >= retained_floor,
                    "drawdown_noninferior": dd_improvement >= -1e-12,
                    "candidate_positive": candidate["equity_multiple"] > 1.0,
                }
            )
    return pd.DataFrame(rows)


def summarize_random_windows(results: pd.DataFrame) -> pd.DataFrame:
    required = {
        "window_trade_count",
        "retained_ratio",
        "retained_floor_passed",
        "drawdown_noninferior",
        "candidate_positive",
        "candidate_sample_count",
        "skipped_trade_count",
    }
    missing = sorted(required - set(results.columns))
    if missing:
        raise ValueError("随机窗口结果缺少字段：" + "、".join(missing))
    rows = []
    for size, group in results.groupby("window_trade_count", sort=True):
        rows.append(
            {
                "window_trade_count": int(size),
                "sample_count": int(len(group)),
                "retained_floor_pass_rate": float(
                    group["retained_floor_passed"].mean()
                ),
                "p10_retained_ratio": float(group["retained_ratio"].quantile(0.10)),
                "median_retained_ratio": float(group["retained_ratio"].median()),
                "minimum_retained_ratio": float(group["retained_ratio"].min()),
                "drawdown_noninferior_rate": float(
                    group["drawdown_noninferior"].mean()
                ),
                "candidate_positive_rate": float(group["candidate_positive"].mean()),
                "average_candidate_trade_count": float(
                    group["candidate_sample_count"].mean()
                ),
                "average_skipped_trade_count": float(
                    group["skipped_trade_count"].mean()
                ),
            }
        )
    return pd.DataFrame(rows)
