"""三级警告：中级（策略复核）、终极（建议停止交易、优化策略），范围、升级、推送标题与终端横幅。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.review_triggers import ReviewTrigger, plan_push
from src.strategy_warnings import (
    TIER_FINAL,
    TIER_MIDDLE,
    WarningSettings,
    _cached_m_inputs,
    banner_lines,
    drawdown,
    escalate,
    leg_trades,
    relative_behind,
    trailing_return,
    warning_report,
    warning_triggers,
)

OPEN = pd.bdate_range("2026-01-01", "2026-12-31").strftime("%Y%m%d").tolist()
SETTINGS = WarningSettings(
    enabled=True,
    middle={"E": -0.255, "M": 0.0, "combo_drawdown": -0.315},
    final={"A": -0.507, "C": -0.709, "E": -0.390, "shadow_drawdown": -0.547, "combo_drawdown": -0.50},
)


def metrics(**over):
    base = {
        "legs": {leg: {"value": 0.10, "trades": 30, "as_of": "20261110"} for leg in ("A", "C", "E", "M")},
        "factor_health": {"A": {"months_below": 0}, "C": {"months_below": 0}},
        "shadow_drawdown": {"value": -0.20, "peak": "20260805", "as_of": "20261110"},
        "combo_drawdown": {"value": -0.20, "peak": "20260813", "as_of": "20261110"},
        "combo_relative": {"value": 0.25, "days_behind": 0, "as_of": "20261110"},
    }
    for key, value in over.items():
        if key in base["legs"]:
            base["legs"][key] = dict(base["legs"][key], value=value)
        else:
            base[key] = value
    return base


class MetricTests(unittest.TestCase):
    def test_trailing_return_needs_enough_trades_in_window(self) -> None:
        trades = [("20250101", 1.0)] + [("202601%02d" % d, 0.01) for d in range(1, 10)]
        self.assertIsNone(trailing_return(trades, window_days=365, min_trades=10)["value"], "2025-01那笔已出窗口")
        result = trailing_return(trades + [("20260120", 0.01)], window_days=365, min_trades=10)
        self.assertAlmostEqual(result["value"], 1.01 ** 10 - 1)
        self.assertEqual((result["trades"], result["as_of"]), (10, "20260120"))

    def test_leg_trades_take_every_observable_plan_net_of_cost(self) -> None:
        legs = {"A": pd.DataFrame([
            {"action_date": "20261102", "position_opened": True, "outcome_observable": True, "status": "OK",
             "stock_return_before_fees": 0.05},
            {"action_date": "20261103", "position_opened": True, "outcome_observable": False, "status": "X",
             "stock_return_before_fees": float("nan")},
        ])}
        self.assertEqual(leg_trades(legs, cost=0.002)["A"], [("20261102", 0.048)])

    def test_drawdown_and_relative(self) -> None:
        series = pd.Series([1.0, 1.2, 0.9, 1.0], index=["d1", "d2", "d3", "d4"])
        self.assertEqual(drawdown(series)["peak"], "d2")
        self.assertAlmostEqual(drawdown(series)["value"], 1.0 / 1.2 - 1)
        index = ["%03d" % i for i in range(30)]
        live = pd.Series([1.0 + 0.01 * i for i in range(30)], index=index)
        base = pd.Series([1.0 + 0.01 * i if i < 25 else 1.0 + 0.02 * i for i in range(30)], index=index)
        result = relative_behind(live, base, window=5)
        self.assertLess(result["value"], 0)
        self.assertEqual(result["days_behind"], 5)


class TriggerTests(unittest.TestCase):
    def test_quiet_when_everything_normal(self) -> None:
        self.assertEqual(warning_triggers(metrics(), SETTINGS), [])

    def test_single_strategy_middle_and_final(self) -> None:
        got = warning_triggers(metrics(factor_health={"A": {"months_below": 4}, "C": {"months_below": 2}},
                                       E=-0.30, M=-0.01), SETTINGS)
        keys = {t.key: t for t in got}
        self.assertEqual(set(keys), {"warn2:leg:A", "warn2:leg:E", "warn2:leg:M", "warn2:all"})
        self.assertEqual(keys["warn2:leg:E"].scope, "E腿")
        self.assertEqual(keys["warn2:all"].scope, "全部策略")
        self.assertIn("A腿、E腿、M同时到中级警告", keys["warn2:all"].detail)
        self.assertTrue(all(t.tier == TIER_MIDDLE for t in got))
        final = warning_triggers(metrics(A=-0.55, C=-0.75), SETTINGS)
        self.assertEqual({t.key for t in final}, {"warn3:leg:A", "warn3:leg:C", "warn3:all"})
        self.assertTrue(all(t.tier == TIER_FINAL for t in final))
        self.assertIn("建议先暂停交易", final[0].action)

    def test_whole_combination_and_all_strategies(self) -> None:
        got = {t.key: t for t in warning_triggers(metrics(
            combo_drawdown={"value": -0.33, "peak": "20260813", "as_of": "20261120"},
            combo_relative={"value": -0.04, "days_behind": 20, "as_of": "20261120"}), SETTINGS)}
        self.assertEqual(set(got), {"warn2:combo:drawdown", "warn2:combo:relative"})
        self.assertEqual(got["warn2:combo:drawdown"].scope, "整体组合")
        self.assertIn("低4.0%", got["warn2:combo:relative"].detail)
        final = {t.key for t in warning_triggers(metrics(
            combo_drawdown={"value": -0.52, "peak": "20260813", "as_of": "20261120"},
            shadow_drawdown={"value": -0.56, "peak": "20260805", "as_of": "20261120"}), SETTINGS)}
        self.assertEqual(final, {"warn3:combo", "warn3:all:shadow"})

    def test_middle_left_unhandled_for_sixty_open_days_escalates(self) -> None:
        middle = warning_triggers(metrics(M=-0.01), SETTINGS)
        start = OPEN[OPEN.index("20260105")]
        state = {"first_seen": {"warn2:leg:M": start}}
        not_yet = escalate(middle, state, acknowledged={}, today=OPEN[OPEN.index(start) + 59], open_dates=OPEN,
                           open_days=60)
        self.assertEqual([t.key for t in not_yet], ["warn2:leg:M"])
        due = escalate(middle, state, acknowledged={}, today=OPEN[OPEN.index(start) + 60], open_dates=OPEN,
                       open_days=60)
        self.assertEqual([t.key for t in due], ["warn2:leg:M", "warn3:escalated:warn2:leg:M"])
        self.assertEqual((due[1].tier, due[1].scope), (TIER_FINAL, "M"))
        handled = escalate(middle, state, acknowledged={"warn2:leg:M": "已复核：维持"},
                           today=OPEN[OPEN.index(start) + 80], open_dates=OPEN, open_days=60)
        self.assertEqual(len(handled), 1, "登记了复核结论就不升级")


class PushAndTerminalTests(unittest.TestCase):
    def _plan(self, triggers):
        return plan_push(triggers, {}, today="20261120", open_dates=OPEN, remind_every_open_days=5)

    def test_titles_follow_the_highest_tier(self) -> None:
        first = ReviewTrigger("shadow_3m", "策略逻辑", "影子账近3个月-25%")
        middle = warning_triggers(metrics(M=-0.01), SETTINGS)
        final = warning_triggers(metrics(A=-0.55), SETTINGS)
        self.assertIn("请发起甲·临时复核", self._plan([first]).title)
        self.assertEqual(self._plan([first] + middle).title, "⚠️ 中级警告：M需要策略复核")
        plan = self._plan([first] + middle + final)
        self.assertEqual(plan.title, "⛔ 终极警告：程序和策略已经走坏，建议停止交易、优化策略（A腿）")
        self.assertTrue(plan.body.startswith("1.【终极·建议停止交易·新】"))
        self.assertIn("系统不会自己停止交易", plan.body)

    def test_terminal_banner(self) -> None:
        triggers = warning_triggers(metrics(A=-0.55, M=-0.01), SETTINGS)
        report = warning_report(triggers, {}, {}, signal_date="20261120", generated_at="t")
        lines = banner_lines(report)
        self.assertIn("⛔ 终极警告（20261120）：程序和策略已经走坏，建议停止交易、优化策略（A腿）", lines)
        self.assertIn("⚠️ 中级警告（20261120）：M需要策略复核", lines)
        self.assertIn("  （系统不会自己停止交易，停不停由你决定）", lines)
        acked = warning_report(triggers, {t.key: "已复核" for t in triggers}, {}, signal_date="20261120",
                               generated_at="t")
        self.assertEqual(banner_lines(acked), [])
        self.assertEqual(banner_lines(None), [])


class CacheTests(unittest.TestCase):
    def test_only_new_and_recent_days_are_recomputed(self) -> None:
        days = OPEN[:60]
        calls = []

        def reference(day):
            calls.append(day)
            return {"status": "OK", "candidates": [{"ts_code": "300001.SZ"}] if day == days[0] else []}

        def outcome(code, day):
            calls.append((code, day))
            return (days[2], 10.0, 0.05)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            candidates, errors, cached, save = _cached_m_inputs(reference, outcome, days, days, path)
            self.assertEqual(cached("300001.SZ", days[0]), (days[2], 10.0, 0.05))
            save()
            self.assertEqual(len(calls), 61)
            calls.clear()
            candidates, errors, cached, save = _cached_m_inputs(reference, outcome, days, days, path)
            cached("300001.SZ", days[0])
            self.assertEqual(len(calls), 20, "只重算最近20个交易日；老日子与已知结果都取缓存")
            self.assertEqual(candidates, {days[0]: ["300001.SZ"]})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)


if __name__ == "__main__":
    unittest.main()
