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
                "strategy_leg": "E",
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
                "strategy_leg": "E",
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
            "strategy_leg": "E",
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


class UnverifiedExitReplacementTest(unittest.TestCase):
    """2026-08-18共进股份卖出价按买入价19.26占位，必须能用券商记录显式替换。"""

    def setUp(self) -> None:
        self.positions = [{
            "order_id": "l-1", "buy_date": "20260817", "ts_code": "603118.SH", "name": "共进股份",
            "signal_date": "20260814", "strategy_leg": "L", "entry_shares": 11800, "shares": 0,
            "status": "closed", "sell_date": "20260818", "sell_price": 19.26,
            "exit_fills_by_date": {"20260818": {"qty": 11800, "amount": 227268.00000000003}},
        }]
        self.record = {
            "evidence_id": "broker-20260818-603118", "entry_date": "20260817", "ts_code": "603118.SH",
            "name": "共进股份", "strategy_leg": "L", "signal_date": "20260814", "exit_date": "20260818",
            "exit_time": "14:55:01", "filled_qty": 11800, "displayed_fill_price": 18.910,
            "fill_amount": 223138.00, "fee": 290.08, "net_sell_amount": 222847.92,
        }

    def test_recorded_amount_is_not_replaced_silently(self) -> None:
        with self.assertRaisesRegex(BrokerExitEvidenceError, "replaces_recorded_exit_amount"):
            build_broker_evidence_plan(self.positions, [self.record])

    def test_replacement_must_name_the_exact_recorded_amount(self) -> None:
        wrong = {**self.record, "replaces_recorded_exit_amount": 227000.00}
        with self.assertRaisesRegex(BrokerExitEvidenceError, "待替换金额与账上不一致"):
            build_broker_evidence_plan(self.positions, [wrong])

    def test_exact_replacement_writes_broker_amount_and_audit(self) -> None:
        record = {**self.record, "replaces_recorded_exit_amount": 227268.00}
        plans = build_broker_evidence_plan(self.positions, [record])
        updated = apply_broker_evidence_plan(self.positions, plans, applied_at="2026-10-03T12:00:00+08:00")
        self.assertEqual(updated[0]["exit_fills_by_date"], {"20260818": {"qty": 11800, "amount": 223138.0}})
        self.assertAlmostEqual(updated[0]["sell_price"], 18.91)
        self.assertEqual(updated[0]["manual_exit_evidence"]["replaced_recorded_exit_amount"], 227268.0)
        again = build_broker_evidence_plan(updated, [record])
        self.assertEqual(len(again), 1, "同一证据重复演练必须幂等")

    def test_replacement_field_rejected_when_nothing_recorded(self) -> None:
        self.positions[0]["exit_fills_by_date"] = {}
        record = {**self.record, "replaces_recorded_exit_amount": 227268.00}
        with self.assertRaisesRegex(BrokerExitEvidenceError, "没有可替换"):
            build_broker_evidence_plan(self.positions, [record])


if __name__ == "__main__":
    unittest.main()
