from __future__ import annotations

import pandas as pd

from scripts.collect_strategy_d_intraday_baostock import to_baostock_code
from src.strategy_d_intraday_ledger import (
    coverage_status,
    daily_consistency_status,
    normalize_hhmm,
    normalize_minute_bars,
    replay_intraday_path,
)


LIMIT = 10.0


def bars(points: list[tuple[int, float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "trade_date": "20250102",
                "hhmm": hhmm,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000,
                "amount": 10000,
            }
            for hhmm, open_, high, low, close in points
        ]
    )


def test_first_eligible_reseal_and_price_penetration_confirm_fill() -> None:
    replay = replay_intraday_path(
        bars(
            [
                (1000, 9.9, 10.0, 9.9, 10.0),
                (1001, 10.0, 10.0, 9.9, 9.9),
                (1002, 9.9, 10.0, 9.9, 10.0),
                (1003, 10.0, 10.0, 9.9, 9.9),
                (1401, 9.9, 10.0, 9.9, 10.0),
                (1402, 10.0, 10.0, 10.0, 10.0),
                (1403, 10.0, 10.0, 9.9, 9.9),
            ]
        ),
        limit_price=LIMIT,
    )

    assert replay["first_seal_hhmm"] == 1000
    assert replay["eligible_signal_hhmm"] == 1401
    assert replay["open_times_at_signal"] == 2
    assert replay["signal_rule_current"] is True
    assert replay["queue_fill_status"] == "CONFIRMED_FILL_PRICE_TRADED_BELOW_LIMIT"
    assert replay["fill_hhmm"] == 1403


def test_sealed_queue_without_depth_is_cancelled_as_unknown_at_1455() -> None:
    replay = replay_intraday_path(
        bars(
            [
                (1000, 9.9, 10.0, 9.9, 10.0),
                (1001, 10.0, 10.0, 9.9, 9.9),
                (1002, 9.9, 10.0, 9.9, 10.0),
                (1003, 10.0, 10.0, 9.9, 9.9),
                (1401, 9.9, 10.0, 9.9, 10.0),
                (1455, 10.0, 10.0, 10.0, 10.0),
            ]
        ),
        limit_price=LIMIT,
    )

    assert replay["eligible_signal_hhmm"] == 1401
    assert replay["queue_fill_status"] == "QUEUE_FILL_UNKNOWN_NO_DEPTH_CANCEL_1455"
    assert replay["fill_hhmm"] == 0
    assert replay["events"][-1]["event_type"] == "CANCEL_UNVERIFIABLE_QUEUE_ORDER"
    assert replay["events"][-1]["hhmm"] == 1455


def test_reseal_at_cancel_time_does_not_generate_order() -> None:
    replay = replay_intraday_path(
        bars(
            [
                (1000, 9.9, 10.0, 9.9, 10.0),
                (1001, 10.0, 10.0, 9.9, 9.9),
                (1002, 9.9, 10.0, 9.9, 10.0),
                (1003, 10.0, 10.0, 9.9, 9.9),
                (1455, 9.9, 10.0, 9.9, 10.0),
            ]
        ),
        limit_price=LIMIT,
    )

    assert replay["eligible_signal_hhmm"] == 0
    assert replay["signal_rule_current"] is False
    assert replay["queue_fill_status"] == "NO_SIGNAL"


def test_only_one_break_never_generates_d_signal() -> None:
    replay = replay_intraday_path(
        bars(
            [
                (1000, 9.9, 10.0, 9.9, 10.0),
                (1001, 10.0, 10.0, 9.9, 9.9),
                (1401, 9.9, 10.0, 9.9, 10.0),
            ]
        ),
        limit_price=LIMIT,
    )

    assert replay["total_open_times"] == 1
    assert replay["signal_rule_current"] is False
    assert replay["queue_fill_status"] == "NO_SIGNAL"


def test_first_seal_after_1400_is_identified_but_not_first_before_1400_variant() -> None:
    replay = replay_intraday_path(
        bars(
            [
                (1401, 9.9, 10.0, 9.9, 10.0),
                (1402, 10.0, 10.0, 9.9, 9.9),
                (1403, 9.9, 10.0, 9.9, 10.0),
                (1404, 10.0, 10.0, 9.9, 9.9),
                (1405, 9.9, 10.0, 9.9, 10.0),
            ]
        ),
        limit_price=LIMIT,
    )

    assert replay["first_time_bucket"] == "afternoon"
    assert replay["eligible_signal_hhmm"] == 1405
    assert replay["signal_rule_current"] is True
    assert replay["signal_rule_first_before_1400"] is False


def test_one_cent_below_limit_is_not_treated_as_sealed() -> None:
    replay = replay_intraday_path(
        bars([(1000, 9.99, 10.0, 9.99, 9.99)]), limit_price=LIMIT
    )
    assert replay["first_seal_hhmm"] == 0
    assert replay["signal_rule_current"] is False


def test_intrabar_touch_and_trade_below_is_explicitly_ambiguous() -> None:
    replay = replay_intraday_path(
        bars([(1000, 9.9, 10.0, 9.8, 9.9)]), limit_price=LIMIT
    )
    assert replay["path_ambiguous"] is True
    assert replay["ambiguous_bar_count"] == 1


def test_baostock_end_label_and_five_minute_coverage() -> None:
    # 显式列出上午结束标签，避免HHMM直接加法跨小时。
    morning = [
        hour * 100 + minute
        for hour, minute in (
            [(9, minute) for minute in range(35, 60, 5)]
            + [(10, minute) for minute in range(0, 60, 5)]
            + [(11, minute) for minute in range(0, 31, 5)]
        )
    ]
    afternoon = [
        hour * 100 + minute
        for hour, minute in (
            [(13, minute) for minute in range(5, 60, 5)]
            + [(14, minute) for minute in range(0, 60, 5)]
            + [(15, 0)]
        )
    ]
    raw = pd.DataFrame(
        {
            "ts_code": "000001.SZ",
            "trade_date": "20250102",
            "bar_time": [f"20250102{hhmm:04d}00000" for hhmm in morning + afternoon],
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 1,
            "amount": 10,
        }
    )
    normalized = normalize_minute_bars(raw)
    status = coverage_status(normalized)
    assert len(normalized) == 48
    assert status["bar_minutes"] == 5
    assert status["minute_status"] == "APPROXIMATE_5M_PATH_NO_QUEUE_DEPTH"
    assert status["path_complete"] is True


def test_tushare_full_day_one_minute_coverage() -> None:
    hhmm = (
        [hour * 100 + minute for hour in (9, 10) for minute in range(60)]
        + [1100 + minute for minute in range(31)]
        + [1300 + minute for minute in range(1, 60)]
        + [1400 + minute for minute in range(60)]
        + [1500]
    )
    hhmm = [value for value in hhmm if value >= 930]
    raw = bars([(value, 10.0, 10.0, 10.0, 10.0) for value in hhmm])

    status = coverage_status(normalize_minute_bars(raw))

    assert len(raw) == 241
    assert status["bar_minutes"] == 1
    assert status["minute_status"] == "READY_1M_PATH_NO_QUEUE_DEPTH"
    assert status["path_complete"] is True


def test_time_and_vendor_code_normalization() -> None:
    assert normalize_hhmm("20250102093500000") == 935
    assert normalize_hhmm("14:05:00") == 1405
    assert to_baostock_code("600000.SH") == "sh.600000"
    assert to_baostock_code("000001.SZ") == "sz.000001"
    assert to_baostock_code("920001.BJ") == ""


def test_daily_touch_mismatch_is_explicit() -> None:
    status = daily_consistency_status(
        bars([(1000, 9.98, 9.99, 9.98, 9.99)]),
        limit_price=10.0,
        daily_high=10.0,
        daily_close=9.99,
    )
    assert status["minute_max_high"] == 9.99
    assert status["minute_confirms_daily_touch"] is False
    assert status["minute_daily_high_diff"] < 0
