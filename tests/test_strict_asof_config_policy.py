from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.mechanical_compound import MECHANICAL_COMPOUND_STANDARD_ID
from src.strict_asof import STRICT_ASOF_STANDARD_ID


ROOT = Path(__file__).resolve().parents[1]


class StrictAsOfConfigPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads((ROOT / "config/config.json").read_text(encoding="utf-8"))

    def test_global_policy_is_active(self) -> None:
        policy = self.config["strict_asof"]
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["standard_id"], STRICT_ASOF_STANDARD_ID)
        self.assertTrue(policy["require_for_all_strategy_return_research"])
        self.assertTrue(policy["require_mechanical_compound"])
        self.assertEqual(
            policy["mechanical_compound_standard_id"],
            MECHANICAL_COMPOUND_STANDARD_ID,
        )
        self.assertEqual(
            policy["official_portfolio_certifier"],
            "scripts/certify_strict_asof_portfolio.py",
        )

    def test_all_shared_research_entrypoints_fail_closed_to_strict(self) -> None:
        for section_name in (
            "analysis",
            "candidate_pool",
            "backtest",
            "optimization",
            "exit_rule_optimization",
            "trade_replay",
        ):
            with self.subTest(section=section_name):
                section = self.config[section_name]
                self.assertEqual(section["asof_mode"], "STRICT")
                self.assertIn(
                    section["research_protocol"],
                    {"STRICT_DISCOVERY", "LOCKED_OOS", "WALK_FORWARD"},
                )

    def test_historical_research_uses_separate_asof_assets(self) -> None:
        analysis = self.config["analysis"]
        self.assertTrue(analysis["input_limit_up_fill_scored_path"].endswith("_asof.csv"))
        self.assertTrue(analysis["output_next_day_trades_path"].endswith("_asof.csv"))
        self.assertTrue(analysis["output_exit_rule_trades_path"].endswith("_asof.csv"))
        self.assertTrue(self.config["candidate_pool"]["output_candidate_pool_path"].endswith("_asof.csv"))

    def test_live_release_requires_formal_strict_asof_audit(self) -> None:
        certification = self.config["portfolio_certification"]
        self.assertTrue(certification["certification_require_strict_asof"])


if __name__ == "__main__":
    unittest.main()
