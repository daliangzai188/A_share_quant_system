from __future__ import annotations

from pathlib import Path
import json
import tempfile

import pandas as pd

from src.acde_rolling_framework import (
    action_metrics,
    build_monthly_research_window,
    build_window_set,
    evaluate_three_window_replacement,
    replay_action_date_cash_portfolio,
    replay_action_date_portfolio,
)
from src.acde_rolling_candidates import (
    apply_a_research_post_pick_gate,
    apply_e_research_style_filter,
    c_variants,
    d_variants,
    e_variants,
)
from src.strategy_e import load_e_spec
from src.utils.config import load_json_config
from scripts.optimize_acde_rolling_three_year import (
    period_breakdown,
    select_main_window_winner,
)
from src.acde_monthly_research import load_monthly_config
from scripts.merge_strategy_d_reseal_event_windows import merge as merge_d_windows


ROOT = Path(__file__).resolve().parents[1]


def candidate(
    leg: str,
    signal_date: str,
    action_date: str,
    exit_date: str,
    value: float | None,
    status: str = "OK",
) -> dict[str, object]:
    row: dict[str, object] = {
        "strategy_leg": leg,
        "signal_date": signal_date,
        "status": status,
        "exit_date": exit_date,
        "account_return": value,
        "ts_code": f"{leg}00001.SZ",
        "name": f"测试{leg}",
    }
    if leg != "D":
        row["buy_date"] = action_date
    return row


def test_three_window_boundaries_for_20260630() -> None:
    windows = build_window_set("20260630")
    assert (windows.main.start, windows.main.end) == ("20230701", "20260630")
    assert (windows.recent.start, windows.recent.end) == ("20240701", "20260630")
    assert (windows.failure_check.start, windows.failure_check.end) == (
        "20260101",
        "20260630",
    )
    assert windows.main.may_rank_candidates is True
    assert windows.recent.may_rank_candidates is False
    assert windows.failure_check.may_rank_candidates is False


def test_monthly_window_uses_36_complete_calendar_months() -> None:
    window = build_monthly_research_window("20260831")
    assert (window.start, window.end) == ("20230901", "20260831")
    try:
        build_monthly_research_window("20260830")
    except ValueError as exc:
        assert "自然月最后一天" in str(exc)
    else:
        raise AssertionError("非月末不得成为月度研究截止日")


def test_monthly_config_removes_half_year_selection_windows() -> None:
    config = load_monthly_config(ROOT / "config/acde_rolling_optimization.json")
    assert config["schedule"]["frequency"] == "monthly"
    assert config["windows"] == {
        "main_months": 36,
        "execution_model_history_start": "20190101",
        "metric_date": "action_date",
        "selection_window": "main_only",
        "older_data_role": "risk_and_data_quality_diagnostics_only",
    }
    assert config["execution"]["initial_cash"] == 500000.0
    assert config["execution"]["minimum_commission"] == 5.0


def test_exact_cash_replay_models_lot_and_minimum_commission() -> None:
    plans = pd.DataFrame(
        [
            {
                "signal_date": "20240101",
                "buy_date": "20240102",
                "status": "OK",
                "strategy_leg": "A",
                "ts_code": "000001.SZ",
                "name": "测试",
                "exit_date": "20240103",
                "position_open_until": "20240103",
                "entry_filled": True,
                "position_opened": True,
                "outcome_observable": True,
                "entry_reference_price": 10.0,
                "entry_price": 10.01,
                "exit_reference_price": 11.0,
                "exit_price": 10.989,
                "stock_return_before_fees": 0.0978021978021978,
                "position_scale": 1.0,
            }
        ]
    )
    detail = replay_action_date_cash_portfolio(
        {"A": plans},
        action_dates=["20240102", "20240103"],
        priority=("A",),
        initial_cash=10_000.0,
        position_pct=0.825,
        max_position_pct=0.85,
    )
    trade = detail.loc[detail["status"].eq("EXECUTED")].iloc[0]
    assert trade["quantity"] == 800
    assert trade["buy_commission"] == 5.0
    assert trade["sell_commission"] == 5.0
    assert trade["stamp_tax"] > 0
    assert detail.iloc[-1]["status"] == "SKIP_OCCUPIED"


def test_half_year_breakdown_rolls_forward_without_fixed_calendar_years() -> None:
    empty = pd.DataFrame()
    current = period_breakdown(empty, start="20230701", end="20260630")
    future = period_breakdown(empty, start="20240101", end="20261231")
    assert list(current) == [
        "2023H2", "2024H1", "2024H2", "2025H1", "2025H2", "2026H1"
    ]
    assert list(future) == [
        "2024H1", "2024H2", "2025H1", "2025H2", "2026H1", "2026H2"
    ]


def d_event_rows(dates: list[str], start_id: int = 0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "event_id": start_id + offset,
                "trade_date": trade_date,
                "ts_code": f"00000{offset + 1}.SZ",
                "signal_hhmm": 1000,
                "open_times_at_signal": 1,
                "queue_price_confirmed": True,
                "execution_status": "OK",
                "exit_date": trade_date,
                "account_return": 0.01,
            }
            for offset, trade_date in enumerate(dates)
        ]
    )


def test_d_window_merge_requires_complete_trading_day_coverage() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        calendar = root / "calendar.csv"
        first = root / "first.csv"
        second = root / "second.csv"
        output = root / "merged.csv"
        pd.DataFrame(
            {
                "cal_date": ["20240102", "20240103", "20240104", "20240105"],
                "is_open": [1, 1, 1, 1],
            }
        ).to_csv(calendar, index=False)
        d_event_rows(["20240102", "20240103"]).to_csv(first, index=False)
        d_event_rows(["20240104", "20240105"], start_id=2).to_csv(
            second, index=False
        )
        payload = merge_d_windows(
            first,
            second,
            output,
            calendar_path=calendar,
            expected_start="20240102",
            expected_end="20240105",
        )
        assert payload["window"]["coverage_passed"] is True
        assert payload["window"]["expected_trade_day_count"] == 4
        assert payload["event_trade_day_count"] == 4


def test_d_window_merge_fails_closed_when_one_trade_day_is_missing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        calendar = root / "calendar.csv"
        first = root / "first.csv"
        second = root / "second.csv"
        output = root / "merged.csv"
        pd.DataFrame(
            {
                "cal_date": ["20240102", "20240103", "20240104", "20240105"],
                "is_open": [1, 1, 1, 1],
            }
        ).to_csv(calendar, index=False)
        d_event_rows(["20240102", "20240103"]).to_csv(first, index=False)
        d_event_rows(["20240105"], start_id=2).to_csv(second, index=False)
        try:
            merge_d_windows(
                first,
                second,
                output,
                calendar_path=calendar,
                expected_start="20240102",
                expected_end="20240105",
            )
        except RuntimeError as exc:
            assert "missing=['20240104']" in str(exc)
        else:
            raise AssertionError("D窗口缺交易日时必须fail-closed")


def test_c_candidates_without_explicit_strong_regime_are_diagnostic_only() -> None:
    config = load_json_config(ROOT / "config/strategy_config.json")
    _baseline, variants = c_variants(config)
    by_id = {item.variant_id: item for item in variants}
    assert by_id["C_STRONG_REGIME_ONLY"].style_gate_passed is True
    assert by_id["C_SEGMENT_STRONG_REGIME_ONLY"].style_gate_passed is True
    assert by_id["C_SEGMENT_HEIGHT_GE4"].style_gate_passed is True
    assert by_id["C_SEGMENT_NON_ICE_POINT"].style_gate_passed is True
    assert by_id["C_MARKET_LIMIT_DOWN_LT30"].style_gate_passed is True
    assert by_id["C_SEGMENT_LIMIT_DOWN_LT15"].style_gate_passed is True
    assert by_id["C_RISK_EXCLUDE_SINGLE_OPEN"].style_gate_passed is True
    assert by_id["C_LEADER_RANK_2_3_LIMIT_DOWN_LT30"].style_gate_passed is True
    assert by_id["C_LEADER_RANK_2_3_SEGMENT_NON_ICE"].style_gate_passed is True
    assert all(
        not item.style_gate_passed
        for item in variants
        if item.variant_id not in {
            "C_STRONG_REGIME_ONLY",
            "C_SEGMENT_STRONG_REGIME_ONLY",
            "C_SEGMENT_HEIGHT_GE4",
            "C_SEGMENT_NON_ICE_POINT",
            "C_MARKET_LIMIT_DOWN_LT30",
            "C_SEGMENT_LIMIT_DOWN_LT15",
            "C_RISK_EXCLUDE_SINGLE_OPEN",
            "C_LEADER_RANK_2_3_LIMIT_DOWN_LT30",
            "C_LEADER_RANK_2_3_SEGMENT_NON_ICE",
        }
    )
    assert "warming/main_rise/climax" in by_id[
        "C_LEADER_RANK_ADJACENT"
    ].style_gate_reason


def test_e_and_d_candidates_require_explicit_style_environment() -> None:
    e_spec = load_e_spec(ROOT)
    _e_baseline, e_candidates = e_variants(e_spec)
    e_by_id = {item.variant_id: item for item in e_candidates}
    assert e_by_id["E_ANY_REPAIR_STATE"].style_gate_passed is True
    assert e_by_id["E_MARKET_REPAIR_STATES"].style_gate_passed is True
    assert e_by_id["E_SEGMENT_ICE_POINT_ONLY"].style_gate_passed is True
    assert e_by_id["E_RISK_EXCLUDE_AMOUNT_RATIO_LT08"].style_gate_passed is True
    assert e_by_id["E_RISK_EXCLUDE_LIMIT_UP_GE120"].style_gate_passed is True
    assert e_by_id["E_RISK_EXCLUDE_LEADER_RANK_11_30"].style_gate_passed is True
    assert e_by_id["E_RISK_LEADER_11_30_OR_LIMIT_UP_LT30"].changed_axis_count == 2
    assert e_by_id[
        "E_RISK_LEADER_11_30_OR_LIMIT_UP_OUTSIDE_30_120"
    ].style_gate_passed is True
    assert e_by_id[
        "E_RISK_LEADER_11_30_OR_LIMIT_UP_LT30_OR_120_180"
    ].changed_axis_count == 2
    assert e_by_id[
        "E_RISK_LEADER_11_30_OR_LIMIT_UP_120_180"
    ].style_gate_passed is True
    assert e_by_id["E_RISK_AMOUNT_LT08_OR_LIMIT_UP_GE120"].changed_axis_count == 2
    assert e_by_id["E_RANK_TURNOVER"].style_gate_passed is False

    d_release = json.loads(
        (ROOT / "config/strategy_d_factor_release.json").read_text(encoding="utf-8")
    )
    _d_baseline, d_candidates = d_variants(d_release)
    d_by_id = {item.variant_id: item for item in d_candidates}
    assert d_by_id["D_STRONG_BREAK_LT75"].style_gate_passed is True
    assert d_by_id["D_STRONG_BREAK_LT50"].style_gate_passed is True
    assert d_by_id["D_STRONG_BREAK_LT75_ACTIVE_GE20"].changed_axis_count == 2
    assert d_by_id["D_STRONG_BREAK_LT75_TOUCH_GE40"].changed_axis_count == 2
    assert {
        profile["conditions"]["market_break_rate_bucket"]
        for profile in d_by_id["D_STRONG_BREAK_LT75"].payload
    } == {"LT25PCT", "25_50PCT", "50_75PCT"}
    assert d_by_id["D_STRONG_ACTIVE_GE20"].style_gate_passed is True
    assert d_by_id["D_STRONG_TOUCH_GE40"].style_gate_passed is True
    assert d_by_id["D_QUALITY_TOUCH_LT40"].style_gate_passed is True
    assert d_by_id["D_QUALITY_BREAK_25_75_TOUCH_LT40"].changed_axis_count == 2
    assert d_by_id["D_TIME_ADJACENT"].style_gate_passed is False


def test_e_any_repair_style_filter_is_or_not_and() -> None:
    pool = pd.DataFrame(
        [
            {"id": 1, "segment_emotion_state": "ice_point", "market_emotion_state": "mixed"},
            {"id": 2, "segment_emotion_state": "mixed", "market_emotion_state": "retreat"},
            {"id": 3, "segment_emotion_state": "mixed", "market_emotion_state": "warming"},
            {"id": 4, "segment_emotion_state": "mixed", "market_emotion_state": "main_rise"},
        ]
    )
    spec = {
        "rolling_research_style_filter_any": [
            {"column": "segment_emotion_state", "values": ["ice_point"]},
            {
                "column": "market_emotion_state",
                "values": ["ice_point", "retreat", "warming"],
            },
        ]
    }
    result = apply_e_research_style_filter(pool, spec)
    assert result["id"].tolist() == [1, 2, 3]


def test_a_post_pick_risk_gate_skips_without_second_candidate_fallback() -> None:
    picks = pd.DataFrame(
        [
            {"trade_date": "20260102", "id": 1, "turnover_rate_bucket": "3_6"},
            {"trade_date": "20260105", "id": 2, "turnover_rate_bucket": "6_10"},
        ]
    )
    config = {
        "rolling_research_post_pick_exclude": {
            "column": "turnover_rate_bucket",
            "values": ["3_6"],
            "fallback_to_second_candidate": False,
        }
    }
    result = apply_a_research_post_pick_gate(picks, config)
    assert result["id"].tolist() == [2]

    broken = json.loads(json.dumps(config))
    broken["rolling_research_post_pick_exclude"]["fallback_to_second_candidate"] = True
    try:
        apply_a_research_post_pick_gate(picks, broken)
    except ValueError as exc:
        assert "禁止回补" in str(exc)
    else:
        raise AssertionError("A研究尾部门禁必须拒绝回补第二名")


def test_unfilled_a_plan_still_blocks_c_e_and_d() -> None:
    legs = {
        "A": pd.DataFrame(
            [
                candidate(
                    "A",
                    "20240701",
                    "20240702",
                    "",
                    None,
                    status="LIMIT_UP_UNBUYABLE",
                )
            ]
        ),
        "C": pd.DataFrame(
            [candidate("C", "20240701", "20240702", "20240704", 0.02)]
        ),
        "E": pd.DataFrame(
            [candidate("E", "20240701", "20240702", "20240703", 0.03)]
        ),
        "D": pd.DataFrame(
            [candidate("D", "20240702", "20240702", "20240704", 0.04)]
        ),
    }
    detail = replay_action_date_portfolio(
        legs, action_dates=["20240702", "20240703", "20240704"]
    )
    assert detail.iloc[0]["status"] == "PLAN_NOT_EXECUTED"
    assert detail.iloc[0]["strategy_leg"] == "A"
    assert not detail["status"].eq("EXECUTED").any()


def test_four_leg_replay_rejects_any_priority_other_than_a_c_e_d() -> None:
    empty = pd.DataFrame(columns=["signal_date", "status", "ts_code"])
    legs = {leg: empty.copy() for leg in ("A", "C", "E", "D")}
    try:
        replay_action_date_portfolio(
            legs,
            action_dates=["20240702"],
            priority=("D", "A", "C", "E"),
        )
    except ValueError as exc:
        assert "A>C>E>D" in str(exc)
    else:
        raise AssertionError("错误四腿顺序必须fail-closed")


def test_explicit_action_date_must_match_derived_execution_date() -> None:
    row = candidate("A", "20240701", "20240702", "20240703", 0.01)
    row["action_date"] = "20240703"
    try:
        replay_action_date_portfolio(
            {"A": pd.DataFrame([row])},
            action_dates=["20240702", "20240703"],
            priority=("A",),
        )
    except ValueError as exc:
        assert "action_date" in str(exc)
    else:
        raise AssertionError("被篡改的显式action_date必须fail-closed")


def test_exit_day_remains_occupied() -> None:
    empty = pd.DataFrame(columns=["signal_date", "status", "ts_code"])
    legs = {
        "A": empty.copy(),
        "C": empty.copy(),
        "E": empty.copy(),
        "D": pd.DataFrame(
            [
                candidate("D", "20240702", "20240702", "20240703", 0.04),
                candidate("D", "20240703", "20240703", "20240704", 0.05),
                candidate("D", "20240704", "20240704", "20240705", 0.06),
            ]
        ),
    }
    detail = replay_action_date_portfolio(
        legs, action_dates=["20240702", "20240703", "20240704"]
    )
    assert detail["status"].tolist() == [
        "EXECUTED",
        "SKIP_OCCUPIED",
        "EXECUTED",
    ]


def test_window_metrics_use_action_date_not_signal_date() -> None:
    detail = pd.DataFrame(
        [
            {
                "action_date": "20230703",
                "signal_date": "20230630",
                "status": "EXECUTED",
                "strategy_leg": "A",
                "account_return": 0.10,
            }
        ]
    )
    included = action_metrics(detail, "20230701", "20231231")
    excluded = action_metrics(detail, "20230601", "20230630")
    assert included["trade_count"] == 1
    assert included["equity_multiple"] == 1.1
    assert excluded["trade_count"] == 0


def test_open_position_with_unobservable_outcome_keeps_capital_occupied() -> None:
    empty = pd.DataFrame(columns=["signal_date", "status", "ts_code"])
    unresolved = candidate(
        "A",
        "20260626",
        "20260629",
        "",
        None,
        status="OUTCOME_NOT_OBSERVABLE_AT_UPDATE",
    )
    unresolved["entry_filled"] = True
    unresolved["position_opened"] = True
    unresolved["outcome_observable"] = False
    unresolved["position_open_until"] = "20260630"
    legs = {
        "A": pd.DataFrame([unresolved]),
        "C": empty.copy(),
        "E": empty.copy(),
        "D": pd.DataFrame(
            [candidate("D", "20260630", "20260630", "20260702", 0.05)]
        ),
    }
    detail = replay_action_date_portfolio(
        legs, action_dates=["20260629", "20260630"]
    )
    assert detail["status"].tolist() == [
        "POSITION_OPEN_OUTCOME_UNOBSERVABLE",
        "SKIP_OCCUPIED",
    ]
    assert action_metrics(detail, "20260101", "20260630")["trade_count"] == 0


def test_recent_half_year_can_veto_but_cannot_change_main_ranking_result() -> None:
    calendar = pd.DataFrame(
        {
            "cal_date": ["20250102", "20260102"],
            "is_open": [1, 1],
        }
    )
    windows = build_window_set("20260630")
    baseline = pd.DataFrame(
        [
            candidate("A", "20250101", "20250102", "20250102", 0.10),
            candidate("A", "20260101", "20260102", "20260102", -0.10),
        ]
    )
    improved_main_but_failed_half = pd.DataFrame(
        [
            candidate("A", "20250101", "20250102", "20250102", 0.50),
            candidate("A", "20260101", "20260102", "20260102", -0.25),
        ]
    )
    empty = pd.DataFrame(columns=["signal_date", "status", "ts_code"])
    result = evaluate_three_window_replacement(
        leg="A",
        baseline_leg=baseline,
        candidate_leg=improved_main_but_failed_half,
        frozen_other_legs={"C": empty, "E": empty, "D": empty},
        calendar=calendar,
        windows=windows,
        gate={
            "minimum_main_sample_retention": 0.7,
            "minimum_recent_sample_retention": 0.7,
            "minimum_absolute_trades": {"A": 0},
            "maximum_main_drawdown_worsening_pp": 1.0,
            "maximum_recent_drawdown_worsening_pp": 1.0,
            "failure_check_minimum_trades": 1,
            "failure_check_equity_floor": 1.0,
            "failure_check_require_nonpositive_average_with_equity_failure": True,
            "failure_check_drawdown_floor": -0.20,
        },
    )
    assert result["selection_gate_passed"] is True
    assert result["main_gate_passed"] is True
    assert result["recent_confirmation_passed"] is True
    assert result["replacement_gate_passed"] is False
    assert "HALF_YEAR_STANDALONE_EQUITY" in result["failure_flags"]
    assert "HALF_YEAR_STANDALONE_DRAWDOWN" in result["failure_flags"]
    assert all(
        not reason.startswith("HALF_YEAR_")
        for reason in result["selection_gate_reasons"]
    )


def test_recent_confirmation_is_separate_from_main_gate() -> None:
    calendar = pd.DataFrame(
        {
            "cal_date": ["20230703", "20250102"],
            "is_open": [1, 1],
        }
    )
    windows = build_window_set("20260630")
    baseline = pd.DataFrame(
        [
            candidate("A", "20230702", "20230703", "20230703", 0.00),
            candidate("A", "20250101", "20250102", "20250102", 0.10),
        ]
    )
    candidate_frame = pd.DataFrame(
        [
            candidate("A", "20230702", "20230703", "20230703", 0.50),
            candidate("A", "20250101", "20250102", "20250102", 0.00),
        ]
    )
    empty = pd.DataFrame(columns=["signal_date", "status", "ts_code"])
    result = evaluate_three_window_replacement(
        leg="A",
        baseline_leg=baseline,
        candidate_leg=candidate_frame,
        frozen_other_legs={"C": empty, "E": empty, "D": empty},
        calendar=calendar,
        windows=windows,
        gate={
            "minimum_main_sample_retention": 0.7,
            "minimum_recent_sample_retention": 0.7,
            "minimum_absolute_trades": {"A": 0},
            "maximum_main_drawdown_worsening_pp": 1.0,
            "maximum_recent_drawdown_worsening_pp": 1.0,
            "failure_check_minimum_trades": 99,
            "failure_check_equity_floor": 1.0,
            "failure_check_require_nonpositive_average_with_equity_failure": True,
            "failure_check_drawdown_floor": -0.20,
        },
    )
    assert result["main_gate_passed"] is True
    assert result["recent_confirmation_passed"] is False
    assert result["replacement_gate_passed"] is False
    assert result["main_gate_reasons"] == []
    assert "RECENT_STANDALONE_COMPOUND" in result["recent_confirmation_reasons"]


def test_recent_confirmation_failure_cannot_promote_runner_up() -> None:
    rows = [
        {
            "evaluation_status": "EVALUATED",
            "variant_id": "MAIN_FIRST_RECENT_FAILED",
            "changed_axis_count": 1,
            "main_gate_passed": True,
            "style_gate_passed": True,
            "recent_confirmation_passed": False,
            "main_min_log_compound_improvement": 0.40,
            "main_candidate_standalone_max_drawdown": -0.10,
            "main_candidate_standalone_trade_count": 50,
        },
        {
            "evaluation_status": "EVALUATED",
            "variant_id": "MAIN_SECOND_RECENT_PASSED",
            "changed_axis_count": 1,
            "main_gate_passed": True,
            "style_gate_passed": True,
            "recent_confirmation_passed": True,
            "main_min_log_compound_improvement": 0.20,
            "main_candidate_standalone_max_drawdown": -0.10,
            "main_candidate_standalone_trade_count": 50,
        },
    ]
    winner = select_main_window_winner(rows, require_style_gate=True)
    assert winner is not None
    assert winner["variant_id"] == "MAIN_FIRST_RECENT_FAILED"
    assert winner["recent_confirmation_passed"] is False
