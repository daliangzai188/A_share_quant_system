"""行情数据独立快照（tools/data_snapshot.py，2026-11-15）。"""
from __future__ import annotations

import datetime as dt
import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools import data_snapshot as ds


class DataSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.source = base / "repo" / "data"
        self.root = base / "snapshots"
        for rel, text in {"raw/daily/20261113.csv": "a,b\n1,2\n", "raw/trade_calendar.csv": "cal\n",
                          "processed/pool.csv": "x\n", "raw/中文 名.csv": "中\n"}.items():
            (self.source / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.source / rel).write_text(text, encoding="utf-8")
        (self.source / ".DS_Store").write_text("junk")
        (self.source / "raw" / "x.csv.tmp").write_text("partial")

    def tearDown(self) -> None:
        for folder, _dirs, names in os.walk(self._tmp.name):
            os.chmod(folder, stat.S_IRWXU)
        self._tmp.cleanup()

    def snap(self, minute: int) -> Path:
        return ds.take_snapshot(self.source, self.root, now=dt.datetime(2026, 11, 15, 8, minute))

    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = ds.main(["--root", str(self.root), "--source", str(self.source), *args])
        return code, out.getvalue()

    def test_snapshot_has_manifest_and_read_only_files_and_verifies(self) -> None:
        path = self.snap(0)
        entries = ds.read_manifest(path)
        self.assertEqual(sorted(entries), ["processed/pool.csv", "raw/daily/20261113.csv", "raw/trade_calendar.csv",
                                           "raw/中文 名.csv"])   # 临时文件和 .DS_Store 不算
        self.assertEqual(entries["processed/pool.csv"][1], 2)
        self.assertFalse(os.access(path / "files/processed/pool.csv", os.W_OK))
        self.assertEqual(ds.verify_snapshot(path), [])

    def test_verify_finds_changed_missing_and_extra_files(self) -> None:
        path = self.snap(0)
        target = path / "files/raw/trade_calendar.csv"
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        target.write_text("tampered\n")
        (path / "files/processed/pool.csv").unlink()
        (path / "files/raw/extra.csv").write_text("e\n")
        problems = ds.verify_snapshot(path)
        self.assertIn("内容不对 raw/trade_calendar.csv", problems)
        self.assertIn("缺文件 processed/pool.csv", problems)
        self.assertIn("清单外的文件 raw/extra.csv", problems)
        (path / ds.MANIFEST).write_text("0" * 64 + "  1  a\n", encoding="utf-8")
        self.assertIn("清单本身被改过", ds.verify_snapshot(path))

    def test_unchanged_files_are_hard_linked_to_the_previous_snapshot(self) -> None:
        first = self.snap(0)
        (self.source / "raw/trade_calendar.csv").write_text("cal\n20261116\n", encoding="utf-8")
        second = self.snap(1)
        same = (first / "files/processed/pool.csv").stat().st_ino == (second / "files/processed/pool.csv").stat().st_ino
        self.assertTrue(same)
        self.assertNotEqual((first / "files/raw/trade_calendar.csv").stat().st_ino,
                            (second / "files/raw/trade_calendar.csv").stat().st_ino)
        self.assertEqual((first / "files/raw/trade_calendar.csv").read_text(), "cal\n")
        self.assertEqual(ds.verify_snapshot(first), [])
        self.assertEqual(ds.verify_snapshot(second), [])

    def test_restore_check_restores_everything_and_reports_later_changes(self) -> None:
        self.snap(0)
        (self.source / "raw/trade_calendar.csv").write_text("changed\n")
        (self.source / "processed/pool.csv").unlink()
        code, out = self.run_cli("restore-check")
        self.assertEqual(code, 0, out)
        self.assertIn("逐个核对校验码全部一致", out)
        self.assertIn("快照之后改过 1 个、新增 0 个、不见了 1 个", out)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".restore")], [])

    def test_restore_writes_a_new_directory_but_never_into_the_data_folder(self) -> None:
        self.snap(0)
        target = Path(self._tmp.name) / "restored"
        code, out = self.run_cli("restore", "20261115-080000", str(target))
        self.assertEqual(code, 0, out)
        self.assertEqual((target / "raw/中文 名.csv").read_text(encoding="utf-8"), "中\n")
        self.assertTrue(os.access(target / "raw/中文 名.csv", os.W_OK))
        with self.assertRaises(SystemExit):
            self.run_cli("restore", "20261115-080000", str(self.source / "restored"))
        with self.assertRaises(SystemExit):
            self.run_cli("restore", "20261115-080000", str(target))      # 目标不是空的

    def test_prune_keeps_the_latest_and_their_shared_files(self) -> None:
        for minute in range(3):
            self.snap(minute)
        code, out = self.run_cli("prune", "--keep", "1")
        self.assertEqual(code, 0)
        self.assertEqual([p.name for p in ds.snapshots(self.root)], ["20261115-080200"])
        self.assertEqual(ds.verify_snapshot(self.root / "20261115-080200"), [])
        self.assertFalse(os.access(self.root / "20261115-080200/files/processed/pool.csv", os.W_OK))

    def test_snapshot_root_cannot_be_inside_the_data_folder(self) -> None:
        with self.assertRaises(SystemExit):
            ds.take_snapshot(self.source, self.source / "snapshots")


if __name__ == "__main__":
    unittest.main()
