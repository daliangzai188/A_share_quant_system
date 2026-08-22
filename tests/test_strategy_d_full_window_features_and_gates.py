from __future__ import annotations

import json
from pathlib import Path
import unittest

import pandas as pd

from scripts.research_strategy_d_full_window_features_and_gates import (
    CandidateRule,
    FUTURE_OR_NON_ASOF_FIELDS,
    SIGNAL_TIME_ALLOWED_FIELDS,
    candidate_rules,
    d_outcome_frame,
    sealed_state_at,
    select_daily_first,
    trading_minutes_between,
)


class StrategyDFullWindowFeaturesAndGatesTest(unittest.TestCase):
    def test_generated_full_window_reports_freeze_no_update_decision(self) -> None:
        root = Path(__file__).resolve().parents[1]
        replay = json.loads(
            (root / "reports/strategy_d_intraday_research/full_window_summary.json")
            .read_text(encoding="utf-8")
        )
        research = json.loads(
            (root / "reports/strategy_d_full_window_features/summary.json")
            .read_text(encoding="utf-8")
        )

        self.assertTrue(
            replay["certification"]["full_window_replay_fail_closed_passed"]
        )
        self.assertEqual(replay["minute_data"]["known_vendor_gap_count"], 4)
        self.assertEqual(replay["minute_data"]["known_price_mismatch_count"], 4)
        self.assertEqual(research["candidate_search"]["rule_count"], 47)
        self.assertEqual(research["candidate_search"]["dual_gate_pass_count"], 0)
        self.assertFalse(research["formal_rule_modified"])
        self.assertEqual(
            research["formal_decision"],
            "KEEP_CURRENT_D_NO_CANDIDATE_PASSED_BOTH_COMPOUND_GATES",
        )

    def test_candidate_rules_never_use_future_or_non_asof_fields(self) -> None:
        for rule in candidate_rules():
            self.assertTrue(set(rule.fields).issubset(SIGNAL_TIME_ALLOWED_FIELDS))
            self.assertFalse(set(rule.fields) & FUTURE_OR_NON_ASOF_FIELDS)

    def test_intraday_state_uses_only_events_at_or_before_signal(self) -> None:
        events = [
            {"hhmm": 1000, "event_type": "FIRST_SEAL"},
            {"hhmm": 1100, "event_type": "LIMIT_OPEN_BREAK"},
            {"hhmm": 1405, "event_type": "RESEAL"},
            {"hhmm": 1410, "event_type": "LIMIT_OPEN_BREAK"},
        ]

        self.assertEqual(sealed_state_at(events, 1405), (True, 1))
        self.assertEqual(sealed_state_at(events, 1410), (False, 2))

    def test_daily_selection_chooses_earliest_qualifying_signal(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "trade_date": "20250102",
                    "ts_code": "000002.SZ",
                    "eligible_signal_hhmm": 1410,
                    "open_times_at_signal": 3,
                    "signal_recent_5m_amount_vs_prev_day": 0.50,
                },
                {
                    "trade_date": "20250102",
                    "ts_code": "000001.SZ",
                    "eligible_signal_hhmm": 1405,
                    "open_times_at_signal": 2,
                    "signal_recent_5m_amount_vs_prev_day": 0.01,
                },
            ]
        )
        rule = CandidateRule(
            "all",
            "all",
            tuple(),
            lambda value: pd.Series(True, index=value.index),
        )

        selected = select_daily_first(frame, rule)

        self.assertEqual(selected.iloc[0]["ts_code"], "000001.SZ")

    def test_queue_evidence_is_applied_after_daily_selection(self) -> None:
        picks = pd.DataFrame(
            [
                {
                    "trade_date": "20250102",
                    "ts_code": "000001.SZ",
                    "name": "先到但队列未知",
                    "confirmed_fill_by_price": False,
                    "execution_status": "OK",
                    "exit_date": "20250106",
                    "account_return": 0.10,
                }
            ]
        )

        conservative = d_outcome_frame(picks, confirmed_only=True)
        optimistic = d_outcome_frame(picks, confirmed_only=False)

        self.assertTrue(conservative.empty)
        self.assertEqual(len(optimistic), 1)

    def test_trading_minutes_excludes_midday_break(self) -> None:
        self.assertEqual(trading_minutes_between(1129, 1301), 3)


if __name__ == "__main__":
    unittest.main()
