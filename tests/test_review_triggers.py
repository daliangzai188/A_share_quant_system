"""临时复核触发器：只要坏了就必须准确、及时、成对地推送（2026-10-03用户要求）。"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

os.environ["A_SYSTEM_DISABLE_NOTIFICATIONS"] = "1"
from src.review_triggers import (
    ReviewTrigger,
    active_acknowledgements,
    alignment_triggers,
    check_failure_trigger,
    factor_health_triggers,
    live_loss_streak_triggers,
    monthly_realized_pnl,
    plan_push,
    regime_stall_triggers,
    shadow_triggers,
)

ROOT = Path(__file__).resolve().parents[1]
OPEN_DATES = pd.bdate_range("2026-09-01", "2026-12-31").strftime("%Y%m%d").tolist()


class ConditionTests(unittest.TestCase):
    def test_shadow_windows_and_missed_gain(self) -> None:
        values = np.concatenate([np.linspace(1.0, 2.0, 200), np.linspace(2.0, 1.4, 63)])
        triggers = shadow_triggers(values, window_3m=63, window_6m=126, fail_3m=-0.241, fail_6m=-0.194,
                                   stop_episode={"start_date": "20260916", "shadow_return": 0.30},
                                   worst_missed_gain=0.269)
        keys = {t.key: t for t in triggers}
        self.assertIn("shadow_3m", keys)
        self.assertNotIn("shadow_6m", keys)
        self.assertIn("stop_missed_gain:20260916", keys)
        self.assertFalse(keys["stop_missed_gain:20260916"].requires_review, "踏空只记入年度复核")
        self.assertEqual(shadow_triggers(values[:50], window_3m=63, window_6m=126, fail_3m=-0.241,
                                         fail_6m=-0.194, stop_episode=None, worst_missed_gain=0.269), [])

    def test_regime_stall_and_factor_health(self) -> None:
        self.assertEqual(regime_stall_triggers({"triggered": False}), [])
        stall = regime_stall_triggers({"triggered": True, "min_limit_up": 40, "streak": 3,
                                       "months": ["202609", "202610", "202611"]})
        self.assertEqual(stall[0].key, "regime_stall")
        health = factor_health_triggers({
            "A": {"triggered": True, "value": -0.0005, "months_below": 2, "line": 0.0054, "samples": 219},
            "C": {"triggered": False, "value": 0.0167},
        })
        self.assertEqual([t.key for t in health], ["factor_health:A"])

    def test_live_loss_streak_needs_three_losing_months_with_trades(self) -> None:
        losing = [("202607", 49446.0, 10), ("202608", -46945.0, 7), ("202609", -12025.0, 5), ("202610", -3000.0, 2)]
        triggers = live_loss_streak_triggers(losing, need_months=3)
        self.assertEqual(triggers[0].key, "live_loss_streak:202608")
        self.assertIn("连续3个月亏损", triggers[0].detail)
        self.assertEqual(live_loss_streak_triggers(losing[:3], need_months=3), [])
        idle = [("202608", -46945.0, 7), ("202609", -12025.0, 5), ("202610", 0.0, 0)]
        self.assertEqual(live_loss_streak_triggers(idle, need_months=2), [], "空仓月份不算亏损月")

    def test_monthly_realized_pnl_fills_empty_months(self) -> None:
        completed = pd.DataFrame({"exit_date": ["20260803", "20260821", "20260902"], "net_pnl": [-5244.0, -33366.0, -3924.0]})
        self.assertEqual(monthly_realized_pnl(completed, ["202607", "202608", "202609"]),
                         [("202607", 0.0, 0), ("202608", -38610.0, 2), ("202609", -3924.0, 1)])

    def test_alignment_catches_missing_extra_stop_day_and_d_conflict(self) -> None:
        replay = pd.DataFrame([
            {"action_date": "20261012", "ts_code": "000001.SZ", "strategy_leg": "A", "position_opened": True},
            {"action_date": "20261013", "ts_code": "000002.SZ", "strategy_leg": "C", "position_opened": True},
            {"action_date": "20261014", "ts_code": "000003.SZ", "strategy_leg": "E", "position_opened": False},
            {"action_date": "20261015", "ts_code": "000004.SZ", "strategy_leg": "A", "position_opened": True},
            {"action_date": "20260930", "ts_code": "000009.SZ", "strategy_leg": "A", "position_opened": True},
        ])
        live = pd.DataFrame([
            {"entry_date": "20261012", "ts_code": "000001.SZ", "strategy_leg": "A"},
            {"entry_date": "20261014", "ts_code": "000003.SZ", "strategy_leg": "E"},
            {"entry_date": "20261015", "ts_code": "300001.SZ", "strategy_leg": "D"},
            {"entry_date": "20261016", "ts_code": "300002.SZ", "strategy_leg": "D"},
            {"entry_date": "20261019", "ts_code": "600001.SH", "strategy_leg": "C"},
        ])
        keys = [t.key for t in alignment_triggers(replay, live, stop_dates={"20261019"},
                                                   start="20261001", end="20261031")]
        self.assertEqual(keys, [
            "alignment:missing:20261013:000002.SZ",
            "alignment:missing:20261015:000004.SZ",
            "alignment:extra:20261014:000003.SZ",
            "alignment:d_conflict:20261015:300001.SZ",
            "alignment:stop_day:20261019:600001.SH",
        ])

    def test_check_failure_is_a_trigger_not_silence(self) -> None:
        trigger = check_failure_trigger("因子健康", "KeyError: 'value'")
        self.assertEqual(trigger.category, "检查失效")
        self.assertTrue(trigger.requires_review)


class PushPlanTests(unittest.TestCase):
    A = ReviewTrigger("factor_health:A", "策略逻辑", "A腿跌破")
    S = ReviewTrigger("shadow_3m", "策略逻辑", "影子账近3个月-25%")
    GAIN = ReviewTrigger("stop_missed_gain:20261001", "停手参数", "踏空+30%", requires_review=False,
                         action="记入复核记录")

    def _plan(self, triggers, state, today, **kwargs):
        return plan_push(triggers, state, today=today, open_dates=OPEN_DATES, remind_every_open_days=5, **kwargs)

    def test_new_condition_alerts_then_reminds_every_five_open_days_then_clears(self) -> None:
        first = self._plan([self.S], {}, "20261012")
        self.assertEqual(first.kind, "alert")
        self.assertIn("请发起甲·临时复核", first.title)
        self.assertIn("甲·临时复核", first.body)
        self.assertEqual(self._plan([self.S], first.state, "20261013").kind, "")
        self.assertEqual(self._plan([self.S], first.state, "20261016").kind, "")
        reminder = self._plan([self.S], first.state, "20261019")
        self.assertEqual(reminder.kind, "reminder")
        self.assertIn("仍需", reminder.title)
        self.assertIn("自20261012起", reminder.body)
        cleared = self._plan([], reminder.state, "20261020")
        self.assertEqual(cleared.kind, "cleared")
        self.assertEqual(self._plan([], cleared.state, "20261021").kind, "")

    def test_another_condition_appearing_alerts_immediately(self) -> None:
        first = self._plan([self.S], {}, "20261012")
        second = self._plan([self.S, self.A], first.state, "20261013")
        self.assertEqual(second.kind, "alert")
        self.assertIn("【策略逻辑·新】A腿跌破", second.body)

    def test_acknowledged_condition_is_not_repeated_but_listed_when_others_fire(self) -> None:
        ack = {"factor_health:A": "2026-09-30已复核：维持"}
        self.assertEqual(self._plan([self.A], {}, "20261012", acknowledged=ack).kind, "")
        plan = self._plan([self.A, self.S], {}, "20261013", acknowledged=ack)
        self.assertEqual(plan.kind, "alert")
        self.assertIn("1项触发", plan.title)
        self.assertIn("另有已复核、仍在观察：A腿跌破（2026-09-30已复核：维持）", plan.body)

    def test_annual_input_only_is_labelled_as_record_not_review(self) -> None:
        plan = self._plan([self.GAIN], {}, "20261012")
        self.assertEqual(plan.kind, "alert")
        self.assertIn("需记录事项", plan.title)
        self.assertNotIn("请发起", plan.title)

    def test_acknowledgement_expires_on_recheck_date(self) -> None:
        entries = [{"key": "factor_health:A", "reviewed_on": "2026-09-30", "conclusion": "维持", "recheck_by": "2027-01-02"}]
        self.assertIn("factor_health:A", active_acknowledgements(entries, "20261231"))
        self.assertEqual(active_acknowledgements(entries, "20270104"), {})
        self.assertEqual(active_acknowledgements([{"key": "x"}], "20261012"), {}, "没有复查日期的登记无效")


def _load_update_module():
    spec = importlib.util.spec_from_file_location("update_equity_curve_stop", ROOT / "scripts" / "update_equity_curve_stop.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DailyCheckTests(unittest.TestCase):
    """⑬每日检查：推送失败下次重发；检查自己失败也推送。"""

    def setUp(self) -> None:
        self.module = _load_update_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "review_state.json"
        dates = pd.bdate_range("2026-01-01", "2026-10-12").strftime("%Y%m%d").tolist()
        values = np.concatenate([np.linspace(1.0, 2.0, len(dates) - 63), np.linspace(2.0, 1.4, 63)])
        self.shadow = {"nav": pd.Series(values, index=dates)}
        self.config = {
            "equity_curve_stop": {"weekly_report": {"enabled": True}},
            "review_triggers": {"enabled": True, "alignment_start_date": "", "remind_every_open_days": 5,
                                "state_path": str(self.state_path), "acknowledged": []},
        }
        self.settings = SimpleNamespace(ma_window=60, lag_trading_days=6)
        self.calendar = Path(self.tmp.name) / "cal.csv"
        pd.DataFrame({"cal_date": dates, "is_open": 1}).to_csv(self.calendar, index=False)
        empty = pd.DataFrame(columns=["exit_date", "net_pnl"])
        self.live = patch.object(self.module, "_live_trades", return_value=(empty, empty))
        self.live.start()
        self.addCleanup(self.live.stop)
        self.stall = patch.object(self.module, "compute_regime_stall", return_value={"triggered": False})
        self.stall.start()
        self.addCleanup(self.stall.stop)

    def _run(self, notify_result=True, factor_error=""):
        with patch.object(self.module, "notify", return_value=notify_result) as notify:
            ok = self.module.run_review_trigger_check(
                self.config, self.settings, self.shadow, self.calendar, "20261012",
                Path(self.tmp.name) / "sentiment.csv", {}, factor_error,
            )
        return ok, notify

    def test_failed_push_leaves_state_untouched_so_next_day_retries(self) -> None:
        ok, notify = self._run(notify_result=False)
        self.assertFalse(ok)
        notify.assert_called_once()
        self.assertIn("请发起甲·临时复核", notify.call_args.args[0])
        self.assertFalse(self.state_path.exists())
        ok, notify = self._run(notify_result=True)
        self.assertTrue(ok)
        notify.assert_called_once()
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["active_keys"], ["shadow_3m"])
        ok, notify = self._run()
        notify.assert_not_called()

    def test_check_failure_is_pushed(self) -> None:
        self.shadow = {"nav": pd.Series([1.0, 1.0, 1.0], index=["20261008", "20261009", "20261012"])}
        _ok, notify = self._run(factor_error="ValueError: 研究池缺列")
        self.assertIn("检查失效", notify.call_args.args[1])
        self.assertIn("因子健康没算成", notify.call_args.args[1])

    def test_live_summary_unreadable_is_pushed(self) -> None:
        self.shadow = {"nav": pd.Series([1.0, 1.0, 1.0], index=["20261008", "20261009", "20261012"])}
        with patch.object(self.module, "_live_trades", side_effect=FileNotFoundError("汇总不存在")):
            _ok, notify = self._run()
        self.assertIn("实盘成交汇总没算成", notify.call_args.args[1])

    def test_formal_config_enables_daily_check_and_registers_a_review(self) -> None:
        config = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        section = config["review_triggers"]
        self.assertTrue(section["enabled"])
        self.assertEqual(section["alignment_start_date"], "20260921")
        self.assertEqual(section["live_loss_months"], 3)
        ack = active_acknowledgements(section["acknowledged"], "20261012")
        self.assertEqual(list(ack), ["factor_health:A"])


class WeeklyReportAcknowledgementTests(unittest.TestCase):
    """周报与每日检查同一口径：已复核的条件单列“仍在观察”，不再每周要求复核。"""

    def test_acknowledged_factor_health_is_watched_not_failure(self) -> None:
        from src.equity_curve_stop import format_weekly_report, load_weekly_report_settings, weekly_report_payload

        settings = load_weekly_report_settings({"equity_curve_stop": {"weekly_report": {"enabled": True}}})
        dates = pd.bdate_range("2026-01-01", periods=200).strftime("%Y%m%d").tolist()
        nav = np.linspace(1.0, 2.0, 200)
        health = {"A": {"triggered": True, "value": -0.0005, "months_below": 2, "need_months": 2,
                        "line": 0.0054, "samples": 219}}
        payload = weekly_report_payload(dates, nav, {"action_date": "20261231", "allowed": True}, settings,
                                        ma_window=60, lag=6, factor_health=health,
                                        acknowledged={"factor_health:A": "2026-09-30已复核：维持"})
        self.assertEqual(payload["failures"], [])
        body = format_weekly_report(payload)[1]
        self.assertNotIn("请在Claude发送", body)
        self.assertIn("已复核、仍在观察：A腿", body)
        plain = weekly_report_payload(dates, nav, {"action_date": "20261231", "allowed": True}, settings,
                                      ma_window=60, lag=6, factor_health=health)
        self.assertEqual(len(plain["failures"]), 1)


class DaemonPipelineAlertTests(unittest.TestCase):
    """⑬被杀或没跑到时，脚本自己发不出告警，必须由daemon补报。"""

    def setUp(self) -> None:
        from scripts import trading_daemon

        self.daemon = trading_daemon

    def test_stale_decision_is_alerted_once_with_failed_steps(self) -> None:
        with patch.object(self.daemon, "_equity_curve_stop_decision_fresh", return_value=(False, "20261013")), \
                patch.object(self.daemon, "_notify_once_per", return_value=True) as alert:
            self.daemon._alert_if_stop_decision_stale("20261012", ["⑬ 方案甲停手门禁（影子净值→次日判定）"])
        alert.assert_called_once()
        self.assertEqual(alert.call_args.args[0], "equity_curve_stop_decision_stale:20261012")
        self.assertIn("没有更新", alert.call_args.args[2])
        self.assertIn("临时复核触发检查也没有执行", alert.call_args.args[3])

    def test_fresh_decision_is_silent(self) -> None:
        with patch.object(self.daemon, "_equity_curve_stop_decision_fresh", return_value=(True, "20261013")), \
                patch.object(self.daemon, "_notify_once_per") as alert:
            self.daemon._alert_if_stop_decision_stale("20261012", [])
        alert.assert_not_called()

    def test_pipeline_not_completed_by_cutoff_is_alerted(self) -> None:
        with patch.object(self.daemon, "_equity_curve_stop_decision_fresh", return_value=(False, "20261013")), \
                patch.object(self.daemon, "_notify", return_value=True) as notify:
            self.daemon._alert_post_market_not_completed("20261012", 23)
        self.assertIn("仍未完成", notify.call_args.args[1])
        self.assertIn("fail-closed", notify.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
