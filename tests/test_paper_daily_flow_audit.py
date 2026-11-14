"""收盘流水线⑥（scripts/run_paper_ab_filtered_daily_ops.py）读审计逐笔交易用的 src/paper_daily_flow.py。

2026-11-14 优化第1步“安全网”补的测试：读文件时只留已执行的交易、日期和数字统一格式、排序固定；安全模式拦截实盘配置。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.paper_daily_flow import PaperDailyFlowRunner


class PaperDailyFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.config = {"trade_mode": "paper", "paper_trade": {"round_lot_size": 100}}

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, **overrides) -> PaperDailyFlowRunner:
        path = self.folder / "strategy_config.json"
        path.write_text(json.dumps(dict(self.config, **overrides)), encoding="utf-8")
        return PaperDailyFlowRunner(path)

    def test_load_audit_trades_keeps_executed_rows_normalized_and_sorted(self):
        audit = self.folder / "audit.csv"
        pd.DataFrame([
            {"trade_date": "20261110", "trade_order": 2, "ts_code": "000002.SZ", "scenario_executed": "True",
             "buy_trade_date": 20261111.0, "dynamic_net_return": "0.05", "equity_after": None},
            {"trade_date": "20261110", "trade_order": 1, "ts_code": "000003.SZ", "scenario_executed": "yes",
             "buy_trade_date": "20261111", "dynamic_net_return": "bad", "equity_after": 101000},
            {"trade_date": "20261109", "trade_order": 1, "ts_code": "000001.SZ", "scenario_executed": "1",
             "buy_trade_date": "20261110", "dynamic_net_return": -0.02, "equity_after": 99000},
            {"trade_date": "20261108", "trade_order": 1, "ts_code": "000009.SZ", "scenario_executed": "False",
             "buy_trade_date": "20261109", "dynamic_net_return": 0.3, "equity_after": 1},
        ]).to_csv(audit, index=False)
        trades = self.runner().load_audit_trades(audit)
        self.assertEqual(trades["ts_code"].tolist(), ["000001.SZ", "000003.SZ", "000002.SZ"])
        self.assertEqual(trades["buy_trade_date"].tolist(), ["20261110", "20261111", "20261111"])
        self.assertEqual(trades["dynamic_net_return"].tolist(), [-0.02, 0.0, 0.05])
        self.assertEqual(trades["equity_after"].tolist(), [99000.0, 101000.0, 0.0])

    def test_missing_audit_file_raises(self):
        with self.assertRaisesRegex(FileNotFoundError, "审计逐笔交易文件不存在"):
            self.runner().load_audit_trades(self.folder / "missing.csv")

    def test_safe_mode_blocks_live_settings(self):
        self.runner().assert_safe_mode()
        for overrides, message in (({"trade_mode": "live"}, "trade_mode 不是安全模式: live"),
                                   ({"live_trading_enabled": True}, "live_trading_enabled=true"),
                                   ({"broker_adapter_enabled": True}, "broker_adapter_enabled=true"),
                                   ({"qmt_enabled": True}, "qmt_enabled=true"),
                                   ({"paper_daily_flow": {"allow_live_order": True}}, "allow_live_order=true")):
            with self.assertRaisesRegex(RuntimeError, message):
                self.runner(**overrides).assert_safe_mode()

    def test_normalizers_and_round_lots(self):
        self.assertEqual([PaperDailyFlowRunner.normalize_date(v) for v in (20261110.0, "20261110", None)],
                         ["20261110", "20261110", ""])
        self.assertEqual([PaperDailyFlowRunner.normalize_bool(v) for v in (True, "YES ", "0", None, float("nan"))],
                         [True, True, False, False, False])
        self.assertEqual([PaperDailyFlowRunner.normalize_number(v, 9.0) for v in ("1.5", "x", None)], [1.5, 9.0, 9.0])
        runner = self.runner()
        self.assertEqual(runner.estimate_shares(10050, 10.0), {"estimated_shares": 1005.0, "round_lot_shares": 1000})
        self.assertEqual(runner.estimate_shares(0, 10.0), {"estimated_shares": 0.0, "round_lot_shares": 0})
        self.assertTrue(PaperDailyFlowRunner.has_loss_overlay_watch("A;LOSS_OVERLAY_WATCH"))


if __name__ == "__main__":
    unittest.main()
