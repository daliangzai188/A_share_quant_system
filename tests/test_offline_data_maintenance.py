from __future__ import annotations

import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ['A_SYSTEM_DISABLE_NOTIFICATIONS'] = '1'
from src.offline_data_maintenance import MaintenanceSettings, run_maintenance
from src.post_market_freshness import producer_identity, decision_producer_fresh
from src.equity_curve_stop import METHOD_ID, load_settings, write_decision, live_gate

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('offline_test_daemon', ROOT / 'scripts/trading_daemon.py')
daemon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = daemon
spec.loader.exec_module(daemon)


class OneTickEvent:
    def __init__(self):
        self.stopped = False
    def is_set(self):
        return self.stopped
    def wait(self, seconds):
        self.stopped = True
    def set(self):
        self.stopped = True


class MaintenanceTests(unittest.TestCase):
    def run_once(self, fresh=False, disabled=False, ledger=None):
        stop = OneTickEvent()
        ledger = ledger or Mock()
        pipeline = Mock(return_value=True)
        error = Mock()
        run_maintenance(stop=stop, settings=MaintenanceSettings(enabled=not disabled),
                        expected_date=lambda: '20261009', refresh_ledger=ledger,
                        data_fresh=lambda date: fresh, start_pipeline=pipeline, on_error=error)
        return ledger, pipeline, error

    def test_stale_data_runs_without_broker_callback(self):
        ledger, pipeline, error = self.run_once()
        ledger.assert_called_once_with('20261009')
        pipeline.assert_called_once_with('20261009')
        error.assert_not_called()

    def test_fresh_data_only_updates_local_ledger(self):
        ledger, pipeline, _ = self.run_once(fresh=True)
        ledger.assert_called_once()
        pipeline.assert_not_called()

    def test_disabled_worker_has_no_effect(self):
        ledger, pipeline, _ = self.run_once(disabled=True)
        ledger.assert_not_called()
        pipeline.assert_not_called()

    def test_stopping_during_ledger_refresh_does_not_launch_pipeline(self):
        stop = OneTickEvent()
        start = Mock()
        run_maintenance(stop=stop, settings=MaintenanceSettings(), expected_date=lambda: '20261009',
                        refresh_ledger=lambda date: stop.set(), data_fresh=lambda date: False,
                        start_pipeline=start, on_error=Mock())
        start.assert_not_called()

    def test_ledger_error_does_not_kill_worker(self):
        _, pipeline, error = self.run_once(ledger=Mock(side_effect=OSError('bad ledger')))
        error.assert_called_once()
        pipeline.assert_called_once_with('20261009')

    def test_stop_wakes_long_wait(self):
        stop = threading.Event()
        entered = threading.Event()
        worker = threading.Thread(target=run_maintenance, kwargs=dict(
            stop=stop, settings=MaintenanceSettings(poll_seconds=300),
            expected_date=lambda: '20261009', refresh_ledger=lambda date: entered.set(),
            data_fresh=lambda date: True, start_pipeline=Mock(), on_error=Mock()))
        worker.start()
        self.assertTrue(entered.wait(2))
        stop.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'src').mkdir()
        (self.root / 'src/market_rules.py').write_text('ROUND = "HALF_UP"')
        (self.root / 'data/raw/daily').mkdir(parents=True)
        self.raw = self.root / 'data/raw/daily/20261009.csv'
        self.raw.write_text('trade_date,ts_code,close\n20261009,000001.SZ,1\n')
        self.config = {'equity_curve_stop': {'enabled': True, 'require_producer_identity': True,
                         'decision_path': 'decision.json', 'ma_window': 60, 'lag_trading_days': 6}}
        self.payload = dict(method_id=METHOD_ID, signal_date='20261009', action_date='20261012',
                            ma_window=60, lag_trading_days=6, allowed=True,
                            producer_identity=producer_identity(self.root, '20261009', self.config))

    def test_old_date_correct_payload_is_stale(self):
        self.assertFalse(decision_producer_fresh(self.root, '20261009', self.config, {'signal_date':'20261009'}))

    def test_price_rule_fix_invalidates_existing_decision(self):
        self.assertTrue(decision_producer_fresh(self.root, '20261009', self.config, self.payload))
        (self.root / 'src/market_rules.py').write_text('ROUND = "HALF_EVEN"')
        self.assertFalse(decision_producer_fresh(self.root, '20261009', self.config, self.payload))

    def test_history_repair_or_removal_invalidates_decision(self):
        self.raw.write_text(self.raw.read_text().replace(',1\n', ',2.03\n'))
        self.assertFalse(decision_producer_fresh(self.root, '20261009', self.config, self.payload))
        self.payload['producer_identity'] = producer_identity(self.root, '20261009', self.config)
        self.raw.unlink()
        self.assertFalse(decision_producer_fresh(self.root, '20261009', self.config, self.payload))

    def test_next_trading_day_data_does_not_invalidate_previous_decision(self):
        (self.raw.parent / '20261012.csv').write_text('new day data')
        self.assertTrue(decision_producer_fresh(self.root, '20261009', self.config, self.payload))

    def test_live_gate_rejects_legacy_and_diagnostic_decisions(self):
        settings = load_settings(self.config, self.root)
        write_decision(settings, self.payload)
        self.assertTrue(live_gate(self.config, self.root, '20261012').allowed)
        self.payload['diagnostic_dataset_reused'] = True
        write_decision(settings, self.payload)
        self.assertFalse(live_gate(self.config, self.root, '20261012').allowed)
        self.payload.pop('diagnostic_dataset_reused')
        self.payload.pop('producer_identity')
        write_decision(settings, self.payload)
        self.assertFalse(live_gate(self.config, self.root, '20261012').allowed)


class DaemonIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.log = Mock()
        self.patch_log = patch.object(daemon, 'logger', return_value=self.log)
        self.patch_log.start()
        self.addCleanup(self.patch_log.stop)

    def test_offline_missing_ledger_never_queries_or_initializes_broker_baseline(self):
        with patch.object(daemon, 'load_json_config', return_value={}), \
             patch.object(daemon, 'load_positions', return_value=[]), \
             patch.object(daemon, 'equity_ledger_requires_bootstrap', return_value=True), \
             patch.object(daemon, '_qmt_get', side_effect=AssertionError('broker called')), \
             patch.object(daemon, 'update_strategy_equity_ledger') as update:
            daemon.snapshot_realized_strategy_equity('20261009', allow_broker_bootstrap=False)
            update.assert_not_called()

    def test_duplicate_pipeline_uses_existing_single_worker_lock(self):
        worker = Mock()
        worker.is_alive.return_value = True
        with patch.object(daemon, '_pipeline_thread', worker):
            self.assertFalse(daemon._start_post_market_pipeline('20261009', offline=True))

    def test_offline_pipeline_completes_without_live_sizing_or_broker_calls(self):
        with patch.object(daemon, 'snapshot_realized_strategy_equity') as ledger, \
             patch.object(daemon, 'load_json_config', return_value={}), \
             patch.object(daemon, 'prev_n_trade_days', return_value=datetime.date(2026,10,7)), \
             patch.object(daemon, 'run_script', return_value=True) as run, \
             patch.object(daemon, '_log_post_market_step_brief'), \
             patch.object(daemon, '_alert_if_stop_decision_stale'), \
             patch.object(daemon, '_log_close_pipeline_candidate_summary'), \
             patch.object(daemon, '_date_in_scored', return_value=True), \
             patch.object(daemon, '_has_signal_for_date', return_value=True), \
             patch.object(daemon, '_checklist_data_quality_blocked', return_value=False), \
             patch.object(daemon, '_equity_curve_stop_decision_fresh', return_value=(True,'20261012')), \
             patch.object(daemon, 'report_signal_readiness_summary'), \
             patch.object(daemon, 'report_next_trade_factor_readiness', return_value=True), \
             patch.object(daemon, 'mark_post_market_done') as done, \
             patch.object(daemon, '_qmt_get', side_effect=AssertionError('broker called')), \
             patch.object(daemon, 'report_next_day_candidates', side_effect=AssertionError('live sizing called')), \
             patch.object(daemon, '_log_decision_chain_summary', side_effect=AssertionError('live summary called')), \
             patch.object(daemon, 'push_open_plan_notification', side_effect=AssertionError('live push called')):
            self.assertTrue(daemon.job_post_market('20261009', offline=True))
            ledger.assert_called_once_with('20261009', allow_broker_bootstrap=False)
            self.assertIn('update_equity_curve_stop.py', [c.args[0] for c in run.call_args_list])
            done.assert_called_once()

    def test_stopped_retry_does_not_launch_another_pipeline(self):
        stop = threading.Event()
        stop.set()
        with patch.object(daemon, 'job_post_market') as job:
            daemon._run_post_market_with_retry('20261009', offline=True, stop_event=stop)
            job.assert_not_called()


if __name__ == '__main__':
    unittest.main()
