from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

import pandas as pd

if "dotenv" not in sys.modules:
    stub = ModuleType("dotenv")
    stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = stub

from scripts.certify_current_executable_portfolio import resolve_m_release_status
from scripts import run_strategy_e_signal, run_strategy_m_signal
from src.combined_live_engine import CombinedLiveEngine
from src.live_certification import validate_live_certification


def make_engine(
    *,
    positions: list[dict] | None = None,
    ac_leg: str | None = None,
    with_m: bool = False,
    with_e: bool = False,
) -> CombinedLiveEngine:
    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = Path(__file__).resolve().parents[1]
    engine.config = {
        "trade_mode": "backtest",
        "position": {"initial_cash": 500_000},
        "live_trade": {"max_single_order_amount": 0},
        "active_strategy_profile": {"mode": 1, "mode_name": "D_A_M_E_C"},
        "strategy_m": {"enabled": True, "live_order_enabled": True},
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
    engine.active_strategy_mode = lambda: 1
    engine.active_strategy_name = lambda: "D_A_M_E_C"
    engine.is_b_strategy_removed = lambda: True
    return engine


class CurrentPortfolioRuntimeTests(unittest.TestCase):
    def test_current_priority_is_a_then_m_then_e_then_c(self) -> None:
        for ac_leg, with_m, with_e, expected in (
            ("A", True, True, "A"),
            (None, True, True, "M"),
            (None, False, True, "E"),
            ("C", False, False, "C"),
        ):
            with self.subTest(expected=expected):
                engine = make_engine(ac_leg=ac_leg, with_m=with_m, with_e=with_e)
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
                    "scenario": "current_d_a_m_e_c",
                }),
                encoding="utf-8",
            )
            check = validate_live_certification(
                root,
                {
                    "certification_summary_path": "cert.json",
                    "certification_required_status": "PASS_WITH_RISK_ACCEPTANCE",
                    "certification_expected_scenario": "current_d_a_m_e_c",
                },
            )
            self.assertTrue(check.ok, check.reason)


if __name__ == "__main__":
    unittest.main()
