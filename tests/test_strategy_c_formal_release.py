"""锁定C_LEADER_RANK23_LD_LT30_20260630_V12的正式OR筛选和审计字段。"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

import pandas as pd

from scripts.run_paper_ab_filtered_daily_ops import (
    condition_strategy_config,
    configured_c_condition_profiles,
    configured_c_conditions,
)
from src.paper_candidate_generator import PaperCandidateGenerator


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config/strategy_config.json"


class StrategyCFormalReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cls.profiles = configured_c_condition_profiles(cls.config)

    def test_release_has_exactly_five_frozen_profiles(self) -> None:
        self.assertEqual(configured_c_conditions(self.config), [])
        self.assertEqual(
            [profile["profile_id"] for profile in self.profiles],
            [
                "C_CORE_REFINEMENT_1100_1330_MULTI_OPEN",
                "C_STRONG_LEADER_RANK4_10_FD01_03",
                "C_STRONG_LEADER_RANK2_3_FD01_03_LD_LT5",
                "C_STRONG_LEADER_RANK2_3_FD01_03_LD_5_15",
                "C_STRONG_LEADER_RANK2_3_FD01_03_LD_15_30",
            ],
        )

    def test_profile_internal_and_external_or_semantics(self) -> None:
        configured = condition_strategy_config(
            self.config,
            [],
            "test_c_union",
            condition_profiles=self.profiles,
        )
        generator = PaperCandidateGenerator.__new__(PaperCandidateGenerator)
        generator.config = configured
        frame = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "market_chain_count_bucket": "15_30",
                    "segment_limit_up_count_bucket": "40_80",
                    "first_time_detail_bucket": "1100_1330",
                    "board_type": "multi_open",
                    "limit_up_count_bucket": "30_50",
                    "market_leader_rank_bucket": "rank_gt_30",
                    "fd_ratio_bucket": "lt_0_1pct",
                },
                {
                    "ts_code": "000002.SZ",
                    "market_chain_count_bucket": "3_8",
                    "segment_limit_up_count_bucket": "5_10",
                    "first_time_detail_bucket": "before_1000",
                    "board_type": "one_word",
                    "limit_up_count_bucket": "50_80",
                    "market_leader_rank_bucket": "rank_4_10",
                    "fd_ratio_bucket": "0_1pct_0_3pct",
                },
                {
                    "ts_code": "000003.SZ",
                    "market_chain_count_bucket": "15_30",
                    "segment_limit_up_count_bucket": "40_80",
                    "first_time_detail_bucket": "1330_1430",
                    "board_type": "multi_open",
                    "limit_up_count_bucket": "50_80",
                    "market_leader_rank_bucket": "rank_11_30",
                    "fd_ratio_bucket": "0_1pct_0_3pct",
                    "market_limit_down_count_bucket": "30_60",
                },
                {
                    "ts_code": "000004.SZ",
                    "market_chain_count_bucket": "3_8",
                    "segment_limit_up_count_bucket": "5_10",
                    "first_time_detail_bucket": "before_1000",
                    "board_type": "multi_open",
                    "limit_up_count_bucket": "50_80",
                    "market_leader_rank_bucket": "rank_2_3",
                    "fd_ratio_bucket": "0_1pct_0_3pct",
                    "market_limit_down_count_bucket": "15_30",
                },
            ]
        )

        selected = generator.apply_include_conditions(frame)

        self.assertEqual(
            selected["ts_code"].tolist(),
            ["000001.SZ", "000002.SZ", "000004.SZ"],
        )
        self.assertEqual(selected["matched_condition_profile_ids"].tolist(), [
            "C_CORE_REFINEMENT_1100_1330_MULTI_OPEN",
            "C_STRONG_LEADER_RANK4_10_FD01_03",
            "C_STRONG_LEADER_RANK2_3_FD01_03_LD_15_30",
        ])

    def test_release_audit_locks_double_compound_gate(self) -> None:
        audit = self.config["paper_ab_filtered_strategy"]["c_strategy"]["latest_2y_audit"]
        self.assertEqual(
            audit["formal_release_decision"],
            "V12_C_E_D_BUNDLE_GATE_PASSED_USER_APPROVED",
        )
        self.assertEqual(audit["c_trade_count"], 58)
        self.assertAlmostEqual(audit["c_equity_multiple"], 16.592266587212748)
        self.assertAlmostEqual(
            audit["released_acde_equity_multiple"], 6046.316593512633
        )
        self.assertEqual(
            sum(audit["branch_plan_counts_before_standalone_occupancy"].values()),
            73,
        )


if __name__ == "__main__":
    unittest.main()
