from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pandas as pd

from scripts.research_strategy_d_exit_fetch import load_d_exit_targets
from scripts.research_strategy_d_exit_pov import (
    EXPECTED_PORTFOLIO_MULTIPLE,
    load_trade_metadata,
    run_replay,
    validate_bar_inputs,
)


def five_minute_times() -> list[str]:
    morning = pd.date_range("09:35", "11:30", freq="5min").strftime("%H%M").tolist()
    afternoon = pd.date_range("13:05", "15:00", freq="5min").strftime("%H%M").tolist()
    return morning + afternoon


def one_minute_tail_times() -> list[str]:
    return pd.date_range("14:45", "15:00", freq="1min").strftime("%H%M").tolist()


class StrategyDExitResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.portfolio, cls.ordinary = load_trade_metadata()

    def test_fetch_targets_include_only_ordinary_d_trades(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "portfolio_trades.csv"
            self.portfolio.to_csv(path, index=False)
            targets = load_d_exit_targets(path)
        # 2026-08-07 A/C改用逐日独立候选后，部分原本单独T+2的D转为接力（旧口径17）。
        self.assertEqual(len(targets), 14)
        self.assertNotIn("strategy_leg", targets.columns)
        self.assertTrue(targets["key"].str.contains(r"\|", regex=True).all())

    def make_constant_bars(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        five_rows = []
        one_rows = []
        for row in self.ordinary.itertuples(index=False):
            price = float(row.exit_close)
            common = {
                "key": row.key,
                "ts_code": row.ts_code,
                "exit_date": row.exit_date,
                "leg": "D",
                "open": price,
                "close": price,
                "high": price,
                "low": price,
                "volume": 100_000_000,
                "amount": 1_000_000_000.0,
            }
            for hhmm in five_minute_times():
                five_rows.append({**common, "hhmm": hhmm})
            for hhmm in one_minute_tail_times():
                one_rows.append({**common, "hhmm": hhmm})
        return pd.DataFrame(five_rows), pd.DataFrame(one_rows)

    def test_constant_close_and_unlimited_capacity_preserve_portfolio(self) -> None:
        five, one = self.make_constant_bars()
        validate_bar_inputs(five, one, self.ordinary)
        _detail, summary, gates = run_replay(
            five=five,
            one=one,
            portfolio=self.portfolio,
            ordinary=self.ordinary,
            position_amounts=(250_000.0,),
            base_participation=0.25,
            late_participation=0.35,
            runway_buffer=1.2,
            capacity_haircut=0.5,
            trigger_pct=0.01,
            pm_extrapolate=0.44,
        )
        all_rows = summary[summary["sample_split"].eq("ALL")]
        self.assertEqual(len(all_rows), 3)
        self.assertTrue(all_rows["complete_final_rate"].eq(1.0).all())
        self.assertTrue(
            all_rows["portfolio_multiple"].map(
                lambda value: abs(float(value) - EXPECTED_PORTFOLIO_MULTIPLE) < 1e-8
            ).all()
        )
        self.assertTrue(gates["certification_passed"].all())

    def test_incomplete_bar_sample_is_rejected(self) -> None:
        five, one = self.make_constant_bars()
        one = one.iloc[1:].copy()
        with self.assertRaisesRegex(ValueError, "bar数量不完整"):
            validate_bar_inputs(five, one, self.ordinary)


if __name__ == "__main__":
    unittest.main()
