from __future__ import annotations

import unittest

import pandas as pd

from src.live_performance import (
    capacity_monitor_status,
    completed_live_trades,
    execution_capacity_metrics,
    rolling_metrics,
)


class LivePerformanceTests(unittest.TestCase):
    def test_incomplete_trade_is_excluded_and_fees_are_deducted(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "trade_key": "1",
                    "entry_date": "20260801",
                    "exit_date": "20260802",
                    "ts_code": "000001.SZ",
                    "strategy_leg": "A",
                    "entry_filled_qty": 1000,
                    "entry_fill_amount": 10000,
                    "exit_filled_qty": 1000,
                    "exit_fill_amount": 11000,
                    "total_slippage_bps": 10,
                },
                {
                    "trade_key": "2",
                    "entry_date": "20260803",
                    "exit_date": "20260804",
                    "ts_code": "000002.SZ",
                    "strategy_leg": "C",
                    "entry_filled_qty": 1000,
                    "entry_fill_amount": 10000,
                    "exit_filled_qty": 1000,
                    "exit_fill_amount": 0,
                    "total_slippage_bps": 0,
                },
            ]
        )
        trades, quality = completed_live_trades(raw, {})
        self.assertEqual(len(trades), 1)
        self.assertEqual(quality["incomplete_trade_rows"], 1)
        self.assertLess(float(trades.iloc[0]["net_return"]), 0.10)
        self.assertGreater(float(trades.iloc[0]["estimated_fees"]), 0)

    def test_missing_exit_date_is_not_counted_as_complete(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "trade_key": "1",
                    "entry_date": "20260801",
                    "exit_date": "",
                    "ts_code": "000001.SZ",
                    "strategy_leg": "A",
                    "entry_filled_qty": 1000,
                    "entry_fill_amount": 10000,
                    "exit_filled_qty": 1000,
                    "exit_fill_amount": 11000,
                }
            ]
        )

        trades, quality = completed_live_trades(raw, {})

        self.assertTrue(trades.empty)
        self.assertEqual(quality["incomplete_trade_rows"], 1)

    def test_stamp_tax_uses_historical_effective_date(self) -> None:
        rows = []
        for key, exit_date in (("old", "20230827"), ("new", "20230828")):
            rows.append({
                "trade_key": key,
                "entry_date": "20230825",
                "exit_date": exit_date,
                "ts_code": "000001.SZ",
                "strategy_leg": "A",
                "entry_filled_qty": 10000,
                "entry_fill_amount": 100000.0,
                "exit_filled_qty": 10000,
                "exit_fill_amount": 100000.0,
            })
        trades, _ = completed_live_trades(pd.DataFrame(rows), {})
        fees = trades.set_index("trade_key")["estimated_fees"]
        self.assertAlmostEqual(float(fees["old"] - fees["new"]), 50.0, places=6)

    def test_rolling_metrics_exposes_actual_pnl_and_loss_streak(self) -> None:
        trades = pd.DataFrame(
            {
                "strategy_leg": ["A", "A", "E"],
                "net_return": [0.10, -0.05, -0.02],
                "net_pnl": [100.0, -50.0, -20.0],
                "entry_fill_amount": [1000.0, 1000.0, 1000.0],
                "total_slippage_bps": [10.0, 20.0, 0.0],
            }
        )
        metrics = rolling_metrics(trades, [2])
        overall = metrics.iloc[0]
        self.assertEqual(int(overall["sample_count"]), 3)
        self.assertEqual(int(overall["max_consecutive_losses"]), 2)
        self.assertAlmostEqual(float(overall["total_net_pnl"]), 30.0)
        e_row = metrics[metrics["segment"].eq("策略E")].iloc[0]
        self.assertAlmostEqual(float(e_row["hypothetical_max_drawdown"]), -0.02)

    def test_capacity_uses_only_frozen_plans_and_exposes_tca_quality(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "strategy_leg": "A",
                    "entry_plan_source": "LIVE_FROZEN",
                    "entry_target_qty": 1000,
                    "entry_filled_qty": 1000,
                    "entry_target_amount": 10000,
                    "entry_fill_amount": 10050,
                    "exit_target_qty": 1000,
                    "exit_filled_qty": 1000,
                    "benchmark_open": 10.0,
                    "benchmark_close": 10.5,
                    "buy_slippage_bps": 50,
                    "sell_slippage_bps": 20,
                    "execution_status": "已平仓",
                    "overnight_residual_qty": 0,
                },
                {
                    "strategy_leg": "A",
                    "entry_plan_source": "LIVE_FROZEN",
                    "entry_target_qty": 1000,
                    "entry_filled_qty": 900,
                    "entry_target_amount": 10000,
                    "entry_fill_amount": 9000,
                    "exit_target_qty": 900,
                    "exit_filled_qty": 900,
                    "benchmark_open": 10.0,
                    "benchmark_close": 0.0,
                    "buy_slippage_bps": 0,
                    "sell_slippage_bps": 0,
                    "execution_status": "已平仓",
                    "overnight_residual_qty": 0,
                },
                {
                    "strategy_leg": "E",
                    "entry_plan_source": "BACKFILLED",
                    "entry_target_qty": 5000,
                    "entry_filled_qty": 5000,
                    "entry_target_amount": 50000,
                    "entry_fill_amount": 50000,
                    "exit_target_qty": 5000,
                    "exit_filled_qty": 5000,
                    "benchmark_open": 10.0,
                    "benchmark_close": 10.5,
                    "buy_slippage_bps": -500,
                    "sell_slippage_bps": -500,
                    "execution_status": "已平仓",
                    "overnight_residual_qty": 0,
                },
            ]
        )
        config = {
            "active_legs": ["A", "E"],
            "capacity_review": {
                "minimum_trustworthy_plans": 2,
                "full_fill_threshold": 0.98,
                "minimum_full_fill_rate": 0.9,
                "minimum_avg_entry_completion": 0.95,
                "minimum_benchmark_coverage": 0.9,
                "enforce_live_gate": False,
            },
        }
        metrics = execution_capacity_metrics(raw, config)
        overall = metrics.iloc[0]
        self.assertEqual(int(overall["trustworthy_plan_count"]), 2)
        self.assertEqual(int(overall["backfilled_plan_count"]), 1)
        self.assertAlmostEqual(float(overall["entry_full_fill_rate"]), 0.5)
        self.assertAlmostEqual(float(overall["avg_entry_qty_completion"]), 0.95)
        self.assertAlmostEqual(float(overall["sell_benchmark_coverage"]), 0.5)
        # 回填交易的-500bps不能污染真实冻结计划TCA。
        self.assertAlmostEqual(float(overall["avg_buy_slippage_bps"]), 25.0)
        status = capacity_monitor_status(metrics, config)
        self.assertEqual(status["status"], "DATA_GAP")
        self.assertFalse(status["capacity_certified"])
        self.assertFalse(status["enforce_live_gate"])

    def test_old_summary_notes_are_compatible_but_not_capacity_evidence(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "strategy_leg": "D",
                    "entry_target_qty": 1000,
                    "entry_filled_qty": 1000,
                    "entry_target_amount": 10000,
                    "entry_fill_amount": 10000,
                    "exit_target_qty": 1000,
                    "exit_filled_qty": 1000,
                    "data_quality_note": "上线前原始计划未统一留档，目标股数由历史持仓/容量档案回填",
                }
            ]
        )
        metrics = execution_capacity_metrics(raw, {"active_legs": ["D"]})
        self.assertEqual(int(metrics.iloc[0]["trustworthy_plan_count"]), 0)
        self.assertEqual(int(metrics.iloc[0]["backfilled_plan_count"]), 1)

    def test_amount_based_top_up_within_budget_is_not_reported_as_overfill(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "strategy_leg": "C",
                    "entry_plan_source": "LIVE_FROZEN",
                    "entry_target_qty": 4800,
                    "entry_filled_qty": 5000,
                    "entry_target_amount": 231445.29,
                    "entry_fill_amount": 227532.00,
                    "exit_target_qty": 5000,
                    "exit_filled_qty": 5000,
                    "execution_status": "已平仓",
                },
                {
                    "strategy_leg": "A",
                    "entry_plan_source": "LIVE_FROZEN",
                    "entry_target_qty": 17200,
                    "entry_filled_qty": 20000,
                    "entry_target_amount": 202960.00,
                    "entry_fill_amount": 228800.00,
                    "exit_target_qty": 20000,
                    "exit_filled_qty": 20000,
                    "execution_status": "已平仓",
                },
            ]
        )

        metrics = execution_capacity_metrics(raw, {"active_legs": ["A", "C"]})

        # C虽因金额型补单多成交200股，但总金额仍低于冻结预算；A的股数、金额
        # 均超过冻结计划，仍应保留为真正的超额成交记录。
        self.assertEqual(int(metrics.iloc[0]["overfill_count"]), 1)
        self.assertEqual(
            int(metrics[metrics["segment"].eq("策略C")].iloc[0]["overfill_count"]),
            0,
        )
        self.assertEqual(
            int(metrics[metrics["segment"].eq("策略A")].iloc[0]["overfill_count"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
