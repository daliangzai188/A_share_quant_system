from __future__ import annotations

import pandas as pd

from src.historical_limit_up_profile import (
    attach_point_in_time_profiles,
    build_event_outcomes,
    prepare_event_schedule,
    rule_mask,
    wilson_interval,
)


def test_profile_does_not_use_unfinished_or_current_event() -> None:
    outcomes = pd.DataFrame(
        [
            {
                "event_date": "20260102",
                "outcome_available_date": "20260107",
                "ts_code": "000001.SZ",
                "d2_return": -0.02,
                "d3_return": -0.03,
                "d23_return": -0.0494,
                "fade": 1.0,
                "both_down": 1.0,
                "entry_to_t2": -0.01,
                "entry_to_t3": -0.04,
            },
            {
                "event_date": "20260108",
                "outcome_available_date": "20260113",
                "ts_code": "000001.SZ",
                "d2_return": 0.02,
                "d3_return": 0.03,
                "d23_return": 0.0506,
                "fade": 0.0,
                "both_down": 0.0,
                "entry_to_t2": 0.04,
                "entry_to_t3": 0.07,
            },
        ]
    )
    signals = pd.DataFrame(
        [
            {"trade_date": "20260106", "ts_code": "000001.SZ"},
            {"trade_date": "20260107", "ts_code": "000001.SZ"},
            {"trade_date": "20260108", "ts_code": "000001.SZ"},
            {"trade_date": "20260113", "ts_code": "000001.SZ"},
        ]
    )

    profiled = attach_point_in_time_profiles(signals, outcomes)

    assert profiled["hist_limit_event_count"].tolist() == [0, 1, 1, 2]
    assert profiled.loc[2, "hist_last_event_date"] == "20260102"
    assert profiled.loc[3, "hist_last_event_date"] == "20260108"


def test_event_returns_link_pre_close_for_corporate_action() -> None:
    schedule = pd.DataFrame(
        [
            {
                "event_date": "20260102",
                "ts_code": "000001.SZ",
                "d1_date": "20260105",
                "d2_date": "20260106",
                "d3_date": "20260107",
                "outcome_available_date": "20260107",
            }
        ]
    )
    quotes = {
        ("20260105", "000001.SZ"): (10.0, 11.0, 10.0),
        # 除权后pre_close变为5.5；close=5.5代表该日收益为0，而不是腰斩。
        ("20260106", "000001.SZ"): (5.5, 5.5, 5.5),
        ("20260107", "000001.SZ"): (5.5, 5.225, 5.5),
    }

    outcomes, audit = build_event_outcomes(schedule, quotes)

    assert audit["valid_outcome_count"] == 1
    assert audit["missing_quote_by_stage"] == {"d1": 0, "d2": 0, "d3": 0}
    assert outcomes.loc[0, "d2_return"] == 0.0
    assert abs(outcomes.loc[0, "d3_return"] + 0.05) < 1e-12
    assert abs(outcomes.loc[0, "entry_to_t3"] - 0.045) < 1e-12


def test_prepare_schedule_and_rule_are_fail_closed() -> None:
    events = pd.DataFrame(
        [
            {"trade_date": "20260102", "ts_code": "000001.SZ"},
            {"trade_date": "20260102", "ts_code": "000001.SZ"},
        ]
    )
    schedule, requests, audit = prepare_event_schedule(
        events,
        ["20260102", "20260105", "20260106", "20260107"],
        start_date="20260101",
        cutoff="20260107",
    )
    assert len(schedule) == 1
    assert audit["source_duplicate_key_count"] == 1
    assert requests["20260107"] == {"000001.SZ"}

    frame = pd.DataFrame(
        {
            "hist_limit_event_count": [3, 2, 3],
            "hist_recent3_fade_rate": [1.0, 1.0, None],
        }
    )
    mask = rule_mask(
        frame,
        {
            "minimum_history_count": 3,
            "metric": "hist_recent3_fade_rate",
            "operator": ">=",
            "threshold": 1.0,
        },
    )
    assert mask.tolist() == [True, False, False]


def test_wilson_interval_contains_observed_ratio() -> None:
    low, high = wilson_interval(46, 174)
    assert low < 46 / 174 < high
    assert 0.0 <= low < high <= 1.0
