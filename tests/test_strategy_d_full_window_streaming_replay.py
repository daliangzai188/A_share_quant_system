from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pandas as pd

from scripts.build_strategy_d_intraday_event_ledger import (
    iter_minute_groups,
    replay_ledger_streaming,
)


def minute_row(date: str, code: str, hhmm: int) -> dict[str, object]:
    return {
        "trade_date": date,
        "ts_code": code,
        "hhmm": hhmm,
        "open": 10.0,
        "high": 11.0,
        "low": 10.0,
        "close": 10.5,
        "volume": 100.0,
        "amount": 1_000.0,
    }


def complete_minute_rows(date: str, code: str) -> list[dict[str, object]]:
    morning = [hour * 100 + minute for hour in (9, 10, 11) for minute in range(60)]
    morning = [value for value in morning if 930 <= value <= 1130]
    afternoon = [hour * 100 + minute for hour in (13, 14, 15) for minute in range(60)]
    afternoon = [value for value in afternoon if 1301 <= value <= 1500]
    return [minute_row(date, code, hhmm) for hhmm in morning + afternoon]


class StrategyDFullWindowStreamingReplayTest(unittest.TestCase):
    def test_iterator_preserves_group_split_across_chunks(self) -> None:
        rows = [
            minute_row("20240102", "000001.SZ", 930),
            minute_row("20240102", "000001.SZ", 931),
            minute_row("20240102", "000002.SZ", 930),
            minute_row("20240102", "000002.SZ", 931),
            minute_row("20240103", "000001.SZ", 930),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "minute.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            groups = list(iter_minute_groups(path, chunksize=3))

        self.assertEqual(
            [key for key, _ in groups],
            [
                ("20240102", "000001.SZ"),
                ("20240102", "000002.SZ"),
                ("20240103", "000001.SZ"),
            ],
        )
        self.assertEqual([len(group) for _, group in groups], [2, 2, 1])

    def test_known_gap_is_kept_in_denominator_as_no_signal(self) -> None:
        mother = pd.DataFrame(
            [
                {
                    "trade_date": "20240102",
                    "ts_code": "000001.SZ",
                    "name": "测试一",
                    "limit_price": 11.0,
                    "daily_high": 11.0,
                    "daily_close": 10.5,
                },
                {
                    "trade_date": "20240102",
                    "ts_code": "920001.BJ",
                    "name": "缺口样本",
                    "limit_price": 13.0,
                    "daily_high": 13.0,
                    "daily_close": 12.0,
                },
            ]
        )
        rows = complete_minute_rows("20240102", "000001.SZ")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "minute.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            ledger, _, coverage = replay_ledger_streaming(
                mother,
                path,
                data_source="UNIT_TEST",
                known_gap_keys={("20240102", "920001.BJ")},
                chunksize=1,
            )

        gap_ledger = ledger.loc[ledger["ts_code"].eq("920001.BJ")].iloc[0]
        gap_coverage = coverage.loc[coverage["ts_code"].eq("920001.BJ")].iloc[0]
        self.assertEqual(len(ledger), 2)
        self.assertEqual(str(gap_coverage["minute_status"]), "MISSING_MINUTE_DATA")
        self.assertEqual(int(gap_coverage["bar_count"]), 0)
        self.assertFalse(bool(gap_ledger["signal_rule_current"]))
        self.assertEqual(str(gap_ledger["execution_status"]), "NO_PATH_SIGNAL")

    def test_unregistered_missing_target_fails_fast(self) -> None:
        mother = pd.DataFrame(
            [
                {
                    "trade_date": "20240102",
                    "ts_code": "000001.SZ",
                    "name": "测试一",
                    "limit_price": 11.0,
                    "daily_high": 11.0,
                    "daily_close": 10.5,
                },
                {
                    "trade_date": "20240102",
                    "ts_code": "000002.SZ",
                    "name": "未备案缺口",
                    "limit_price": 11.0,
                    "daily_high": 11.0,
                    "daily_close": 10.5,
                },
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "minute.csv"
            pd.DataFrame(complete_minute_rows("20240102", "000001.SZ")).to_csv(
                path, index=False
            )
            with self.assertRaisesRegex(RuntimeError, "未备案缺口"):
                replay_ledger_streaming(
                    mother,
                    path,
                    data_source="UNIT_TEST",
                    known_gap_keys=set(),
                    chunksize=1,
                )


if __name__ == "__main__":
    unittest.main()
