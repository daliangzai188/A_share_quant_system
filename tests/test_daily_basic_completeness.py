"""每日基本面完整性：15:52拉到的半成品（量比/自由流通字段为空）不能永久留在本地。"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from src.data_collector import DataCollector

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {"collection": {"daily_basic_required_complete": ["volume_ratio", "turnover_rate_f", "free_share"],
                         "daily_basic_max_null_ratio": 0.05,
                         "daily_basic_fields": "ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,free_share"}}


def frame(volume_ratio: float | None = 1.2, rows: int = 100, drop: tuple[str, ...] = ()) -> pd.DataFrame:
    data = pd.DataFrame({
        "ts_code": [f"{i:06d}.SZ" for i in range(rows)],
        "trade_date": ["20260930"] * rows,
        "turnover_rate": [3.0] * rows,
        "turnover_rate_f": [5.0] * rows,
        "volume_ratio": [np.nan if volume_ratio is None else volume_ratio] * rows,
        "free_share": [1000.0] * rows,
    })
    return data.drop(columns=list(drop))


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        collector = DataCollector.__new__(DataCollector)
        collector.config = CONFIG
        collector.logger = MagicMock()
        collector.daily_basic_dir = Path(self.tmp.name)
        collector.data_source = MagicMock()
        collector.daily_basic_incomplete_dates = []
        self.collector = collector
        self.path = Path(self.tmp.name) / "20260930.csv"

    def test_complete_file_is_skipped(self) -> None:
        frame().to_csv(self.path, index=False)
        self.assertFalse(self.collector.collect_daily_basic_by_date("20260930"))
        self.collector.data_source.get_daily_basic.assert_not_called()

    def test_half_finished_file_is_refetched_and_overwritten(self) -> None:
        frame(volume_ratio=None).to_csv(self.path, index=False)          # 15:52拉到的半成品
        self.collector.data_source.get_daily_basic.return_value = frame(volume_ratio=1.5)
        self.assertTrue(self.collector.collect_daily_basic_by_date("20260930"))
        saved = pd.read_csv(self.path)
        self.assertEqual(float(saved["volume_ratio"].isna().mean()), 0.0)
        self.assertEqual(self.collector.daily_basic_incomplete_dates, [])

    def test_still_incomplete_is_saved_and_reported(self) -> None:
        self.collector.data_source.get_daily_basic.return_value = frame(volume_ratio=None)
        self.assertTrue(self.collector.collect_daily_basic_by_date("20260930"))
        self.assertTrue(self.path.exists())
        self.assertEqual(self.collector.daily_basic_incomplete_dates, ["20260930"])

    def test_missing_column_counts_as_incomplete(self) -> None:
        frame(drop=("free_share",)).to_csv(self.path, index=False)
        self.collector.data_source.get_daily_basic.return_value = frame()
        self.assertTrue(self.collector.collect_daily_basic_by_date("20260930"))
        self.collector.data_source.get_daily_basic.assert_called_once()

    def test_a_few_missing_stocks_still_complete(self) -> None:
        data = frame()
        data.loc[:2, "volume_ratio"] = np.nan                            # 3%：新股/停牌等正常缺值
        self.assertTrue(self.collector.daily_basic_is_complete(data))

    def test_overwrite_forces_refetch(self) -> None:
        frame().to_csv(self.path, index=False)
        self.collector.data_source.get_daily_basic.return_value = frame()
        self.assertTrue(self.collector.collect_daily_basic_by_date("20260930", overwrite=True))

    def test_formal_config_declares_required_fields(self) -> None:
        config = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["collection"]["daily_basic_required_complete"],
                         ["volume_ratio", "turnover_rate_f", "free_share"])
        self.assertEqual(config["collection"]["daily_basic_max_null_ratio"], 0.05)
        self.assertEqual(config["equity_curve_stop"]["research_max_volume_ratio_null"], 0.5)


def _load_step13():
    spec = importlib.util.spec_from_file_location("update_equity_curve_stop", ROOT / "scripts" / "update_equity_curve_stop.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ResearchCoverageGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_step13()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pool.csv"

    def _pool(self, rows: dict[str, list]) -> None:
        records = [{"trade_date": d, "volume_ratio": v} for d, vals in rows.items() for v in vals]
        pd.DataFrame(records).to_csv(self.path, index=False)

    def test_missing_history_day_stops_step13(self) -> None:
        self._pool({"20260929": [np.nan] * 20, "20260930": [1.1] * 20})
        with self.assertRaisesRegex(RuntimeError, "研究池量比缺失"):
            self.module.check_volume_ratio_coverage(self.path, "20260930", 0.5)

    def test_only_latest_day_pending_is_a_warning(self) -> None:
        self._pool({"20260929": [1.1] * 20, "20260930": [np.nan] * 20})
        self.assertEqual(self.module.check_volume_ratio_coverage(self.path, "20260930", 0.5), ["20260930"])

    def test_one_missing_stock_in_small_pool_passes(self) -> None:
        self._pool({"20260929": [1.1] * 12 + [np.nan], "20260930": [1.1] * 15})
        self.assertEqual(self.module.check_volume_ratio_coverage(self.path, "20260930", 0.5), [])

    def test_refresh_failure_does_not_skip_the_gate(self) -> None:
        """重拉失败不阻断构建，但覆盖率门禁照样执行：数据仍缺就报错。"""
        root = Path(self.tmp.name)

        class FakeBuilder:
            def __init__(self, research_root):
                self.root = Path(research_root)

            def build_base_tables(self, **kwargs):
                self.root.mkdir(parents=True, exist_ok=True)

            def build_strict_features(self, amount):
                pd.DataFrame({"trade_date": ["20260929"] * 5 + ["20260930"] * 5,
                              "volume_ratio": [np.nan] * 5 + [1.0] * 5}).to_csv(self.root / "strict_feature_pool.csv", index=False)

        settings = SimpleNamespace(work_dir=root, history_start="20190101")
        with patch.object(self.module, "refresh_recent_daily_basic", side_effect=RuntimeError("tushare down")), \
                patch.object(self.module, "require_history", return_value={"status": "PASS"}), \
                patch("src.five_year_research.FiveYearResearchDatasetBuilder", FakeBuilder):
            with self.assertRaisesRegex(RuntimeError, "研究池量比缺失"):
                self.module.build_dataset(settings, "20260930")


if __name__ == "__main__":
    unittest.main()
