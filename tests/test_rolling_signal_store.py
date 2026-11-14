"""信号滚动存储（src/rolling_signal_store.py）：E、M信号脚本写，守护进程和实盘组合读，决定第二天买什么。

2026-11-14 优化第1步“安全网”补的测试。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src import rolling_signal_store as store


class SignalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "signals" / "rolling.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_replaces_same_date_and_keeps_latest_n(self):
        for day in ("20261105", "20261106", "20261109", "20261110"):
            store.save_recent_signal(self.path, {"signal_date": day, "ts_code": "A" + day}, strategy_leg="E", max_trade_days=3)
        store.save_recent_signal(self.path, {"signal_date": "20261109", "ts_code": "again"}, strategy_leg="E", max_trade_days=3)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((payload["strategy_leg"], payload["max_trade_days"]), ("E", 3))
        self.assertEqual([(s["signal_date"], s["ts_code"]) for s in payload["signals"]],
                         [("20261106", "A20261106"), ("20261109", "again"), ("20261110", "A20261110")])

    def test_load_tolerates_missing_corrupt_and_bare_list(self):
        self.assertEqual(store.load_recent_signals(self.path), [])
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{broken", encoding="utf-8")
        self.assertEqual(store.load_recent_signals(self.path), [])
        self.path.write_text(json.dumps([{"signal_date": "20261110"}, "junk"]), encoding="utf-8")
        self.assertEqual(store.load_recent_signals(self.path), [{"signal_date": "20261110"}])
        self.path.write_text(json.dumps({"signals": "junk"}), encoding="utf-8")
        self.assertEqual(store.load_recent_signals(self.path), [])

    def test_buy_date_lookup_needs_earlier_signal_date(self):
        for signal in ({"signal_date": "20261109", "planned_buy_date": "20261111", "n": 1},
                       {"signal_date": "20261110", "planned_buy_date": "20261111", "n": 2},
                       {"signal_date": "20261111", "planned_buy_date": "20261111", "n": 3}):
            store.save_recent_signal(self.path, signal, strategy_leg="M")
        self.assertEqual(store.latest_signal_for_buy_date(self.path, "20261111")["n"], 2, "同日信号不能当天买")
        self.assertIsNone(store.latest_signal_for_buy_date(self.path, "20261112"))
        self.assertEqual(store.signal_by_signal_date(self.path, "20261109")["n"], 1)
        self.assertIsNone(store.signal_by_signal_date(self.path, "20261108"))


class SignalRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runs.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_statuses(self):
        self.assertEqual(store.NORMAL_SIGNAL_RUN_STATUSES, {"SIGNAL_READY", "NO_SIGNAL_OCCUPIED", "NO_CANDIDATE"})
        self.assertEqual(store.ALL_SIGNAL_RUN_STATUSES, store.NORMAL_SIGNAL_RUN_STATUSES | {"ERROR"})

    def test_rerun_overrides_error_and_keeps_latest_n(self):
        store.save_recent_signal_run(self.path, {"signal_date": "20261110", "status": "error"}, strategy_leg="E")
        store.save_recent_signal_run(self.path, {"signal_date": " 20261110 ", "status": "no_candidate",
                                                 "generated_at": "fixed"}, strategy_leg="E")
        run = store.signal_run_by_signal_date(self.path, "20261110")
        self.assertEqual((run["status"], run["strategy_leg"], run["generated_at"]), ("NO_CANDIDATE", "E", "fixed"))
        for day in ("20261111", "20261112"):
            store.save_recent_signal_run(self.path, {"signal_date": day, "status": "SIGNAL_READY"}, strategy_leg="E",
                                         max_trade_days=2)
        self.assertEqual([r["signal_date"] for r in store.load_recent_signal_runs(self.path)], ["20261111", "20261112"])
        self.assertIn("generated_at", store.signal_run_by_signal_date(self.path, "20261112"))
        self.assertIsNone(store.signal_run_by_signal_date(self.path, "20261110"))

    def test_rejects_missing_date_and_unknown_status(self):
        with self.assertRaisesRegex(ValueError, "缺少signal_date"):
            store.save_recent_signal_run(self.path, {"status": "ERROR"}, strategy_leg="E")
        with self.assertRaisesRegex(ValueError, "未知信号运行状态：DONE"):
            store.save_recent_signal_run(self.path, {"signal_date": "20261110", "status": "done"}, strategy_leg="E")
        self.assertFalse(self.path.exists())

    def test_corrupt_run_file_reads_as_empty(self):
        self.path.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(store.load_recent_signal_runs(self.path), [])


class LegacyFileTests(unittest.TestCase):
    def test_migrate_then_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "e_signal_20261109.json").write_text(json.dumps({"signal_date": "20261109"}), encoding="utf-8")
            (folder / "e_signal_20261110.json").write_text("not json", encoding="utf-8")
            (folder / "e_signal_20261111.json").write_text(json.dumps({"ts_code": "no date"}), encoding="utf-8")
            rolling = folder / "rolling.json"
            self.assertEqual(store.migrate_legacy_daily_signal_files(folder, "e_signal_*.json", rolling, strategy_leg="E"), 1)
            self.assertEqual([s["signal_date"] for s in store.load_recent_signals(rolling)], ["20261109"])
            self.assertEqual(store.cleanup_legacy_daily_signal_files(folder, "e_signal_*.json"), 3)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["rolling.json"])


if __name__ == "__main__":
    unittest.main()
