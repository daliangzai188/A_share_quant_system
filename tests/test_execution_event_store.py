from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from src.execution_completion_tracker import ExecutionCompletionTracker
from src.execution_event_store import ExecutionEventStore


class ExecutionEventStoreTests(unittest.TestCase):
    def test_same_payload_is_idempotent_and_changed_payload_appends_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.sqlite3"
            store = ExecutionEventStore(path)
            first = store.append_event(
                event_uid="BUY|ORDER-1",
                event_type="BUY",
                trade_key="20260811|000001.SZ|A|20260810",
                payload={"filled_qty": 100, "status": "部分成交"},
            )
            duplicate = store.append_event(
                event_uid="BUY|ORDER-1",
                event_type="BUY",
                trade_key="20260811|000001.SZ|A|20260810",
                payload={"status": "部分成交", "filled_qty": 100},
            )
            changed = store.append_event(
                event_uid="BUY|ORDER-1",
                event_type="BUY",
                trade_key="20260811|000001.SZ|A|20260810",
                payload={"filled_qty": 200, "status": "全部成交"},
            )
            self.assertTrue(first["inserted"])
            self.assertFalse(duplicate["inserted"])
            self.assertEqual(int(duplicate["revision"]), 1)
            self.assertTrue(changed["inserted"])
            self.assertEqual(int(changed["revision"]), 2)

            history = ExecutionEventStore(path).event_history("BUY|ORDER-1")
            self.assertEqual([row["revision"] for row in history], [1, 2])
            self.assertEqual(history[0]["payload"]["filled_qty"], 100)
            self.assertEqual(history[1]["payload"]["filled_qty"], 200)
            audit = store.audit()
            self.assertEqual(audit["status"], "PASS")
            self.assertEqual(audit["event_head_count"], 1)
            self.assertEqual(audit["event_revision_count"], 2)
            self.assertEqual(audit["head_count_by_type"], {"BUY": 1})

    def test_payload_can_restore_an_existing_historical_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.sqlite3"
            store = ExecutionEventStore(path)
            payload_a = {"filled_qty": 100, "status": "部分成交"}
            payload_b = {"filled_qty": 200, "status": "全部成交"}
            first = store.append_event(
                event_uid="BUY|ORDER-RESTORE",
                event_type="BUY",
                trade_key="20260811|000001.SZ|A|20260810",
                payload=payload_a,
            )
            second = store.append_event(
                event_uid="BUY|ORDER-RESTORE",
                event_type="BUY",
                trade_key="20260811|000001.SZ|A|20260810",
                payload=payload_b,
            )
            restored = store.append_event(
                event_uid="BUY|ORDER-RESTORE",
                event_type="BUY",
                trade_key="20260811|000001.SZ|A|20260810",
                payload=payload_a,
            )
            duplicate_after_restore = store.append_event(
                event_uid="BUY|ORDER-RESTORE",
                event_type="BUY",
                trade_key="20260811|000001.SZ|A|20260810",
                payload=payload_a,
            )
            third = store.append_event(
                event_uid="BUY|ORDER-RESTORE",
                event_type="BUY",
                trade_key="20260811|000001.SZ|A|20260810",
                payload={"filled_qty": 200, "status": "已撤"},
            )

            self.assertTrue(first["inserted"])
            self.assertTrue(second["inserted"])
            self.assertFalse(restored["inserted"])
            self.assertTrue(restored["restored_existing_revision"])
            self.assertEqual(restored["revision"], 1)
            self.assertFalse(duplicate_after_restore["inserted"])
            self.assertFalse(duplicate_after_restore["restored_existing_revision"])
            self.assertEqual(duplicate_after_restore["revision"], 1)
            self.assertTrue(third["inserted"])
            self.assertEqual(third["revision"], 3)
            self.assertEqual(
                [row["revision"] for row in store.event_history("BUY|ORDER-RESTORE")],
                [1, 2, 3],
            )
            audit = store.audit()
            self.assertEqual(audit["status"], "PASS")
            self.assertEqual(audit["event_revision_count"], 3)
            self.assertEqual(audit["event_head_count"], 1)

    def test_tracker_mirrors_plan_buy_and_sell_without_replacing_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data" / "processed").mkdir(parents=True)
            tracker = ExecutionCompletionTracker(root)
            tracker.register_entry_plan(
                entry_date="20260811",
                ts_code="000001.SZ",
                name="平安银行",
                strategy_leg="A",
                signal_date="20260810",
                target_qty=1000,
                target_amount=10000,
                reference_price=10,
                planned_exit_date="20260813",
            )
            tracker.record_buy_slice(
                event_id="BUY-1",
                entry_date="20260811",
                ts_code="000001.SZ",
                name="平安银行",
                strategy_leg="A",
                signal_date="20260810",
                channel="集合竞价买入",
                order_id="QMT-BUY-1",
                order_qty=1000,
                filled_qty=0,
                status="已报",
            )
            tracker.record_sell_slice(
                event_id="SELL-1",
                entry_date="20260811",
                exit_date="20260813",
                ts_code="000001.SZ",
                name="平安银行",
                strategy_leg="A",
                signal_date="20260810",
                channel="卖出POV",
                broker_order_id="QMT-SELL-1",
                order_qty=1000,
                filled_qty=0,
                status="已报",
            )
            # CSV仍存在且可独立读取，SQLite只是镜像。
            for path in (tracker.plan_path, tracker.buy_path, tracker.sell_path):
                with path.open("r", newline="", encoding="utf-8-sig") as handle:
                    self.assertEqual(len(list(csv.DictReader(handle))), 1)

            audit = tracker.mirror_existing_events()
            self.assertEqual(audit["status"], "PASS")
            self.assertTrue(audit["mirror_complete"])
            self.assertEqual(
                audit["expected_head_count_by_type"],
                {"PLAN": 1, "BUY": 1, "SELL": 1},
            )
            self.assertEqual(
                audit["head_count_by_type"],
                {"BUY": 1, "PLAN": 1, "SELL": 1},
            )
            # 追加账本允许保留已离开当前CSV视图的历史事件，不能把历史保留误报为不一致。
            ExecutionEventStore(tracker.event_store_path).append_event(
                event_uid="BUY|HISTORICAL-REMOVED",
                event_type="BUY",
                trade_key="20260701|000002.SZ|E2|20260630",
                payload={"status": "历史记录"},
            )
            retained = tracker.mirror_existing_events()
            self.assertEqual(retained["status"], "PASS")
            self.assertEqual(retained["retained_history_head_count"], 1)
            self.assertEqual(retained["missing_event_uids"], [])


if __name__ == "__main__":
    unittest.main()
