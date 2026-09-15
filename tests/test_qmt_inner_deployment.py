import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from qmt_inner.engine import Engine
from qmt_inner.protocol import atomic_json
from scripts.prepare_qmt_inner import prepare
from scripts.qmt_inner_backup import backup
from scripts.stage_qmt_inner_live import stage
from src.qmt_inner_start_gate import assert_selected_transport_ready


class DeploymentTests(unittest.TestCase):
    def test_live_stage_keeps_manual_stop_and_removes_only_migration_cap(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / 'project'
            (project / 'config').mkdir(parents=True)
            (project / 'qmt_inner').mkdir()
            # install_bundle 需要完整源包；测试中让它不复制，只验证私有配置事务。
            marker = project / '.manual_stop.json'
            atomic_json(marker, {'status': 'MANUAL_STOP'})
            private = Path(temporary) / 'private/config.json'
            private.parent.mkdir()
            settings = dict(account_id='TEST', account_type='STOCK', token='x'*64,
                            mode='read_only', spool_dir=str(private.parent/'spool'),
                            runtime_config=str(project/'config/config.json'),
                            max_order_notional=1000)
            atomic_json(private, settings)
            with patch('scripts.stage_qmt_inner_live.install_bundle'):
                result = stage(project, private)
            updated = json.loads(private.read_text())
            self.assertEqual(result['status'], 'LIVE_STAGED_MANUAL_STOP_ACTIVE')
            self.assertTrue(marker.exists())
            self.assertEqual(updated['mode'], 'live')
            self.assertEqual(updated['max_order_notional'], 0)
            self.assertEqual(Path(updated['manual_stop_path']), marker.resolve())
            self.assertTrue(Path(result['backup']).exists())

    def test_start_gate_keeps_manual_stop_when_inner_is_not_live(self):
        import os
        with patch.dict(os.environ, {'QMT_TRANSPORT': 'qmt_inner'}), \
             patch('qmt_inner.protocol.load_settings', return_value={
                 'runtime_config': str(Path.cwd() / 'config/config.json'),
                 'spool_dir': 'unused', 'token': 'x' * 64, 'account_id': 'TEST'}), \
             patch('qmt_inner.protocol.FileClient') as client:
            client.return_value.connect.return_value = {'mode': 'read_only'}
            with self.assertRaisesRegex(RuntimeError, '只读模式'):
                assert_selected_transport_ready(Path.cwd())

    def test_start_gate_accepts_live_inner(self):
        import os
        with patch.dict(os.environ, {'QMT_TRANSPORT': 'qmt_inner'}), \
             patch('qmt_inner.protocol.load_settings', return_value={
                 'runtime_config': str(Path.cwd() / 'config/config.json'),
                 'spool_dir': 'unused', 'token': 'x' * 64, 'account_id': 'TEST'}), \
             patch('qmt_inner.protocol.FileClient') as client:
            client.return_value.connect.return_value = {'mode': 'live'}
            result = assert_selected_transport_ready(Path.cwd())
        self.assertTrue(result['checked'])
        self.assertEqual(result['mode'], 'live')

    def test_inner_readiness_does_not_require_xtquant_or_mini_path(self):
        import os
        from scripts.check_qmt_live_readiness import build_report
        with patch('src.qmt_market_data.selected_transport', return_value='qmt_inner'), \
             patch('qmt_inner.protocol.load_settings', return_value={'account_id':'TEST'}), \
             patch('qmt_inner.protocol.FileClient') as client, \
             patch('scripts.check_qmt_live_readiness.check_xtquant_import', side_effect=AssertionError('legacy import')), \
             patch.dict(os.environ, {'QMT_ACCOUNT_ID':'TEST', 'QMT_PATH':'does-not-exist'}):
            client.return_value.heartbeat.return_value = {'mode':'read_only'}
            report = build_report({'trade_mode':'paper'})
        text = report.to_string()
        self.assertIn('qmt_inner_heartbeat', text)
        self.assertNotIn('xtquant_import', text)
        self.assertNotIn('QMT_PATH_EXISTS', text)

    def test_backup_includes_uncheckpointed_wal_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            state = root / 'data/state'
            state.mkdir(parents=True)
            (root / 'data/processed').mkdir()
            database = state / 'execution_events.sqlite3'
            conn = sqlite3.connect(database)
            try:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA wal_autocheckpoint=0')
                conn.execute('CREATE TABLE trade_intents(id TEXT)')
                conn.execute("INSERT INTO trade_intents VALUES ('pending')")
                conn.commit()
                result = backup(root, Path(temporary)/'backups', require_stopped=True)
                copied = Path(result['directory']) / 'data/state/execution_events.sqlite3'
                with closing(sqlite3.connect(copied)) as check:
                    self.assertEqual(check.execute('SELECT id FROM trade_intents').fetchall(), [('pending',)])
                self.assertEqual(conn.execute('SELECT id FROM trade_intents').fetchall(), [('pending',)])
                self.assertTrue((database.with_name(database.name+'-wal')).exists())
            finally:
                conn.close()

    def test_installer_refuses_to_replace_running_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            settings = dict(account_id='TEST', account_type='STOCK', token='x'*64,
                            mode='read_only', spool_dir=str(destination/'spool'))
            atomic_json(destination/'config.json', settings)
            engine = Engine(None, {}, settings)
            try:
                with self.assertRaisesRegex(RuntimeError, '正在运行'):
                    prepare(destination=destination)
                self.assertFalse((destination/'bundle').exists())
            finally:
                engine.close()
            result = prepare(destination=destination)
            self.assertEqual(result['mode'], 'read_only')
            self.assertTrue(Path(result['entry']).exists())
