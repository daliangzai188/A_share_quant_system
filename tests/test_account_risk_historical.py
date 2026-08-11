from __future__ import annotations

import unittest

import pandas as pd

from src.account_risk_historical import (
    RiskOverlaySpec,
    performance_metrics,
    replay_risk_overlay,
    validate_inputs,
)


class AccountRiskHistoricalTest(unittest.TestCase):
    @staticmethod
    def daily() -> pd.DataFrame:
        return pd.DataFrame(
            {"signal_date": ["20260102", "20260105", "20260106", "20260107", "20260108"]}
        )

    @staticmethod
    def trades() -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"signal_date": "20260102", "exit_date": "20260105", "account_return": -0.20, "strategy_leg": "A", "ts_code": "000001.SZ"},
                {"signal_date": "20260105", "exit_date": "20260106", "account_return": 0.50, "strategy_leg": "L", "ts_code": "000002.SZ"},
                {"signal_date": "20260106", "exit_date": "20260107", "account_return": 0.40, "strategy_leg": "D", "ts_code": "000003.SZ"},
                {"signal_date": "20260107", "exit_date": "20260108", "account_return": 0.10, "strategy_leg": "M", "ts_code": "000004.SZ"},
            ]
        )

    def test_baseline_reproduces_all_trades(self) -> None:
        trades, calendar = validate_inputs(self.trades(), self.daily())
        selected, decisions, triggers = replay_risk_overlay(
            trades, calendar, RiskOverlaySpec(None, None, None, 1)
        )
        self.assertEqual(len(selected), 4)
        self.assertTrue(decisions["risk_decision"].eq("EXECUTED").all())
        self.assertTrue(triggers.empty)
        self.assertAlmostEqual(
            performance_metrics(selected)["equity_multiple"],
            (1 - 0.20) * 1.50 * 1.40 * 1.10,
        )

    def test_exit_loss_blocks_only_current_and_next_cooldown_signal(self) -> None:
        trades, calendar = validate_inputs(self.trades(), self.daily())
        selected, decisions, triggers = replay_risk_overlay(
            trades, calendar, RiskOverlaySpec(None, 0.15, None, 2)
        )
        self.assertEqual(selected["ts_code"].tolist(), ["000001.SZ", "000004.SZ"])
        self.assertEqual(
            decisions["risk_decision"].tolist(),
            ["EXECUTED", "SKIP_RISK_COOLDOWN", "SKIP_RISK_COOLDOWN", "EXECUTED"],
        )
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers.iloc[0]["trigger_exit_date"], "20260105")
        self.assertEqual(triggers.iloc[0]["blocked_through_date"], "20260106")

    def test_daily_loss_pauses_one_day_even_when_long_cooldown_is_configured(self) -> None:
        trades, calendar = validate_inputs(self.trades(), self.daily())
        selected, decisions, _ = replay_risk_overlay(
            trades, calendar, RiskOverlaySpec(0.10, None, None, 10)
        )
        self.assertEqual(selected["ts_code"].tolist(), ["000001.SZ", "000003.SZ", "000004.SZ"])
        self.assertEqual(decisions.iloc[1]["risk_decision"], "SKIP_RISK_COOLDOWN")
        self.assertEqual(decisions.iloc[2]["risk_decision"], "EXECUTED")

    def test_duplicate_signal_date_is_rejected(self) -> None:
        duplicate = pd.concat([self.trades(), self.trades().iloc[[0]]], ignore_index=True)
        with self.assertRaisesRegex(ValueError, "signal_date重复"):
            validate_inputs(duplicate, self.daily())


if __name__ == "__main__":
    unittest.main()
