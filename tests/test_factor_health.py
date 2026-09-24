"""因子健康监控：用整个涨停池衡量条件集优势是否退化（比等成交快十倍）。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from src.factor_health import (
    attach_forward_returns,
    condition_mask,
    load_settings,
    monthly_edge,
    parse_condition_profiles,
    rolling_health,
)

ROOT = Path(__file__).resolve().parents[1]


class ConditionParsingTests(unittest.TestCase):
    def test_reads_a_and_c_profiles_from_formal_config(self) -> None:
        config = json.loads((ROOT / "config" / "strategy_config.json").read_text(encoding="utf-8"))
        parsed = parse_condition_profiles(config)
        self.assertGreaterEqual(len(parsed["A"]), 3)
        self.assertGreaterEqual(len(parsed["C"]), 5)
        columns = {column for branch in parsed["A"] for column, _ in branch}
        self.assertIn("fd_ratio_bucket", columns)

    def test_branches_union_conditions_intersect(self) -> None:
        frame = pd.DataFrame({"a": ["x", "x", "y", "y"], "b": ["1", "2", "1", "2"]})
        mask, used = condition_mask(frame, [[("a", ["x"]), ("b", ["1"])], [("a", ["y"]), ("b", ["2"])]])
        self.assertEqual(used, 2)
        self.assertEqual(list(mask), [True, False, False, True])

    def test_branch_with_missing_column_is_skipped_not_fatal(self) -> None:
        frame = pd.DataFrame({"a": ["x", "y"]})
        mask, used = condition_mask(frame, [[("a", ["x"])], [("missing", ["1"])]])
        self.assertEqual(used, 1)
        self.assertEqual(list(mask), [True, False])


class ForwardReturnTests(unittest.TestCase):
    def _write(self, folder: Path, date: str, rows: list[dict]) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(folder / f"{date}.csv", index=False)

    def test_entry_next_open_exit_t2_close_with_adjustment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily, adj = Path(tmp) / "daily", Path(tmp) / "adj"
            self._write(daily, "20260902", [{"ts_code": "000001.SZ", "trade_date": "20260902", "open": 10.0, "close": 10.5}])
            self._write(daily, "20260903", [{"ts_code": "000001.SZ", "trade_date": "20260903", "open": 11.0, "close": 12.0}])
            self._write(adj, "20260902", [{"ts_code": "000001.SZ", "trade_date": "20260902", "adj_factor": 2.0}])
            self._write(adj, "20260903", [{"ts_code": "000001.SZ", "trade_date": "20260903", "adj_factor": 2.0}])
            pool = pd.DataFrame({"trade_date": ["20260901"], "ts_code": ["000001.SZ"]})
            out = attach_forward_returns(pool, calendar_dates=["20260901", "20260902", "20260903"],
                                         daily_dir=daily, adj_dir=adj)
            self.assertEqual(len(out), 1)
            self.assertAlmostEqual(float(out.ret.iloc[0]), 12.0 / 10.0 - 1.0)

    def test_rows_without_quotes_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily, adj = Path(tmp) / "daily", Path(tmp) / "adj"
            self._write(daily, "20260902", [{"ts_code": "000001.SZ", "trade_date": "20260902", "open": 10.0, "close": 10.5}])
            pool = pd.DataFrame({"trade_date": ["20260901"], "ts_code": ["000001.SZ"]})
            out = attach_forward_returns(pool, calendar_dates=["20260901", "20260902", "20260903"],
                                         daily_dir=daily, adj_dir=adj)
            self.assertTrue(out.empty)


class RollingHealthTests(unittest.TestCase):
    @staticmethod
    def _monthly(edges: list[float], n: int = 20) -> pd.DataFrame:
        months = [f"2026{i:02d}" for i in range(1, len(edges) + 1)]
        return pd.DataFrame({"n": [n] * len(edges), "hit": edges, "miss": [0.0] * len(edges),
                             "edge": edges}, index=months)

    def test_monthly_edge_is_hit_minus_miss(self) -> None:
        frame = pd.DataFrame({"trade_date": ["20260105", "20260106", "20260205"],
                              "ret": [0.10, 0.00, -0.02]})
        mask = pd.Series([True, False, True], index=frame.index)
        out = monthly_edge(frame, mask)
        self.assertAlmostEqual(float(out.loc["202601", "edge"]), 0.10)
        self.assertEqual(int(out.loc["202601", "n"]), 1)

    def test_value_is_the_rolling_window_mean(self) -> None:
        monthly = self._monthly([0.03] * 10 + [-0.02, -0.03])
        out = rolling_health(monthly, window=12, min_periods=10, min_samples=60,
                             line=0.03, consecutive_months=2)
        self.assertAlmostEqual(out["value"], (0.03 * 10 - 0.02 - 0.03) / 12)
        self.assertEqual(out["as_of"], "202612")
        self.assertGreaterEqual(out["months_below"], 1)

    def test_not_triggered_before_required_months(self) -> None:
        # 只有最近一个滚动值跌破：上一个月仍在线上，连续数=1
        monthly = self._monthly([0.05] * 10 + [0.60, -1.50])
        out = rolling_health(monthly, window=12, min_periods=10, min_samples=60,
                             line=0.0, consecutive_months=2)
        self.assertEqual(out["months_below"], 1)
        self.assertFalse(out["triggered"])

    def test_triggers_after_two_consecutive_months(self) -> None:
        monthly = self._monthly([0.05] * 10 + [-0.60, -0.60])
        out = rolling_health(monthly, window=12, min_periods=10, min_samples=60,
                             line=0.0, consecutive_months=2)
        self.assertEqual(out["months_below"], 2)
        self.assertTrue(out["triggered"])

    def test_thin_samples_report_nothing(self) -> None:
        monthly = self._monthly([-0.5] * 12, n=1)
        out = rolling_health(monthly, window=12, min_periods=10, min_samples=60,
                             line=0.0, consecutive_months=2)
        self.assertIsNone(out["value"])
        self.assertFalse(out["triggered"])


class FormalConfigTests(unittest.TestCase):
    def test_frozen_lines_and_parameters(self) -> None:
        config = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        settings = load_settings(config)
        self.assertTrue(settings.enabled)
        self.assertEqual((settings.window, settings.min_periods, settings.min_samples), (12, 10, 60))
        self.assertEqual(settings.consecutive_months, 2)
        self.assertAlmostEqual(settings.lines["A"], 0.0018567462365949444)
        self.assertAlmostEqual(settings.lines["C"], -0.004743001882088658)


if __name__ == "__main__":
    unittest.main()
