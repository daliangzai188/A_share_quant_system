from __future__ import annotations

import unittest

import pandas as pd

from src.execution_data_quality import analyze_execution_data_quality


def row(key: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "trade_key": key,
        "entry_date": "20260801",
        "planned_exit_date": "20260805",
        "exit_date": "20260805",
        "ts_code": "000001.SZ",
        "name": "测试",
        "strategy_leg": "A",
        "entry_plan_source": "LIVE_FROZEN",
        "entry_filled_qty": 1000,
        "entry_fill_amount": 10000,
        "exit_filled_qty": 1000,
        "exit_fill_amount": 11000,
        "exit_remaining_qty": 0,
        "overnight_residual_qty": 0,
        "execution_status": "已平仓",
    }
    base.update(overrides)
    return base


class ExecutionDataQualityTests(unittest.TestCase):
    def test_normal_open_position_is_not_a_data_gap(self) -> None:
        raw = pd.DataFrame(
            [
                row(
                    "OPEN",
                    planned_exit_date="20260812",
                    exit_date="",
                    exit_filled_qty=0,
                    exit_fill_amount=0,
                    execution_status="持仓中",
                )
            ]
        )

        detail, summary = analyze_execution_data_quality(
            raw, {}, as_of_date="20260811"
        )

        self.assertEqual(detail.iloc[0]["gap_category"], "OPEN_NOT_DUE")
        self.assertFalse(bool(detail.iloc[0]["is_data_gap"]))
        self.assertEqual(summary["status"], "OPEN_POSITION_ONLY")
        self.assertEqual(summary["settled_trade_rows"], 0)

    def test_overdue_open_position_is_p0(self) -> None:
        raw = pd.DataFrame(
            [
                row(
                    "OVERDUE",
                    planned_exit_date="20260810",
                    exit_date="",
                    exit_filled_qty=0,
                    exit_fill_amount=0,
                    execution_status="持仓中",
                )
            ]
        )

        detail, summary = analyze_execution_data_quality(
            raw, {}, as_of_date="20260811"
        )

        self.assertEqual(detail.iloc[0]["gap_category"], "OPEN_OVERDUE")
        self.assertEqual(detail.iloc[0]["severity"], "P0")
        self.assertEqual(summary["status"], "P0_OVERDUE_POSITION")

    def test_due_today_becomes_p0_only_after_close_grace_period(self) -> None:
        raw = pd.DataFrame(
            [
                row(
                    "DUE",
                    planned_exit_date="20260811",
                    exit_date="",
                    exit_filled_qty=0,
                    exit_fill_amount=0,
                    execution_status="持仓中",
                )
            ]
        )

        before, _summary_before = analyze_execution_data_quality(
            raw, {}, as_of_date="20260811", as_of_time="150959"
        )
        after, summary_after = analyze_execution_data_quality(
            raw, {}, as_of_date="20260811", as_of_time="151000"
        )

        self.assertEqual(before.iloc[0]["gap_category"], "OPEN_DUE_TODAY")
        self.assertEqual(after.iloc[0]["gap_category"], "OPEN_OVERDUE")
        self.assertEqual(summary_after["status"], "P0_OVERDUE_POSITION")

    def test_open_position_without_exit_plan_is_p0(self) -> None:
        raw = pd.DataFrame(
            [
                row(
                    "NO_PLAN",
                    planned_exit_date="",
                    exit_date="",
                    exit_filled_qty=0,
                    exit_fill_amount=0,
                    execution_status="持仓中",
                )
            ]
        )

        detail, summary = analyze_execution_data_quality(
            raw, {}, as_of_date="20260811", as_of_time="100000"
        )

        self.assertEqual(detail.iloc[0]["gap_category"], "OPEN_EXIT_PLAN_MISSING")
        self.assertEqual(detail.iloc[0]["severity"], "P0")
        self.assertEqual(summary["status"], "P0_EXECUTION_STATE")

    def test_legacy_and_live_missing_exit_amount_have_different_priority(self) -> None:
        raw = pd.DataFrame(
            [
                row("LEGACY", exit_fill_amount=0, entry_plan_source="BACKFILLED"),
                row("LIVE", exit_fill_amount=0, entry_plan_source="LIVE_FROZEN"),
            ]
        )

        detail, summary = analyze_execution_data_quality(
            raw, {}, as_of_date="20260811"
        )

        legacy = detail[detail["trade_key"].eq("LEGACY")].iloc[0]
        live = detail[detail["trade_key"].eq("LIVE")].iloc[0]
        self.assertEqual(legacy["severity"], "P2")
        self.assertEqual(legacy["recoverability"], "BROKER_STATEMENT_REQUIRED_LEGACY")
        self.assertEqual(live["severity"], "P1")
        self.assertEqual(live["recoverability"], "BROKER_HISTORY_OR_LOCAL_LOG_REQUIRED")
        self.assertEqual(summary["true_data_gap_rows"], 2)
        self.assertEqual(summary["settled_data_complete_rate"], 0.0)

    def test_complete_sell_event_marks_gap_as_rebuild_candidate(self) -> None:
        raw = pd.DataFrame([row("EVENT", exit_fill_amount=0)])
        events = pd.DataFrame(
            [
                {
                    "event_id": "SELL-1",
                    "trade_key": "EVENT",
                    "broker_order_id": "QMT-1",
                    "filled_qty": 1000,
                    "fill_amount": 10800,
                    "fill_price": 10.8,
                }
            ]
        )

        detail, summary = analyze_execution_data_quality(
            raw, {}, as_of_date="20260811", sell_events=events
        )

        self.assertEqual(detail.iloc[0]["recoverability"], "AUTO_REBUILD_FROM_SELL_EVENTS")
        self.assertEqual(summary["auto_rebuild_candidate_rows"], 1)
        self.assertFalse(summary["automatic_writeback"])

    def test_settled_rate_excludes_normal_open_but_keeps_real_gap(self) -> None:
        raw = pd.DataFrame(
            [
                row("COMPLETE"),
                row("GAP", exit_fill_amount=0),
                row(
                    "OPEN",
                    planned_exit_date="20260812",
                    exit_date="",
                    exit_filled_qty=0,
                    exit_fill_amount=0,
                    execution_status="持仓中",
                ),
            ]
        )

        _detail, summary = analyze_execution_data_quality(
            raw, {}, as_of_date="20260811"
        )

        self.assertEqual(summary["active_trade_rows"], 3)
        self.assertEqual(summary["settled_trade_rows"], 2)
        self.assertEqual(summary["complete_trade_rows"], 1)
        self.assertAlmostEqual(summary["settled_data_complete_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
