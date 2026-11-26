"""开仓平仓记录（2026-11-18，用户要求“把开仓平仓记录好，方便以后研究和复盘”）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src import live_trade_ledger as ledger
from src.daemon import buy_m

COLUMNS = ["trade_key", "entry_date", "ts_code", "name", "strategy_leg", "signal_date", "entry_filled_qty",
           "entry_fill_amount", "exit_date", "exit_filled_qty", "exit_fill_amount", "total_slippage_bps",
           "execution_status", "updated_at"]
REPORT = {"active_legs": ["A", "C", "D", "E", "M"], "commission_rate": 0.0003, "transfer_fee_rate": 0.00001,
          "minimum_commission": 5.0, "stamp_tax_rate": 0.0005}


def row(key, leg, buy, sell, updated, exit_date="20261119", qty=100):
    return {"trade_key": key, "entry_date": key[:8], "ts_code": "000001.SZ", "name": "示例", "strategy_leg": leg,
            "signal_date": "20261117", "entry_filled_qty": qty, "entry_fill_amount": buy,
            "exit_date": exit_date if sell else "", "exit_filled_qty": qty if sell else 0,
            "exit_fill_amount": sell, "total_slippage_bps": 30, "execution_status": "已平仓" if sell else "持仓中",
            "updated_at": updated}


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.summary = self.base / "trade_completion_summary.csv"
        self.ledger = self.base / "live_trades" / ledger.LEDGER_NAME

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def export(self, rows) -> None:
        pd.DataFrame(rows, columns=COLUMNS).to_csv(self.summary, index=False, encoding="utf-8-sig")

    def test_ledger_keeps_old_trades_and_takes_the_latest_version(self) -> None:
        self.export([row("20261116|A1|E|20261113", "E", 13437, 12683, "2026-11-17 15:00"),
                     row("20261118|M1|M|20261117", "M", 8368, 0, "2026-11-18 09:35")])
        self.assertEqual(ledger.update_ledger(self.summary, self.ledger), {"rows": 2, "added": 2})
        # 第二天的导出：M平仓了，旧的E不在导出里了
        self.export([row("20261118|M1|M|20261117", "M", 8368, 9900, "2026-11-19 15:00")])
        self.assertEqual(ledger.update_ledger(self.summary, self.ledger), {"rows": 2, "added": 0})
        saved = pd.read_csv(self.ledger, dtype=str, encoding="utf-8-sig")
        self.assertEqual(sorted(saved["trade_key"]), ["20261116|A1|E|20261113", "20261118|M1|M|20261117"])
        self.assertEqual(saved.set_index("trade_key").loc["20261118|M1|M|20261117", "exit_fill_amount"], "9900")
        # 导出读不到时总账不动
        self.summary.unlink()
        self.assertEqual(ledger.update_ledger(self.summary, self.ledger), {"rows": 2, "added": 0})

    def test_older_export_never_overwrites_a_newer_row(self) -> None:
        self.export([row("20261118|M1|M|20261117", "M", 8368, 9900, "2026-11-19 15:00")])
        ledger.update_ledger(self.summary, self.ledger)
        self.export([row("20261118|M1|M|20261117", "M", 8368, 0, "2026-11-18 09:35")])
        ledger.update_ledger(self.summary, self.ledger)
        saved = pd.read_csv(self.ledger, dtype=str, encoding="utf-8-sig")
        self.assertEqual(saved.loc[0, "exit_fill_amount"], "9900")

    def test_performance_by_leg_includes_m_and_only_closed_trades(self) -> None:
        self.export([row("20261116|A1|E|20261113", "E", 10000, 11000, "1"),
                     row("20261117|A2|E|20261114", "E", 10000, 9500, "1"),
                     row("20261118|M1|M|20261117", "M", 8000, 9000, "1"),
                     row("20261118|M2|M|20261117", "M", 8000, 0, "1")])
        ledger.update_ledger(self.summary, self.ledger)
        md = ledger.write_leg_performance(self.ledger, self.ledger.parent, REPORT, "20261118")
        table = pd.read_csv(self.ledger.parent / "live_performance_by_leg.csv", encoding="utf-8-sig")
        counts = dict(zip(table["segment"], table["sample_count"]))
        self.assertEqual(counts, {"全部真实成交": 3, "策略E": 2, "策略M": 1})
        self.assertAlmostEqual(table.set_index("segment").loc["策略E", "win_rate"], 0.5)
        text = md.read_text(encoding="utf-8")
        self.assertIn("截至20261118，总账4笔，只算已平仓", text)
        self.assertIn("| 策略M | 1 | 100.0% |", text)


class AccountAndRuleTests(unittest.TestCase):
    def test_account_daily_only_grows(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source, target = base / "account_daily.csv", base / "live_trades" / ledger.ACCOUNT_NAME
            pd.DataFrame([{"date": "20261117", "recorded_at": "2026-11-17 15:05:10", "total_asset": 68000},
                          {"date": "20261118", "recorded_at": "2026-11-18 15:05:10", "total_asset": 69400}]
                         ).to_csv(source, index=False, encoding="utf-8-sig")
            self.assertEqual(ledger.update_account_daily(source, target), {"rows": 2, "added": 2})
            # QMT状态重置后导出只剩当天一行：总账照样保留前面的日子
            pd.DataFrame([{"date": "20261119", "recorded_at": "2026-11-19 15:05:10", "total_asset": 70100}]
                         ).to_csv(source, index=False, encoding="utf-8-sig")
            self.assertEqual(ledger.update_account_daily(source, target), {"rows": 3, "added": 1})

    def test_rule_and_live_gross_returns_are_listed_per_leg(self) -> None:
        frame = pd.DataFrame([
            dict(row("20261116|A1|E|20261113", "E", 10000, 10500, "1"), benchmark_open=10.0, benchmark_close=10.6),
            dict(row("20261118|M1|M|20261117", "M", 8000, 9000, "1"), benchmark_open=0.0, benchmark_close=24.0),
        ])
        table = ledger.leg_performance(frame, REPORT).set_index("segment")
        self.assertEqual(int(table.loc["策略E", "rule_compared"]), 1)
        self.assertAlmostEqual(table.loc["策略E", "avg_rule_gross_return"], 0.06)
        self.assertAlmostEqual(table.loc["策略E", "avg_live_gross_return"], 0.05)
        self.assertEqual(int(table.loc["策略M", "rule_compared"]), 0)      # 没有开盘基准的不硬算


class AccountDailyRecordTests(unittest.TestCase):
    def test_one_row_per_trading_day_after_the_close(self) -> None:
        import datetime as dt
        from types import SimpleNamespace
        from src.daemon import qmt

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "account_daily.csv"
            account = SimpleNamespace(total_asset=69_400.0, available_cash=55_000.0)
            positions = [SimpleNamespace(ts_code="002652.SZ", volume=3000, market_value=13_290.0),
                         SimpleNamespace(ts_code="110001.SH", volume=10, market_value=1_000.0)]
            values = {"ACE": 13_290.0, "M": 0.0, "L": 0.0}
            beijing = dt.timezone(dt.timedelta(hours=8))
            with patch.object(qmt, "ACCOUNT_DAILY_PATH", path), patch.object(qmt, "_account_daily_done", ""), \
                    patch.object(qmt, "is_trade_day", return_value=True), \
                    patch.object(qmt, "_framework_is_ace_m_l", return_value=False), \
                    patch.object(qmt, "_strategy_only_market_value", side_effect=lambda p, family: values[family]):
                with patch.object(qmt, "now_beijing", return_value=dt.datetime(2026, 11, 18, 14, 50, tzinfo=beijing)):
                    qmt._record_account_daily(account, positions)
                self.assertFalse(path.exists())                        # 收盘前不记
                with patch.object(qmt, "now_beijing", return_value=dt.datetime(2026, 11, 18, 15, 6, tzinfo=beijing)):
                    qmt._record_account_daily(account, positions)
                    qmt._record_account_daily(account, positions)      # 一天只记一次
            saved = pd.read_csv(path, encoding="utf-8-sig", dtype={"date": str})
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved.loc[0, "date"], "20261118")
        self.assertEqual((saved.loc[0, "total_asset"], saved.loc[0, "ace_market_value"],
                          saved.loc[0, "other_market_value"], saved.loc[0, "position_count"]), (69400.0, 13290.0, 1000.0, 2))
        self.assertNotIn("l_market_value", saved.columns)            # 现行框架：文件格式与原来相同

    def test_new_framework_records_l_market_value(self) -> None:
        import datetime as dt
        from types import SimpleNamespace
        from src.daemon import qmt

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "account_daily.csv"
            account = SimpleNamespace(total_asset=100_000.0, available_cash=60_000.0)
            positions = [SimpleNamespace(ts_code="300100.SZ", volume=2000, market_value=25_000.0),
                         SimpleNamespace(ts_code="002652.SZ", volume=1000, market_value=15_000.0)]
            values = {"ACE": 15_000.0, "M": 0.0, "L": 25_000.0}
            beijing = dt.timezone(dt.timedelta(hours=8))
            with patch.object(qmt, "ACCOUNT_DAILY_PATH", path), patch.object(qmt, "_account_daily_done", ""), \
                    patch.object(qmt, "is_trade_day", return_value=True), \
                    patch.object(qmt, "_framework_is_ace_m_l", return_value=True), \
                    patch.object(qmt, "_strategy_only_market_value", side_effect=lambda p, family: values[family]), \
                    patch.object(qmt, "now_beijing", return_value=dt.datetime(2026, 12, 1, 15, 6, tzinfo=beijing)):
                qmt._record_account_daily(account, positions)
            saved = pd.read_csv(path, encoding="utf-8-sig", dtype={"date": str})
        self.assertEqual((saved.loc[0, "l_market_value"], saved.loc[0, "other_market_value"]), (25000.0, 0.0))


class MPlanTests(unittest.TestCase):
    def test_m_plan_is_registered_with_target_from_signal_close(self) -> None:
        item = {"ts_code": "301288.SZ", "name": "清研环境", "signal_date": "20261117", "exit_date": "20261119",
                "target_amount": 11_900.0, "reference_price": 20.14, "auction_qty": 400}
        with patch.object(buy_m, "_track_execution") as track:
            buy_m._m_register_plan(item, "20261118", "M竞价预挂")
        method, values = track.call_args.args[0], track.call_args.kwargs
        self.assertEqual(method, "register_entry_plan")
        self.assertEqual((values["strategy_leg"], values["target_qty"], values["auction_planned_qty"],
                          values["pov_planned_qty"], values["planned_exit_date"]), ("M", 500, 400, 100, "20261119"))
        self.assertAlmostEqual(values["target_amount"], 11_900.0)


if __name__ == "__main__":
    unittest.main()
