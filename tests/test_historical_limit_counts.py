from __future__ import annotations

import csv
import hashlib
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.data_cleaner import DataCleaner
from src.historical_limit_counts import load_historical_limit_count


class HistoricalCountTests(unittest.TestCase):
    def fixture(self, root: Path):
        raw = root / "data/raw/kpl_limit_list/20190102.csv"
        raw.parent.mkdir(parents=True)
        pd.DataFrame([
            {"trade_date": "20190102", "ts_code": "600001.SH", "name": "测试甲", "tag": "涨停"},
            {"trade_date": "20190102", "ts_code": "300001.SZ", "name": "测试乙", "tag": "涨停"},
            {"trade_date": "20190102", "ts_code": "000001.SZ", "name": "ST测试", "tag": "涨停"},
        ]).to_csv(raw, index=False, encoding="utf-8-sig")
        table = raw.parent.parent / "market_limit_counts.csv"
        pd.DataFrame([{"trade_date": "20190102", "limit_up_count": 2, "source_row_count": 3,
                       "source": "kpl_list", "scope": "SH_SZ_NON_ST",
                       "raw_file": "data/raw/kpl_limit_list/20190102.csv",
                       "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest()}]).to_csv(table, index=False)
        return raw, table

    def test_real_count_is_used_but_execution_fields_remain_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root)
            cleaner = DataCleaner.__new__(DataCleaner)
            cleaner.project_root = root
            cleaner.limit_list_start_date = "20191128"
            daily = pd.DataFrame({"ts_code": ["600001.SH", "300001.SZ"], "pct_chg": [10.0, 10.0], "amount": [100, 100]})
            row = cleaner.build_market_sentiment_row("20190102", daily, pd.DataFrame())
            self.assertEqual(row["limit_up_count"], 2)
            self.assertEqual(row["sh_main_limit_up_count"], 1)
            self.assertEqual(row["chi_next_limit_up_count"], 1)
            self.assertEqual(row["limit_data_quality"], "counts_only")
            self.assertFalse(row["strategy_compatible"])
            self.assertIsNone(row["opened_limit_count"])
            self.assertIsNone(row["limit_up_fd_amount_sum"])

    def test_missing_source_is_not_treated_as_zero_and_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaisesRegex(RuntimeError, "不可按0"):
                load_historical_limit_count(root, "20190102")
            raw, _ = self.fixture(root)
            self.assertEqual(load_historical_limit_count(root, "20190102").count, 2)
            raw.write_bytes(raw.read_bytes() + b"\n")
            with self.assertRaisesRegex(RuntimeError, "哈希"):
                load_historical_limit_count(root, "20190102")

    def test_available_modern_pool_does_not_depend_on_early_archive(self):
        with tempfile.TemporaryDirectory() as d:
            cleaner = DataCleaner.__new__(DataCleaner)
            cleaner.project_root = Path(d)
            cleaner.limit_list_start_date = "20191128"
            row = {"limit_up_count": 100, "limit_data_quality": "full"}
            self.assertIs(cleaner._apply_historical_limit_count("20261008", row), row)
            self.assertEqual(row["limit_up_count"], 100)


if __name__ == "__main__":
    unittest.main()
