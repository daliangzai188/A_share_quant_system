"""现金回放自检容差：浮点舍入不能在复利倍数变大后误报，真实账务错误仍须报错。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

import src.acde_rolling_framework as fw


def _winning_streak(trades: int, stock_return: float) -> tuple[pd.DataFrame, list[str]]:
    days = pd.bdate_range("2020-01-02", periods=trades * 2 + 2).strftime("%Y%m%d").tolist()
    rows = []
    for i in range(trades):
        buy, sell = days[2 * i], days[2 * i + 1]
        rows.append(
            {
                "signal_date": days[2 * i - 1] if i else "20200101",
                "buy_date": buy,
                "status": "OK",
                "strategy_leg": "A",
                "ts_code": "000001.SZ",
                "name": "容差测试",
                "exit_date": sell,
                "position_open_until": sell,
                "entry_filled": True,
                "position_opened": True,
                "outcome_observable": True,
                "entry_reference_price": 10.0,
                "entry_price": 10.0,
                "exit_reference_price": 10.0 * (1 + stock_return),
                "exit_price": 10.0 * (1 + stock_return),
                "stock_return_before_fees": stock_return,
                "position_scale": 1.0,
            }
        )
    return pd.DataFrame(rows), days


class CashReplayToleranceTests(unittest.TestCase):
    def test_huge_multiple_no_longer_trips_on_rounding(self) -> None:
        """复利到上亿倍时两条路径的舍入差远超1e-10，旧的绝对容差会误报。"""
        plan, days = _winning_streak(trades=320, stock_return=0.08)
        seen: dict[str, float] = {}
        original = fw.mechanical_compound

        def spy(values):
            result = original(values)
            seen["compound"] = result.equity_multiple
            return result

        with patch.object(fw, "mechanical_compound", side_effect=spy):
            detail = fw.replay_action_date_cash_portfolio(
                {"A": plan}, action_dates=days, priority=("A",), initial_cash=10_000_000.0
            )
        cash_multiple = float(detail.iloc[-1]["equity_after"]) / 10_000_000.0
        self.assertGreater(cash_multiple, 1e6)
        # 证明本测试确实覆盖了旧缺陷：舍入差已超过旧的绝对容差
        self.assertGreater(abs(seen["compound"] - cash_multiple), 1e-10)

    def test_real_bookkeeping_mismatch_still_raises(self) -> None:
        plan, days = _winning_streak(trades=20, stock_return=0.05)
        original = fw.mechanical_compound

        def skewed(values):
            result = original(values)
            return type(result)(**{**result.__dict__, "equity_multiple": result.equity_multiple * (1 + 1e-8)}) \
                if hasattr(result, "__dict__") else result

        with patch.object(fw, "mechanical_compound", side_effect=skewed):
            with self.assertRaisesRegex(RuntimeError, "精确现金流水与逐笔复利不一致"):
                fw.replay_action_date_cash_portfolio(
                    {"A": plan}, action_dates=days, priority=("A",), initial_cash=1_000_000.0
                )

    def test_ordinary_multiple_passes(self) -> None:
        plan, days = _winning_streak(trades=10, stock_return=0.03)
        detail = fw.replay_action_date_cash_portfolio(
            {"A": plan}, action_dates=days, priority=("A",), initial_cash=1_000_000.0
        )
        self.assertEqual(int(detail["status"].eq("EXECUTED").sum()), 10)


if __name__ == "__main__":
    unittest.main()
