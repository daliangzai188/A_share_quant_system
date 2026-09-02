from __future__ import annotations

import json
import datetime
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

if "dotenv" not in sys.modules:
    stub = ModuleType("dotenv")
    stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = stub

from scripts import run_strategy_e_signal, trading_daemon
from src.combined_live_engine import CombinedLiveEngine
from src.live_certification import validate_live_certification


def make_engine(
    *,
    positions: list[dict] | None = None,
    ac_leg: str | None = None,
    with_e: bool = False,
) -> CombinedLiveEngine:
    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = Path(__file__).resolve().parents[1]
    engine.config = {
        "trade_mode": "backtest",
        "position": {"initial_cash": 500_000},
        "live_trade": {"max_single_order_amount": 0},
        "active_strategy_profile": {"mode": 1, "mode_name": "A_C_E_D"},
    }
    engine.load_positions = lambda: list(positions or [])
    if ac_leg:
        orders = pd.DataFrame(
            [{
                "strategy_leg": ac_leg,
                "side": "BUY",
                "ts_code": "000001.SZ",
                "name": f"测试{ac_leg}",
                "planned_order_date": "20260803",
                "reference_price": 10.0,
                "round_lot_shares": 10_000,
                "estimated_shares": 10_000,
            }]
        )
    else:
        orders = pd.DataFrame()
    engine.load_latest_abc_orders = lambda: (Path("orders.csv"), orders.copy())
    engine.load_yesterday_e_signal = lambda _today: (
        {
            "signal_date": "20260731",
            "ts_code": "300002.SZ",
            "name": "测试E",
            "limit_close": 10.0,
            "exit_offset": 2,
        }
        if with_e else None
    )
    engine.load_today_e_signal = lambda _today: None
    engine.active_strategy_mode = lambda: 1
    engine.active_strategy_name = lambda: "A_C_E_D"
    engine.is_b_strategy_removed = lambda: True
    return engine


class CurrentPortfolioRuntimeTests(unittest.TestCase):
    @staticmethod
    def _formal_buy_order(**overrides: object) -> pd.DataFrame:
        row: dict[str, object] = {
            "strategy_leg": "A",
            "side": "BUY",
            "ts_code": "000001.SZ",
            "signal_date": "20260831",
            "planned_order_date": "20260901",
            "planned_action": "PLAN_BUY_T1_OPEN",
        }
        row.update(overrides)
        return pd.DataFrame([row])

    def test_formal_buy_order_contract_accepts_only_today_t1_plan(self) -> None:
        with patch.object(
            trading_daemon,
            "today_beijing",
            return_value=datetime.date(2026, 9, 1),
        ):
            selected = trading_daemon._select_unique_buy_order_for_action(
                self._formal_buy_order(),
                "ALLOW_ABC_BUY_PREVIEW",
                context="测试正式计划契约",
            )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected.iloc[0]["ts_code"], "000001.SZ")

    def test_stale_or_same_day_signal_buy_plan_fails_closed(self) -> None:
        invalid_cases = (
            {"planned_order_date": "20260831"},
            {"planned_action": "PLAN_BUY_T0_OPEN"},
            {"signal_date": "20260901"},
            {"signal_date": ""},
        )
        with patch.object(
            trading_daemon,
            "today_beijing",
            return_value=datetime.date(2026, 9, 1),
        ):
            for overrides in invalid_cases:
                with self.subTest(overrides=overrides):
                    selected = trading_daemon._select_unique_buy_order_for_action(
                        self._formal_buy_order(**overrides),
                        "ALLOW_ABC_BUY_PREVIEW",
                        context="测试异常计划契约",
                    )
                    self.assertTrue(selected.empty)

    def test_duplicate_same_leg_buy_plan_fails_closed(self) -> None:
        orders = pd.concat(
            [
                self._formal_buy_order(ts_code="000001.SZ"),
                self._formal_buy_order(ts_code="000002.SZ"),
            ],
            ignore_index=True,
        )
        with patch.object(
            trading_daemon,
            "today_beijing",
            return_value=datetime.date(2026, 9, 1),
        ):
            selected = trading_daemon._select_unique_buy_order_for_action(
                orders,
                "ALLOW_ABC_BUY_PREVIEW",
                context="测试重复计划契约",
            )

        self.assertTrue(selected.empty)

    def test_current_priority_is_a_then_c_then_e_then_d(self) -> None:
        for ac_leg, with_e, expected in (
            ("A", True, "A"),
            ("C", True, "C"),
            (None, True, "E"),
            ("C", False, "C"),
        ):
            with self.subTest(expected=expected):
                engine = make_engine(ac_leg=ac_leg, with_e=with_e)
                _state, _decisions, orders = engine.build_mode1_plan("20260803")
                buys = orders[orders["side"].astype(str).str.upper().eq("BUY")]
                self.assertEqual(str(buys.iloc[0]["strategy_leg"]), expected)

    def test_static_candidate_blocks_all_d_activity(self) -> None:
        """任意非D正式候选存在时，状态机只输出D全活动阻断。"""

        engine = make_engine(ac_leg="A")
        _state, decisions, _orders = engine.build_mode1_plan("20260803")
        actions = set(decisions["action"].astype(str))

        self.assertIn("BLOCK_D_INTRADAY_MONITOR", actions)
        self.assertNotIn("BLOCK_D_INTRADAY_ENTRY", actions)
        self.assertNotIn("ALLOW_D_INTRADAY_TRACKING_ONLY", actions)

    def test_any_strategy_candidate_blocks_d_scan_and_entry_all_day(self) -> None:
        """A/C/E及未来新增策略的候选，均不能因执行窗口结束而释放D。"""

        for action, leg in (
            ("ALLOW_ABC_BUY_PREVIEW", "A"),
            ("ALLOW_E_BUY", "E"),
            ("ALLOW_ABC_BUY_PREVIEW", "C"),
            ("ALLOW_F_BUY", "F"),
        ):
            decisions = pd.DataFrame([{
                "action": action,
                "strategy_leg": leg,
                "side": "BUY",
            }])
            with (
                patch.object(trading_daemon, "has_open_local_position", return_value=False),
                patch.object(trading_daemon, "has_position_bought_today", return_value=False),
                patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False),
                patch.object(trading_daemon, "load_pending_buys", return_value=[]),
                patch.object(trading_daemon, "_pov_active_today", return_value=False),
            ):
                monitor_allowed, monitor_reason = trading_daemon.d_intraday_monitor_gate(decisions)
                entry_allowed, entry_reason = trading_daemon.d_intraday_entry_gate(decisions)

            with self.subTest(strategy_leg=leg):
                self.assertFalse(monitor_allowed)
                self.assertFalse(entry_allowed)
                self.assertIn("全日互斥", monitor_reason)
                self.assertEqual(entry_reason, monitor_reason)

    def test_non_d_pending_order_or_pov_blocks_all_d_activity(self) -> None:
        decisions = pd.DataFrame([{
            "action": "ALLOW_D_INTRADAY_MONITOR",
            "strategy_leg": "D",
        }])

        for pending, pov, expected in (
            ([{"ts_code": "000001.SZ"}], False, "买单待确认"),
            ([], True, "开仓POV"),
        ):
            with (
                patch.object(trading_daemon, "has_open_local_position", return_value=False),
                patch.object(trading_daemon, "has_position_bought_today", return_value=False),
                patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False),
                patch.object(trading_daemon, "load_pending_buys", return_value=pending),
                patch.object(trading_daemon, "_pov_active_today", return_value=pov),
            ):
                monitor_allowed, monitor_reason = trading_daemon.d_intraday_monitor_gate(decisions)
                entry_allowed, entry_reason = trading_daemon.d_intraday_entry_gate(decisions)

            self.assertFalse(monitor_allowed)
            self.assertFalse(entry_allowed)
            self.assertIn(expected, monitor_reason)
            self.assertEqual(entry_reason, monitor_reason)

    def test_d_runs_only_when_position_candidate_and_plan_are_all_absent(self) -> None:
        decisions = pd.DataFrame([{
            "action": "ALLOW_D_INTRADAY_MONITOR",
            "strategy_leg": "D",
        }])
        with (
            patch.object(trading_daemon, "has_open_local_position", return_value=False),
            patch.object(trading_daemon, "has_position_bought_today", return_value=False),
            patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False),
            patch.object(trading_daemon, "load_pending_buys", return_value=[]),
            patch.object(trading_daemon, "_pov_active_today", return_value=False),
        ):
            monitor_allowed, monitor_reason = trading_daemon.d_intraday_monitor_gate(decisions)
            entry_allowed, entry_reason = trading_daemon.d_intraday_entry_gate(decisions)

        self.assertTrue(monitor_allowed)
        self.assertTrue(entry_allowed)
        self.assertIn("无持仓、无非D正式候选", monitor_reason)
        self.assertEqual(entry_reason, monitor_reason)

    def test_direct_d_start_is_rejected_before_qmt_or_monitor_setup(self) -> None:
        """即使绕过调度器直接调用D入口，候选总门也必须最先拦截。"""

        decisions = pd.DataFrame([{
            "action": "ALLOW_E_BUY",
            "strategy_leg": "E",
            "side": "BUY",
        }])
        with (
            patch.object(trading_daemon, "_strategy_d_monitor_running", return_value=False),
            patch.object(
                trading_daemon,
                "d_intraday_monitor_gate",
                return_value=(False, "今日存在E候选，D全日关闭"),
            ),
            patch.object(trading_daemon, "load_json_config") as load_config,
            patch.object(trading_daemon, "logger", return_value=MagicMock()),
        ):
            trading_daemon.job_strategy_d(decisions)

        load_config.assert_not_called()

    def test_startup_recovery_restores_e_before_d_gate_rejects_d(self) -> None:
        """盘中重启有E候选时应恢复E自己的重试，同时绝不启动D。"""

        frozen_now = datetime.datetime(
            2026, 8, 24, 10, 0, tzinfo=trading_daemon.BEIJING_TZ
        )
        decisions = pd.DataFrame([{
            "action": "ALLOW_E_BUY",
            "strategy_leg": "E",
            "side": "BUY",
        }, {
            "action": "BLOCK_D_INTRADAY_MONITOR",
            "strategy_leg": "D",
        }])
        orders_path = Path("e_orders.csv")
        with (
            patch.object(trading_daemon, "now_beijing", return_value=frozen_now),
            patch.object(trading_daemon, "is_trade_day", return_value=True),
            patch.object(trading_daemon, "load_pending_buys", return_value=[]),
            patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False),
            patch.object(trading_daemon, "_pov_active_today", return_value=False),
            patch.object(trading_daemon, "_strategy_d_monitor_running", return_value=False),
            patch.object(
                trading_daemon,
                "read_cached_combined_decisions",
                return_value=(decisions, orders_path),
            ),
            patch.object(trading_daemon, "intraday_history_is_complete", return_value=True),
            patch.object(
                trading_daemon,
                "_strategy_d_checkpoint_recovery_check",
                return_value=SimpleNamespace(ok=False, reason="无检查点"),
            ),
            patch.object(trading_daemon, "_e_retry_running", return_value=False),
            patch.object(trading_daemon, "_start_e_retry_thread") as start_e,
            patch.object(
                trading_daemon,
                "d_intraday_monitor_gate",
                return_value=(False, "今日存在E候选，D全日关闭"),
            ),
            patch.object(trading_daemon, "job_strategy_d") as start_d,
            patch.object(trading_daemon, "logger", return_value=MagicMock()),
        ):
            trading_daemon.startup_catchup_strategy_d()

        start_e.assert_called_once()
        self.assertEqual(start_e.call_args.args[0], orders_path)
        self.assertIs(start_e.call_args.args[1], decisions)
        start_d.assert_not_called()

    def test_blocked_e_run_records_priority_and_counterfactual_candidate_result(self) -> None:
        """A阻断正式信号后，E候选仍应独立计算并写入可审计字段。"""

        with (
            patch.object(
                run_strategy_e_signal,
                "inspect_blocked_e_candidates",
                return_value=(0, "E只读候选检查为0只，即使账户空仓也不会触发", None),
            ),
            patch.object(run_strategy_e_signal, "save_run_status") as save_status,
        ):
            run_strategy_e_signal.finish_occupied_without_e_signal(
                "20260821",
                "A今日已生成计划委托（腿序A>C>E），E不触发",
                dry_run=False,
            )

        kwargs = save_status.call_args.kwargs
        self.assertEqual(kwargs["candidate_count"], 0)
        self.assertEqual(kwargs["candidate_check_status"], "CALCULATED")
        self.assertEqual(kwargs["counterfactual_e_status"], "NO_CANDIDATE")
        self.assertIn("A今日已生成计划委托", kwargs["priority_blocker"])

    def test_e_signal_and_candidate_audit_require_v14_factor_values(self) -> None:
        """正式信号和候选审计必须保留V14排序及联合门禁证据。"""

        candidate = pd.Series(
            {
                "ts_code": "300001.SZ",
                "name": "测试股票",
                "market_segment": "chi_next",
                "segment_retreat_state_bucket": "neutral",
                "scenario": "test",
                "scenario_rank": 1,
                "exit_rule": "fixed_t2_close",
                "turnover_rate": 12.0,
                "amount_ratio_1d": 1.5,
                "amount_ratio_bucket": "1_2_2",
                "open_times_bucket": "1",
                "fd_amount_to_circ_mv": 0.008,
                "_e_rank_turnover_rate": 1.0,
                "_e_rank_fd_amount_to_circ_mv": 0.5,
                "_e_final_score": 0.75,
            }
        )
        signal = run_strategy_e_signal.build_signal("20260821", candidate, {})
        self.assertEqual(signal["turnover_rate"], 12.0)
        self.assertEqual(signal["amount_ratio_1d"], 1.5)
        self.assertEqual(signal["fd_amount_to_circ_mv"], 0.008)
        self.assertEqual(signal["final_ranking_score"], 0.75)

        missing = candidate.drop(labels=["fd_amount_to_circ_mv"])
        with self.assertRaisesRegex(RuntimeError, "fd_amount_to_circ_mv"):
            run_strategy_e_signal.build_signal("20260821", missing, {})

        with tempfile.TemporaryDirectory(prefix="e_candidate_audit_") as temp_dir:
            with patch.object(run_strategy_e_signal, "OUTPUT_DIR", Path(temp_dir)):
                path = run_strategy_e_signal.save_candidates(
                    "20260821",
                    pd.DataFrame([candidate]),
                    dry_run=False,
                )
            saved = pd.read_csv(path)
        self.assertTrue(
            {
                "turnover_rate",
                "amount_ratio_1d",
                "fd_amount_to_circ_mv",
                "amount_ratio_bucket",
                "open_times_bucket",
                "_e_rank_turnover_rate",
                "_e_rank_fd_amount_to_circ_mv",
                "_e_final_score",
            }.issubset(saved.columns)
        )

    def test_e_status_log_distinguishes_a_block_from_no_candidate(self) -> None:
        """播报必须明确：A阻断存在，但E候选也确实已单独算成0只。"""

        run = {
            "signal_date": "20260821",
            "status": "NO_SIGNAL_OCCUPIED",
            "reason": "A今日已生成计划委托；E只读候选检查为0只",
            "priority_blocker": "A今日已生成计划委托（腿序A>C>E），E不触发",
            "candidate_check_status": "CALCULATED",
            "counterfactual_e_status": "NO_CANDIDATE",
            "candidate_count": 0,
        }
        log = MagicMock()
        with (
            patch.object(trading_daemon, "_load_e_signal_for_signal_date", return_value=None),
            patch.object(trading_daemon, "_json_signal_has_date", return_value=False),
            patch.object(trading_daemon, "signal_run_by_signal_date", return_value=run),
            patch.object(trading_daemon, "logger", return_value=log),
        ):
            trading_daemon._log_e_signal_status("20260821")

        template, *args = log.info.call_args.args
        message = template % tuple(args)
        self.assertIn("优先级结论=A今日已生成计划委托", message)
        self.assertIn("E候选检查=已独立计算，结果=无候选（0只）", message)
        self.assertIn("即使没有上游策略占用，E也不会触发", message)

    def test_e_candidate_count_does_not_fallback_to_stale_csv_after_failed_run(self) -> None:
        """当日状态存在但候选检查失败时，不能从旧CSV伪造候选数。"""

        run = {
            "signal_date": "20260821",
            "status": "NO_SIGNAL_OCCUPIED",
            "candidate_check_status": "FAILED",
        }
        with (
            patch.object(trading_daemon, "signal_run_by_signal_date", return_value=run),
            patch.object(pd, "read_csv") as read_csv,
        ):
            self.assertIsNone(trading_daemon._load_e_candidate_count("20260821"))
        read_csv.assert_not_called()

    def test_close_pipeline_candidate_summary_reads_artifacts_only(self) -> None:
        """候选统计只读ACDE既有产物，并正确识别空CSV和D的BUY记录。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ac_dir = root / "reports" / "paper_trade" / "ab_filtered_daily_ops"
            e_dir = root / "reports" / "strategy_e"
            d_dir = root / "reports" / "strategy_d"
            ac_dir.mkdir(parents=True)
            e_dir.mkdir(parents=True)
            d_dir.mkdir(parents=True)

            pd.DataFrame([
                {"candidate_rank": 2, "ts_code": "300002.SZ", "name": "A第二名"},
                {"candidate_rank": 1, "ts_code": "300001.SZ", "name": "A第一名"},
            ]).to_csv(ac_dir / "profile_20260821_a_candidates.csv", index=False)
            (ac_dir / "profile_20260821_c_candidates.csv").write_text(
                "", encoding="utf-8"
            )
            pd.DataFrame(columns=["ts_code", "name"]).to_csv(
                e_dir / "e_signal_20260821_candidates.csv", index=False
            )
            pd.DataFrame([
                {"signal_type": "WATCH", "ts_code": "600001.SH", "name": "D观察"},
                {"signal_type": "BUY", "ts_code": "600002.SH", "name": "D候选"},
            ]).to_csv(d_dir / "intraday_signals_20260821.csv", index=False)

            with patch.object(trading_daemon, "PROJECT_ROOT", root):
                a_item = trading_daemon._read_close_candidate_artifact("A", "20260821")
                c_item = trading_daemon._read_close_candidate_artifact("C", "20260821")
                e_item = trading_daemon._read_close_candidate_artifact("E", "20260821")
                d_item = trading_daemon._read_close_candidate_artifact("D", "20260821")

        self.assertEqual(a_item["candidate_count"], 2)
        self.assertEqual(a_item["first_candidate"], "300001.SZ A第一名")
        self.assertTrue(c_item["calculated"])
        self.assertEqual(c_item["candidate_count"], 0)
        self.assertTrue(e_item["calculated"])
        self.assertEqual(e_item["candidate_count"], 0)
        self.assertEqual(d_item["candidate_count"], 1)
        self.assertEqual(d_item["first_candidate"], "600002.SH D候选")
        self.assertIn("WATCH记录=1", d_item["detail"])

    def test_close_pipeline_summary_does_not_call_missing_artifact_no_candidate(self) -> None:
        """D产物缺失只能记为未知，不能误报为D确实没有候选。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ac_dir = root / "reports" / "paper_trade" / "ab_filtered_daily_ops"
            e_dir = root / "reports" / "strategy_e"
            ac_dir.mkdir(parents=True)
            e_dir.mkdir(parents=True)
            pd.DataFrame([
                {"candidate_rank": 1, "ts_code": "300016.SZ", "name": "北陆药业"},
            ]).to_csv(ac_dir / "profile_20260821_a_candidates.csv", index=False)
            (ac_dir / "profile_20260821_c_candidates.csv").write_text(
                "", encoding="utf-8"
            )
            pd.DataFrame(columns=["ts_code", "name"]).to_csv(
                e_dir / "e_signal_20260821_candidates.csv", index=False
            )
            log = MagicMock()
            with (
                patch.object(trading_daemon, "PROJECT_ROOT", root),
                patch.object(trading_daemon, "logger", return_value=log),
            ):
                trading_daemon._log_close_pipeline_candidate_summary("20260821")

        message = str(log.info.call_args.args[0])
        self.assertIn("④ D盘中兜底：未完成统计", message)
        self.assertIn("不能据此断言D无候选", message)
        self.assertIn("① A主策略：已计算｜候选1只｜第一名 300016.SZ 北陆药业", message)
        self.assertIn("② C强势龙头：已计算｜候选0只", message)
        self.assertIn("③ E策略：已计算｜候选0只", message)

    def test_d_shared_proxy_checks_release_certification_before_buy(self) -> None:
        proxy = trading_daemon.SharedQMTBrokerProxy({"adapter": "qmt"})
        with (
            patch(
                "src.live_order_gateway.LiveOrderGateway.assert_real_order_allowed",
                side_effect=RuntimeError("认证失效"),
            ) as gate,
            patch.object(trading_daemon, "_qmt_get") as qmt_get,
        ):
            with self.assertRaisesRegex(RuntimeError, "认证失效"):
                proxy.place_order(SimpleNamespace(side="BUY"))
        gate.assert_called_once_with("A_SYSTEM_REAL_ORDER_CONFIRMED", side="BUY")
        qmt_get.assert_not_called()

    def test_opening_buy_gate_block_never_claims_pov_handoff(self) -> None:
        decisions = pd.DataFrame([{"action": "ALLOW_ABC_BUY_PREVIEW"}])
        log = MagicMock()
        with (
            patch.object(trading_daemon, "check_and_close_positions"),
            patch.object(trading_daemon, "confirm_pending_premarket_buys"),
            patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False),
            patch.object(trading_daemon, "_pov_active_today", return_value=False),
            patch.object(trading_daemon, "has_position_bought_today", return_value=False),
            patch.object(trading_daemon, "load_pending_buys", return_value=[]),
            patch.object(
                trading_daemon,
                "load_combined_decisions",
                return_value=(decisions, Path("orders.csv")),
            ),
            patch.object(trading_daemon, "d_intraday_monitor_gate", return_value=(False, "A占用")),
            patch.object(
                trading_daemon,
                "_new_buy_execution_gate",
                return_value=(False, "认证状态=FAIL_STRICT_RELEASE_REQUIRED"),
            ),
            patch.object(trading_daemon, "_notify_new_buy_gate_block") as blocked_notify,
            patch.object(trading_daemon, "_enqueue_opening_pov_from_plan") as enqueue,
            patch.object(trading_daemon, "logger", return_value=log),
        ):
            trading_daemon.job_opening_buy()

        blocked_notify.assert_called_once_with(
            "09:30开盘执行",
            "认证状态=FAIL_STRICT_RELEASE_REQUIRED",
        )
        enqueue.assert_not_called()
        self.assertFalse(
            any("转09:30持久化POV" in str(call_item) for call_item in log.mock_calls)
        )

    def test_candidate_broadcast_marks_failed_buy_gate_as_non_executable(self) -> None:
        orders = pd.DataFrame([{
            "strategy_leg": "A",
            "side": "BUY",
            "ts_code": "300016.SZ",
            "name": "北陆药业",
            "round_lot_shares": 6_400,
            "reference_price": 12.53,
        }])
        frozen_now = datetime.datetime(
            2026, 8, 24, 8, 50, tzinfo=trading_daemon.BEIJING_TZ
        )
        log = MagicMock()
        previous = trading_daemon._last_final_plan
        try:
            with (
                patch.object(trading_daemon.glob, "glob", return_value=["orders.csv"]),
                patch.object(pd, "read_csv", return_value=orders),
                patch.object(trading_daemon, "_load_e_signal_for_signal_date", return_value=None),
                patch.object(trading_daemon, "load_positions", return_value=[]),
                patch.object(trading_daemon, "today_beijing", return_value=frozen_now.date()),
                patch.object(trading_daemon, "now_beijing", return_value=frozen_now),
                patch.object(
                    trading_daemon,
                    "_new_buy_execution_gate",
                    return_value=(False, "认证状态=FAIL_STRICT_RELEASE_REQUIRED"),
                ),
                patch.object(trading_daemon, "_live_plan_sizing") as sizing,
                patch.object(trading_daemon, "logger", return_value=log),
            ):
                trading_daemon._log_decision_chain_summary("20260821")
        finally:
            current = trading_daemon._last_final_plan
            trading_daemon._last_final_plan = previous

        self.assertEqual(
            current["execution_block_reason"],
            "认证状态=FAIL_STRICT_RELEASE_REQUIRED",
        )
        self.assertEqual(current["final_buy"]["ts_code"], "300016.SZ")
        message = str(log.info.call_args.args[0])
        self.assertIn("不开新仓", message)
        self.assertIn("新增BUY安全门禁阻断", message)
        self.assertNotIn("★ 开仓计划：", message)
        sizing.assert_not_called()

    def test_plan_notification_calls_blocked_candidate_non_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "open_plan_push.json"
            previous = trading_daemon._last_final_plan
            trading_daemon._last_final_plan = {
                "action_date": "20260824",
                "final_buy": {
                    "strategy": "A",
                    "ts_code": "300016.SZ",
                    "name": "北陆药业",
                },
                "execution_block_reason": "认证状态=FAIL_STRICT_RELEASE_REQUIRED",
                "hold_line": "",
                "live_sizing": None,
            }
            try:
                with patch.object(
                    trading_daemon,
                    "_PLAN_PUSH_STATE",
                    state_path,
                ), patch.object(
                    trading_daemon,
                    "_notify",
                    return_value=True,
                ) as notify:
                    trading_daemon.push_open_plan_notification("早盘")
            finally:
                trading_daemon._last_final_plan = previous

        _event, title, body = notify.call_args.args[:3]
        self.assertIn("候选不可执行", title)
        self.assertIn("FAIL_STRICT_RELEASE_REQUIRED", body)
        self.assertIn("不会自动或建议手动绕过", body)

    def test_expired_candidate_notification_keeps_d_closed_all_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "open_plan_push.json"
            previous = trading_daemon._last_final_plan
            trading_daemon._last_final_plan = {
                "action_date": "20260824",
                "final_buy": {
                    "strategy": "F",
                    "ts_code": "600001.SH",
                    "name": "测试候选",
                },
                "execution_expired": True,
                "execution_block_reason": "",
                "hold_line": "",
                "live_sizing": None,
            }
            try:
                with patch.object(
                    trading_daemon,
                    "_PLAN_PUSH_STATE",
                    state_path,
                ), patch.object(
                    trading_daemon,
                    "_notify",
                    return_value=True,
                ) as notify:
                    trading_daemon.push_open_plan_notification("早盘")
            finally:
                trading_daemon._last_final_plan = previous

        _event, title, body = notify.call_args.args[:3]
        self.assertIn("原候选已错过执行窗口", title)
        self.assertIn("全日互斥规则阻断D", body)
        self.assertIn("D今日不扫描、不记录、不下单", body)

    def test_existing_position_blocks_all_new_buys(self) -> None:
        engine = make_engine(
            positions=[{
                "strategy_leg": "A",
                "status": "open",
                "ts_code": "600001.SH",
                "shares": 1000,
                "planned_exit_date": "20260803",
            }],
            ac_leg="A",
            with_e=True,
        )
        _state, decisions, orders = engine.build_mode1_plan("20260803")
        self.assertFalse(
            not orders.empty
            and orders.get("side", pd.Series(dtype=str)).astype(str).str.upper().eq("BUY").any()
        )
        self.assertIn("BLOCK_ABC_BUY", set(decisions["action"]))
        self.assertIn("BLOCK_D_INTRADAY_MONITOR", set(decisions["action"]))
        self.assertNotIn("ALLOW_D_INTRADAY_MONITOR", set(decisions["action"]))

    def test_certification_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "cert.json").write_text(
                json.dumps({
                    "status": "PASS_WITH_RISK_ACCEPTANCE",
                    "current_executable": True,
                    "scenario": "current_a_c_e_d",
                }),
                encoding="utf-8",
            )
            check = validate_live_certification(
                root,
                {
                    "certification_summary_path": "cert.json",
                    "certification_required_status": "PASS_WITH_RISK_ACCEPTANCE",
                    "certification_expected_scenario": "current_a_c_e_d",
                },
            )
            self.assertTrue(check.ok, check.reason)

    def test_empty_ac_plan_file_is_treated_as_no_candidate_in_broadcast(self) -> None:
        log = MagicMock()
        previous = trading_daemon._last_final_plan
        try:
            with (
                patch.object(
                    trading_daemon.glob,
                    "glob",
                    return_value=["empty_planned_orders.csv"],
                ),
                patch.object(
                    pd,
                    "read_csv",
                    side_effect=pd.errors.EmptyDataError("No columns to parse from file"),
                ),
                patch.object(
                    trading_daemon,
                    "_load_e_signal_for_signal_date",
                    return_value=None,
                ),
                patch.object(trading_daemon, "load_positions", return_value=[]),
                patch.object(trading_daemon, "logger", return_value=log),
            ):
                trading_daemon._log_decision_chain_summary("20260818")
        finally:
            current = trading_daemon._last_final_plan
            trading_daemon._last_final_plan = previous

        self.assertIsNone(current["final_buy"])
        message = str(log.info.call_args.args[0])
        self.assertIn("决策优先级流程图", message)
        self.assertIn("开仓决策链", message)
        self.assertIn("最终开仓计划", message)
        self.assertIn("A/C/E均无开仓计划", message)
        log.warning.assert_not_called()

    def test_e_readonly_candidate_is_shown_when_position_blocks_formal_signal(self) -> None:
        log = MagicMock()
        previous = trading_daemon._last_final_plan

        def run_status(path: Path, _signal_date: str) -> dict:
            if "strategy_e" in str(path):
                return {
                    "signal_date": "20260820",
                    "status": "NO_SIGNAL_OCCUPIED",
                    "reason": "账户有未平仓头寸；当前持仓阻断，不生成正式信号",
                    "candidate_count": 1,
                    "candidate_ts_code": "688433.SH",
                    "candidate_name": "华曙高科",
                }
            return None

        try:
            with (
                patch.object(
                    trading_daemon.glob,
                    "glob",
                    return_value=["empty_planned_orders.csv"],
                ),
                patch.object(
                    pd,
                    "read_csv",
                    side_effect=pd.errors.EmptyDataError("No columns to parse from file"),
                ),
                patch.object(
                    trading_daemon,
                    "_load_e_signal_for_signal_date",
                    return_value=None,
                ),
                patch.object(
                    trading_daemon,
                    "signal_run_by_signal_date",
                    side_effect=run_status,
                ),
                patch.object(
                    trading_daemon,
                    "_load_candidate_csv_for_broadcast",
                    return_value=None,
                ),
                patch.object(
                    trading_daemon,
                    "load_positions",
                    return_value=[{
                        "strategy_leg": "E",
                        "status": "open",
                        "ts_code": "301211.SZ",
                        "name": "亨迪药业",
                        "shares": 15_700,
                        "planned_exit_date": "20260821",
                    }],
                ),
                patch.object(trading_daemon, "logger", return_value=log),
            ):
                trading_daemon._log_decision_chain_summary("20260820")
        finally:
            current = trading_daemon._last_final_plan
            trading_daemon._last_final_plan = previous

        self.assertIsNone(current["final_buy"])
        message = str(log.info.call_args.args[0])
        self.assertIn(
            "③ E策略：不触发（当前有持仓）｜候选 688433.SH 华曙高科",
            message,
        )
        self.assertIn(
            "账户空仓时首选：E策略 688433.SH 华曙高科",
            message,
        )
        log.warning.assert_not_called()

    def test_all_static_legs_show_candidate_when_current_position_blocks(self) -> None:
        log = MagicMock()
        orders = pd.DataFrame([
            {
                "strategy_leg": "A",
                "side": "BUY",
                "ts_code": "000001.SZ",
                "name": "测试A",
                "round_lot_shares": 1000,
                "reference_price": 10.0,
            }
        ])
        e_signal = {
            "planned_buy_date": "20260821",
            "allow_buy_reliable": True,
            "limit_close": 10.0,
            "position_pct": 0.825,
            "ts_code": "300001.SZ",
            "name": "测试E",
        }
        def readonly_candidate(strategy: str, _signal_date: str) -> tuple[dict | None, str]:
            if strategy == "C":
                return {
                    "strategy": "C",
                    "ts_code": "600001.SH",
                    "name": "测试C",
                }, "上游策略占用"
            return None, ""

        previous = trading_daemon._last_final_plan
        try:
            with (
                patch.object(trading_daemon.glob, "glob", return_value=["orders.csv"]),
                patch.object(pd, "read_csv", return_value=orders),
                patch.object(
                    trading_daemon,
                    "_load_e_signal_for_signal_date",
                    return_value=e_signal,
                ),
                patch.object(
                    trading_daemon,
                    "_load_readonly_candidate_for_broadcast",
                    side_effect=readonly_candidate,
                ),
                patch.object(
                    trading_daemon,
                    "load_positions",
                    return_value=[{
                        "strategy_leg": "E",
                        "status": "open",
                        "ts_code": "301211.SZ",
                        "shares": 15_700,
                        "planned_exit_date": "20260821",
                    }],
                ),
                patch.object(trading_daemon, "logger", return_value=log),
            ):
                trading_daemon._log_decision_chain_summary("20260820")
        finally:
            trading_daemon._last_final_plan = previous

        message = str(log.info.call_args.args[0])
        for expected in (
            "① A主策略：不触发（当前有持仓）｜候选 000001.SZ 测试A",
            "② C强势龙头：不触发（当前有持仓）｜候选 600001.SH 测试C",
            "③ E策略：不触发（当前有持仓）｜候选 300001.SZ 测试E",
        ):
            self.assertIn(expected, message)
        self.assertIn(
            "④ D盘中兜底：不触发｜无候选",
            message,
        )
        log.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
