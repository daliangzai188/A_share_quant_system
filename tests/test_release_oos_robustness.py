from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.release_oos_robustness import evaluate_release_oos, write_release_oos_report


class ReleaseOosRobustnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "reports/oos_shadow").mkdir(parents=True)
        release = {
            "schema_version": 1,
            "status": "FROZEN",
            "release_id": "release-new",
            "oos_start_date": "20260105",
            "strategy_priority_order": ["D", "A", "M", "E", "C"],
        }
        (self.root / "config/strategy_release_freeze.json").write_text(json.dumps(release), encoding="utf-8")
        config = {
            "analysis": {"commission_rate": 0.0003, "stamp_tax_rate": 0.001, "transfer_fee_rate": 0.00001},
            "live_performance_report": {"minimum_samples_for_decision": 20, "minimum_commission": 5.0},
        }
        (self.root / "config/config.json").write_text(json.dumps(config), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_ledger(self) -> None:
        rows = []
        for day_index, date in enumerate(["20260105", "20260106", "20260107"]):
            winner_return = [0.02, -0.01, 0.03][day_index]
            challenger_return = [0.03, 0.00, 0.04][day_index]
            rows.extend([
                {
                    "release_id": "release-new", "signal_date": date, "strategy_leg": "A",
                    "priority_rank": 2, "candidate_status": "CANDIDATE", "counterfactual_status": "RESOLVED",
                    "account_empty_winner": True, "live_selected": False, "account_net_return": winner_return,
                },
                {
                    "release_id": "release-new", "signal_date": date, "strategy_leg": "C",
                    "priority_rank": 3, "candidate_status": "CANDIDATE", "counterfactual_status": "RESOLVED",
                    "account_empty_winner": False, "live_selected": day_index == 0, "account_net_return": challenger_return,
                },
            ])
            for rank, leg in enumerate(["D", "M", "E"], start=1):
                rows.append({
                    "release_id": "release-new", "signal_date": date, "strategy_leg": leg,
                    "priority_rank": rank, "candidate_status": "NO_CANDIDATE", "counterfactual_status": "NOT_APPLICABLE",
                    "account_empty_winner": False, "live_selected": False, "account_net_return": "",
                })
        # 旧发布和OOS起点之前的高收益不得混入。
        rows.extend([
            {"release_id": "release-old", "signal_date": "20260106", "strategy_leg": "D", "priority_rank": 1,
             "candidate_status": "CANDIDATE", "counterfactual_status": "RESOLVED", "account_empty_winner": True,
             "live_selected": True, "account_net_return": 9.0},
            {"release_id": "release-new", "signal_date": "20260102", "strategy_leg": "D", "priority_rank": 1,
             "candidate_status": "CANDIDATE", "counterfactual_status": "RESOLVED", "account_empty_winner": True,
             "live_selected": True, "account_net_return": 9.0},
        ])
        pd.DataFrame(rows).to_csv(self.root / "reports/oos_shadow/shadow_candidates.csv", index=False)

    def test_filters_exact_release_and_oos_start(self) -> None:
        self._write_ledger()
        result = evaluate_release_oos(self.root)
        self.assertEqual(result["status"], "EARLY_OBSERVATION")
        self.assertEqual(len(result["ledger"]), 15)
        self.assertEqual(int(result["overall"].iloc[1]["sample_count"]), 3)
        self.assertAlmostEqual(float(result["overall"].iloc[1]["avg_return"]), 0.0133333333)
        self.assertEqual(int(result["pairs"].loc[result["pairs"]["challenger_leg"].eq("C"), "paired_sample_count"].iloc[0]), 3)
        self.assertEqual(
            result["pairs"].loc[result["pairs"]["challenger_leg"].eq("C"), "priority_change_evidence"].iloc[0],
            "INSUFFICIENT_OR_NO_EDGE",
        )

    def test_writes_all_report_artifacts_without_enforcing_gate(self) -> None:
        self._write_ledger()
        status = write_release_oos_report(self.root)
        self.assertEqual(status["optimization_decision"], "HOLD_RELEASE")
        self.assertFalse(status["live_gate_enforced"])
        output = self.root / "reports/oos_evaluation"
        for name in (
            "release_oos_metrics.csv", "release_oos_by_leg.csv",
            "release_oos_priority_pairs.csv", "release_oos_coverage.csv",
            "release_oos_status.json", "release_oos_report.md",
        ):
            self.assertTrue((output / name).exists(), name)

    def test_no_sample_does_not_make_false_pass_claim(self) -> None:
        status = write_release_oos_report(self.root)
        self.assertEqual(status["status"], "NO_SAMPLE")
        self.assertEqual(status["optimization_decision"], "HOLD_RELEASE")
        self.assertEqual(status["priority_winner_resolved_count"], 0)


if __name__ == "__main__":
    unittest.main()
