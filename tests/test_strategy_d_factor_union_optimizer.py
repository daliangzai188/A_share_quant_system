from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.monitor_strategy_d_intraday import StockState, StrategyDMonitor
from scripts.optimize_strategy_d_factor_union import minimize_profile_union
from src.strategy_d_factor_rules import (
    FACTOR_SCHEMA_ID,
    FACTOR_UNION_MODE,
    add_factor_values,
    factor_values_from_raw,
    load_factor_release,
    matching_profile_ids,
)


class DummyLogger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def raw_factor_row(**overrides):
    row = {
        "signal_hhmm": 945,
        "first_seal_hhmm": 935,
        "open_times_at_signal": 1,
        "first_to_signal_minutes": 10,
        "last_break_to_signal_minutes": 3,
        "previous_seal_to_break_minutes": 4,
        "last_break_close_depth_pct": 0.004,
        "open_gap_pct": 0.02,
        "pre_signal_min_return": 0.01,
        "signal_cumulative_amount_vs_prev_day": 0.8,
        "market_ever_sealed_count": 50,
        "market_active_sealed_count": 25,
        "market_seal_rate": 0.5,
        "market_break_event_rate": 0.3,
        "market_segment": "sz_main",
        "same_segment_seal_rate": 0.7,
    }
    row.update(overrides)
    return row


def factor_release(profile_id: str = "P1") -> dict:
    return {
        "schema_version": 1,
        "factor_schema_id": FACTOR_SCHEMA_ID,
        "release_id": "D_TEST_FACTOR_UNION",
        "strategy_mode": FACTOR_UNION_MODE,
        "effective_from": "20260701",
        "research_window": {"start": "20240630", "end": "20260630"},
        "profiles": [
            {
                "profile_id": profile_id,
                "priority": 1,
                "conditions": {
                    "reseal_time_bucket": "0930_1000",
                    "open_count_bucket": "1",
                },
            }
        ],
        "selection_policy": "EARLIEST_RESEAL_THEN_OPEN2_THEN_CODE",
    }


def test_factor_values_use_frozen_boundaries_and_profile_or_matching() -> None:
    values = factor_values_from_raw(raw_factor_row())

    assert values["reseal_time_bucket"] == "0930_1000"
    assert values["open_count_bucket"] == "1"
    assert values["reseal_speed_bucket"] == "2_5"
    assert values["market_seal_rate_bucket"] == "40_60PCT"
    assert values["segment_bucket"] == "MAIN_BOARD"
    assert matching_profile_ids(values, factor_release()["profiles"]) == ["P1"]


def test_factor_release_file_is_validated(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text(json.dumps(factor_release()), encoding="utf-8")

    loaded = load_factor_release(path)

    assert loaded["strategy_mode"] == FACTOR_UNION_MODE
    assert loaded["profiles"][0]["conditions"] == {
        "open_count_bucket": "1",
        "reseal_time_bucket": "0930_1000",
    }


def test_redundant_qualified_if_is_removed_without_changing_or_union() -> None:
    raw = pd.DataFrame(
        [
            raw_factor_row(event_id=1, trade_date="20250102", market_segment="sz_main"),
            raw_factor_row(event_id=2, trade_date="20250103", market_segment="sh_main"),
            raw_factor_row(event_id=3, trade_date="20250106", market_segment="chi_next"),
        ]
    )
    events = add_factor_values(raw)
    conditions = {
        "broad": {"reseal_time_bucket": "0930_1000"},
        "redundant": {
            "reseal_time_bucket": "0930_1000",
            "segment_bucket": "MAIN_BOARD",
        },
    }
    qualified = pd.DataFrame(
        [
            {
                "profile_id": "broad", "factor_count": 1, "trade_count": 30,
                "avg_account_return": 0.03,
            },
            {
                "profile_id": "redundant", "factor_count": 2, "trade_count": 25,
                "avg_account_return": 0.04,
            },
        ]
    )

    effective, mask = minimize_profile_union(events, qualified, conditions)

    assert effective["profile_id"].tolist() == ["broad"]
    assert mask.tolist() == [True, True, True]


def test_monitor_factor_union_only_accepts_fresh_reseal(tmp_path: Path) -> None:
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(factor_release()), encoding="utf-8")
    monitor = StrategyDMonitor(
        broker=None,
        live_order=False,
        logger=DummyLogger(),
        signal_csv=tmp_path / "signals.csv",
        config={"strategy_d": {"factor_release_path": str(release_path)}},
    )
    monitor.scan_round = 7
    state = StockState(
        ts_code="000001.SZ",
        name="测试",
        market_segment="sz_main",
        upper_limit=11.0,
        was_sealed=True,
        ever_sealed=True,
        open_times_today=1,
        first_seal_hhmm=935,
        last_seal_hhmm=945,
        last_break_hhmm=942,
        previous_seal_to_break_minutes=4,
        last_break_price=10.95,
        last_reseal_scan_round=7,
        pre_close=10.0,
        open_price=10.2,
        session_low_price=10.1,
        cumulative_amount=8_000_000,
        previous_day_amount_yuan=10_000_000,
    )
    monitor.states = {state.ts_code: state}

    passed, reason = monitor._factor_release_match(
        state, require_fresh_reseal=True
    )
    state.last_reseal_scan_round = 6
    stale, stale_reason = monitor._factor_release_match(
        state, require_fresh_reseal=True
    )

    assert passed is True
    assert "P1" in reason
    assert stale is False
    assert "不是本轮新发生" in stale_reason


def test_factor_union_consumes_earliest_signal_without_later_substitution(
    tmp_path: Path,
) -> None:
    release_path = tmp_path / "release.json"
    release = factor_release()
    release["profiles"][0]["conditions"] = {"reseal_time_bucket": "0930_1000"}
    release_path.write_text(json.dumps(release), encoding="utf-8")
    monitor = StrategyDMonitor(
        broker=None,
        live_order=False,
        logger=DummyLogger(),
        signal_csv=tmp_path / "signals.csv",
        config={"strategy_d": {"factor_release_path": str(release_path)}},
    )
    monitor.scan_round = 3

    def state(code: str, opens: int) -> StockState:
        return StockState(
            ts_code=code,
            name="测试",
            market_segment="sz_main",
            upper_limit=11.0,
            was_sealed=True,
            ever_sealed=True,
            open_times_today=opens,
            first_seal_hhmm=935,
            last_seal_hhmm=945,
            last_break_hhmm=943,
            last_break_price=10.95,
            previous_seal_to_break_minutes=3,
            last_reseal_scan_round=3,
            pre_close=10.0,
            open_price=10.2,
            session_low_price=10.1,
        )

    monitor.states = {
        "000002.SZ": state("000002.SZ", 1),
        "000003.SZ": state("000003.SZ", 2),
    }
    monitor._refresh_fill_gate = lambda _state: (False, "模拟无法成交")

    monitor._check_and_fire_factor_union()
    first_locked = monitor.factor_signal_locked_ts_code
    monitor.states["000002.SZ"].last_reseal_scan_round = 4
    monitor.scan_round = 4
    monitor._check_and_fire_factor_union()

    assert first_locked == "000003.SZ"  # 同一分钟与历史一致，炸板2次优先。
    assert monitor.factor_signal_consumed is True
    assert monitor.factor_signal_locked_ts_code == "000003.SZ"
    assert monitor.order_placed is False
