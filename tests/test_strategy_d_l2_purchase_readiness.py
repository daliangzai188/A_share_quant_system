from __future__ import annotations

import unittest

from scripts.audit_strategy_d_l2_purchase_readiness import build_audit


class StrategyDL2PurchaseReadinessTest(unittest.TestCase):
    def test_current_permissions_fail_closed_and_purchase_waits_for_samples(self) -> None:
        audit = build_audit()

        self.assertFalse(
            audit["current_permissions"]["sufficient_for_strict_d_replay"]
        )
        self.assertEqual(
            audit["window"]["expected_exchange_day_file_count"], 1452
        )
        self.assertEqual(
            audit["current_permissions"]["qmt_three_market_read_only_probe"][
                "one_minute_available_count"
            ],
            3,
        )
        self.assertEqual(
            audit["current_permissions"]["qmt_three_market_read_only_probe"][
                "tick_available_count"
            ],
            0,
        )
        self.assertTrue(audit["purchase_decision"]["permission_missing"])
        self.assertFalse(audit["purchase_decision"]["buy_now"])
        self.assertEqual(
            audit["purchase_decision"]["status"],
            "DEFERRED_COMPLETE_MINUTE_RESEARCH_FIRST",
        )
        self.assertFalse(audit["certification_impact"]["formal_d_change_allowed"])
        self.assertFalse(
            audit["current_permissions"]["vendor_sample_content_gate"]["passed"]
        )

    def test_all_three_markets_are_required_before_payment(self) -> None:
        audit = build_audit()

        self.assertEqual(
            audit["prepayment_sample_gate"]["required_markets_each_date"],
            ["SSE", "SZSE", "BSE"],
        )
        self.assertEqual(
            audit["prepayment_sample_gate"]["decision"],
            "DO_NOT_PAY_UNTIL_ALL_REQUIRED_MARKETS_PASS",
        )
        self.assertEqual(
            audit["official_provider_findings"]["bse"]["status"],
            "EXACT_HISTORICAL_L2_PRODUCT_NOT_PUBLICLY_CONFIRMED",
        )
        self.assertFalse(
            audit["official_provider_findings"]["sse"][
                "historical_price_publicly_confirmed"
            ]
        )
        self.assertIn(
            "SSE_SZSE",
            audit["official_provider_findings"]["myquant"]["status"],
        )


if __name__ == "__main__":
    unittest.main()
