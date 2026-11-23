"""收盘流水线各步的单步超时（2026-11-23）。

11-23 涨停候选147只，第①步逐只取逐笔超过600秒，每次被终止后从头再来，重跑15次到18:22才完成。
第①步放宽到1800秒；其余各步的超时、步骤顺序和参数都不变。
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import unittest
from pathlib import Path

os.environ['A_SYSTEM_DISABLE_NOTIFICATIONS'] = '1'

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('step_timeout_test_daemon', ROOT / 'scripts/trading_daemon.py')
daemon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = daemon
spec.loader.exec_module(daemon)
from tests.daemon_testing import patch_daemon  # noqa: E402

EXPECTED = [
    ('collect_all_data.py', 1800),
    ('clean_collected_data.py', 600),
    ('build_dynamic_features.py', 600),
    ('score_limit_up_fill_probability.py', 600),
    ('build_live_enhanced_features.py', 600),
    ('run_paper_ab_filtered_daily_ops.py', 900),
    ('run_strategy_e_signal.py', 900),
    ('run_strategy_m_signal.py', 900),
    ('strategy_health_monitor.py', 600),
    ('live_execution_audit.py', 600),
    ('update_execution_completion.py', 600),
    ('report_rolling_live_performance.py', 600),
    ('update_release_oos_observation.py', 600),
    ('update_equity_curve_stop.py', 1800),
]


class StepTimeoutTests(unittest.TestCase):
    def test_only_step_one_gets_the_longer_limit(self):
        with patch_daemon('snapshot_realized_strategy_equity'), \
             patch_daemon('load_json_config', return_value={}), \
             patch_daemon('prev_n_trade_days', return_value=datetime.date(2026, 11, 19)), \
             patch_daemon('run_script', return_value=True) as run, \
             patch_daemon('_log_post_market_step_brief'), \
             patch_daemon('_alert_if_stop_decision_stale'), \
             patch_daemon('_log_close_pipeline_candidate_summary'), \
             patch_daemon('_date_in_scored', return_value=True), \
             patch_daemon('_has_signal_for_date', return_value=True), \
             patch_daemon('_checklist_data_quality_blocked', return_value=False), \
             patch_daemon('_equity_curve_stop_decision_fresh', return_value=(True, '20261124')), \
             patch_daemon('report_signal_readiness_summary'), \
             patch_daemon('report_next_trade_factor_readiness', return_value=True), \
             patch_daemon('mark_post_market_done'):
            self.assertTrue(daemon.job_post_market('20261123', offline=True))
        calls = [(c.args[0], c.kwargs['timeout']) for c in run.call_args_list]
        self.assertEqual(calls, EXPECTED)

    def test_collect_limit_constant(self):
        self.assertEqual(daemon.TIMEOUT_COLLECT_STEP, 1800)
        self.assertEqual(daemon.TIMEOUT_DATA_STEP, 600)


if __name__ == '__main__':
    unittest.main()
