from __future__ import annotations

import json
from pathlib import Path

from scripts import monitor_strategy_d_intraday as d_monitor
from scripts.monitor_strategy_d_intraday import StockState, StrategyDMonitor
from src.strategy_d_minute_alignment import replay_completed_minute_path


class _Logger:
    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None


class _MinuteBroker:
    def __init__(self, bars_by_code: dict[str, list[dict]]) -> None:
        self.bars_by_code = bars_by_code

    def get_minute_bars(
        self,
        ts_codes: list[str],
        *,
        start_time: str,
        end_time: str,
    ) -> dict[str, list[dict]]:
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
