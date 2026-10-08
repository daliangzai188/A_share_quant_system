from __future__ import annotations

import contextlib
import csv
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts import market_data_backup as backup


EXPORT = Path(__file__).resolve().parents[1]


class MarketDataBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        shutil.copyfile(EXPORT / "config/market_data_backup.json", self.root / backup.POLICY_PATH)
        self.policy = json.loads((self.root / backup.POLICY_PATH).read_text())
        self.csv("data/raw/trade_calendar.csv", ["cal_date", "is_open"],
                 [["20261008", "1"], ["20261009", "1"]])
        for group in self.policy["daily_groups"].values():
            for date in ("20261008", "20261009"):
                fields = group["required_columns"]
                values = {name: "1" for name in fields}
                values.update(trade_date=date, ts_code="000001.SZ", limit="U")
                self.csv(f"{group['path']}/{date}.csv", fields, [[values[field] for field in fields]])
        for relative in self.policy["required_files"]:
            if not (self.root / relative).exists():
                self.csv(relative, ["trade_date", "limit_up_count"], [["20261008", "20"], ["20261009", "30"]])

    def csv(self, relative: str, fields: list[str], rows: list[list[str]]) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)

    def create(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return backup.main(["create", "--project-root", str(self.root),
                                "--start-date", "20261008", "--end-date", "20261009"])

    def test_valid_snapshot_survives_copy_and_detects_byte_tampering(self) -> None:
        self.assertEqual(self.create(), 0)
        self.assertEqual(backup.verify(self.root)["status"], "RESTORE_VERIFIED")
        with tempfile.TemporaryDirectory() as destination:
            clone = Path(destination) / "clone"
            shutil.copytree(self.root, clone)
            self.assertEqual(backup.verify(clone)["status"], "RESTORE_VERIFIED")
            path = clone / "data/raw/daily/20261008.csv"
            path.write_bytes(path.read_bytes().replace(b"000001.SZ", b"000002.SZ"))
            result = backup.verify(clone)
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertIn("data/raw/daily/20261008.csv", result["problems"])

    def test_missing_day_empty_pool_and_unresolved_lfs_are_not_complete(self) -> None:
        path = self.root / "data/raw/limit_list/20261008.csv"
        original = path.read_bytes()
        for contents in (None, original.splitlines(keepends=True)[0],
                         backup.LFS_PREFIX + b"\noid sha256:abc\nsize 100\n"):
            with self.subTest(contents=contents):
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(contents)
                self.assertEqual(self.create(), 1)
                self.assertFalse((self.root / self.policy["manifest_path"]).exists())

    def test_missing_early_market_counts_still_block_snapshot(self) -> None:
        self.csv("data/research/five_year_strict/market_sentiment.csv", ["trade_date", "limit_up_count"],
                 [["20261009", "30"]])
        result = backup.audit(self.root, "20261008", "20261009")
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("20261008", result["problems"]["data/research/five_year_strict/market_sentiment.csv"])

    def test_snapshot_excludes_secrets_and_account_ledgers(self) -> None:
        for relative in (".env", "data/state/positions.json", "data/database/execution_events.sqlite3"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE_TEST_VALUE")
        self.assertEqual(self.create(), 0)
        saved = json.loads((self.root / self.policy["manifest_path"]).read_text())
        content = json.dumps(saved)
        self.assertNotIn("PRIVATE_TEST_VALUE", content)
        self.assertFalse(any(record["path"].startswith(("data/state/", "data/database/")) for record in saved["files"]))

    def test_traversal_duplicate_dates_and_symlink_escape_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            backup.safe_path(self.root, "data/../../outside.csv")
        with self.assertRaises(ValueError):
            backup.safe_path(self.root, ".env")
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "data/escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                backup.safe_path(self.root, "data/escape/file.csv")
        self.csv("data/raw/trade_calendar.csv", ["cal_date", "is_open"], [["20261008", "1"], ["20261008", "1"]])
        with self.assertRaisesRegex(ValueError, "重复"):
            backup.audit(self.root, "20261008", "20261008")

    def test_git_rules_keep_required_market_files_and_exclude_runtime(self) -> None:
        for name in (".gitignore", ".gitattributes"):
            shutil.copyfile(EXPORT / name, self.root / name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        tracked = ["data/raw/adj_factor/20261008.csv", "data/raw/daily/20261008.csv",
                   "data/raw/daily_basic/20261008.csv", "data/raw/limit_list/20261008.csv",
                   "data/processed/daily_merged.csv", "data/processed/daily_merged_by_date/20261008.csv",
                   "data/raw/minute/000001.SZ/20261008.csv", "data/raw/minute_d/000001_20261008.csv",
                   "data/raw/kpl_limit_list/20190102.csv", "data/raw/market_limit_counts.csv",
                   "data/research/monthly_acde/20260831/strict_feature_pool.csv",
                   "data/research/monthly_acde/20260831/market_sentiment.csv",
                   "data/research/five_year_strict/strict_feature_pool.csv", "data/market_backup/manifest.json"]
        excluded = [".env", ".env.local", "data/state/positions.json", "data/database/execution_events.sqlite3",
                    "logs/trading_daemon.log", "data/research/strategy_d_l2_vendor_sample/book.csv"]
        for relative in tracked + excluded:
            result = subprocess.run(["git", "check-ignore", "-q", relative], cwd=self.root)
            self.assertEqual(result.returncode, 0 if relative in excluded else 1, relative)
        result = subprocess.run(["git", "check-attr", "filter", "--", *tracked[:-1]],
                                cwd=self.root, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.count(": filter: lfs"), len(tracked) - 1)
        reference = subprocess.run(["git", "check-attr", "filter", "--", "data/raw/trade_calendar.csv"],
                                   cwd=self.root, text=True, capture_output=True, check=True)
        self.assertIn("unspecified", reference.stdout)

    def test_early_group_is_bounded_by_its_actual_source_range(self) -> None:
        result = backup.audit(self.root, "20261008", "20261009")
        self.assertEqual(result["groups"]["kpl_limit_list"]["expected_dates"], 0)
        self.assertEqual(result["status"], "BACKUP_COMPLETE")


if __name__ == "__main__":
    unittest.main()
