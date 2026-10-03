from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
            "live_performance_report": {"minimum_commission": 5.0, "active_legs": ["A", "C"]},
        }

    def test_bootstrap_once_then_only_realized_trade_pnl_moves_equity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "equity_ledger.json"
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
                        "strategy_leg": "C",
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



def _row(key: str, leg: str, entry: str, exit_: str, buy: float, sell: float, qty: int = 1000) -> dict:
    return {
        "trade_key": key, "entry_date": entry, "exit_date": exit_, "ts_code": key.split("|")[1],
        "strategy_leg": leg, "entry_filled_qty": qty, "entry_fill_amount": buy * qty,
        "exit_filled_qty": qty, "exit_fill_amount": sell * qty, "total_slippage_bps": 0,
    }


def _net(buy: float, sell: float, qty: int) -> float:
    """与completed_live_trades同一费用口径：佣金万3最低5元、过户费十万分之一、卖出印花税千1。"""
    buy_amount, sell_amount = buy * qty, sell * qty
    fees = (max(buy_amount * 0.0003, 5.0) + max(sell_amount * 0.0003, 5.0)
            + (buy_amount + sell_amount) * 0.00001 + sell_amount * 0.001)
    return sell_amount - buy_amount - fees


class RecomputedLedgerTests(unittest.TestCase):
    """2026-09-30复核：旧增量账本重复计入基线前交易、漏掉停用腿、更正进不去。"""

    CONFIG = {
        "analysis": {"commission_rate": 0.0003, "stamp_tax_rate": 0.001, "transfer_fee_rate": 0.00001},
        "live_performance_report": {"minimum_commission": 5.0, "active_legs": ["A", "C", "D", "E"]},
        "strategy_equity_ledger": {"legacy_v2_baseline_cutoff_entry_date": "20260810"},
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "ledger.json"
        self.summary = root / "summary.csv"

    def _write_summary(self, rows: list[dict]) -> None:
        pd.DataFrame(rows).to_csv(self.summary, index=False)

    def _write_v2(self, processed: list[str], last_equity: float) -> None:
        self.state.write_text(json.dumps({
            "schema_version": 2, "baseline_equity": 270_000.0, "last_equity": last_equity,
            "peak_equity": last_equity, "realized_pnl": last_equity - 270_000.0,
            "processed_trade_keys": processed,
        }), encoding="utf-8")

    def _update(self, config: dict | None = None):
        return update_strategy_equity_ledger(
            state_path=self.state, completion_summary_path=self.summary,
            signal_date="20260930", config=config or self.CONFIG,
        )

    def test_renamed_pre_baseline_trades_are_never_double_counted(self) -> None:
        # 基线前的E2交易后来改名为E（新编号），旧账本把它当新交易又加了一遍。
        self._write_v2(["20260728|001358.SZ|E2|20260727"], last_equity=270_000.0 + 26_000.0)
        self._write_summary([
            _row("20260728|001358.SZ|E|20260727", "E", "20260728", "20260729", 22.0, 48.0),
            _row("20260810|600815.SH|D|20260810", "D", "20260810", "20260812", 3.6, 3.7, 60000),
        ])
        result = self._update()
        self.assertAlmostEqual(result.equity, 270_000.0 + _net(3.6, 3.7, 60000), places=2)
        self.assertTrue(result.ledger_ready)
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(payload["baseline_cutoff_entry_date"], "20260810")
        self.assertEqual(payload["migrated_from"]["last_equity"], 296_000.0)

    def test_retired_leg_trade_after_baseline_counts_and_blocks_until_complete(self) -> None:
        self._write_v2([], last_equity=270_000.0)
        rows = [_row("20260820|301211.SZ|N|20260819", "N", "20260820", "20260821", 14.5, 0.0, 15700)]
        self._write_summary(rows)
        pending = self._update()
        self.assertFalse(pending.ledger_ready)
        self.assertEqual(pending.pending_trade_keys, ("20260820|301211.SZ|N|20260819",))
        rows[0]["exit_fill_amount"] = 12.43 * 15700
        self._write_summary(rows)
        done = self._update()
        self.assertTrue(done.ledger_ready)
        self.assertAlmostEqual(done.equity, 270_000.0 + _net(14.5, 12.43, 15700), places=2)

    def test_correction_of_included_trade_flows_into_equity(self) -> None:
        self._write_v2([], last_equity=270_000.0)
        rows = [_row("20260817|603118.SH|L|20260814", "L", "20260817", "20260818", 19.26, 19.26, 11800)]
        self._write_summary(rows)
        self.assertAlmostEqual(self._update().equity, 270_000.0 + _net(19.26, 19.26, 11800), places=2)
        rows[0]["exit_fill_amount"] = 18.91 * 11800
        self._write_summary(rows)
        corrected = self._update()
        self.assertAlmostEqual(corrected.equity, 270_000.0 + _net(19.26, 18.91, 11800), places=2)

    def test_peak_follows_exit_order_and_drawdown_is_real(self) -> None:
        self._write_v2([], last_equity=270_000.0)
        self._write_summary([
            _row("20260901|000002.SZ|C|20260831", "C", "20260901", "20260903", 10.0, 9.0, 10000),
            _row("20260810|000001.SZ|D|20260810", "D", "20260810", "20260812", 10.0, 11.0, 10000),
        ])
        result = self._update()
        win, loss = _net(10.0, 11.0, 10000), _net(10.0, 9.0, 10000)
        self.assertAlmostEqual(result.peak_equity, 270_000.0 + win, places=2)
        self.assertEqual(result.peak_date, "20260812")
        self.assertAlmostEqual(result.equity, 270_000.0 + win + loss, places=2)

    def test_legacy_migration_requires_configured_cutoff(self) -> None:
        self._write_v2([], last_equity=270_000.0)
        self._write_summary([])
        config = {key: value for key, value in self.CONFIG.items() if key != "strategy_equity_ledger"}
        with self.assertRaises(ValueError):
            self._update(config)

    def test_bootstrap_only_without_usable_ledger(self) -> None:
        self.assertTrue(equity_ledger_requires_bootstrap(self.state))
        self._write_v2([], last_equity=270_000.0)
        self.assertFalse(equity_ledger_requires_bootstrap(self.state), "旧版账本走迁移，不重新取券商基线")
        self._write_summary([])
        self._update()
        self.assertFalse(equity_ledger_requires_bootstrap(self.state))


if __name__ == "__main__":
    unittest.main()
