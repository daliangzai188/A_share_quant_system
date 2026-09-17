import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qmt_inner.engine import ENGINE_REVISION
from scripts.repair_qmt_inner_ledger import repair_ledger


class LedgerRepairSafetyTests(unittest.TestCase):
    def test_requires_manual_stop_before_connection(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch('scripts.repair_qmt_inner_ledger.assert_standalone_qmt_allowed'), \
             patch('scripts.repair_qmt_inner_ledger.FileClient') as client:
            with self.assertRaisesRegex(RuntimeError, 'requires manual stop'):
                repair_ledger(Path(temporary))
            client.assert_not_called()

    def test_rejects_cached_revision_before_backup_or_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / '.manual_stop.json'
            marker.write_text(json.dumps({'status': 'MANUAL_STOP'}))
            before = marker.read_bytes()
            with patch('scripts.repair_qmt_inner_ledger.assert_standalone_qmt_allowed'), \
                 patch('scripts.repair_qmt_inner_ledger.load_dotenv'), \
                 patch('scripts.repair_qmt_inner_ledger.load_settings', return_value={}), \
                 patch('scripts.repair_qmt_inner_ledger.FileClient') as client, \
                 patch('scripts.repair_qmt_inner_ledger.backup') as saved:
                client.return_value.heartbeat.return_value = {'engine_revision': 'OLD'}
                with self.assertRaisesRegex(RuntimeError, 'revision'):
                    repair_ledger(root)
                saved.assert_not_called()
            self.assertEqual(marker.read_bytes(), before)

    def test_financial_calls_are_blocked_inside_original_recovery(self):
        from scripts import trading_daemon as daemon
        from src.qmt_inner_adapter import QMTInnerBrokerAdapter
        for method in ('place_order', 'cancel_order'):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                marker = root / '.manual_stop.json'
                marker.write_text(json.dumps({'status': 'MANUAL_STOP'}))
                before = marker.read_bytes()
                def attempt():
                    getattr(QMTInnerBrokerAdapter, method)(None, None)
                with patch('scripts.repair_qmt_inner_ledger.assert_standalone_qmt_allowed'), \
                     patch('scripts.repair_qmt_inner_ledger.load_dotenv'), \
                     patch('scripts.repair_qmt_inner_ledger.load_settings', return_value={}), \
                     patch('scripts.repair_qmt_inner_ledger.FileClient') as client, \
                     patch('scripts.repair_qmt_inner_ledger.backup', return_value={'directory': 'backup'}), \
                     patch.object(daemon, 'setup'), \
                     patch.object(daemon, '_recover_trade_execution_state_once', side_effect=attempt):
                    client.return_value.heartbeat.return_value = {'engine_revision': ENGINE_REVISION}
                    with self.assertRaisesRegex(RuntimeError, 'Financial operation forbidden'):
                        repair_ledger(root)
                self.assertEqual(marker.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
