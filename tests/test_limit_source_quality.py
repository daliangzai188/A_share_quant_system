from pathlib import Path
import tempfile
import unittest

import pandas as pd

from scripts.rebuild_equity_curve_stop_inputs import validate
from src.data_cleaner import DataCleaner
from src.limit_source_quality import suspended_placeholder_mask


class LimitSourceQualityTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame([
            dict(trade_date="20260428", ts_code="600001.SH", name="正常", close=11., pct_chg=10.,
                 fd_amount=1000, first_time="093000", last_time="143000", open_times=1, limit_times=1, limit="U"),
            dict(trade_date="20260428", ts_code="603272.SH", name="源占位", close=0., pct_chg=-100.,
                 fd_amount=0, first_time="0", last_time="0", open_times=0, limit_times=2, limit="U"),
        ])

    def test_absence_alone_or_missing_basic_row_cannot_trigger_filter(self):
        frame = self.frame()
        self.assertEqual(suspended_placeholder_mask(frame, {"600001.SH"}).tolist(), [False, True])
        self.assertFalse(suspended_placeholder_mask(frame, {"600001.SH", "603272.SH"}).any())
        self.assertFalse(suspended_placeholder_mask(frame.iloc[:1], set()).any())

    def test_raw_validation_requires_primary_quote_evidence(self):
        frame = self.frame()
        with self.assertRaisesRegex(ValueError, "invalid close"):
            validate(frame, "20260428", "limit_list")
        self.assertEqual(validate(frame, "20260428", "limit_list", daily_codes={"600001.SH"}), 2)
        with self.assertRaisesRegex(ValueError, "invalid close"):
            validate(frame, "20260428", "limit_list", daily_codes={"600001.SH", "603272.SH"})

    def test_cleaner_retains_raw_evidence_and_excludes_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            cleaner = DataCleaner()
            cleaner.daily_dir = Path(directory) / "daily"
            cleaner.limit_list_dir = Path(directory) / "limit"
            cleaner.daily_dir.mkdir()
            cleaner.limit_list_dir.mkdir()
            self.frame().to_csv(cleaner.limit_list_dir / "20260428.csv", index=False)
            raw = pd.DataFrame(dict(trade_date=["20260428"], ts_code=["600001.SH"], open=[10.],
                                    high=[11.], low=[10.], close=[11.], pre_close=[10.], pct_chg=[10.],
                                    vol=[100.], amount=[1000.], circ_mv=[10000.], turnover_rate=[1.]))
            raw.to_csv(cleaner.daily_dir / "20260428.csv", index=False)
            result = cleaner.clean_limit_up_by_date("20260428", raw)
            self.assertEqual(result.ts_code.tolist(), ["600001.SH"])
            self.assertEqual(len(pd.read_csv(cleaner.limit_list_dir / "20260428.csv")), 2)
            row = cleaner.build_market_sentiment_row("20260428", raw, result)
            self.assertEqual(row["limit_up_count"], 1)
