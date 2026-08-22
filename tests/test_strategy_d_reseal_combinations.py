from __future__ import annotations

import pandas as pd

from scripts.research_strategy_d_reseal_combinations import (
    FORBIDDEN_SELECTION_FIELDS,
    SELECTION_FIELDS,
    build_rule_space,
    daily_first_indexes,
    merge_picks_with_baseline,
)


def test_rule_space_is_frozen_and_covers_unrestricted_core_axes() -> None:
    rules = build_rule_space()

    assert len(rules) == 5944
    assert len({rule.name for rule in rules}) == len(rules)
    assert any("TIME_FREE" in rule.family for rule in rules)
    assert any("OPEN_COUNT_FREE" in rule.family for rule in rules)
    assert any("SPEED_FREE" in rule.family for rule in rules)
    assert any(rule.family == "PURE_QUALITY_PAIR_ALL_TIME_AND_OPEN_COUNT" for rule in rules)
    for rule in rules:
        assert set(rule.fields) <= SELECTION_FIELDS
        assert not (set(rule.fields) & FORBIDDEN_SELECTION_FIELDS)


def test_daily_first_indexes_uses_precomputed_asof_order() -> None:
    # order已经按日期、信号分钟和同分钟排序排好；mask只负责过滤，不能看收益倒选。
    dates = pd.Series(["20250102", "20250102", "20250103", "20250103"]).to_numpy()
    order = pd.Series([1, 0, 3, 2]).to_numpy()
    mask = pd.Series([True, True, True, False]).to_numpy()

    selected = daily_first_indexes(mask, order, dates)

    assert selected.tolist() == [1, 2]


def test_merge_uses_real_signal_minute_and_baseline_wins_tie() -> None:
    candidate = pd.DataFrame(
        [
            {
                "trade_date": "20250102",
                "signal_hhmm": 1350,
                "event_id": 1,
                "ts_code": "000001.SZ",
                "name": "候选早",
                "queue_price_confirmed": True,
                "execution_status": "OK",
                "exit_date": "20250106",
                "account_return": 0.02,
            },
            {
                "trade_date": "20250103",
                "signal_hhmm": 1410,
                "event_id": 2,
                "ts_code": "000002.SZ",
                "name": "候选同分钟",
                "queue_price_confirmed": True,
                "execution_status": "OK",
                "exit_date": "20250107",
                "account_return": 0.03,
            },
        ]
    )
    baseline = pd.DataFrame(
        [
            {
                "trade_date": "20250102",
                "signal_date": "20250102",
                "signal_hhmm": 1400,
                "event_id": 10,
                "ts_code": "600001.SH",
                "name": "正式晚",
                "strategy_leg": "D",
                "status": "OK",
                "exit_date": "20250106",
                "account_return": 0.01,
                "source_rule": "FORMAL_D_BASELINE",
                "source_priority": 0,
                "queue_price_confirmed": True,
            },
            {
                "trade_date": "20250103",
                "signal_date": "20250103",
                "signal_hhmm": 1410,
                "event_id": 11,
                "ts_code": "600002.SH",
                "name": "正式同分钟",
                "strategy_leg": "D",
                "status": "OK",
                "exit_date": "20250107",
                "account_return": 0.01,
                "source_rule": "FORMAL_D_BASELINE",
                "source_priority": 0,
                "queue_price_confirmed": True,
            },
        ]
    )

    outcomes, chosen = merge_picks_with_baseline(candidate, baseline)

    assert chosen.set_index("trade_date").loc["20250102", "ts_code"] == "000001.SZ"
    assert chosen.set_index("trade_date").loc["20250103", "ts_code"] == "600002.SH"
    assert set(outcomes["ts_code"]) == {"000001.SZ", "600002.SH"}


def test_unknown_early_queue_blocks_later_baseline_without_fake_fill() -> None:
    candidate = pd.DataFrame(
        [
            {
                "trade_date": "20250102",
                "signal_hhmm": 1330,
                "event_id": 1,
                "ts_code": "000001.SZ",
                "name": "早回封未知队列",
                "queue_price_confirmed": False,
                "execution_status": "OK",
                "exit_date": "20250106",
                "account_return": 0.02,
            }
        ]
    )
    baseline = pd.DataFrame(
        [
            {
                "trade_date": "20250102",
                "signal_date": "20250102",
                "signal_hhmm": 1400,
                "event_id": 10,
                "ts_code": "600001.SH",
                "name": "正式晚",
                "strategy_leg": "D",
                "status": "OK",
                "exit_date": "20250106",
                "account_return": 0.01,
                "source_rule": "FORMAL_D_BASELINE",
                "source_priority": 0,
                "queue_price_confirmed": True,
            }
        ]
    )

    outcomes, chosen = merge_picks_with_baseline(candidate, baseline)

    assert chosen.iloc[0]["ts_code"] == "000001.SZ"
    assert outcomes.empty
