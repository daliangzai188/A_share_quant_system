from __future__ import annotations

import json
from pathlib import Path
import unittest

import pandas as pd

from scripts.generate_live_limit_pool_daily_ops import add_runtime_buckets, select_candidates
from scripts.run_paper_ab_filtered_daily_ops import condition_strategy_config
from src.paper_candidate_generator import PaperCandidateGenerator


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "strategy_config.json"


class StrategyAFdExpansionLiveChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def candidate(
        ts_code: str,
        fd_bucket: str,
        limit_times: int,
        *,
        board_type: str = "multi_open",
        first_time_bucket: str = "midday",
    ) -> dict[str, object]:
        fd_ratio = {
            "0_1pct_0_3pct": 0.002,
            "0_3pct_0_5pct": 0.004,
            "0_5pct_1pct": 0.007,
            "1pct_2pct": 0.015,
        }[fd_bucket]
        return {
            "trade_date": "20260824",
            "ts_code": ts_code,
            "name": "测试股",
            "market_segment": "sz_main",
            "is_st": False,
            "allow_buy_reliable": True,
            "is_fill_score_reliable": True,
            "fill_probability": 0.9,
            "fd_amount_to_circ_mv": fd_ratio,
            "segment_limit_up_count_bucket": "lt_5",
            "market_chain_count_bucket": "8_15",
            "fd_ratio_bucket": fd_bucket,
            "board_type": board_type,
            "first_time_bucket": first_time_bucket,
            "amount_ratio_bucket": "1_2_2",
            "prev_pct_chg_bucket": "3_5",
            "limit_times": limit_times,
            "turnover_rate": 10.0,
            "amount": 100000.0,
        }

    def test_primary_generator_accepts_both_frozen_a_profiles(self) -> None:
        generator = PaperCandidateGenerator.__new__(PaperCandidateGenerator)
        generator.config = self.config
        frame = pd.DataFrame([
            self.candidate("000001.SZ", "0_5pct_1pct", 2),
            self.candidate("000002.SZ", "0_3pct_0_5pct", 3),
            self.candidate("000003.SZ", "0_1pct_0_3pct", 4),
        ])

        selected = generator.apply_include_conditions(frame)

        self.assertEqual(selected["ts_code"].tolist(), ["000001.SZ", "000002.SZ"])
        self.assertEqual(selected["matched_condition_profile_ids"].tolist(), [
            "A_FD_0_5PCT_1PCT",
            "A_FD_0_3PCT_0_5PCT",
        ])

    def test_live_limit_pool_fallback_accepts_new_fd_branch(self) -> None:
        frame = pd.DataFrame([
            self.candidate("000002.SZ", "0_3pct_0_5pct", 3),
            self.candidate("000003.SZ", "0_1pct_0_3pct", 4),
        ])

        strategy_leg, selected = select_candidates(frame, self.config, top_n=10)

        self.assertEqual(strategy_leg, "LIVE_LIMIT_POOL_A")
        self.assertEqual(selected["ts_code"].tolist(), ["000002.SZ"])
        self.assertEqual(
            selected["matched_condition_profile_ids"].tolist(),
            ["A_FD_0_3PCT_0_5PCT"],
        )

    def test_primary_a_prevents_fd_1_2_fallback_from_competing(self) -> None:
        frame = pd.DataFrame([
            self.candidate("000001.SZ", "0_5pct_1pct", 1),
            self.candidate("000002.SZ", "1pct_2pct", 9),
        ])

        strategy_leg, selected = select_candidates(frame, self.config, top_n=10)

        self.assertEqual(strategy_leg, "LIVE_LIMIT_POOL_A")
        self.assertEqual(selected["ts_code"].tolist(), ["000001.SZ"])
        self.assertEqual(
            selected["matched_condition_profile_ids"].tolist(),
            ["A_FD_0_5PCT_1PCT"],
        )

    def test_fd_1_2_non_one_word_fills_only_when_primary_empty(self) -> None:
        frame = pd.DataFrame([
            self.candidate("000001.SZ", "1pct_2pct", 2, board_type="one_word"),
            self.candidate("000002.SZ", "1pct_2pct", 1, board_type="t_board"),
        ])

        strategy_leg, selected = select_candidates(frame, self.config, top_n=10)

        self.assertEqual(strategy_leg, "LIVE_LIMIT_POOL_A")
        self.assertEqual(selected["ts_code"].tolist(), ["000002.SZ"])
        self.assertEqual(
            selected["matched_condition_profile_ids"].tolist(),
            ["A_FD_1PCT_2PCT_NON_ONE_WORD_FALLBACK"],
        )

    def test_historical_generator_uses_same_daily_fallback_semantics(self) -> None:
        generator = PaperCandidateGenerator.__new__(PaperCandidateGenerator)
        generator.config = self.config
        frame = pd.DataFrame([
            self.candidate("000001.SZ", "0_5pct_1pct", 1),
            self.candidate("000002.SZ", "1pct_2pct", 9),
        ])
        second_date = self.candidate(
            "000003.SZ", "1pct_2pct", 2, board_type="t_board"
        )
        second_date["trade_date"] = "20260825"
        frame = pd.concat([frame, pd.DataFrame([second_date])], ignore_index=True)

        selected = generator.apply_strategy_filters(frame)

        self.assertEqual(
            set(zip(selected["trade_date"], selected["ts_code"])),
            {("20260824", "000001.SZ"), ("20260825", "000003.SZ")},
        )

    def test_c_config_copy_cannot_inherit_a_fallback(self) -> None:
        c_strategy = self.config["paper_ab_filtered_strategy"]["c_strategy"]
        c_config = condition_strategy_config(
            self.config,
            [],
            "C_TEST",
            condition_profiles=c_strategy["condition_profiles"],
        )

        self.assertNotIn(
            "fallback_when_primary_empty",
            c_config["candidate_filters"],
        )

    def test_afternoon_first_rank_is_empty_without_second_a_fallback(self) -> None:
        """A第一名命中下午门禁后，第二名不能被升级为A买入。"""

        frame = pd.DataFrame([
            self.candidate(
                "000001.SZ", "0_5pct_1pct", 5, first_time_bucket="afternoon"
            ),
            self.candidate(
                "000002.SZ", "0_5pct_1pct", 1, first_time_bucket="midday"
            ),
        ])

        strategy_leg, _selected = select_candidates(frame, self.config, top_n=10)

        self.assertEqual(strategy_leg, "LIVE_LIMIT_POOL_WATCH")

    def test_runtime_first_time_bucket_matches_formal_boundary(self) -> None:
        frame = pd.DataFrame({
            "trade_date": ["20260831"] * 5,
            "first_time": [93100, 93200, 120000, 141500, 143000],
            "limit_times": [1] * 5,
            "open_times": [1] * 5,
            "amount": [100000.0] * 5,
        })

        bucketed = add_runtime_buckets(frame)

        self.assertEqual(
            bucketed["first_time_bucket"].tolist(),
            ["open_limit", "early_morning", "midday", "afternoon", "late"],
        )


if __name__ == "__main__":
    unittest.main()
