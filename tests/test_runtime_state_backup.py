from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.runtime_state_backup import (
    create_runtime_snapshot,
    restore_snapshot_to_staging,
    verify_runtime_snapshot,
)


class RuntimeStateBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "project"
        self.root.mkdir()
        (self.root / "config").mkdir()
        (self.root / "data/state").mkdir(parents=True)
        (self.root / "reports/execution_tracking").mkdir(parents=True)
        (self.root / "config/config.json").write_text(
            json.dumps({"mode": "paper"}), encoding="utf-8"
        )
        (self.root / "reports/execution_tracking/trades.csv").write_text(
            "trade_key,qty\nT1,100\n", encoding="utf-8"
        )
        connection = sqlite3.connect(self.root / "data/state/events.sqlite3")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO events(value) VALUES ('one')")
        connection.commit()
        connection.close()
        self.config = {
            "schema_version": 1,
            "snapshot_root": "backups/runtime_state",
            "items": [
                {"path": "config/config.json", "kind": "json", "required": True},
                {"path": "reports/execution_tracking/trades.csv", "kind": "csv", "required": True},
                {"path": "data/state/events.sqlite3", "kind": "sqlite", "required": True},
                {"path": "data/state/optional.json", "kind": "json", "required": False},
            ],
        }
        self.now = dt.datetime(2026, 8, 11, 6, 0, tzinfo=dt.timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_snapshot_verify_and_isolated_restore(self) -> None:
        snapshot, manifest = create_runtime_snapshot(
            self.root, self.config, now=self.now
        )

        self.assertEqual(manifest["status"], "PASS")
        self.assertEqual(manifest["file_count"], 3)
        self.assertEqual(manifest["optional_missing_count"], 1)
        self.assertEqual(verify_runtime_snapshot(snapshot)["status"], "PASS")

        target = Path(self.temp_dir.name) / "restored"
        report = restore_snapshot_to_staging(self.root, snapshot, target)
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["production_root_untouched"])
        self.assertEqual(
            json.loads((target / "config/config.json").read_text(encoding="utf-8")),
            {"mode": "paper"},
        )
        connection = sqlite3.connect(target / "data/state/events.sqlite3")
        try:
            self.assertEqual(connection.execute("SELECT value FROM events").fetchone(), ("one",))
        finally:
            connection.close()

    def test_tampered_snapshot_fails_hash_verification(self) -> None:
        snapshot, _manifest = create_runtime_snapshot(
            self.root, self.config, now=self.now
        )
        (snapshot / "files/config/config.json").write_text(
            json.dumps({"mode": "live"}), encoding="utf-8"
        )

        verification = verify_runtime_snapshot(snapshot)

        self.assertEqual(verification["status"], "FAIL")
        self.assertIn("不一致", verification["reason"])

    def test_truncated_empty_manifest_cannot_pass(self) -> None:
        snapshot, manifest = create_runtime_snapshot(
            self.root, self.config, now=self.now
        )
        manifest["files"] = []
        manifest["file_count"] = 0
        manifest["required_file_count"] = 0
        manifest["optional_missing_count"] = 0
        (snapshot / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        verification = verify_runtime_snapshot(snapshot)

        self.assertEqual(verification["status"], "FAIL")
        self.assertIn("文件列表为空", verification["reason"])

    def test_restore_refuses_production_tree_and_existing_target(self) -> None:
        snapshot, _manifest = create_runtime_snapshot(
            self.root, self.config, now=self.now
        )
        with self.assertRaisesRegex(ValueError, "生产项目目录"):
            restore_snapshot_to_staging(
                self.root, snapshot, self.root / "restore-drill"
            )
        existing = Path(self.temp_dir.name) / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(FileExistsError, "尚不存在"):
            restore_snapshot_to_staging(self.root, snapshot, existing)

    def test_missing_required_file_leaves_no_half_snapshot(self) -> None:
        config = dict(self.config)
        config["items"] = list(self.config["items"]) + [
            {"path": "data/state/missing-required.json", "kind": "json", "required": True}
        ]

        with self.assertRaisesRegex(FileNotFoundError, "缺少必需"):
            create_runtime_snapshot(self.root, config, now=self.now)

        snapshot_root = self.root / "backups/runtime_state"
        self.assertFalse(snapshot_root.exists() and any(snapshot_root.iterdir()))


if __name__ == "__main__":
    unittest.main()
