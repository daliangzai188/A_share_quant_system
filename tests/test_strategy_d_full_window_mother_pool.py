from __future__ import annotations

from pathlib import Path
import unittest

import pandas as pd

from scripts.build_strategy_d_intraday_event_ledger import (
    EXPECTED_FAILED_CLOSE_COUNT,
    EXPECTED_LEGACY_STRONG_SUBPOOL_COUNT,
    EXPECTED_MOTHER_POOL_COUNT,
    EXPECTED_STATIC_D_POOL_COUNT,
    EXPECTED_WINDOW_OPEN_DAY_COUNT,
    historical_name,
    load_historical_identity_overrides,
)


ROOT = Path(__file__).resolve().parents[1]
OLD_MOTHER = ROOT / "data/research/strategy_d_intraday/mother_pool.csv"
FULL_MOTHER = (
    ROOT / "data/research/strategy_d_intraday/mother_pool_full_window.csv"
)
FULL_TARGETS = (
    ROOT
    / "data/research/strategy_d_intraday/minute_target_manifest_full_window.csv"
)


class StrategyDFullWindowMotherPoolTest(unittest.TestCase):
    def test_precise_historical_identity_override_does_not_leak_dates(self) -> None:
        overrides = load_historical_identity_overrides()

        exact = historical_name(
            "300114.SZ", "20241010", {}, {}, overrides
        )
        outside = historical_name(
            "300114.SZ", "20241011", {}, {}, overrides
        )

        self.assertEqual(exact[0], "中航电测")
        self.assertEqual(
            exact[1], "TUSHARE_LIMIT_LIST_D_Z_AND_BAK_BASIC_ASOF"
        )
        self.assertEqual(outside, ("", "CURRENT_NAME_FALLBACK"))

    def test_generated_full_window_manifest_preserves_legacy_subpool(self) -> None:
        old = pd.read_csv(
            OLD_MOTHER, dtype={"trade_date": str, "ts_code": str}, low_memory=False
        )
        full = pd.read_csv(
            FULL_MOTHER, dtype={"trade_date": str, "ts_code": str}, low_memory=False
        )
        targets = pd.read_csv(
            FULL_TARGETS,
            dtype={"trade_date": str, "ts_code": str, "target_key": str},
            low_memory=False,
        )
        full_keys = full["trade_date"] + "|" + full["ts_code"]
        old_keys = old["trade_date"] + "|" + old["ts_code"]
        legacy_mask = full[
            "historical_is_current_final_close_strong_day"
        ].astype(bool)
        failed_mask = full["failed_to_close_at_limit"].astype(bool)

        self.assertEqual(full["trade_date"].nunique(), EXPECTED_WINDOW_OPEN_DAY_COUNT)
        self.assertEqual(len(full), EXPECTED_MOTHER_POOL_COUNT)
        self.assertEqual(int(failed_mask.sum()), EXPECTED_FAILED_CLOSE_COUNT)
        self.assertEqual(int(legacy_mask.sum()), EXPECTED_LEGACY_STRONG_SUBPOOL_COUNT)
        self.assertEqual(set(full_keys[legacy_mask]), set(old_keys))
        self.assertEqual(int(full["in_current_static_d_pool"].sum()), EXPECTED_STATIC_D_POOL_COUNT)
        self.assertEqual(len(targets), EXPECTED_MOTHER_POOL_COUNT)
        self.assertFalse(full_keys.duplicated().any())
        self.assertFalse(targets["target_key"].duplicated().any())
        self.assertEqual(set(targets["target_key"]), set(full_keys))
        self.assertFalse(full["name_metadata_missing"].astype(bool).any())
        self.assertTrue(pd.to_numeric(full["limit_price"], errors="coerce").gt(0).all())


if __name__ == "__main__":
    unittest.main()
