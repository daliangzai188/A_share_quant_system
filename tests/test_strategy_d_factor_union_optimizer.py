from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from scripts import monitor_strategy_d_intraday as d_monitor
from scripts.monitor_strategy_d_intraday import StockState, StrategyDMonitor
from scripts.optimize_strategy_d_factor_union import (
    minimize_profile_union,
    latest_completed_update_node,
    rank_profile_comparisons,
    render_best_result_text,
    select_best_dual_gate_profile,
)
from src.strategy_d_factor_rules import (
    FACTOR_SCHEMA_ID,
    FACTOR_UNION_MODE,
    add_factor_values,
    factor_values_from_raw,
    load_factor_release,
    matching_profile_ids,
)
from src.strategy_d_minute_alignment import StrictMinutePath


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
        "entry_alignment": {
            "historical_signal_time_fill_gate_certified": True,
            "runtime_new_buy_enabled": True,
            "min_fill_probability": 0.8,
            "planned_buy_amount_source": "ACTUAL_ORDER_GROSS",
        },
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
    monitor.strict_minute_paths = {
        state.ts_code: StrictMinutePath(
            certifiable=True,
            last_completed_hhmm=945,
            was_sealed=True,
            ever_sealed=True,
            first_seal_hhmm=935,
            last_seal_hhmm=945,
            last_reseal_hhmm=945,
            open_times=1,
            last_break_hhmm=942,
            last_break_close=10.95,
            previous_seal_to_break_minutes=4,
        )
    }

    passed, reason = monitor._factor_release_match(
        state, require_fresh_reseal=True
    )
    monitor.strict_minute_paths[state.ts_code] = StrictMinutePath(
        certifiable=True,
        last_completed_hhmm=946,
        was_sealed=True,
        ever_sealed=True,
        first_seal_hhmm=935,
        last_seal_hhmm=945,
        last_reseal_hhmm=945,
        open_times=1,
        last_break_hhmm=942,
        last_break_close=10.95,
        previous_seal_to_break_minutes=4,
    )
    stale, stale_reason = monitor._factor_release_match(
        state, require_fresh_reseal=True
    )

    assert passed is True
    assert "P1" in reason
    assert stale is False
    assert "不是最新完成分钟" in stale_reason


def test_factor_union_consumes_earliest_signal_without_later_substitution(
    tmp_path: Path, monkeypatch,
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
    monkeypatch.setattr(d_monitor, "now_hhmm", lambda: 945)
    monitor.strict_minute_refresh_hhmm = 945
    monitor.strict_minute_paths = {
        code: StrictMinutePath(
            certifiable=True,
            last_completed_hhmm=945,
            was_sealed=True,
            ever_sealed=True,
            first_seal_hhmm=935,
            last_seal_hhmm=945,
            last_reseal_hhmm=945,
            open_times=opens,
            last_break_hhmm=943,
            last_break_close=10.95,
            previous_seal_to_break_minutes=3,
        )
        for code, opens in (("000002.SZ", 1), ("000003.SZ", 2))
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


def test_dynamic_entry_gate_blocks_buy_selection_but_not_monitor_lifecycle(
    tmp_path: Path,
) -> None:
    """上游候选占用资金时只跳过BUY选择，D监控对象和路径状态继续保留。"""

    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(factor_release()), encoding="utf-8")
    gate_state = {"allowed": False}
    tracking_state = {"allowed": True}
    monitor = StrategyDMonitor(
        broker=None,
        live_order=False,
        logger=DummyLogger(),
        signal_csv=tmp_path / "signals.csv",
        config={"strategy_d": {"factor_release_path": str(release_path)}},
        entry_gate=lambda: (
            gate_state["allowed"],
            "候选窗口已结束" if gate_state["allowed"] else "候选仍在补仓窗口",
        ),
        tracking_gate=lambda: (
            tracking_state["allowed"],
            "账户仍空仓" if tracking_state["allowed"] else "候选已经成交",
        ),
    )
    monitor.scan_round = 12
    monitor.states = {"sentinel": StockState(ts_code="sentinel")}
    factor_check = MagicMock()
    monitor._check_and_fire_factor_union = factor_check  # type: ignore[method-assign]

    monitor._check_and_fire()

    factor_check.assert_not_called()
    assert monitor.scan_round == 12
    assert "sentinel" in monitor.states

    gate_state["allowed"] = True
    monitor._check_and_fire()

    factor_check.assert_called_once_with()
    tracking_state["allowed"] = False
    tracking_allowed, tracking_reason = monitor._tracking_gate_allows_monitor()
    assert tracking_allowed is False
    assert "候选已经成交" in tracking_reason


def test_best_factor_release_early_reseal_is_not_blocked_by_legacy_tail_gate(
    tmp_path: Path, monkeypatch,
) -> None:
    release = factor_release("DIF_6501c8c095f9")
    release["profiles"][0]["conditions"] = {
        "reseal_time_bucket": "0930_1000",
        "break_close_depth_bucket": "LT0_2PCT",
        "segment_bucket": "GROWTH_BOARD",
    }
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(release), encoding="utf-8")
    monitor = StrategyDMonitor(
        broker=None,
        live_order=False,
        logger=DummyLogger(),
        signal_csv=tmp_path / "signals.csv",
        config={
            "strategy_d": {
                "factor_release_path": str(release_path),
                "tail_reseal_hhmm": 1400,
                "first_time_buckets": ["midday", "afternoon", "late"],
            }
        },
    )
    state = StockState(
        ts_code="300001.SZ",
        name="测试",
        market_segment="chi_next",
        upper_limit=12.0,
        was_sealed=True,
        ever_sealed=True,
        open_times_today=1,
        first_seal_hhmm=935,
        last_seal_hhmm=945,
        last_break_hhmm=943,
        last_break_price=11.99,
        previous_seal_to_break_minutes=3,
        pre_close=10.0,
        open_price=10.2,
        session_low_price=10.1,
    )
    monkeypatch.setattr(d_monitor, "now_hhmm", lambda: 945)
    monitor.strict_minute_paths = {
        state.ts_code: StrictMinutePath(
            certifiable=True,
            last_completed_hhmm=945,
            was_sealed=True,
            ever_sealed=True,
            first_seal_hhmm=935,
            last_seal_hhmm=945,
            last_reseal_hhmm=945,
            open_times=1,
            last_break_hhmm=943,
            last_break_close=11.99,
            previous_seal_to_break_minutes=3,
        )
    }
    monitor._refresh_fill_gate = lambda _state: (True, "测试成交门通过")

    valid, reason = monitor._validate_buy_candidate(state)

    assert valid is True
    assert "DIF_6501c8c095f9" in reason


def test_best_profile_requires_dual_gate_then_maximizes_acde() -> None:
    comparisons = pd.DataFrame(
        [
            {
                "profile_id": "D_ONLY_TOP",
                "factor_count": 2,
                "d_equity_multiple": 7.0,
                "acde_equity_multiple": 40.0,
                "acde_max_drawdown": -0.40,
                "dual_compound_gate_passed": False,
            },
            {
                "profile_id": "DUAL_LOWER",
                "factor_count": 2,
                "d_equity_multiple": 3.0,
                "acde_equity_multiple": 400.0,
                "acde_max_drawdown": -0.25,
                "dual_compound_gate_passed": True,
            },
            {
                "profile_id": "DUAL_BEST",
                "factor_count": 3,
                "d_equity_multiple": 2.8,
                "acde_equity_multiple": 486.0,
                "acde_max_drawdown": -0.23,
                "dual_compound_gate_passed": True,
            },
        ]
    )

    ranked = rank_profile_comparisons(comparisons)
    best = select_best_dual_gate_profile(comparisons)

    assert ranked.iloc[0]["profile_id"] == "DUAL_BEST"
    assert best is not None
    assert best["profile_id"] == "DUAL_BEST"


def test_direct_run_uses_latest_completed_semiannual_node() -> None:
    assert latest_completed_update_node(dt.date(2026, 2, 1)) == "20251231"
    assert latest_completed_update_node(dt.date(2026, 6, 30)) == "20260630"
    assert latest_completed_update_node(dt.date(2026, 8, 24)) == "20260630"
    assert latest_completed_update_node(dt.date(2026, 12, 31)) == "20261231"


def test_complete_text_report_contains_conditions_baselines_and_decision() -> None:
    def metrics(multiple: float) -> dict:
        return {
            "trade_count": 33,
            "win_rate": 0.63,
            "avg_account_return": 0.034,
            "median_account_return": 0.019,
            "equity_multiple": multiple,
            "max_drawdown": -0.22,
            "max_profit": 0.21,
            "max_loss": -0.07,
            "profit_loss_ratio": 1.93,
            "max_consecutive_losses": 3,
        }

    d_metrics = {
        **metrics(2.8112400677),
        "first_12m_trade_count": 10,
        "first_12m_multiple": 1.0001720948,
        "second_12m_trade_count": 23,
        "second_12m_multiple": 2.8107563512,
        "candidate_day_count": 50,
        "price_confirmed_day_count": 42,
        "queue_unknown_day_count": 8,
        "unresolved_exit_count": 0,
    }
    profile = {
        "profile_id": "DIF_6501c8c095f9",
        "conditions": {
            "reseal_time_bucket": "0930_1000",
            "break_close_depth_bucket": "LT0_2PCT",
            "segment_bucket": "GROWTH_BOARD",
        },
        "readable_conditions": [
            "本次回封发生时间：09:30～10:00",
            "最后炸板时相对涨停价的回落深度：小于0.2%",
            "股票所属交易板块：创业板或科创板",
        ],
        "d_metrics": d_metrics,
        "acde_metrics": {**metrics(486.3661434308), "leg_counts": {"D": 17}},
        "d_compound_improved": True,
        "acde_compound_improved": True,
        "dual_compound_gate_passed": True,
    }
    payload = {
        "window": {"start": "20240630", "end": "20260630"},
        "search_space": {
            "factor_column_count": 16,
            "observed_factor_value_group_count": 51110,
            "evaluated_profile_count": 34157,
            "threshold_qualified_profile_count": 337,
            "portfolio_evaluated_profile_count": 337,
            "dual_gate_passed_profile_count": 1,
        },
        "incumbent": {
            "d_metrics": metrics(2.0261239236),
            "acde_metrics": {**metrics(327.7267189755), "leg_counts": {"D": 22}},
        },
        "best_dual_gate_profile": profile,
        "best_observed_profile": profile,
        "formal_strategy_modified": False,
    }

    text = render_best_result_text(payload)

    assert "【一眼结论】" in text
    assert "最佳组合：DIF_6501c8c095f9" in text
    assert "D复利：2.8112400677倍" in text
    assert "ACDE复利：486.3661434308倍" in text
    assert "双复利闸门：通过" in text
    assert "正式D是否修改：否" in text
    assert "DIF_6501c8c095f9" in text
    assert "486.3661434308倍" in text
    assert "候选具备替换资格：是" in text
    assert "本次是否已经修改正式D：否" in text


def test_v15_formal_release_keeps_core_and_adds_only_weak_active_open2() -> None:
    """V15只能扩展弱广度二次回封，不能改写V13的12个正式档位。"""

    project_root = Path(__file__).resolve().parents[1]
    release = load_factor_release(
        project_root / "config" / "strategy_d_factor_release.json"
    )
    assert release["release_id"] == "D_ACTIVE_LT20_OPEN2_EXTENSION_20260831_V15"
    assert len(release["profiles"]) == 15

    core = [
        item for item in release["profiles"]
        if item["conditions"].get("market_active_count_bucket") != "LT20"
    ]
    extension = [
        item for item in release["profiles"]
        if item["conditions"].get("market_active_count_bucket") == "LT20"
    ]
    assert len(core) == 12
    assert all("open_count_bucket" not in item["conditions"] for item in core)
    assert len(extension) == 3
    assert {
        item["conditions"]["market_break_rate_bucket"] for item in extension
    } == {"LT25PCT", "25_50PCT", "50_75PCT"}
    assert all(
        item["conditions"].get("open_count_bucket") == "2"
        for item in extension
    )

    weak_open2 = factor_values_from_raw(
        raw_factor_row(
            open_times_at_signal=2,
            last_break_close_depth_pct=0.001,
            market_ever_sealed_count=25,
            market_active_sealed_count=10,
            market_break_event_rate=0.30,
            market_segment="chi_next",
        )
    )
    assert matching_profile_ids(weak_open2, release["profiles"]) == [
        "D_WEAK_ACTIVE_LT20_OPEN2_BREAK_25_50"
    ]

    # 相邻的第1次回封必须继续拒绝，锁住本次用户批准的精确规则边界。
    weak_open1 = dict(weak_open2, open_count_bucket="1")
    assert matching_profile_ids(weak_open1, release["profiles"]) == []
