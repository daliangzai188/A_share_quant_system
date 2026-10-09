from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.strategy_optimizer import StrategyConditionOptimizer


class DailyHistoryConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.optimizer = StrategyConditionOptimizer()
        self.optimizer.raw_daily_dir = self.root / "daily"
        self.optimizer.raw_daily_basic_dir = self.root / "daily_basic"
        self.optimizer.daily_merged_by_date_dir = self.root / "partitions"
        self.optimizer.input_trades_path = self.root / "live_candidates.csv"
        for path in [self.optimizer.raw_daily_dir, self.optimizer.raw_daily_basic_dir,
                     self.optimizer.daily_merged_by_date_dir]:
            path.mkdir()
        for date, pct, amount in [("20260929", 1, 100), ("20260930", 2, 200),
                                  ("20261008", 3, 300), ("20261009", 4, 600)]:
            pd.DataFrame([{"trade_date": date, "ts_code": "000001.SZ", "pct_chg": pct,
                           "amount": amount}]).to_csv(self.optimizer.raw_daily_dir / f"{date}.csv", index=False)
            pd.DataFrame([{"trade_date": date, "ts_code": "000001.SZ", "turnover_rate": 2}]).to_csv(
                self.optimizer.raw_daily_basic_dir / f"{date}.csv", index=False)

    def test_conflict_files_cannot_duplicate_or_shift_previous_trade_days(self) -> None:
        conflict = self.optimizer.raw_daily_dir / "20261008.sync-conflict-20261008-214310-TLV5W4A.csv"
        conflict.write_bytes((self.optimizer.raw_daily_dir / "20261008.csv").read_bytes())
        (self.optimizer.daily_merged_by_date_dir / "20261008.backup.csv").write_bytes(conflict.read_bytes())
        (self.optimizer.raw_daily_dir / "20260230.csv").write_bytes(conflict.read_bytes())
        self.assertEqual(self.optimizer.expand_needed_daily_dates(["20261009"]),
                         ["20260930", "20261008", "20261009"])
        trades = pd.DataFrame([{"trade_date": "20261009", "ts_code": "000001.SZ"}])
        result = self.optimizer.add_historical_features(trades)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0].prev_pct_chg, 3)
        self.assertEqual(result.iloc[0].prev2_pct_chg, 2)
        self.assertEqual(result.iloc[0].prev_amount, 300)
        self.assertEqual(result.iloc[0].amount_ratio_1d, 2)
        self.assertTrue(conflict.exists())

    def test_direct_conflict_date_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "YYYYMMDD"):
            self.optimizer.load_daily_one_date("20261008.sync-conflict-backup", ["trade_date", "ts_code"])

    def test_real_duplicate_keys_in_canonical_file_still_fail_closed(self) -> None:
        path = self.optimizer.raw_daily_dir / "20261008.csv"
        frame = pd.read_csv(path)
        pd.concat([frame, frame], ignore_index=True).to_csv(path, index=False)
        with self.assertRaises(pd.errors.MergeError):
            self.optimizer.add_historical_features(pd.DataFrame([{"trade_date": "20261009", "ts_code": "000001.SZ"}]))


if __name__ == "__main__":
    unittest.main()
