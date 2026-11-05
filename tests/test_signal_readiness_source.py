import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from scripts import audit_signal_readiness
from scripts import trading_daemon


class SignalReadinessSourceTests(unittest.TestCase):
    """QMT 原生涨停池（qmt_native）按 limit_list_d 规则重建并已核对一致，收盘审计应判“通过”；未知来源仍不通过。"""

    def run_summary(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live_limit_up_fill_scored.csv"
            pd.DataFrame({
                "trade_date": ["20261104", "20261104"],
                "ts_code": ["000001.SZ", "600000.SH"],
                "limit_data_source": [source, source],
                "limit_data_quality": ["full", "full"],
                "strategy_compatible": [True, True],
                "fd_ratio": [0.5, 0.7],
            }).to_csv(path, index=False)
            configs = {
                "config.json": {},
                "strategy_config.json": {"paper_ab_filtered_strategy": {
                    "data_quality_requirements": {"required_columns": ["fd_ratio"]}}},
            }
            lines = []
            handler = logging.Handler()
            handler.emit = lambda record: lines.append(record.getMessage())
            log = logging.getLogger("signal_readiness_source_test")
            log.addHandler(handler)
            log.setLevel(logging.INFO)
            with mock.patch.object(trading_daemon, "load_json_config", lambda p: configs[Path(p).name]), \
                    mock.patch.object(trading_daemon, "_prefer_live_processed_path", lambda *a: path), \
                    mock.patch.object(trading_daemon, "_log_market_environment", lambda *a: None), \
                    mock.patch.object(trading_daemon, "logger", lambda: log):
                trading_daemon.report_signal_readiness_summary("20261104")
            log.removeHandler(handler)
        return [line for line in lines if "数据口径" in line]

    def test_qmt_native_source_matches_backtest(self):
        verdict = self.run_summary("qmt_native")
        self.assertTrue(verdict and "✅ 通过" in verdict[0], verdict)

    def test_unknown_source_still_fails(self):
        verdict = self.run_summary("kpl_list")
        self.assertTrue(verdict and "❌ 不通过" in verdict[0], verdict)

    def test_audit_script_accepts_the_same_sources(self):
        self.assertEqual(audit_signal_readiness.LIMIT_SOURCES_MATCHING_BACKTEST,
                         trading_daemon.LIMIT_SOURCES_MATCHING_BACKTEST)
        self.assertFalse(trading_daemon._limit_sources_match_backtest([]))
        self.assertFalse(trading_daemon._limit_sources_match_backtest(["limit_list_d", ""]))


if __name__ == "__main__":
    unittest.main()
