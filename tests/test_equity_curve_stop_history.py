from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

from src.equity_curve_stop_history import audit_history, require_history


class StopHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {"cleaning": {"limit_list_start_date": "20191128"}}
        self.write("data/raw/trade_calendar.csv", ["cal_date", "is_open"], [
            ["20261007", 0], ["20261008", 1], ["20261009", 1], ["20261012", 1],
        ])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, relative: str, columns: list[str], rows: list[list[object]]) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(columns)
            writer.writerows(rows)

    def fill(self, date: str) -> None:
        for kind, columns in {
            "daily": ["trade_date", "ts_code", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"],
            "daily_basic": ["trade_date", "ts_code", "turnover_rate", "volume_ratio", "circ_mv"],
            "limit_list": ["trade_date", "ts_code", "first_time", "last_time", "open_times", "fd_amount", "limit_times"],
        }.items():
            self.write(f"data/raw/{kind}/{date}.csv", columns, [[date, "000001.SZ", *([1] * (len(columns) - 2))]])

    def test_missing_previous_signal_date_is_explained_and_remains_closed(self) -> None:
        self.fill("20261009")
        with self.assertRaisesRegex(RuntimeError, "daily缺少或无效1天（首日20261008）"):
            require_history(self.root, self.config, "20261009", "20261009")
        self.assertFalse((self.root / "data/state/equity_curve_stop_decision.json").exists())

    def test_middle_day_missing_is_not_hidden_by_first_and_last_files(self) -> None:
        self.fill("20261008")
        self.fill("20261012")
        report = audit_history(self.root, self.config, "20261008", "20261012")
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(set(report["groups"]["daily"]["problems"]), {"20261009"})

    def test_complete_inputs_pass_without_writing_a_decision(self) -> None:
        self.fill("20261008")
        self.fill("20261009")
        report = require_history(self.root, self.config, "20261008", "20261009")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["expected_trade_days"], 2)
        self.assertFalse((self.root / "data/state").exists())

    def test_empty_or_basic_limit_file_is_rejected(self) -> None:
        self.fill("20261008")
        self.fill("20261009")
        path = self.root / "data/raw/limit_list/20261009.csv"
        with path.open(encoding="utf-8-sig") as stream:
            columns = next(csv.reader(stream))
        self.write(str(path.relative_to(self.root)), columns, [])
        self.assertIn("20261009", audit_history(self.root, self.config, "20261008", "20261009")["groups"]["limit_list"]["problems"])
        self.write(str(path.relative_to(self.root)), columns + ["limit_data_quality"], [["20261009", "000001.SZ", *([1] * (len(columns) - 2)), "basic"]])
        self.assertIn("不是完整", audit_history(self.root, self.config, "20261008", "20261009")["groups"]["limit_list"]["problems"]["20261009"])

    def test_closed_end_date_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "未覆盖收盘日"):
            audit_history(self.root, self.config, "20261008", "20261010")
