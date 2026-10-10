from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import pandas as pd
from scripts import report_rolling_live_performance as report

ROOT = Path(__file__).resolve().parents[1]


class ReportLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = json.loads((ROOT / 'config/account_risk_shadow.json').read_text())
        (self.root / 'config').mkdir()
        self.policy_path = self.root / 'config/account_risk_shadow.json'
        self.policy_path.write_text(json.dumps(self.policy))
        self.ledger = self.root / self.policy['bootstrap_equity_path']
        self.ledger.parent.mkdir(parents=True)
        self.state = self.root / self.policy['state_path']
        self.state.write_text('{"prior_untouched_state":true}')
        self.trades = pd.DataFrame(columns=['trade_key','exit_date','net_pnl'])
        self.patches = [patch.object(report,'PROJECT_ROOT',self.root),
                        patch.object(report,'ACCOUNT_RISK_POLICY_PATH',self.policy_path)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def assert_unready(self):
        with patch.object(report, 'update_account_risk_shadow') as update:
            result = report.account_risk_status(self.trades, '20261010')
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertIsNone(result['account_drawdown'])
        self.assertIsNone(result['current_equity'])
        self.assertFalse(result['activation_review_eligible'])
        update.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text()), {'prior_untouched_state':True})
        self.assertEqual(json.loads((self.root / self.policy['latest_status_path']).read_text())['status'],'NOT_READY')

    def test_missing_ledger_preserves_original_state(self):
        self.assert_unready()

    def test_truncated_ledger_generates_unknown_risk_instead_of_zero_loss(self):
        self.ledger.write_text('{')
        self.assert_unready()

    def test_empty_trade_count_and_recent_baseline_cannot_certify_history(self):
        self.ledger.write_text(json.dumps({'last_equity':68907.78,'ledger_ready':False,
                                          'historical_coverage_ready':False,'included_trade_count':0}))
        self.assert_unready()

    def test_infinite_equity_is_rejected(self):
        self.ledger.write_text(json.dumps({'last_equity':'Infinity','ledger_ready':True,'historical_coverage_ready':True}))
        self.assert_unready()

    def test_ready_ledger_still_runs_existing_shadow_calculation(self):
        self.ledger.write_text(json.dumps({'last_equity':10000,'ledger_ready':True,'historical_coverage_ready':True}))
        with patch.object(report,'update_account_risk_shadow',return_value={'status':'NORMAL'}) as update:
            self.assertEqual(report.account_risk_status(self.trades,'20261010')['status'],'NORMAL')
        self.assertEqual(update.call_args.kwargs['bootstrap_equity'],10000)


if __name__ == '__main__':
    unittest.main()
