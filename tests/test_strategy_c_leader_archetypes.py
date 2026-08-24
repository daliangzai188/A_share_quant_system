"""锁定C强势龙头研究脚本的概念边界与机械选择语义。"""

from __future__ import annotations

import math
import unittest

import pandas as pd

from scripts.research_strategy_c_leader_archetypes import (
    LEADER_FACTORS,
    LEADER_VALUE_DOMAINS,
    MARKET_ENV_FACTORS,
    RANK_RULES,
    SEGMENT_ENV_FACTORS,
    STRONG_VALUE_DOMAINS,
    factor_sets,
    json_ready,
    select_profiles,
    six_month_segments,
)


class StrategyCLeaderArchetypeTests(unittest.TestCase):
    def test_semantic_factor_sets_always_contain_environment_and_leader(self) -> None:
        environments = set(MARKET_ENV_FACTORS) | set(SEGMENT_ENV_FACTORS)
        leaders = set(LEADER_FACTORS)
        sets = factor_sets()

        self.assertEqual(len(sets), 740)
        self.assertTrue(all(set(names) & environments for names in sets))
        self.assertTrue(all(set(names) & leaders for names in sets))
        self.assertTrue(all(2 <= len(names) <= 3 for names in sets))

    def test_frozen_domains_reject_weak_environment_and_pseudo_leader_values(self) -> None:
        self.assertNotIn("ice_point", STRONG_VALUE_DOMAINS["market_emotion_state_bucket"])
        self.assertNotIn("lt_20", STRONG_VALUE_DOMAINS["limit_up_count_bucket"])
        self.assertNotIn("rank_gt_30", LEADER_VALUE_DOMAINS["market_leader_rank_bucket"])
        self.assertIn("rank_1", LEADER_VALUE_DOMAINS["market_leader_rank_bucket"])
        self.assertIn("50_80", STRONG_VALUE_DOMAINS["limit_up_count_bucket"])

    def test_profile_union_filters_risk_then_ranks_and_falls_back_on_same_day(self) -> None:
        pool = pd.DataFrame(
            [
                {
                    "trade_date": "20250102", "ts_code": "000001.SZ",
                    "style": "A", "profit_source_score": 100.0,
                    "turnover_rate": 5.0, "amount": 100.0, "_risk_rejected": True,
                },
                {
                    "trade_date": "20250102", "ts_code": "000002.SZ",
                    "style": "B", "profit_source_score": 90.0,
                    "turnover_rate": 6.0, "amount": 90.0, "_risk_rejected": False,
                },
                {
                    "trade_date": "20250102", "ts_code": "000003.SZ",
                    "style": "A", "profit_source_score": 80.0,
                    "turnover_rate": 8.0, "amount": 80.0, "_risk_rejected": False,
                },
                {
                    "trade_date": "20250103", "ts_code": "000004.SZ",
                    "style": "X", "profit_source_score": 200.0,
                    "turnover_rate": 9.0, "amount": 200.0, "_risk_rejected": False,
                },
            ]
        )

        selected = select_profiles(
            pool,
            [{"style": "A"}, {"style": "B"}],
            RANK_RULES[0],
        )

        self.assertEqual(selected[["trade_date", "ts_code"]].values.tolist(), [
            ["20250102", "000002.SZ"]
        ])

    def test_two_year_window_is_split_into_exact_half_year_diagnostics(self) -> None:
        self.assertEqual(six_month_segments("20240630", "20260630"), [
            ("20240630", "20241231"),
            ("20250101", "20250630"),
            ("20250701", "20251231"),
            ("20260101", "20260630"),
        ])

    def test_json_ready_normalizes_numpy_style_nan(self) -> None:
        self.assertEqual(json_ready({"value": math.nan, "items": (1, 2)}), {
            "value": None,
            "items": [1, 2],
        })


if __name__ == "__main__":
    unittest.main()
