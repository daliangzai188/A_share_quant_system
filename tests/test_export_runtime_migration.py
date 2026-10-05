"""只在临时目录测试，不读取或写入真实项目/券商账本。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts import export_runtime_migration as migration


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'project'
        self.spool = self.base / 'private/spool'
        self.output = self.base / 'exports'
        (self.root / 'config').mkdir(parents=True)
        (self.root / 'data/processed').mkdir(parents=True)
        (self.root / 'data/state').mkdir(parents=True)
        self.spool.mkdir(parents=True)
        (self.spool / 'owner.lock').write_bytes(b'0')
        (self.root / '.manual_stop.json').write_text('{"reason":"migration"}')
        (self.root / '.env').write_text('PRIVATE_TEST_TOKEN=not-for-export')
        (self.root / '.daemon_pid').write_text('999999')
        (self.root / 'data/processed/positions.json').write_text('[{"code":"test","volume":100}]')
        spec = {'items': [
            {'path': 'data/processed/positions.json', 'required': True},
            {'path': 'data/state/execution_events.sqlite3', 'required': True},
        ]}
        (self.root / 'config/runtime_state_backup.json').write_text(json.dumps(spec))
        self.active = sqlite3.connect(self.root / 'data/state/execution_events.sqlite3')
        self.active.execute('PRAGMA journal_mode=WAL')
        self.active.execute('PRAGMA wal_autocheckpoint=0')
        self.active.execute('CREATE TABLE intents (id TEXT PRIMARY KEY, status TEXT)')
        self.active.execute("INSERT INTO intents VALUES ('one','submitted')")
        self.active.commit()
        with closing(sqlite3.connect(self.spool / 'journal.sqlite3')) as conn:
            conn.execute('CREATE TABLE submissions (tag TEXT PRIMARY KEY)')
            conn.execute("INSERT INTO submissions VALUES ('original-order')")
            conn.commit()
        (self.spool / 'requests').mkdir()
        (self.spool / 'requests/pending.json').write_text('{"method":"submit"}')
        self.cfg = {'spool_dir': str(self.spool), 'token': 'private-test-secret'}

    def tearDown(self):
        self.active.close()
        self.temp.cleanup()

    def test_wal_and_private_order_history_preserved(self):
        target, count = migration.export_snapshot(self.root, self.cfg, self.output)
        self.assertEqual(migration.verify_snapshot(target), count)
        self.assertEqual(migration.sqlite_check(target / 'project/data/state/execution_events.sqlite3'), {'intents': 1})
        self.assertEqual(migration.sqlite_check(target / 'private_qmt/journal.sqlite3'), {'submissions': 1})
        self.assertTrue((target / 'evidence/old_spool/requests/pending.json').exists())
        self.assertFalse((target / 'private_qmt/requests').exists())
        self.assertFalse(list(target.rglob('*.sqlite3-wal')))
        self.assertFalse(list(target.rglob('.env')))
        self.assertFalse(list(target.rglob('.daemon_pid')))
        self.assertNotIn('private-test-secret', (target / 'manifest.json').read_text())

    def test_tampered_snapshot_is_rejected(self):
        target, _ = migration.export_snapshot(self.root, self.cfg, self.output)
        (target / 'project/data/processed/positions.json').write_text('[]')
        with self.assertRaises(RuntimeError):
            migration.verify_snapshot(target)

    def test_missing_required_file_leaves_no_partial_backup(self):
        (self.root / 'data/processed/positions.json').unlink()
        with self.assertRaises(FileNotFoundError):
            migration.export_snapshot(self.root, self.cfg, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_running_model_lock_blocks_export(self):
        with migration.model_lock(self.spool):
            with self.assertRaises(OSError):
                migration.export_snapshot(self.root, self.cfg, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_export_into_synced_project_is_rejected(self):
        with self.assertRaises(ValueError):
            migration.export_snapshot(self.root, self.cfg, self.root / 'exports')

    def test_missing_stop_marker_blocks_export(self):
        (self.root / '.manual_stop.json').unlink()
        with self.assertRaises(RuntimeError):
            migration.export_snapshot(self.root, self.cfg, self.output)

    def test_path_traversal_is_rejected(self):
        for relative in ('../outside', '/tmp/outside', 'C:/outside', 'project\\..\\outside'):
            with self.assertRaises(ValueError):
                migration.safe_relative(relative)


if __name__ == '__main__':
    unittest.main()
