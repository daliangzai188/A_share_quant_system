from __future__ import annotations

import json
from pathlib import Path

from scripts import monitor_strategy_d_intraday as d_monitor
from scripts.monitor_strategy_d_intraday import (
    StockState,
    StrategyDMonitor,
    calculate_d_order_capacity,
)
from src.strategy_d_minute_alignment import (
    expected_completed_minute_hhmm,
    replay_completed_minute_path,
)


class _Logger:
    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None


class _MinuteBroker:
    def __init__(self, bars_by_code: dict[str, list[dict]]) -> None:
        self.bars_by_code = bars_by_code
        self.calls = 0

    def get_minute_bars(
        self,
        ts_codes: list[str],
        *,
        start_time: str,
        end_time: str,
    ) -> dict[str, list[dict]]:
        self.calls += 1
        assert start_time.endswith("093000")
        assert end_time
        return {code: list(self.bars_by_code.get(code, [])) for code in ts_codes}


def _bar(hhmm: int, close: float, *, low: float | None = None) -> dict:
    return {
        "hhmm": hhmm,
        "open": close,
        "high": close,
        "low": close if low is None else low,
        "close": close,
        "volume": 1000,
        "amount": 100_000,
    }


def _release(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "factor_schema_id": "D_RESEAL_FACTOR_VALUES_V1",
                "release_id": "D_TEST_STRICT_1M",
                "strategy_mode": "FACTOR_UNION",
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
                        "profile_id": "P_STRICT",
                        "priority": 1,
                        "conditions": {
                            "reseal_time_bucket": "0930_1000",
                            "break_close_depth_bucket": "LT0_2PCT",
                            "segment_bucket": "GROWTH_BOARD",
                        },
                    }
                ],
                "selection_policy": "EARLIEST_RESEAL_THEN_OPEN2_THEN_CODE",
            }
        ),
        encoding="utf-8",
    )
    return path


def _growth_state() -> StockState:
    return StockState(
        ts_code="301630.SZ",
        name="同宇新材",
        market_segment="chi_next",
        upper_limit=196.81,
        was_sealed=True,
        ever_sealed=True,
        open_times_today=1,
        first_seal_hhmm=930,
        last_seal_hhmm=932,
        last_break_hhmm=930,
        last_break_price=196.50,
        pre_close=164.01,
        open_price=196.39,
        session_low_price=190.30,
        cumulative_amount=330_000_000,
        previous_day_amount_yuan=182_996_014.02,
    )


def test_expected_completed_minutes_follow_real_clock_boundaries() -> None:
    expected_counts = {
        929: 0,
        930: 1,
        931: 2,
        959: 30,
        1000: 31,
        1027: 58,
        1130: 121,
        1131: 121,
        1200: 121,
        1259: 121,
        1300: 121,
        1301: 122,
        1459: 240,
        1500: 241,
        1501: 241,
    }

    for current_hhmm, count in expected_counts.items():
        labels = expected_completed_minute_hhmm(current_hhmm)
        assert len(labels) == count
        assert all(divmod(label, 100)[1] < 60 for label in labels)

    assert expected_completed_minute_hhmm(1000)[-3:] == [958, 959, 1000]
    assert expected_completed_minute_hhmm(1300)[-1] == 1130
    assert expected_completed_minute_hhmm(1301)[-2:] == [1130, 1301]


def test_one_cent_below_limit_is_not_treated_as_sealed() -> None:
    path = replay_completed_minute_path(
        [_bar(930, 196.80)],
        limit_price=196.81,
        current_hhmm=930,
    )

    assert path.certifiable is True
    assert path.ever_sealed is False
    assert path.first_seal_hhmm == 0


def test_today_301630_is_first_minute_seal_not_reseal() -> None:
    path = replay_completed_minute_path(
        [
            _bar(930, 196.39),
            _bar(931, 193.28, low=190.30),
            _bar(932, 196.00),
            _bar(933, 196.81, low=195.60),
        ],
        limit_price=196.81,
        current_hhmm=933,
    )

    assert path.certifiable is True
    assert path.first_seal_hhmm == 933
    assert path.open_times == 0
    assert path.last_reseal_hhmm == 0
    assert path.has_reseal is False
    assert path.has_fresh_reseal is False


def test_completed_minute_close_sequence_can_form_strict_reseal() -> None:
    path = replay_completed_minute_path(
        [
            _bar(930, 10.00),
            _bar(931, 11.00),
            _bar(932, 10.98, low=10.95),
            _bar(933, 11.00),
        ],
        limit_price=11.00,
        current_hhmm=933,
    )

    assert path.first_seal_hhmm == 931
    assert path.open_times == 1
    assert path.last_break_hhmm == 932
    assert path.last_break_close == 10.98
    assert path.last_reseal_hhmm == 933
    assert path.has_fresh_reseal is True


def test_missing_completed_minute_fails_closed() -> None:
    path = replay_completed_minute_path(
        [_bar(930, 10.0), _bar(932, 11.0)],
        limit_price=11.0,
        current_hhmm=932,
    )

    assert path.certifiable is False
    assert "缺1根" in path.reason


def test_live_factor_gate_rejects_today_snapshot_false_reseal(
    tmp_path: Path, monkeypatch,
) -> None:
    release_path = _release(tmp_path / "release.json")
    bars = [
        _bar(930, 196.39),
        _bar(931, 193.28, low=190.30),
        _bar(932, 196.00),
        _bar(933, 196.81, low=195.60),
    ]
    monitor = StrategyDMonitor(
        broker=_MinuteBroker({"301630.SZ": bars}),
        live_order=False,
        logger=_Logger(),
        signal_csv=tmp_path / "signals.csv",
        config={"strategy_d": {"factor_release_path": str(release_path)}},
    )
    state = _growth_state()
    monitor.states = {state.ts_code: state}
    monkeypatch.setattr(d_monitor, "now_hhmm", lambda: 933)

    assert monitor._refresh_strict_minute_paths() is True
    matched, reason = monitor._factor_release_match(
        state, require_fresh_reseal=True
    )

    assert matched is False
    assert "尚未形成封板→炸板→回封" in reason

    fired: list[str] = []
    monitor._fire_buy_signal = lambda candidate: fired.append(candidate.ts_code) or True
    monitor._check_and_fire_factor_union()
    assert fired == []
    assert monitor.factor_signal_consumed is False
    assert monitor.order_placed is False


def test_live_factor_gate_accepts_completed_minute_reseal(
    tmp_path: Path, monkeypatch,
) -> None:
    release_path = _release(tmp_path / "release.json")
    bars = [
        _bar(930, 180.00),
        _bar(931, 196.81),
        _bar(932, 196.50),
        _bar(933, 196.81),
    ]
    monitor = StrategyDMonitor(
        broker=_MinuteBroker({"301630.SZ": bars}),
        live_order=False,
        logger=_Logger(),
        signal_csv=tmp_path / "signals.csv",
        config={"strategy_d": {"factor_release_path": str(release_path)}},
    )
    state = _growth_state()
    monitor.states = {state.ts_code: state}
    monkeypatch.setattr(d_monitor, "now_hhmm", lambda: 933)

    assert monitor._refresh_strict_minute_paths() is True
    matched, reason = monitor._factor_release_match(
        state, require_fresh_reseal=True
    )

    assert matched is True
    assert "P_STRICT" in reason
    values = json.loads(state.factor_values_json)
    assert values["reseal_time_bucket"] == "0930_1000"
    assert values["break_close_depth_bucket"] == "LT0_2PCT"


def test_live_minute_paths_are_queried_in_batches_above_subscription_limit(
    tmp_path: Path, monkeypatch,
) -> None:
    release_path = _release(tmp_path / "release.json")
    broker = _MinuteBroker({})
    monitor = StrategyDMonitor(
        broker=broker,
        live_order=False,
        logger=_Logger(),
        signal_csv=tmp_path / "signals.csv",
        config={"strategy_d": {"factor_release_path": str(release_path)}},
    )
    states: dict[str, StockState] = {}
    for index in range(51):
        state = _growth_state()
        state.ts_code = f"{index:06d}.SZ"
        states[state.ts_code] = state
    monitor.states = states
    monkeypatch.setattr(d_monitor, "now_hhmm", lambda: 1027)

    assert monitor._refresh_strict_minute_paths() is False
    assert "一分钟路径不完整" in monitor.strict_minute_refresh_error
    assert "超过QMT单股订阅安全上限" not in monitor.strict_minute_refresh_error
    assert broker.calls == 2


def test_d_order_capacity_uses_actual_82_5_percent_order_amount() -> None:
    class Account:
        available_cash = 1_000_000.0
        total_asset = 1_000_000.0

    capacity = calculate_d_order_capacity(
        Account(),
        price=10.0,
        position_pct=0.825,
        live_config={
            "max_position_pct": 0.85,
            "max_total_position_pct": 0.825,
            "cash_buffer_amount": 1000,
        },
    )

    assert capacity.shares == 82_400
    assert capacity.actual_amount == 824_000.0
    assert capacity.actual_amount != 412_500.0


def test_d_market_breadth_excludes_st_previous_limit_and_multi_board(
    tmp_path: Path,
) -> None:
    monitor = StrategyDMonitor(
        broker=None,
        live_order=False,
        logger=_Logger(),
        signal_csv=tmp_path / "signals.csv",
        config={"strategy_d": {"factor_release_path": str(_release(tmp_path / "release.json"))}},
    )

    eligible = _growth_state()
    eligible.ts_code = "300001.SZ"
    eligible.open_times_today = 2
    st_stock = _growth_state()
    st_stock.ts_code = "300002.SZ"
    st_stock.st_suspect = True
    previous_limit = _growth_state()
    previous_limit.ts_code = "300003.SZ"
    main_board = _growth_state()
    main_board.ts_code = "000004.SZ"
    main_board.market_segment = "sz_main"
    monitor.states = {
        item.ts_code: item
        for item in (eligible, st_stock, previous_limit, main_board)
    }
    monitor.yesterday_limit_codes = {previous_limit.ts_code}
    monitor.strict_minute_paths = {
        code: replay_completed_minute_path(
            [_bar(930, state.upper_limit)],
            limit_price=state.upper_limit,
            current_hhmm=930,
        )
        for code, state in monitor.states.items()
    }

    assert monitor.market_ever_sealed_count == 2
    assert monitor.sealed_ever_count == 2
    assert monitor.market_break_event_count == 3
    # 市场级广度统计全部已启用板块里的非ST首板；只有segment两列按查询的
    # chi_next收窄。因此这里应包含创业板首板和深主板首板共2只。
    assert monitor._strict_market_context("chi_next") == (2, 2, 0, 1, 1)
