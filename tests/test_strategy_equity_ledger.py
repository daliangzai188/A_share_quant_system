from __future__ import annotations

import json
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.strategy_equity_ledger import (
    equity_ledger_requires_bootstrap,
    update_strategy_equity_ledger,
)


class StrategyEquityLedgerTests(unittest.TestCase):
    def _config(self) -> dict:
        return {
            "analysis": {
                "commission_rate": 0.0003,
                "stamp_tax_rate": 0.001,
                "transfer_fee_rate": 0.00001,
            },
            "live_performance_report": {"minimum_commission": 5.0, "active_legs": ["A", "L"]},
        }

    def test_bootstrap_once_then_only_realized_trade_pnl_moves_equity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "m_equity_peak.json"
            summary = root / "summary.csv"
            columns = [
                "trade_key", "entry_date", "exit_date", "ts_code", "strategy_leg",
                "entry_filled_qty", "entry_fill_amount", "exit_filled_qty",
                "exit_fill_amount", "total_slippage_bps",
            ]
            pd.DataFrame(columns=columns).to_csv(summary, index=False)
            first = update_strategy_equity_ledger(
                state_path=state,
                completion_summary_path=summary,
                signal_date="20260801",
                config=self._config(),
                bootstrap_equity=500_000,
            )
            self.assertTrue(first.initialized_now)
            self.assertFalse(equity_ledger_requires_bootstrap(state))

            pd.DataFrame(
                [
                    {
                        "trade_key": "new-win",
                        "entry_date": "20260802",
                        "exit_date": "20260803",
                        "ts_code": "000001.SZ",
                        "strategy_leg": "A",
                        "entry_filled_qty": 1000,
                        "entry_fill_amount": 10000,
                        "exit_filled_qty": 1000,
                        "exit_fill_amount": 11000,
                        "total_slippage_bps": 0,
                    }
                ]
            ).to_csv(summary, index=False)
            second = update_strategy_equity_ledger(
                state_path=state,
                completion_summary_path=summary,
                signal_date="20260803",
                config=self._config(),
                bootstrap_equity=900_000,
            )
            self.assertEqual(second.new_trade_count, 1)
            self.assertGreater(second.equity, 500_000)
            self.assertLess(second.equity, 501_000)
            self.assertLess(second.equity, 900_000, "后续入金不得抬高策略净值")

            again = update_strategy_equity_ledger(
                state_path=state,
                completion_summary_path=summary,
                signal_date="20260804",
                config=self._config(),
                bootstrap_equity=1_200_000,
            )
            self.assertEqual(again.new_trade_count, 0)
            self.assertAlmostEqual(again.equity, second.equity)

    def test_new_incomplete_trade_makes_ledger_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state.json"
            summary = root / "summary.csv"
            pd.DataFrame().to_csv(summary, index=False)
            update_strategy_equity_ledger(
                state_path=state,
                completion_summary_path=summary,
                signal_date="20260801",
                config=self._config(),
                bootstrap_equity=500_000,
            )
            pd.DataFrame(
                [
                    {
                        "trade_key": "missing-exit",
                        "entry_date": "20260802",
                        "exit_date": "20260803",
                        "ts_code": "000001.SZ",
                        "strategy_leg": "L",
                        "entry_filled_qty": 1000,
                        "entry_fill_amount": 10000,
                        "exit_filled_qty": 1000,
                        "exit_fill_amount": 0,
                        "total_slippage_bps": 0,
                    }
                ]
            ).to_csv(summary, index=False)
            result = update_strategy_equity_ledger(
                state_path=state,
                completion_summary_path=summary,
                signal_date="20260803",
                config=self._config(),
            )
            self.assertFalse(result.ledger_ready)
            self.assertEqual(result.pending_incomplete_trade_count, 1)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertFalse(payload["ledger_ready"])


class StrategyMStartupRecoveryTests(unittest.TestCase):
    def _patch_common(self, daemon, now: dt.datetime):
        return (
            patch.object(daemon, "load_json_config", return_value={"strategy_m": {"enabled": True}}),
            patch.object(daemon, "_has_signal_for_date", return_value=True),
            patch.object(daemon, "_processed_data_ready_for_date", return_value=True),
            patch.object(
                daemon,
                "load_equity_ledger",
                return_value={"schema_version": 2, "ledger_ready": True},
            ),
            patch.object(
                daemon,
                "_load_m_signal_run",
                return_value={"status": "ERROR", "note": "净值数据缺失"},
            ),
            patch.object(daemon, "next_n_trade_days", return_value=dt.date(2026, 8, 10)),
            patch.object(daemon, "now_beijing", return_value=now),
            patch.object(daemon, "run_script", return_value=True),
        )

    def test_0925前账本恢复会重算m信号(self) -> None:
        from scripts import trading_daemon as daemon

        patches = self._patch_common(
            daemon,
            dt.datetime(2026, 8, 10, 9, 0, tzinfo=daemon.BEIJING_TZ),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7] as run:
            daemon.refresh_m_signal_after_startup_if_needed("20260807")
        run.assert_called_once_with(
            "run_strategy_m_signal.py",
            "--signal-date",
            "20260807",
            timeout=daemon.TIMEOUT_SIGNAL_STEP,
        )

    def test_0925后账本恢复不补生成过期m信号(self) -> None:
        from scripts import trading_daemon as daemon

        patches = self._patch_common(
            daemon,
            dt.datetime(2026, 8, 10, 9, 25, tzinfo=daemon.BEIJING_TZ),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7] as run:
            daemon.refresh_m_signal_after_startup_if_needed("20260807")
        run.assert_not_called()

    def test_数据未齐时不抢跑m信号(self) -> None:
        from scripts import trading_daemon as daemon

        with (
            patch.object(daemon, "load_json_config", return_value={"strategy_m": {"enabled": True}}),
            patch.object(daemon, "_has_signal_for_date", return_value=False),
            patch.object(daemon, "run_script") as run,
        ):
            daemon.refresh_m_signal_after_startup_if_needed("20260807")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
