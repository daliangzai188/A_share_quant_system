from __future__ import annotations

import pandas as pd

from scripts.research_strategy_d_intraday_1m_paths import (
    bool_series,
    candidate_rule_diagnostics,
    event_metrics,
)


def sample_signals() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": ["20240926", "20240926", "20260101", "20260102"],
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"],
            "account_return": [0.10, -0.05, 0.02, -0.01],
            "first_seal_hhmm": [1300, 1410, 1200, 1350],
            "eligible_signal_hhmm": [1410, 1440, 1420, 1450],
            "open_times_at_signal": [3, 2, 3, 2],
        }
    )


def test_event_metrics_keep_event_and_equal_day_denominators_separate() -> None:
    metrics = event_metrics(sample_signals(), seed=7)

    assert metrics["sample_count"] == 4
    assert metrics["trading_day_count"] == 3
    assert metrics["win_rate"] == 0.5
    assert metrics["explosion_count_gte_10pct"] == 1
    assert metrics["big_loss_count_lte_minus_5pct"] == 1
    assert abs(metrics["avg_account_return"] - 0.015) < 1e-12
    assert abs(metrics["equal_day_mean_return"] - (0.035 / 3)) < 1e-12


def test_candidate_rules_use_only_signal_time_columns() -> None:
    result = candidate_rule_diagnostics(sample_signals())
    row = result[
        result["rule"].eq("open_times_3_and_signal_before_1445")
        & result["scope"].eq("full_24m")
    ].iloc[0]

    assert row["sample_count"] == 2
    assert bool(row["uses_only_signal_time_known_fields"]) is True
    assert bool(row["formal_d_compound_certifiable"]) is False
    assert bool(row["acde_replacement_certifiable"]) is False


def test_bool_series_accepts_csv_boolean_text() -> None:
    result = bool_series(pd.Series([True, False, "true", "1", "no", None]))
    assert result.tolist() == [True, False, True, True, False, False]
