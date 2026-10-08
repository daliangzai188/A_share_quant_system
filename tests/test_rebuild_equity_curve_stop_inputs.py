from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts import rebuild_equity_curve_stop_inputs as collect


def sample(kind: str, date: str) -> pd.DataFrame:
    columns = collect.ENDPOINTS[kind][1]
    row = dict.fromkeys(columns, 1)
    row.update(trade_date=date, ts_code="000001.SZ")
    if kind == "limit_list":
        row.update(name="测试", first_time="093000", last_time="093000", limit="U")
    return pd.DataFrame([row])


class HistoricalRecollectionTests(unittest.TestCase):
    def test_invalid_day_duplicate_and_empty_response_are_rejected(self) -> None:
        for kind in collect.ENDPOINTS:
            frame = sample(kind, "20261008")
            self.assertEqual(collect.validate(frame, "20261008", kind), 1)
            for label, bad in [("wrong date", frame.assign(trade_date="20261009")),
                               ("duplicate", pd.concat([frame, frame])), ("empty", frame.iloc[:0])]:
                with self.subTest(kind=kind, issue=label), self.assertRaises(ValueError):
                    collect.validate(bad, "20261008", kind)

    def test_incomplete_basic_cannot_be_recorded_as_collected(self) -> None:
        for column in ["volume_ratio", "turnover_rate_f", "free_share"]:
            with self.subTest(column=column), self.assertRaisesRegex(ValueError, "incomplete basic"):
                collect.validate(sample("daily_basic", "20261008").assign(**{column: None}), "20261008", "daily_basic")

    def test_basic_uses_configured_five_percent_limit(self) -> None:
        frame = pd.concat([sample("daily_basic", "20261008")] * 10, ignore_index=True)
        frame["ts_code"] = [f"{index:06d}.SZ" for index in range(10)]
        frame.loc[0, "volume_ratio"] = None
        with self.assertRaisesRegex(ValueError, "incomplete basic"):
            collect.validate(frame, "20261008", "daily_basic")
        self.assertEqual(collect.validate(frame, "20261008", "daily_basic", 0.2), 10)

    def test_isolated_recollection_is_resumable_and_does_not_generate_decision(self) -> None:
        calls: list[tuple[str, str]] = []

        class FakePro:
            def query(self, api: str, trade_date: str, **kwargs):
                calls.append((api, trade_date))
                kind = next((k for k, values in collect.ENDPOINTS.items() if values[0] == api), None)
                return sample(kind, trade_date) if kind else pd.DataFrame()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            target = Path(directory) / "isolated"
            (root / "config").mkdir(parents=True)
            (root / "config/config.json").write_text(json.dumps({"collection": {}}))
            calendar = root / "data/raw/trade_calendar.csv"
            calendar.parent.mkdir(parents=True)
            pd.DataFrame({"cal_date": ["20261008", "20261009"], "is_open": [1, 1]}).to_csv(calendar, index=False)
            args = ["collect", "--project-root", str(root), "--output", str(target),
                    "--start-date", "20261008", "--end-date", "20261009"]
            with patch("sys.argv", args), patch.object(collect.ts, "pro_api", return_value=FakePro()), \
                    patch("src.secret_config.load_tushare_token", return_value="FAKE_LOCAL_TEST_VALUE"), \
                    patch.object(collect.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                collect.main()
                first_calls = len(calls)
                collect.main()
                corrupt = target / "data/raw/daily/20261008.csv"
                corrupt.write_text("bad cache", encoding="utf-8")
                collect.main()
            result = json.loads((target / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "INPUTS_COLLECTED_NEEDS_VALIDATION")
            self.assertEqual(len(calls) - first_calls, 9)  # 两轮各4次探测，损坏缓存额外重拉1次。
            for kind in collect.ENDPOINTS:
                self.assertEqual(result["kinds"][kind]["reused"], 1 if kind == "daily" else 2)
                manifest = json.loads((target / "data/raw" / kind / "manifest.json").read_text())
                self.assertEqual(set(manifest), {"20261008", "20261009"})
            self.assertFalse((root / "data/raw/daily").exists())
            self.assertFalse((root / "data/state").exists())
            self.assertFalse((target / "data/state").exists())
            self.assertEqual(len(list((target / "rejected_cache/daily").glob("*.csv"))), 1)

    def test_output_inside_production_is_rejected_before_loading_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("sys.argv", ["collect", "--project-root", str(root), "--output", str(root / "data"), "--end-date", "20261008"]), \
                    self.assertRaisesRegex(RuntimeError, "outside production"):
                collect.main()
