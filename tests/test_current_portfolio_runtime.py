from __future__ import annotations

import json
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

from scripts import trading_daemon
from src.combined_live_engine import CombinedLiveEngine
from src.live_certification import validate_live_certification


def make_engine(
    *,
    positions: list[dict] | None = None,
    ac_leg: str | None = None,
    with_e: bool = False,
    with_n: bool = False,
) -> CombinedLiveEngine:
    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = Path(__file__).resolve().parents[1]
    engine.config = {
        "trade_mode": "backtest",
        "position": {"initial_cash": 500_000},
        "live_trade": {"max_single_order_amount": 0},
        "active_strategy_profile": {"mode": 1, "mode_name": "D_A_E_C_N"},
        "strategy_n": {"enabled": True, "live_order_enabled": True, "position_pct": 0.825},
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
    engine.build_n_buy_order_if_any = lambda _today, _codes=None: (
        {
            "strategy_leg": "N", "side": "BUY", "ts_code": "000004.SZ",
            "name": "测试N", "planned_order_date": "20260803",
            "round_lot_shares": 10_000, "planned_amount_by_equity": 412_500.0,
        }
        if with_n else None
    )
    engine.active_strategy_mode = lambda: 1
    engine.active_strategy_name = lambda: "D_A_E_C_N"
    engine.is_b_strategy_removed = lambda: True
    return engine


class CurrentPortfolioRuntimeTests(unittest.TestCase):
    def test_n_live_order_uses_t1_open_t2_close_and_825pct(self) -> None:
        engine = object.__new__(CombinedLiveEngine)
        engine.config = {
            "trade_mode": "backtest",
            "position": {"initial_cash": 500_000},
            "live_trade": {"max_single_order_amount": 0},
            "strategy_n": {
                "enabled": True,
                "live_order_enabled": True,
                "position_pct": 0.825,
                "exit_hold_offset": 2,
            },
        }
        engine.load_yesterday_n_signal = lambda _today: {
            "signal_date": "20260818",
            "planned_buy_date": "20260819",
            "ts_code": "300001.SZ",
            "name": "测试N",
            "limit_close": 10.0,
        }
        order = CombinedLiveEngine.build_n_buy_order_if_any(engine, "20260819")
        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order["strategy_leg"], "N")
        self.assertEqual(order["planned_action"], "PLAN_BUY_T1_OPEN")
        self.assertEqual(order["round_lot_shares"], 41_200)
        self.assertEqual(order["exit_n_days"], 1)
        self.assertAlmostEqual(order["planned_amount_by_equity"], 412_000.0)

    def test_current_priority_is_a_then_e_then_c_then_n(self) -> None:
        for ac_leg, with_e, with_n, expected in (
            ("A", True, True, "A"),
            (None, True, True, "E"),
            ("C", False, True, "C"),
            (None, False, True, "N"),
        ):
            with self.subTest(expected=expected):
                engine = make_engine(ac_leg=ac_leg, with_e=with_e, with_n=with_n)
                _state, _decisions, orders = engine.build_mode1_plan("20260803")
                buys = orders[orders["side"].astype(str).str.upper().eq("BUY")]
                self.assertEqual(str(buys.iloc[0]["strategy_leg"]), expected)

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

    def test_certification_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "cert.json").write_text(
                json.dumps({
                    "status": "PASS_WITH_RISK_ACCEPTANCE",
                    "current_executable": True,
                    "scenario": "current_d_a_e_c_n",
                }),
                encoding="utf-8",
            )
            check = validate_live_certification(
                root,
                {
                    "certification_summary_path": "cert.json",
                    "certification_required_status": "PASS_WITH_RISK_ACCEPTANCE",
                    "certification_expected_scenario": "current_d_a_e_c_n",
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
                patch.object(
                    trading_daemon,
                    "_load_n_signal_for_signal_date",
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
        self.assertIn("A/E/C/N均无开仓计划", message)
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
            return {
                "signal_date": "20260820",
                "status": "NO_SIGNAL_OCCUPIED",
                "reason": "账户有未平仓头寸，N仅保留只读候选",
                "candidate_count": 1,
            }

        def csv_candidate(strategy: str, _signal_date: str) -> dict | None:
            if strategy == "N":
                return {
                    "strategy": "N",
                    "ts_code": "002491.SZ",
                    "name": "通鼎互联",
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
                    "_load_n_signal_for_signal_date",
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
                    side_effect=csv_candidate,
                ),
                patch.object(
                    trading_daemon,
                    "load_positions",
                    return_value=[{
                        "strategy_leg": "N",
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
            "⑤ N最低优先级：不触发（当前有持仓）｜候选 002491.SZ 通鼎互联",
            message,
        )
        self.assertIn("账户空仓时首选：E策略 688433.SH 华曙高科", message)
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
        n_signal = {
            "planned_buy_date": "20260821",
            "limit_close": 10.0,
            "position_pct": 0.825,
            "ts_code": "002001.SZ",
            "name": "测试N",
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
                    "_load_n_signal_for_signal_date",
                    return_value=n_signal,
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
                        "strategy_leg": "N",
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
            "② A主策略：不触发（当前有持仓）｜候选 000001.SZ 测试A",
            "③ E策略：不触发（当前有持仓）｜候选 300001.SZ 测试E",
            "④ C垫底：不触发（当前有持仓）｜候选 600001.SH 测试C",
            "⑤ N最低优先级：不触发（当前有持仓）｜候选 002001.SZ 测试N",
        ):
            self.assertIn(expected, message)
        self.assertIn(
            "① D盘中：不触发｜无候选",
            message,
        )
        log.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
