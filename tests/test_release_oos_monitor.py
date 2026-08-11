from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.release_oos_monitor import daily_log_lines, is_last_open_day_of_week, record_and_maybe_remind


class ReleaseOosMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data/raw").mkdir(parents=True)
        pd.DataFrame({
            # 1月8日周四后直接到下周一，覆盖周五休市的周报场景。
            "cal_date": [20260105, 20260106, 20260107, 20260108, 20260112],
            "is_open": [1, 1, 1, 1, 1],
        }).to_csv(self.root / "data/raw/trade_calendar.csv", index=False)
        self.payload = {
            "release_id": "release-test",
            "status": "EARLY_OBSERVATION",
            "reason": "样本不足，只观察",
            "optimization_decision": "HOLD_RELEASE",
            "minimum_samples_for_review": 20,
            "signal_day_count": 4,
            "evaluated_coverage": 0.75,
            "candidate_count": 6,
            "resolved_candidate_count": 4,
            "priority_winner_resolved_count": 3,
            "actual_complete_trade_count": 2,
            "priority_winner_metrics": {
                "avg_return": 0.01, "median_return": 0.008,
                "compound_multiple": 1.03, "max_drawdown": -0.02,
            },
            "actual_metrics": {"avg_return": 0.005, "max_drawdown": -0.01},
            "by_leg_metrics": [
                {"segment": "策略D", "sample_count": 1, "avg_return": -0.01},
                {"segment": "策略L", "sample_count": 2, "avg_return": 0.02},
            ],
            "priority_pair_metrics": [],
            "generated_at": "2026-01-08T10:00:00+00:00",
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_last_open_day_recognizes_holiday_shortened_week(self) -> None:
        self.assertFalse(is_last_open_day_of_week(self.root, "20260107"))
        self.assertTrue(is_last_open_day_of_week(self.root, "20260108"))

    def test_daily_log_is_human_readable_and_explicitly_non_blocking(self) -> None:
        lines = daily_log_lines(self.payload, "20260108")
        text = "\n".join(lines)
        self.assertIn("优先级样本=3/20", text)
        self.assertIn("真实成交=2/20", text)
        self.assertIn("不自动改策略/不阻断下单", text)

    def test_daily_history_idempotent_and_weekly_notification_deduplicated(self) -> None:
        calls: list[tuple] = []

        def fake_notify(*args, **kwargs):
            calls.append((args, kwargs))
            return True

        first = record_and_maybe_remind(self.root, "20260108", self.payload, fake_notify)
        second = record_and_maybe_remind(self.root, "20260108", self.payload, fake_notify)
        self.assertTrue(first["weekly_notification_sent"])
        self.assertFalse(second["weekly_notification_sent"])
        self.assertEqual(len(calls), 1)
        daily = pd.read_csv(self.root / "reports/oos_evaluation/release_oos_daily_history.csv")
        weekly = pd.read_csv(self.root / "reports/oos_evaluation/release_oos_weekly_history.csv")
        self.assertEqual(len(daily), 1)
        self.assertEqual(len(weekly), 1)
        state = json.loads((self.root / "data/state/oos_analysis_reminder_state.json").read_text())
        self.assertEqual(state["last_week_key"], "release-test|2026-W02")


if __name__ == "__main__":
    unittest.main()
