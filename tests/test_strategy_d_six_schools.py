from __future__ import annotations

import pandas as pd
import unittest

from scripts.research_strategy_d3_halfway_full_denominator import (
    ALLOWED_SIGNAL_FIELDS,
    add_market_context,
    rules as d3_rules,
    select_daily,
)
from scripts.research_strategy_d_six_schools import (
    AFTER_SIGNAL_FIELDS,
    SIGNAL_FIELDS,
    merge_with_d6,
    rules as other_school_rules,
)


class StrategyDSixSchoolsTest(unittest.TestCase):
    def test_all_frozen_rule_fields_are_signal_time_known(self) -> None:
        other = other_school_rules()
        d3 = d3_rules()
        self.assertEqual(len(other), 63)
        self.assertEqual(len(d3), 54)
        self.assertEqual({rule.style for rule in other}, {"D2", "D4", "D5"})
        for rule in other:
            self.assertLessEqual(set(rule.fields), SIGNAL_FIELDS)
            self.assertFalse(set(rule.fields) & AFTER_SIGNAL_FIELDS)
        for rule in d3:
            self.assertLessEqual(set(rule.fields), ALLOWED_SIGNAL_FIELDS)

    def test_unfilled_earlier_school_blocks_same_day_d6_fallback(self) -> None:
        d6 = pd.DataFrame(
            [
                {"signal_date": "20250102", "ts_code": "D6A", "status": "OK"},
                {"signal_date": "20250103", "ts_code": "D6B", "status": "OK"},
            ]
        )
        merged = merge_with_d6(
            pd.DataFrame(columns=d6.columns), {"20250102"}, d6
        )
        self.assertEqual(
            merged[["signal_date", "ts_code"]].to_dict("records"),
            [{"signal_date": "20250103", "ts_code": "D6B"}],
        )

    def test_d3_market_context_never_counts_future_signal_or_seal(self) -> None:
        events = pd.DataFrame(
            [
                {
                    "trade_date": "20250102",
                    "ts_code": "A",
                    "threshold": 0.07,
                    "signal_hhmm": 1000,
                    "market_segment": "sh_main",
                    "first_seal_hhmm": 1005,
                },
                {
                    "trade_date": "20250102",
                    "ts_code": "B",
                    "threshold": 0.07,
                    "signal_hhmm": 1010,
                    "market_segment": "sh_main",
                    "first_seal_hhmm": 0,
                },
            ]
        )
        result = add_market_context(events)
        self.assertEqual(result["market_same_threshold_hit_count"].tolist(), [1, 2])
        self.assertEqual(result["same_segment_threshold_hit_count"].tolist(), [1, 2])
        self.assertEqual(result["market_sealed_count"].tolist(), [0, 1])

    def test_d3_daily_pick_uses_earliest_then_signal_time_amount(self) -> None:
        rule = d3_rules()[0]
        events = pd.DataFrame(
            [
                {
                    "trade_date": "20250102",
                    "ts_code": "B",
                    "threshold": 0.07,
                    "signal_hhmm": 1001,
                    "recent_3m_amount_vs_prev_day": 0.20,
                },
                {
                    "trade_date": "20250102",
                    "ts_code": "A",
                    "threshold": 0.07,
                    "signal_hhmm": 1000,
                    "recent_3m_amount_vs_prev_day": 0.01,
                },
                {
                    "trade_date": "20250102",
                    "ts_code": "C",
                    "threshold": 0.07,
                    "signal_hhmm": 1000,
                    "recent_3m_amount_vs_prev_day": 0.10,
                },
            ]
        )
        picked = select_daily(events, rule)
        self.assertEqual(picked.iloc[0]["ts_code"], "C")


if __name__ == "__main__":
    unittest.main()
