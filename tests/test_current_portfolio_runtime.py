from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pandas as pd

if "dotenv" not in sys.modules:
    stub = ModuleType("dotenv")
    stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = stub

from scripts.certify_current_executable_portfolio import resolve_m_release_status
from scripts import run_strategy_e_signal, run_strategy_m_signal, trading_daemon
from src.combined_live_engine import CombinedLiveEngine
from src.live_certification import validate_live_certification


def make_engine(
    *,
    positions: list[dict] | None = None,
    ac_leg: str | None = None,
    with_m: bool = False,
    with_e: bool = False,
    with_n: bool = False,
) -> CombinedLiveEngine:
    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = Path(__file__).resolve().parents[1]
    engine.config = {
        "trade_mode": "backtest",
        "position": {"initial_cash": 500_000},
        "live_trade": {"max_single_order_amount": 0},
        "active_strategy_profile": {"mode": 1, "mode_name": "D_A_M_E_C_N"},
        "strategy_m": {"enabled": True, "live_order_enabled": True},
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
    engine.build_m_buy_order_if_any = lambda _today, _codes=None: (
        {
            "strategy_leg": "M",
            "side": "BUY",
            "ts_code": "000003.SZ",
            "name": "测试M",
            "planned_order_date": "20260803",
            "round_lot_shares": 10_000,
            "planned_amount_by_equity": 412_500.0,
        }
        if with_m else None
    )
    engine.build_n_buy_order_if_any = lambda _today, _codes=None: (
        {
            "strategy_leg": "N", "side": "BUY", "ts_code": "000004.SZ",
            "name": "测试N", "planned_order_date": "20260803",
            "round_lot_shares": 10_000, "planned_amount_by_equity": 412_500.0,
        }
        if with_n else None
    )
    engine.active_strategy_mode = lambda: 1
    engine.active_strategy_name = lambda: "D_A_M_E_C_N"
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

    def test_current_priority_is_a_then_m_then_e_then_c_then_n(self) -> None:
        for ac_leg, with_m, with_e, with_n, expected in (
            ("A", True, True, True, "A"),
            (None, True, True, True, "M"),
            (None, False, True, True, "E"),
            ("C", False, False, True, "C"),
            (None, False, False, True, "N"),
        ):
            with self.subTest(expected=expected):
                engine = make_engine(ac_leg=ac_leg, with_m=with_m, with_e=with_e, with_n=with_n)
                _state, _decisions, orders = engine.build_mode1_plan("20260803")
                buys = orders[orders["side"].astype(str).str.upper().eq("BUY")]
                self.assertEqual(str(buys.iloc[0]["strategy_leg"]), expected)

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
            with_m=True,
            with_e=True,
        )
        _state, decisions, orders = engine.build_mode1_plan("20260803")
        self.assertFalse(
            not orders.empty
            and orders.get("side", pd.Series(dtype=str)).astype(str).str.upper().eq("BUY").any()
        )
        self.assertIn("BLOCK_ABC_BUY", set(decisions["action"]))

    def test_m_upstream_gate_only_checks_a(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            old = run_strategy_e_signal.DAILY_OPS_DIR
            root = Path(temporary)
            run_strategy_e_signal.DAILY_OPS_DIR = root
            try:
                pd.DataFrame([{
                    "strategy_leg": "C",
                    "side": "BUY",
                    "ts_code": "000001.SZ",
                }]).to_csv(root / "ops_20260803_planned_orders.csv", index=False)
                self.assertEqual(
                    run_strategy_m_signal.higher_priority_leg_has_signal("20260803")[0],
                    False,
                )
                pd.DataFrame([{
                    "strategy_leg": "A",
                    "side": "BUY",
                    "ts_code": "000002.SZ",
                }]).to_csv(root / "ops_20260803_planned_orders.csv", index=False)
                self.assertEqual(
                    run_strategy_m_signal.higher_priority_leg_has_signal("20260803")[0],
                    True,
                )
            finally:
                run_strategy_e_signal.DAILY_OPS_DIR = old

    def test_certification_validator_and_risk_status(self) -> None:
        self.assertEqual(
            resolve_m_release_status(
                m_live_enabled=True,
                m_noninferior=False,
                risk_accepted=True,
                noninferiority_reason="分段回撤变差",
            ),
            "PASS_WITH_RISK_ACCEPTANCE",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "cert.json").write_text(
                json.dumps({
                    "status": "PASS_WITH_RISK_ACCEPTANCE",
                    "current_executable": True,
                    "scenario": "current_d_a_m_e_c_n",
                }),
                encoding="utf-8",
            )
            check = validate_live_certification(
                root,
                {
                    "certification_summary_path": "cert.json",
                    "certification_required_status": "PASS_WITH_RISK_ACCEPTANCE",
                    "certification_expected_scenario": "current_d_a_m_e_c_n",
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
                    "_load_m_signal_for_signal_date",
                    return_value=None,
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
        self.assertIn("A/M/E/C/N均无开仓计划", message)
        log.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
