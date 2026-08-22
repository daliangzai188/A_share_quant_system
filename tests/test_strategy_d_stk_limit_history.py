from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from scripts.collect_strategy_d_stk_limit_history import (
    audit_existing,
    collect_missing,
    normalize_stk_limit,
)


def sample(date: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": [date] * 100,
            "ts_code": [f"{index:06d}.SZ" for index in range(100)],
            "pre_close": [10.0] * 100,
            "up_limit": [11.0] * 100,
            "down_limit": [9.0] * 100,
        }
    )


class StrategyDStkLimitHistoryTest(unittest.TestCase):
    def test_normalize_rejects_duplicate_stock(self) -> None:
        frame = sample("20240701")
        frame.loc[1, "ts_code"] = frame.loc[0, "ts_code"]
        with self.assertRaisesRegex(ValueError, "为空或重复"):
            normalize_stk_limit(frame, trade_date="20240701")

    def test_collect_saves_atomically_and_resume_skips_complete_date(self) -> None:
        class Source:
            calls: list[str] = []

            def get_stk_limit(self, *, trade_date: str, fields: str) -> pd.DataFrame:
                self.calls.append(trade_date)
                self.assert_fields = fields
                return sample(trade_date)

        with TemporaryDirectory() as directory:
            output = Path(directory)
            source = Source()
            dates = ["20240701", "20240702"]
            first = collect_missing(
                source,
                dates,
                output_dir=output,
                request_interval_seconds=0,
                sleep_fn=lambda _seconds: None,
            )
            second = collect_missing(
                source,
                dates,
                output_dir=output,
                request_interval_seconds=0,
                sleep_fn=lambda _seconds: None,
            )

            self.assertTrue(first["passed"])
            self.assertEqual(first["saved_this_run_count"], 2)
            self.assertTrue(second["passed"])
            self.assertEqual(second["saved_this_run_count"], 0)
            self.assertEqual(source.calls, dates)
            self.assertFalse(list(output.glob("*.tmp")))

    def test_audit_marks_wrong_date_invalid(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory)
            sample("20240702").to_csv(output / "20240701.csv", index=False)
            complete, invalid = audit_existing(["20240701"], output_dir=output)
            self.assertEqual(complete, [])
            self.assertEqual(invalid[0]["trade_date"], "20240701")


if __name__ == "__main__":
    unittest.main()
