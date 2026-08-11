from __future__ import annotations

import copy
import unittest

from src.broker_exit_evidence import (
    BrokerExitEvidenceError,
    apply_broker_evidence_plan,
    build_broker_evidence_plan,
)


class BrokerExitEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.positions = [
            {
                "order_id": "auction",
                "buy_date": "20260728",
                "ts_code": "001358.SZ",
                "name": "兴欣新材",
                "signal_date": "20260727",
                "strategy_leg": "E2",
                "entry_shares": 2000,
                "shares": 2000,
                "status": "closed",
                "sell_date": "20260729",
                "sell_price": 0.0,
                "exit_fills_by_date": {},
            },
            {
                "order_id": "pov",
                "buy_date": "20260728",
                "ts_code": "001358.SZ",
                "name": "兴欣新材",
                "signal_date": "20260727",
                "strategy_leg": "E2",
                "entry_shares": 4100,
                "shares": 4100,
                "status": "closed",
                "sell_date": "20260729",
                "sell_price": 0.0,
                "exit_fills_by_date": {},
            },
        ]
        self.record = {
            "evidence_id": "screenshot-001358",
            "entry_date": "20260728",
            "ts_code": "001358.SZ",
            "name": "兴欣新材",
            "strategy_leg": "E2",
            "signal_date": "20260727",
            "exit_date": "20260729",
            "exit_time": "09:35:15",
            "filled_qty": 6100,
            "displayed_fill_price": 26.980,
            "fill_amount": 164578.00,
            "fee": 96.34,
            "net_sell_amount": 164481.66,
            "source": "同花顺App截图",
        }

    def test_split_positions_preserve_exact_group_amount(self) -> None:
        original = copy.deepcopy(self.positions)
        plans = build_broker_evidence_plan(self.positions, [self.record])
        updated = apply_broker_evidence_plan(
            self.positions, plans, applied_at="2026-08-11T17:00:00+08:00"
        )
        self.assertEqual(self.positions, original)
        self.assertEqual(sum(row["entry_shares"] for row in updated), 6100)
        self.assertEqual(
            sum(row["exit_fills_by_date"]["20260729"]["amount"] for row in updated),
            164578.00,
        )
        self.assertTrue(all(row["shares"] == 0 for row in updated))
        self.assertTrue(all(row["sell_price"] == 26.98 for row in updated))
        self.assertTrue(
            all(
                row["manual_exit_evidence"]["broker_order_id_status"]
                == "NOT_VISIBLE_IN_SCREENSHOT"
                for row in updated
            )
        )

    def test_displayed_price_rounding_can_differ_from_exact_amount(self) -> None:
        position = [{
            "buy_date": "20260623", "ts_code": "002014.SZ", "strategy_leg": "D",
            "signal_date": "20260623", "shares": 4300, "status": "closed",
            "sell_date": "20260624", "sell_price": 0.0,
        }]
        record = {
            "entry_date": "20260623", "ts_code": "002014.SZ", "strategy_leg": "D",
            "signal_date": "20260623", "exit_date": "20260624", "filled_qty": 4300,
            "displayed_fill_price": 10.932, "displayed_price_decimals": 3,
            "fill_amount": 47007.00, "fee": 34.33, "net_sell_amount": 46972.67,
        }
        plan = build_broker_evidence_plan(position, [record])
        updated = apply_broker_evidence_plan(position, plan, applied_at="now")
        self.assertEqual(updated[0]["exit_fills_by_date"]["20260624"]["amount"], 47007.0)
        self.assertAlmostEqual(updated[0]["sell_price"] * 4300, 47007.0)

    def test_quantity_mismatch_is_rejected(self) -> None:
        record = dict(self.record)
        record["filled_qty"] = 6000
        record["fill_amount"] = 161880.00
        record["net_sell_amount"] = 161783.66
        with self.assertRaisesRegex(BrokerExitEvidenceError, "数量"):
            build_broker_evidence_plan(self.positions, [record])

    def test_fee_mismatch_is_rejected(self) -> None:
        record = dict(self.record)
        record["net_sell_amount"] = 1.0
        with self.assertRaisesRegex(BrokerExitEvidenceError, "税费"):
            build_broker_evidence_plan(self.positions, [record])

    def test_open_position_is_rejected(self) -> None:
        positions = copy.deepcopy(self.positions)
        positions[0]["status"] = "open"
        with self.assertRaisesRegex(BrokerExitEvidenceError, "尚未全部平仓"):
            build_broker_evidence_plan(positions, [self.record])


if __name__ == "__main__":
    unittest.main()
