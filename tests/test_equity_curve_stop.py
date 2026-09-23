"""方案甲影子净值停手门禁：计算定义、fail-closed、组合状态机、回撤报警与复核提醒。"""
from __future__ import annotations

import datetime
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

if "dotenv" not in sys.modules:
    stub = ModuleType("dotenv")
    stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = stub

from scripts import trading_daemon
from src.combined_live_engine import CombinedLiveEngine
from src.equity_curve_stop import (
    METHOD_ID,
    allowed_flags,
    current_stop_episode,
    decision_for_next_action_date,
    earliest_resume_if_flat,
    filter_plans_to_allowed_dates,
    format_weekly_report,
    is_week_last_open_day,
    live_gate,
    load_regime_buckets,
    load_weekly_report_settings,
    monthly_limit_up_mean,
    regime_calibration,
    shadow_nav_from_detail,
    weekly_report_payload,
)


ROOT = Path(__file__).resolve().parents[1]


def research_reference(nav: np.ndarray, ma_window: int, lag: int) -> np.ndarray:
    """2026-09-19研究脚本中的原始写法，作为唯一定义的对照。"""
    moving = pd.Series(nav).rolling(ma_window).mean().to_numpy()
    flags = np.ones(len(nav), dtype=bool)
    for index in range(len(nav)):
        lagged = index - lag
        flags[index] = True if lagged < ma_window else bool(nav[lagged] >= moving[lagged])
    return flags


class EquityCurveStopDefinitionTests(unittest.TestCase):
    def setUp(self) -> None:
        rng = np.random.default_rng(20260919)
        self.nav = np.cumprod(1 + rng.normal(0.002, 0.03, 400))
        self.dates = [f"2025{i:04d}" for i in range(400)]

    def test_flags_match_research_formula(self) -> None:
        for ma_window, lag in ((60, 6), (40, 10), (20, 1)):
            np.testing.assert_array_equal(
                allowed_flags(self.nav, ma_window=ma_window, lag=lag),
                research_reference(self.nav, ma_window, lag),
            )

    def test_warm_up_allows_entries(self) -> None:
        flags = allowed_flags(self.nav[:66], ma_window=60, lag=6)
        self.assertTrue(flags.all())

    def test_next_day_decision_equals_flag_on_appended_series(self) -> None:
        for count in range(60, 400, 7):
            decision = decision_for_next_action_date(
                self.dates[:count], self.nav[:count], "29990101", ma_window=60, lag=6
            )
            appended = np.append(self.nav[:count], np.nan)
            self.assertEqual(
                decision["allowed"],
                bool(allowed_flags(appended, ma_window=60, lag=6)[-1]),
            )

    def test_next_day_must_be_after_last_shadow_date(self) -> None:
        with self.assertRaises(ValueError):
            decision_for_next_action_date(self.dates, self.nav, self.dates[-1], ma_window=60, lag=6)

    def test_blocked_decision_explains_lag_nav_and_ma(self) -> None:
        nav = np.concatenate([np.linspace(1.0, 2.0, 80), np.linspace(2.0, 1.2, 20)])
        dates = [f"d{i:03d}" for i in range(len(nav))]
        decision = decision_for_next_action_date(dates, nav, "d999", ma_window=60, lag=6)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["lag_date"], dates[len(nav) - 6])
        self.assertLess(decision["lag_nav"], decision["lag_ma"])
        self.assertIn("方案甲停手", decision["reason"])

    def test_filter_drops_only_stop_day_entries(self) -> None:
        plans = {
            "A": pd.DataFrame({"action_date": ["20260901", "20260902"], "ts_code": ["1", "2"]}),
            "D": pd.DataFrame(),
        }
        filtered = filter_plans_to_allowed_dates(plans, {"20260902"})
        self.assertEqual(filtered["A"]["ts_code"].tolist(), ["2"])
        self.assertTrue(filtered["D"].empty)

    def test_shadow_nav_forward_fills_days_without_records(self) -> None:
        detail = pd.DataFrame({"action_date": ["d1", "d3"], "equity_after": [510_000.0, 540_000.0]})
        nav = shadow_nav_from_detail(detail, ["d0", "d1", "d2", "d3"], initial_cash=500_000.0)
        self.assertEqual(nav.round(4).tolist(), [1.0, 1.02, 1.02, 1.08])


class LiveGateFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {
            "equity_curve_stop": {
                "enabled": True,
                "ma_window": 60,
                "lag_trading_days": 6,
                "decision_path": "state/decision.json",
            }
        }
        self.path = self.root / "state" / "decision.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, **overrides: object) -> None:
        payload = {
            "method_id": METHOD_ID,
            "action_date": "20260921",
            "ma_window": 60,
            "lag_trading_days": 6,
            "allowed": True,
            "reason": "正常开仓",
        }
        payload.update(overrides)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_disabled_gate_always_allows(self) -> None:
        check = live_gate({"equity_curve_stop": {"enabled": False}}, self.root, "20260921")
        self.assertTrue(check.allowed)
        self.assertTrue(live_gate({}, self.root, "20260921").allowed)

    def test_missing_decision_blocks(self) -> None:
        check = live_gate(self.config, self.root, "20260921")
        self.assertFalse(check.allowed)
        self.assertIn("fail-closed", check.reason)

    def test_stale_decision_blocks(self) -> None:
        self.write(action_date="20260918")
        self.assertFalse(live_gate(self.config, self.root, "20260921").allowed)

    def test_method_or_parameter_mismatch_blocks(self) -> None:
        self.write(method_id="other")
        self.assertFalse(live_gate(self.config, self.root, "20260921").allowed)
        self.write(ma_window=40)
        self.assertFalse(live_gate(self.config, self.root, "20260921").allowed)

    def test_corrupt_or_incomplete_decision_blocks(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertFalse(live_gate(self.config, self.root, "20260921").allowed)
        self.write(allowed=None)
        self.assertFalse(live_gate(self.config, self.root, "20260921").allowed)

    def test_valid_decision_passes_through(self) -> None:
        self.write(allowed=True)
        self.assertTrue(live_gate(self.config, self.root, "20260921").allowed)
        self.write(allowed=False, reason="影子净值低于60日均线，方案甲停手")
        check = live_gate(self.config, self.root, "20260921")
        self.assertFalse(check.allowed)
        self.assertIn("方案甲停手", check.reason)

    def test_formal_config_enables_gate_with_frozen_parameters(self) -> None:
        config = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        section = config["equity_curve_stop"]
        self.assertTrue(section["enabled"])
        self.assertEqual((section["ma_window"], section["lag_trading_days"]), (60, 6))
        self.assertEqual(section["history_start"], "20190101")


def make_engine(*, allowed: bool, positions: list[dict] | None = None, ac_leg: str | None = None) -> CombinedLiveEngine:
    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = ROOT
    engine.config = {
        "trade_mode": "backtest",
        "position": {"initial_cash": 500_000},
        "live_trade": {"max_single_order_amount": 0},
        "active_strategy_profile": {"mode": 1, "mode_name": "A_C_E_D"},
    }
    engine.load_positions = lambda: list(positions or [])
    orders = pd.DataFrame(
        [{
            "strategy_leg": ac_leg,
            "side": "BUY",
            "ts_code": "000001.SZ",
            "name": f"测试{ac_leg}",
            "planned_order_date": "20260921",
            "reference_price": 10.0,
            "round_lot_shares": 10_000,
            "estimated_shares": 10_000,
        }]
    ) if ac_leg else pd.DataFrame()
    engine.load_latest_abc_orders = lambda: (Path("orders.csv"), orders.copy())
    engine.load_yesterday_e_signal = lambda _today: None
    engine.load_today_e_signal = lambda _today: None
    engine.compute_e_preview = lambda _today: {"has_candidate": False, "has_scored_data": True, "neutral_segs": []}
    engine.active_strategy_mode = lambda: 1
    engine.active_strategy_name = lambda: "A_C_E_D"
    engine.is_b_strategy_removed = lambda: True
    reason = "影子净值低于60日均线，方案甲停手" if not allowed else "正常开仓"
    engine.equity_curve_stop_check = lambda _today: SimpleNamespace(allowed=allowed, reason=reason, source="test")
    return engine


class CombinedEngineStopGateTests(unittest.TestCase):
    def test_stop_blocks_all_new_entries_and_d_monitor(self) -> None:
        engine = make_engine(allowed=False, ac_leg="A")
        _state, decisions, orders = engine.build_mode1_plan("20260921")
        actions = set(decisions["action"])
        self.assertTrue({"BLOCK_ABC_BUY", "BLOCK_E_BUY", "BLOCK_D_INTRADAY_MONITOR"} <= actions)
        self.assertFalse(any(action.startswith("ALLOW_") and action != "ALLOW_E_SIGNAL" for action in actions))
        self.assertNotIn("ALLOW_E_SIGNAL", actions)
        self.assertTrue(orders.empty)
        block_reasons = decisions[decisions["action"].eq("BLOCK_D_INTRADAY_MONITOR")]["reason"].tolist()
        self.assertTrue(all("方案甲停手" in reason for reason in block_reasons))

    def test_allowed_gate_keeps_normal_a_plan(self) -> None:
        engine = make_engine(allowed=True, ac_leg="A")
        _state, decisions, _orders = engine.build_mode1_plan("20260921")
        self.assertIn("ALLOW_ABC_BUY_PREVIEW", set(decisions["action"]))
        self.assertIn("BLOCK_D_INTRADAY_MONITOR", set(decisions["action"]))

    def test_allowed_gate_without_candidates_starts_d(self) -> None:
        engine = make_engine(allowed=True)
        _state, decisions, _orders = engine.build_mode1_plan("20260921")
        self.assertIn("ALLOW_D_INTRADAY_MONITOR", set(decisions["action"]))

    def test_existing_d_position_branch_still_wins(self) -> None:
        position = {"strategy_leg": "D", "status": "open", "ts_code": "300001.SZ", "planned_exit_date": "20260922", "shares": 100}
        engine = make_engine(allowed=False, positions=[position])
        _state, decisions, _orders = engine.build_mode1_plan("20260921")
        d_reasons = decisions[decisions["action"].eq("BLOCK_D_INTRADAY_MONITOR")]["reason"].tolist()
        self.assertTrue(any("D持仓" in reason for reason in d_reasons))


class DaemonStopAndDrawdownTests(unittest.TestCase):
    def test_block_reason_reads_same_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "config.json").write_text(
                json.dumps({"equity_curve_stop": {"enabled": True, "decision_path": "d.json"}}),
                encoding="utf-8",
            )
            with patch.object(trading_daemon, "PROJECT_ROOT", root):
                reason = trading_daemon._equity_curve_stop_block_reason("20260921")
        self.assertIn("fail-closed", reason)

    def test_drawdown_alert_fires_once_per_peak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {"drawdown_alert": {"enabled": True, "threshold": -0.30, "state_path": "state/dd.json"}}
            snapshot = SimpleNamespace(equity=65_000.0, peak_equity=100_000.0, pending_incomplete_trade_count=2)
            with patch.object(trading_daemon, "PROJECT_ROOT", root), \
                    patch.object(trading_daemon, "_notify", return_value=True) as notify:
                trading_daemon._maybe_alert_strategy_drawdown(snapshot, -0.35, cfg)
                trading_daemon._maybe_alert_strategy_drawdown(snapshot, -0.36, cfg)
                self.assertEqual(notify.call_count, 1)
                body = notify.call_args[0][2]
                self.assertIn("甲·临时复核", body)
                self.assertIn("2笔成交待补全", body)
                new_peak = SimpleNamespace(equity=80_000.0, peak_equity=120_000.0, pending_incomplete_trade_count=0)
                trading_daemon._maybe_alert_strategy_drawdown(new_peak, -0.333, cfg)
                self.assertEqual(notify.call_count, 2)

    def test_drawdown_above_threshold_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(trading_daemon, "PROJECT_ROOT", Path(tmp)), \
                patch.object(trading_daemon, "_notify", return_value=True) as notify:
            snapshot = SimpleNamespace(equity=90_000.0, peak_equity=100_000.0, pending_incomplete_trade_count=0)
            trading_daemon._maybe_alert_strategy_drawdown(
                snapshot, -0.10, {"drawdown_alert": {"enabled": True, "threshold": -0.30}}
            )
            notify.assert_not_called()



class WeeklyReportTests(unittest.TestCase):
    """空仓期周报：只读已算好的影子净值，逐条比对写死的失败线。"""

    def setUp(self) -> None:
        self.settings = load_weekly_report_settings(
            {"equity_curve_stop": {"weekly_report": {"enabled": True, "window_3m": 3, "window_6m": 5}}}
        )

    @staticmethod
    def _stopped_series(length: int = 200) -> tuple[list[str], np.ndarray]:
        """前段稳步上涨、后段持续下跌，保证最后一天处于停手。"""
        calendar = pd.bdate_range("2026-01-01", periods=length).strftime("%Y%m%d").tolist()
        nav = np.concatenate([np.linspace(1.0, 3.0, length - 40), np.linspace(3.0, 1.8, 40)])
        return calendar, nav

    def test_week_last_open_day_detects_friday_and_holiday_short_week(self) -> None:
        opens = ["20260921", "20260922", "20260923", "20260924", "20260925", "20260928"]
        self.assertTrue(is_week_last_open_day(opens, "20260925"))
        self.assertFalse(is_week_last_open_day(opens, "20260924"))
        # 周五休市时自动前移到本周实际最后一个开市日
        short = ["20260921", "20260922", "20260923", "20260924", "20260928"]
        self.assertTrue(is_week_last_open_day(short, "20260924"))
        self.assertFalse(is_week_last_open_day(short, "20260923"))
        self.assertFalse(is_week_last_open_day(opens, "20260926"))

    def test_current_stop_episode_counts_days_and_shadow_move(self) -> None:
        dates, nav = self._stopped_series()
        episode = current_stop_episode(dates, nav, ma_window=60, lag=6)
        self.assertIsNotNone(episode)
        flags = allowed_flags(nav, ma_window=60, lag=6)
        expected_days = 0
        for flag in reversed(flags):
            if flag:
                break
            expected_days += 1
        self.assertEqual(episode["trading_days"], expected_days)
        self.assertEqual(episode["start_date"], dates[len(flags) - expected_days])
        self.assertLess(episode["shadow_return"], 0.0)  # 本段影子账下跌=躲掉下跌

    def test_no_episode_when_entries_allowed(self) -> None:
        dates = pd.bdate_range("2026-01-01", periods=200).strftime("%Y%m%d").tolist()
        nav = np.linspace(1.0, 4.0, 200)
        self.assertIsNone(current_stop_episode(dates, nav, ma_window=60, lag=6))

    def test_payload_reports_returns_gap_and_no_failure(self) -> None:
        dates, nav = self._stopped_series()
        decision = {"action_date": "20261231", "allowed": False, "lag_nav": 100.0, "lag_ma": 110.0}
        future = pd.bdate_range("2027-01-01", periods=200).strftime("%Y%m%d").tolist()
        payload = weekly_report_payload(
            dates, nav, decision, self.settings, ma_window=60, lag=6, future_open_dates=future
        )
        self.assertEqual(
            payload["earliest_resume_if_flat"],
            earliest_resume_if_flat(nav, future, ma_window=60, lag=6),
        )
        self.assertAlmostEqual(payload["return_3m"], nav[-1] / nav[-4] - 1.0)
        self.assertAlmostEqual(payload["return_6m"], nav[-1] / nav[-6] - 1.0)
        self.assertAlmostEqual(payload["gap_to_resume"], 0.10)
        self.assertEqual(payload["failures"], [])
        self.assertFalse(payload["allowed"])
        title, body = format_weekly_report(payload)
        self.assertIn("停手第", title)
        self.assertIn("恢复开仓还差", body)
        self.assertIn("原地不动", body)
        self.assertIn("未触发", body)

    def test_failure_lines_use_frozen_thresholds(self) -> None:
        settings = load_weekly_report_settings(
            {
                "equity_curve_stop": {
                    "weekly_report": {
                        "enabled": True,
                        "window_3m": 3,
                        "window_6m": 5,
                        "shadow_3m_fail": -0.10,
                        "shadow_6m_fail": -0.15,
                        "worst_stop_missed_gain": 0.05,
                    }
                }
            }
        )
        dates = pd.bdate_range("2026-01-01", periods=200).strftime("%Y%m%d").tolist()
        nav = np.concatenate([np.linspace(1.0, 3.0, 160), np.linspace(3.0, 1.2, 40)])
        payload = weekly_report_payload(dates, nav, {"action_date": "20261231", "allowed": False},
                                        settings, ma_window=60, lag=6)
        self.assertTrue(any("近3个月" in item for item in payload["failures"]))
        self.assertTrue(any("近6个月" in item for item in payload["failures"]))
        body = format_weekly_report(payload)[1]
        self.assertIn("甲·临时复核", body)

    def test_missed_rally_during_stop_is_flagged(self) -> None:
        dates = pd.bdate_range("2026-01-01", periods=200).strftime("%Y%m%d").tolist()
        nav = np.concatenate([np.linspace(1.0, 3.0, 150), np.linspace(3.0, 1.5, 30), np.linspace(1.5, 2.6, 20)])
        settings = load_weekly_report_settings(
            {
                "equity_curve_stop": {
                    "weekly_report": {"enabled": True, "window_3m": 3, "window_6m": 5, "worst_stop_missed_gain": 0.05}
                }
            }
        )
        payload = weekly_report_payload(dates, nav, {"action_date": "20261231", "allowed": False},
                                        settings, ma_window=60, lag=6)
        episode = payload["stop_episode"]
        self.assertIsNotNone(episode)
        self.assertGreater(episode["shadow_return"], 0.0)
        self.assertTrue(any("踏空" in item for item in payload["failures"]))

    def test_earliest_resume_if_flat_matches_flag_definition(self) -> None:
        dates, nav = self._stopped_series()
        future = pd.bdate_range("2027-01-01", periods=200).strftime("%Y%m%d").tolist()
        resume = earliest_resume_if_flat(nav, future, ma_window=60, lag=6)
        self.assertIsNotNone(resume)
        index = future.index(resume)
        # 把"原地不动"的净值补齐到该日，用唯一定义重算，应当正好在这天放行
        extended = np.concatenate([nav, np.repeat(nav[-1], index + 1)])
        flags = allowed_flags(extended, ma_window=60, lag=6)
        self.assertTrue(bool(flags[-1]))
        self.assertFalse(bool(flags[-2]))

    def test_no_resume_within_window_returns_none(self) -> None:
        dates = pd.bdate_range("2026-01-01", periods=200).strftime("%Y%m%d").tolist()
        nav = np.concatenate([np.linspace(1.0, 6.0, 190), np.linspace(6.0, 2.0, 10)])
        self.assertIsNone(
            earliest_resume_if_flat(nav, dates[:5], ma_window=60, lag=6, max_days=5)
        )

    def test_formal_config_enables_weekly_report_with_frozen_thresholds(self) -> None:
        config = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        settings = load_weekly_report_settings(config)
        self.assertTrue(settings.enabled)
        self.assertEqual((settings.window_3m, settings.window_6m), (63, 126))
        self.assertAlmostEqual(settings.shadow_3m_fail, -0.241)
        self.assertAlmostEqual(settings.shadow_6m_fail, -0.194)
        self.assertAlmostEqual(settings.worst_stop_missed_gain, 0.269)

    def test_report_failure_never_breaks_the_gate(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "update_equity_curve_stop", ROOT / "scripts" / "update_equity_curve_stop.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        config = {"equity_curve_stop": {"weekly_report": {"enabled": True}}}
        settings = SimpleNamespace(ma_window=60, lag_trading_days=6)
        broken = SimpleNamespace(index=[], to_numpy=lambda: np.array([]))
        with patch.object(module, "notify") as notify:
            self.assertFalse(
                module.push_weekly_report(
                    config, settings, broken, {}, ROOT / "data" / "raw" / "missing.csv", "20260925"
                )
            )
            notify.assert_not_called()


class RegimeCalibrationTests(unittest.TestCase):
    """行情校准：把"行情不好"和"策略变坏"分开；只报告，不参与门禁。"""

    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        self.buckets, self.min_months = load_regime_buckets(self.config)

    def test_formal_config_freezes_four_buckets(self) -> None:
        self.assertEqual([b.label for b in self.buckets], ["0~40", "40~60", "60~80", "80以上"])
        self.assertEqual(self.min_months, 5)
        sixty = self.buckets[2]
        self.assertEqual(sixty.oos_months, 16)
        self.assertAlmostEqual(sixty.oos_median, 0.0229)
        self.assertAlmostEqual(sixty.oos_win_rate, 0.562)

    def test_bucket_edges_are_left_closed_right_open(self) -> None:
        self.assertEqual(regime_calibration(39.9, 0.0, self.buckets, min_months=5)["bucket"], "0~40")
        self.assertEqual(regime_calibration(40.0, 0.0, self.buckets, min_months=5)["bucket"], "40~60")
        self.assertEqual(regime_calibration(79.9, 0.0, self.buckets, min_months=5)["bucket"], "60~80")
        self.assertEqual(regime_calibration(300.0, 0.0, self.buckets, min_months=5)["bucket"], "80以上")

    def test_gap_is_actual_minus_bucket_median(self) -> None:
        out = regime_calibration(70.0, 0.10, self.buckets, min_months=5)
        self.assertTrue(out["sufficient_sample"])
        self.assertAlmostEqual(out["gap"], 0.10 - 0.0229)

    def test_thin_bucket_reports_regime_without_comparison(self) -> None:
        out = regime_calibration(150.0, 0.10, self.buckets, min_months=5)
        self.assertFalse(out["sufficient_sample"])
        self.assertIsNone(out["gap"])

    def test_missing_inputs_return_none(self) -> None:
        self.assertIsNone(regime_calibration(None, 0.1, self.buckets, min_months=5))
        self.assertIsNone(regime_calibration(70.0, 0.1, [], min_months=5))

    def test_monthly_limit_up_mean_reads_only_that_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market_sentiment.csv"
            pd.DataFrame(
                {
                    "trade_date": ["20260831", "20260901", "20260902", "20261001"],
                    "limit_up_count": [100, 60, 80, 10],
                }
            ).to_csv(path, index=False)
            self.assertAlmostEqual(monthly_limit_up_mean(path, "202609"), 70.0)
            self.assertIsNone(monthly_limit_up_mean(Path(tmp) / "missing.csv", "202609"))

    def test_report_line_shows_expectation_and_gap(self) -> None:
        dates = pd.bdate_range("2026-01-01", periods=200).strftime("%Y%m%d").tolist()
        nav = np.concatenate([np.linspace(1.0, 3.0, 160), np.linspace(3.0, 2.4, 40)])
        settings = load_weekly_report_settings(self.config)
        payload = weekly_report_payload(
            dates, nav, {"action_date": "20261231", "allowed": False}, settings,
            ma_window=60, lag=6,
            regime=regime_calibration(70.0, -0.05, self.buckets, min_months=5),
        )
        body = format_weekly_report(payload)[1]
        self.assertIn("行情校准", body)
        self.assertIn("60~80档", body)
        self.assertIn("+2.3%", body)

    def test_report_without_regime_keeps_old_shape(self) -> None:
        dates = pd.bdate_range("2026-01-01", periods=200).strftime("%Y%m%d").tolist()
        nav = np.linspace(1.0, 4.0, 200)
        settings = load_weekly_report_settings(self.config)
        payload = weekly_report_payload(dates, nav, {"action_date": "20261231", "allowed": True},
                                        settings, ma_window=60, lag=6)
        self.assertIsNone(payload["regime"])
        self.assertNotIn("行情校准", format_weekly_report(payload)[1])


class ReviewReminderTests(unittest.TestCase):
    def test_quarterly_and_annual_messages(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "mac_monthly_maintenance_reminder", ROOT / "scripts" / "mac_monthly_maintenance_reminder.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        self.assertIn("甲·年度复核", module.plan_jia_review_message(datetime.date(2027, 1, 2))[1])
        for month in (4, 7, 10):
            self.assertIn("甲·季度体检", module.plan_jia_review_message(datetime.date(2026, month, 3))[1])
        self.assertIsNone(module.plan_jia_review_message(datetime.date(2026, 11, 7)))


if __name__ == "__main__":
    unittest.main()
