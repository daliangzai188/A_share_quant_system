from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import trading_daemon as daemon


class HistoryCoverageNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {'drawdown_alert': {'enabled': True, 'threshold': -.30}}
        self.snapshot = SimpleNamespace(
            pending_trade_keys=(), historical_coverage_ready=False,
            historical_coverage_reasons=('BASELINE_NEWER_THAN_CONFIGURED_LEGACY_CUTOFF',),
            missing_previously_included_trade_keys=(), ledger_ready=False,
            equity=68907.78, peak_equity=68907.78,
        )

    def test_empty_current_csv_still_alerts_the_missing_history(self):
        with patch.object(daemon, 'PROJECT_ROOT', self.root), patch.object(daemon, '_notify', return_value=True) as notify:
            daemon._maybe_alert_ledger_incomplete(self.snapshot, self.cfg)
            notify.assert_called_once()
            self.assertIn('历史净值账本未恢复', notify.call_args.args[1])
            self.assertIn('不能证明历史完整', notify.call_args.args[2])

    def test_same_history_gap_alert_is_not_repeated(self):
        with patch.object(daemon, 'PROJECT_ROOT', self.root), patch.object(daemon, '_notify', return_value=True) as notify:
            daemon._maybe_alert_ledger_incomplete(self.snapshot, self.cfg)
            daemon._maybe_alert_ledger_incomplete(self.snapshot, self.cfg)
            notify.assert_called_once()

    def test_history_alert_clears_only_when_history_is_ready(self):
        with patch.object(daemon, 'PROJECT_ROOT', self.root), patch.object(daemon, '_notify', return_value=True):
            daemon._maybe_alert_ledger_incomplete(self.snapshot, self.cfg)
            path = self.root/'data/state/equity_ledger_pending_alert.json'
            self.assertTrue(json.loads(path.read_text())['alerted_history_missing'])
            self.snapshot.historical_coverage_ready = True
            self.snapshot.historical_coverage_reasons = ()
            self.snapshot.ledger_ready = True
            daemon._maybe_alert_ledger_incomplete(self.snapshot, self.cfg)
            self.assertFalse(path.exists())

    def test_unready_equity_cannot_emit_a_numeric_drawdown_alarm(self):
        with patch.object(daemon, 'PROJECT_ROOT', self.root), patch.object(daemon, '_notify', return_value=True) as notify:
            daemon._maybe_alert_strategy_drawdown(self.snapshot, -.75, self.cfg)
            notify.assert_not_called()
            self.assertFalse((self.root/'data/state/drawdown_alert.json').exists())

    def test_history_gap_wins_over_a_stale_ready_flag(self):
        self.snapshot.ledger_ready = True
        with patch.object(daemon, 'PROJECT_ROOT', self.root), patch.object(daemon, '_notify', return_value=True) as notify:
            daemon._maybe_alert_strategy_drawdown(self.snapshot, -.75, self.cfg)
            notify.assert_not_called()

    def test_pending_trade_keeps_the_existing_qualified_drawdown_alert(self):
        self.snapshot.historical_coverage_ready = True
        self.snapshot.historical_coverage_reasons = ()
        self.snapshot.pending_incomplete_trade_count = 2
        self.snapshot.equity = 65000.0
        self.snapshot.peak_equity = 100000.0
        with patch.object(daemon, 'PROJECT_ROOT', self.root), patch.object(daemon, '_notify', return_value=True) as notify:
            daemon._maybe_alert_strategy_drawdown(self.snapshot, -.35, self.cfg)
            notify.assert_called_once()
            self.assertIn('2笔成交待补全', notify.call_args.args[2])

    def test_a_changed_missing_observed_trade_is_alerted_again(self):
        self.snapshot.historical_coverage_reasons = ('PREVIOUSLY_OBSERVED_TRADES_MISSING',)
        self.snapshot.missing_previously_observed_trade_keys = ('20260820|301211.SZ|N|20260819',)
        with patch.object(daemon, 'PROJECT_ROOT', self.root), patch.object(daemon, '_notify', return_value=True) as notify:
            daemon._maybe_alert_ledger_incomplete(self.snapshot, self.cfg)
            self.snapshot.missing_previously_observed_trade_keys = ('20260821|000001.SZ|A|20260820',)
            daemon._maybe_alert_ledger_incomplete(self.snapshot, self.cfg)
            self.assertEqual(notify.call_count, 2)
            self.assertIn('整行丢失', notify.call_args.args[2])

    def test_snapshot_does_not_log_false_zero_drawdown_when_history_is_missing(self):
        self.snapshot.initialized_now = False
        with patch.object(daemon, 'PROJECT_ROOT', self.root), \
             patch.object(daemon, 'load_json_config', return_value=self.cfg), \
             patch.object(daemon, 'load_positions', return_value=[]), \
             patch.object(daemon, 'equity_ledger_requires_bootstrap', return_value=False), \
             patch.object(daemon, 'update_strategy_equity_ledger', return_value=self.snapshot), \
             patch.object(daemon, '_maybe_alert_ledger_incomplete'), \
             patch.object(daemon, '_maybe_alert_strategy_drawdown'), \
             patch.object(daemon, 'logger') as get_logger:
            daemon.snapshot_realized_strategy_equity('20261009')
            get_logger.return_value.info.assert_not_called()
            self.assertTrue(any('回撤不可认证' in str(call.args[0])
                                for call in get_logger.return_value.warning.call_args_list))


if __name__ == '__main__':
    unittest.main()
