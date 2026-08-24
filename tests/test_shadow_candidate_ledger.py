from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.shadow_candidate_ledger import collect_signal_date, load_release, upsert_ledger


class ShadowCandidateLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "data" / "raw").mkdir(parents=True)
        release = {
            "schema_version": 1,
            "status": "FROZEN",
            "release_id": "test-release",
            "oos_start_date": "20260105",
            "strategy_priority_order": ["A", "C", "E", "D"],
        }
        (self.root / "config" / "strategy_release_freeze.json").write_text(
            json.dumps(release), encoding="utf-8"
        )
        config = {
            "analysis": {
                "commission_rate": 0.0003,
                "stamp_tax_rate": 0.001,
                "transfer_fee_rate": 0.00001,
                "slippage_rate": 0.001,
            },
            "live_performance_report": {"minimum_commission": 5.0},
            "portfolio_certification": {"initial_equity": 500000},
        }
        (self.root / "config" / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (self.root / "config" / "strategy_e_r1_scenarios.json").write_text(
            json.dumps({
                "exit_rules": {
                    "fixed_t2_close": {"hold_offset": 2},
                    "fixed_hold3_close": {"hold_offset": 3},
                }
            }),
            encoding="utf-8",
        )
        pd.DataFrame({
            "cal_date": [20260105, 20260106, 20260107, 20260108, 20260109],
            "is_open": [1, 1, 1, 1, 1],
        }).to_csv(self.root / "data" / "raw" / "trade_calendar.csv", index=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _csv(self, relative: str, rows: list[dict]) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(path, index=False)

    def _install_candidates(self) -> None:
        base = "reports/paper_trade/ab_filtered_daily_ops/a_strict_plus_c_hold3_20260105"
        self._csv(base + "_a_candidates.csv", [{
            "candidate_rank": 1, "ts_code": "000003.SZ", "name": "A股",
            "planned_position_pct": 0.825, "selection_reason": "A通过",
        }])
        self._csv(base + "_c_candidates.csv", [{
            "candidate_rank": 1, "ts_code": "000006.SZ", "name": "C股",
            "planned_position_pct": 0.825, "selection_reason": "C通过",
        }])
        self._csv("reports/strategy_e/e_signal_20260105_candidates.csv", [{
            "ts_code": "000005.SZ", "name": "E股", "exit_rule": "fixed_hold3_close",
        }])
        self._csv("reports/strategy_d/intraday_signals_20260105.csv", [{
            "signal_type": "BUY", "ts_code": "000001.SZ", "name": "D股",
            "upper_limit": 11.0, "source": "测试", "filled_qty": 1000,
            "filled_amount": 11000, "order_status": "FILLED",
        }])

    def _install_daily(self) -> None:
        codes = [f"00000{i}.SZ" for i in range(1, 8)]
        for date, open_price, close_price in [
            ("20260105", 10.0, 11.0),
            ("20260106", 10.0, 10.5),
            ("20260107", 10.6, 11.0),
            ("20260108", 11.0, 11.5),
        ]:
            rows = [{
                "ts_code": code, "trade_date": date, "open": open_price,
                "high": max(open_price, close_price), "low": min(open_price, close_price),
                "close": close_price, "pre_close": 10.0, "limit_pct": 0.10,
            } for code in codes]
            self._csv(f"data/processed/daily_merged_by_date/{date}.csv", rows)

    def test_collects_all_four_legs_and_keeps_priority_counterfactual(self) -> None:
        self._install_candidates()
        release = load_release(self.root)
        rows = collect_signal_date(self.root, release, "20260105")
        self.assertEqual([row["strategy_leg"] for row in rows], ["A", "C", "E", "D"])
        self.assertTrue(all(row["candidate_status"] == "CANDIDATE" for row in rows))
        self.assertFalse(any(row["account_empty_winner"] for row in rows))
        self.assertTrue(next(row for row in rows if row["strategy_leg"] == "D")["live_selected"])

    def test_upsert_is_idempotent_and_resolves_conservative_returns(self) -> None:
        self._install_candidates()
        self._install_daily()
        release = load_release(self.root)
        rows = collect_signal_date(self.root, release, "20260105")
        first = upsert_ledger(self.root, rows)
        second = upsert_ledger(self.root, rows)
        self.assertEqual(len(first), 4)
        self.assertEqual(len(second), 4)
        self.assertTrue(second["counterfactual_status"].eq("RESOLVED").all())
        self.assertTrue(second["account_net_return"].astype(float).notna().all())
        e_row = second[second["strategy_leg"].eq("E")].iloc[0]
        self.assertEqual(str(e_row["planned_exit_date"]), "20260108")
        self.assertIn("fixed_hold3_close", str(e_row["exit_rule"]))
        winners = second[second["account_empty_winner"].astype(bool)]
        self.assertEqual(
            set(zip(winners["planned_buy_date"].astype(str), winners["strategy_leg"])),
            {("20260105", "D"), ("20260106", "A")},
        )
        self.assertGreater(
            float(second.loc[second["strategy_leg"].eq("A"), "account_net_return"].iloc[0]),
            0.0,
        )
        self.assertEqual(
            (self.root / "reports" / "oos_shadow" / "shadow_candidates.csv").exists(),
            True,
        )

    def test_d_not_monitored_is_not_mislabeled_as_no_candidate(self) -> None:
        release = load_release(self.root)
        rows = collect_signal_date(self.root, release, "20260105")
        d = next(row for row in rows if row["strategy_leg"] == "D")
        self.assertEqual(d["candidate_status"], "NOT_OBSERVED")
        self.assertEqual(d["source_status"], "NOT_MONITORED")

    def test_pre_release_date_is_never_added(self) -> None:
        release = load_release(self.root)
        self.assertEqual(collect_signal_date(self.root, release, "20260102"), [])

    def test_first_final_candidate_is_immutable(self) -> None:
        self._install_candidates()
        release = load_release(self.root)
        rows = collect_signal_date(self.root, release, "20260105")
        upsert_ledger(self.root, rows)
        path = self.root / "reports/strategy_e/e_signal_20260105_candidates.csv"
        pd.DataFrame([{"ts_code": "999999.SZ", "name": "事后改票"}]).to_csv(path, index=False)
        changed = collect_signal_date(self.root, release, "20260105")
        ledger = upsert_ledger(self.root, changed)
        actual = ledger.loc[ledger["strategy_leg"].eq("E"), "ts_code"].iloc[0]
        self.assertEqual(actual, "000005.SZ")


if __name__ == "__main__":
    unittest.main()
