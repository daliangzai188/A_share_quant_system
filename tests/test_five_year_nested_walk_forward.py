from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.five_year_research import FiveYearResearchDatasetBuilder
from src.nested_walk_forward import NestedWalkForwardResearch, return_metrics


class FiveYearNestedWalkForwardTests(unittest.TestCase):
    def test_research_config_cannot_release_live(self) -> None:
        config = json.loads(
            Path("config/five_year_strategy_research.json").read_text(encoding="utf-8")
        )
        self.assertEqual(config["mode"], "research_only")
        self.assertFalse(config["live_release_allowed"])
        for leg in ("D", "A", "E", "C"):
            baseline = [
                item
                for item in config["variants"]
                if item["leg"] == leg and item["id"].endswith("CURRENT_BASELINE")
            ]
            self.assertEqual(len(baseline), 1)
            self.assertFalse(baseline[0]["eligible_for_optimization"])
        d_baseline = next(
            item for item in config["variants"] if item["id"] == "D_CURRENT_BASELINE"
        )
        # 当前D用整个元组descending，完全同分时ts_code也是降序。
        self.assertEqual(d_baseline["rank_ascending"], [False, False, False])

    def test_research_builder_rejects_live_processed_directory(self) -> None:
        with self.assertRaisesRegex(ValueError, "完全隔离"):
            FiveYearResearchDatasetBuilder(research_root="data/processed")

    def test_post_gate_does_not_fallback_to_second_candidate(self) -> None:
        research = NestedWalkForwardResearch.__new__(NestedWalkForwardResearch)
        pool = pd.DataFrame(
            [
                {
                    "trade_date": "20240102",
                    "ts_code": "000001.SZ",
                    "name": "第一名",
                    "allow_buy_reliable": True,
                    "is_fill_score_reliable": True,
                    "is_fd_amount_abnormal": False,
                    "strategy_compatible": True,
                    "is_st": False,
                    "amount": 200.0,
                    "first_time_detail_bucket": "1330_1430",
                },
                {
                    "trade_date": "20240102",
                    "ts_code": "000002.SZ",
                    "name": "第二名",
                    "allow_buy_reliable": True,
                    "is_fill_score_reliable": True,
                    "is_fd_amount_abnormal": False,
                    "strategy_compatible": True,
                    "is_st": False,
                    "amount": 100.0,
                    "first_time_detail_bucket": "before_1000",
                },
            ]
        )
        variant = {
            "id": "NO_FALLBACK",
            "rank_columns": ["amount", "ts_code"],
            "rank_ascending": [False, True],
            "post_gate_excludes": {"first_time_detail_bucket": ["1330_1430"]},
        }
        selected = research.select_generic(pool, variant)
        self.assertTrue(selected.empty)

    @staticmethod
    def _variant_trades(variant_id: str, train_return: float, test_return: float) -> pd.DataFrame:
        rows = []
        for year in (2019, 2020, 2021):
            for month in range(1, 5):
                rows.append(
                    {
                        "trade_date": f"{year}{month:02d}15",
                        "strategy_leg": "D",
                        "variant_id": variant_id,
                        "status": "OK",
                        "account_return": train_return,
                    }
                )
        rows.append(
            {
                "trade_date": "20220315",
                "strategy_leg": "D",
                "variant_id": variant_id,
                "status": "OK",
                "account_return": test_return,
            }
        )
        return pd.DataFrame(rows)

    def test_outer_test_returns_cannot_change_variant_selection(self) -> None:
        research = NestedWalkForwardResearch.__new__(NestedWalkForwardResearch)
        research.outer_years = [2022]
        research.variants = [
            {"id": "D_ONE", "leg": "D", "eligible_for_optimization": True},
            {"id": "D_TWO", "leg": "D", "eligible_for_optimization": True},
        ]
        research.config = {
            "selection_gate": {
                "minimum_train_trades": 12,
                "minimum_inner_validation_trades": 2,
                "minimum_train_years_with_trades": 2,
                "minimum_positive_train_year_ratio": 0.5,
                "minimum_train_equity_multiple": 1.0,
                "minimum_inner_validation_equity_multiple": 0.9,
                "maximum_train_drawdown": -0.35,
            }
        }
        one = self._variant_trades("D_ONE", 0.02, -0.9)
        two = self._variant_trades("D_TWO", 0.01, 5.0)

        def choose(all_trades: dict[str, pd.DataFrame]) -> str:
            candidates = []
            for item in research.variants:
                score = research.score_train_candidate(
                    all_trades[item["id"]], test_year=2022
                )
                if score["selection_gate_passed"]:
                    candidates.append((score["selection_score"], item["id"]))
            return sorted(candidates, key=lambda value: (-value[0], value[1]))[0][1]

        first = choose({"D_ONE": one, "D_TWO": two})
        one.loc[one["trade_date"].str.startswith("2022"), "account_return"] = 10.0
        two.loc[two["trade_date"].str.startswith("2022"), "account_return"] = -0.99
        second = choose({"D_ONE": one, "D_TWO": two})
        self.assertEqual(first, "D_ONE")
        self.assertEqual(second, "D_ONE")

    def test_return_metrics_include_required_risk_fields(self) -> None:
        metrics = return_metrics([0.1, -0.05, 0.02], bootstrap=True)
        self.assertEqual(metrics["trade_count"], 3)
        self.assertIn("max_drawdown", metrics)
        self.assertIn("profit_loss_ratio", metrics)
        self.assertIn("win_rate_wilson_95_lower", metrics)
        self.assertIn("avg_return_bootstrap_95_lower", metrics)


if __name__ == "__main__":
    unittest.main()
