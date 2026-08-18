from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from src.execution_completion_tracker import ExecutionCompletionTracker


class ExecutionCompletionTrackerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "data" / "processed").mkdir(parents=True)
        (self.root / "reports").mkdir(parents=True)
        self.tracker = ExecutionCompletionTracker(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _position(order_id: str, shares: int, buy_price: float) -> dict:
        return {
            "order_id": order_id,
            "ts_code": "603161.SH",
            "name": "科华控股",
            "signal_date": "20260717",
            "buy_date": "20260720",
            "planned_exit_date": "20260721",
            "entry_shares": shares,
            "shares": 0,
            "buy_price": buy_price,
            "strategy_leg": "E",
            "status": "closed",
            "sell_date": "20260721",
            "sell_price": 14.51,
            "exit_fills_by_date": {
                "20260721": {"qty": shares, "amount": shares * 14.51}
            },
        }

    def _write_positions_and_audit(self) -> None:
        positions = [
            self._position("1090521385", 3400, 13.35),
            self._position("pov-20260720-603161.SH", 7400, 99729 / 7400),
        ]
        (self.root / "data" / "processed" / "positions.json").write_text(
            json.dumps(positions, ensure_ascii=False), encoding="utf-8"
        )
        audit_path = self.root / "reports" / "live_execution_audit.csv"
        with audit_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["order_id", "bench_open", "bench_close"],
            )
            writer.writeheader()
            writer.writerow({"order_id": "1090521385", "bench_open": 13.35, "bench_close": 14.52})
            writer.writerow({"order_id": "pov-20260720-603161.SH", "bench_open": 13.35, "bench_close": 14.52})

    def test_summary_uses_initial_target_and_position_ledger_as_authority(self) -> None:
        self._write_positions_and_audit()
        self.tracker.register_entry_plan(
            entry_date="20260720",
            ts_code="603161.SH",
            name="科华控股",
            strategy_leg="E",
            signal_date="20260717",
            target_qty=11200,
            target_amount=149072,
            reference_price=13.31,
            auction_planned_qty=3400,
            pov_planned_qty=7400,
            pov_target_amount=99072,
            planned_exit_date="20260721",
        )
        self.tracker.record_buy_slice(
            event_id="买入POV-1",
            entry_date="20260720",
            ts_code="603161.SH",
            name="科华控股",
            strategy_leg="E",
            signal_date="20260717",
            channel="买入POV",
            slice_no=1,
            order_id="pov-20260720-603161.SH",
            order_qty=7400,
            filled_qty=7400,
            fill_price=99729 / 7400,
            benchmark_open=13.35,
        )
        self.tracker.record_sell_slice(
            event_id="卖出POV-1",
            entry_date="20260720",
            exit_date="20260721",
            ts_code="603161.SH",
            name="科华控股",
            strategy_leg="E",
            signal_date="20260717",
            channel="卖出POV",
            local_order_id="pov-20260720-603161.SH",
            broker_order_id="SELL-1",
            order_qty=2000,
            filled_qty=2000,
            fill_price=14.51,
            remaining_qty=5400,
        )

        with self.tracker.summary_path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(int(row["entry_target_qty"]), 11200)
        self.assertEqual(row["entry_plan_source"], "LIVE_FROZEN")
        self.assertEqual(int(row["entry_filled_qty"]), 10800)
        self.assertEqual(int(row["entry_unfilled_qty"]), 400)
        self.assertEqual(float(row["auction_completion_pct"]), 100.0)
        self.assertEqual(float(row["pov_completion_pct"]), 100.0)
        self.assertAlmostEqual(float(row["entry_qty_completion_pct"]), 96.4286, places=4)
        self.assertAlmostEqual(float(row["entry_amount_completion_pct"]), 97.3483, places=4)
        self.assertEqual(int(row["pov_filled_qty"]), 7400)
        self.assertEqual(int(row["exit_filled_qty"]), 10800)
        self.assertEqual(int(row["exit_pov_filled_qty"]), 2000)
        self.assertEqual(int(row["exit_other_filled_qty"]), 8800)
        self.assertEqual(float(row["exit_completion_pct"]), 100.0)
        self.assertEqual(row["execution_status"], "已平仓")
        self.assertAlmostEqual(float(row["buy_slippage_bps"]), 65.1269, places=3)
        self.assertAlmostEqual(float(row["sell_slippage_bps"]), 6.8918, places=3)
        audit = self.tracker.mirror_existing_events()
        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["head_count_by_type"], {"BUY": 1, "PLAN": 1, "SELL": 1})

    def test_backfilled_plan_is_explicitly_excluded_from_capacity_evidence(self) -> None:
        self._write_positions_and_audit()
        self.tracker.register_entry_plan(
            entry_date="20260720",
            ts_code="603161.SH",
            name="科华控股",
            strategy_leg="E",
            signal_date="20260717",
            target_qty=10800,
            target_amount=145119,
            reference_price=13.44,
            planned_exit_date="20260721",
            status="已回填",
        )
        with self.tracker.summary_path.open("r", newline="", encoding="utf-8-sig") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["entry_plan_source"], "BACKFILLED")
        self.assertIn("历史持仓/容量档案回填", row["data_quality_note"])

    def test_same_slice_event_is_upserted_instead_of_double_counted(self) -> None:
        values = {
            "event_id": "SELL-ONE",
            "entry_date": "20260720",
            "exit_date": "20260721",
            "ts_code": "603161.SH",
            "name": "科华控股",
            "strategy_leg": "E",
            "signal_date": "20260717",
            "channel": "卖出POV",
            "local_order_id": "LOCAL-1",
            "broker_order_id": "QMT-1",
            "order_qty": 1000,
            "filled_qty": 400,
            "fill_price": 14.5,
        }
        self.tracker.record_sell_slice(**values)
        values["filled_qty"] = 600
        self.tracker.record_sell_slice(**values)

        with self.tracker.sell_path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["filled_qty"]), 600)

    def test_same_broker_order_live_and_legacy_events_are_not_double_counted(self) -> None:
        self._write_positions_and_audit()
        common = {
            "entry_date": "20260720",
            "exit_date": "20260721",
            "ts_code": "603161.SH",
            "name": "科华控股",
            "strategy_leg": "E",
            "signal_date": "20260717",
            "channel": "卖出POV",
            "local_order_id": "pov-20260720-603161.SH",
            "broker_order_id": "QMT-DUPLICATE-1",
            "order_qty": 2_000,
            "filled_qty": 2_000,
        }
        self.tracker.record_sell_slice(
            event_id="卖出POV|实时事件",
            fill_price=14.51,
            external_flow=1_000_000,
            **common,
        )
        self.tracker.record_sell_slice(
            event_id="历史退出|同一委托",
            fill_price=14.51,
            note="退出安全账本回填",
            **common,
        )

        with self.tracker.summary_path.open("r", newline="", encoding="utf-8-sig") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(int(row["exit_filled_qty"]), 10_800)
        self.assertEqual(int(row["exit_pov_filled_qty"]), 2_000)
        self.assertEqual(int(row["exit_other_filled_qty"]), 8_800)

    def test_legacy_closed_position_with_retained_shares_is_not_marked_overnight(self) -> None:
        legacy = self._position("LEGACY-1", 4000, 11.77)
        legacy.pop("entry_shares")
        legacy["shares"] = 4000
        legacy["sell_price"] = 12.32
        legacy["sell_date"] = "20260623"
        legacy.pop("exit_fills_by_date")
        (self.root / "data" / "processed" / "positions.json").write_text(
            json.dumps([legacy], ensure_ascii=False), encoding="utf-8"
        )

        self.tracker.rebuild_summary()
        with self.tracker.summary_path.open("r", newline="", encoding="utf-8-sig") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(int(row["exit_filled_qty"]), 4000)
        self.assertEqual(float(row["exit_completion_pct"]), 100.0)
        self.assertEqual(int(row["overnight_residual_qty"]), 0)
        self.assertEqual(row["execution_status"], "已平仓")


if __name__ == "__main__":
    unittest.main()
