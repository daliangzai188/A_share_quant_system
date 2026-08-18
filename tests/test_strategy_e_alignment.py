from __future__ import annotations

import unittest
from pathlib import Path
from types import ModuleType
import sys

import pandas as pd

# 纯规则测试不加载.env；开发机未安装python-dotenv时注入无副作用最小桩。
if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from src.combined_live_engine import CombinedLiveEngine
from src.strategy_e import (
    E_VERSION,
    FORBIDDEN_SELECTION_COLUMNS,
    apply_e_entry_gate,
    build_r1_universe_from_pool,
    load_e_spec,
    parse_scenario_name,
    required_signal_fields,
    select_e_candidates,
    select_e_daily_picks,
)
from src.strategy_identity import normalize_strategy_frame, normalize_strategy_leg


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_test_spec() -> dict:
    return {
        "universe_prefilters": {
            "exclude_st_or_delisting": True,
            "exclude_amount_ratio_bucket": ["0_8_1_2"],
        },
        "sort_rules": {
            "turnover_desc": {"columns": ["turnover_rate"], "ascending": [False]},
            "fd_ratio_asc": {"columns": ["fd_amount_to_circ_mv"], "ascending": [True]},
        },
        "exit_rules": {
            "fixed_t2_close": {"hold_offset": 2},
            "fixed_hold3_close": {"hold_offset": 3},
        },
        "scenarios": [
            {
                "scenario_rank": 1,
                "scenario": "测试规则1",
                "conditions": {"factor_bucket": "good"},
                "sort_rule": "turnover_desc",
                "exit_rule": "fixed_t2_close",
            },
            {
                "scenario_rank": 2,
                "scenario": "测试规则2",
                "conditions": {"volume_ratio_bucket": "1_2"},
                "sort_rule": "fd_ratio_asc",
                "exit_rule": "fixed_hold3_close",
            },
        ],
    }


def make_pool() -> pd.DataFrame:
    common = {
        "trade_date": "20260731",
        "limit_data_quality": "full",
        "strategy_compatible": True,
        "allow_buy_reliable": True,
        "is_fill_score_reliable": True,
        "is_fd_amount_abnormal": False,
        "is_st": False,
        "amount_ratio_bucket": "2_3",
        "segment_retreat_state_bucket": "neutral",
        "volume_ratio_bucket": "1_2",
    }
    return pd.DataFrame(
        [
            {
                **common,
                "ts_code": "300001.SZ",
                "name": "甲公司",
                "circ_mv": 200_000,
                "factor_bucket": "good",
                "turnover_rate": 8.0,
                "fd_amount_to_circ_mv": 0.008,
                "net_return": -0.50,
                "buy_executed": False,
            },
            {
                **common,
                "ts_code": "300002.SZ",
                "name": "乙公司",
                "circ_mv": 100_000,
                "factor_bucket": "good",
                "turnover_rate": 12.0,
                "fd_amount_to_circ_mv": 0.010,
                "net_return": 0.50,
                "buy_executed": True,
            },
        ]
    )


class StrategyEAlignmentTests(unittest.TestCase):
    def test_production_spec_has_40_rules_and_no_future_columns(self) -> None:
        spec = load_e_spec(PROJECT_ROOT)
        self.assertEqual(len(spec["scenarios"]), 40)
        self.assertTrue({"fixed_t2_close", "fixed_hold3_close"}.issubset(spec["exit_rules"]))
        self.assertFalse(required_signal_fields(spec) & FORBIDDEN_SELECTION_COLUMNS)
        self.assertEqual(
            spec["entry_gate"]["exclude_values"]["first_time_detail_bucket"],
            ["1330_1430"],
        )
        self.assertIn("ENTRY_GATE_V4", E_VERSION)

    def test_scenario_parser_restores_conditions_sort_and_exit(self) -> None:
        parsed = parse_scenario_name(
            "large_universe_sort_volume_ratio_bucket=1_2;limit_times_bucket=1"
            "|sort=turnover_desc|exit=fixed_t2_close_desc",
            7,
        )
        self.assertEqual(parsed["scenario_rank"], 7)
        self.assertEqual(parsed["conditions"], {"volume_ratio_bucket": "1_2", "limit_times_bucket": "1"})
        self.assertEqual(parsed["sort_rule"], "turnover_desc")
        self.assertEqual(parsed["exit_rule"], "fixed_t2_close")

    def test_selection_ignores_future_profit_and_execution_results(self) -> None:
        spec = make_test_spec()
        original = select_e_candidates(build_r1_universe_from_pool(make_pool(), spec)).iloc[0]

        changed = make_pool()
        changed["net_return"] = [-9.0, 9.0]
        changed["buy_executed"] = [True, False]
        after = select_e_candidates(build_r1_universe_from_pool(changed, spec)).iloc[0]

        self.assertEqual(original["ts_code"], after["ts_code"])
        self.assertEqual(original["scenario_rank"], after["scenario_rank"])

    def test_missing_required_signal_field_fails_closed(self) -> None:
        pool = make_pool().drop(columns=["is_fill_score_reliable"])
        with self.assertRaisesRegex(RuntimeError, "is_fill_score_reliable"):
            build_r1_universe_from_pool(pool, make_test_spec())

    def test_complete_102_day_sample_is_canonical_pre_gate_reference(self) -> None:
        path = PROJECT_ROOT / "reports" / "strategy_e_samples" / "e_r1_daily_candidates_full.csv"
        trades = pd.read_csv(path, dtype={"trade_date": str}, low_memory=False)
        returns = pd.to_numeric(trades["net_return"], errors="raise") * 0.825
        equity = (1 + returns).cumprod()
        drawdown = equity / equity.cummax() - 1

        self.assertEqual(len(trades), 102)
        self.assertEqual(trades["trade_date"].nunique(), 102)
        self.assertTrue(trades["strategy_leg"].eq("E").all())
        self.assertTrue(trades["strategy_variant"].eq("E_R1").all())
        self.assertAlmostEqual(float(equity.iloc[-1]), 14.2402504740, places=8)
        self.assertAlmostEqual(float(drawdown.min()), -0.2546037202, places=8)

    def test_entry_gate_complete_sample_has_82_candidate_days(self) -> None:
        path = PROJECT_ROOT / "reports" / "strategy_e_samples" / "e_r1_daily_candidates_full.csv"
        trades = pd.read_csv(path, dtype={"trade_date": str}, low_memory=False)
        spec = load_e_spec(PROJECT_ROOT)
        eligible = apply_e_entry_gate(trades, spec)
        returns = pd.to_numeric(eligible["net_return"], errors="raise") * 0.825
        equity = (1 + returns).cumprod()
        drawdown = equity / equity.cummax() - 1

        self.assertEqual(len(eligible), 82)
        self.assertAlmostEqual(float(equity.iloc[-1]), 9.6457121227, places=8)
        self.assertAlmostEqual(float(drawdown.min()), -0.2766138433, places=8)

    def test_legacy_e2_identity_is_read_as_e_with_explicit_variant(self) -> None:
        self.assertEqual(normalize_strategy_leg("E2"), "E")
        frame = normalize_strategy_frame(
            pd.DataFrame(
                [{"strategy_leg": "E2", "signal_date": "20260727"}]
            )
        )
        self.assertEqual(frame.iloc[0]["strategy_leg"], "E")
        self.assertEqual(frame.iloc[0]["strategy_variant"], "E_JULY")

    def test_active_runtime_logs_never_expose_legacy_e2_name(self) -> None:
        """旧E2只允许用于读取兼容，正式运行日志必须统一显示E。"""

        active_runtime_files = (
            PROJECT_ROOT / "scripts" / "run_strategy_e_signal.py",
            PROJECT_ROOT / "scripts" / "trading_daemon.py",
            PROJECT_ROOT / "src" / "combined_live_engine.py",
        )
        forbidden_log_fragments = (
            "E2不触发",
            "E2只读候选检查",
            "[E2信号]",
            "策略E2",
            "E2开仓",
            "E2平仓",
        )
        for path in active_runtime_files:
            source = path.read_text(encoding="utf-8-sig")
            for fragment in forbidden_log_fragments:
                self.assertNotIn(fragment, source, f"{path.name}仍包含旧日志文案：{fragment}")

    def test_entry_gate_does_not_fallback_to_second_candidate(self) -> None:
        spec = make_test_spec()
        spec["entry_gate"] = {
            "exclude_values": {"first_time_detail_bucket": ["1330_1430"]},
            "apply_after_daily_first_pick": True,
            "fallback_to_second_candidate": False,
        }
        universe = pd.DataFrame(
            [
                {
                    "trade_date": "20260731",
                    "ts_code": "300001.SZ",
                    "segment_retreat_state_bucket": "neutral",
                    "circ_mv": 100_000,
                    "scenario_rank": 1,
                    "first_time_detail_bucket": "1330_1430",
                },
                {
                    "trade_date": "20260731",
                    "ts_code": "300002.SZ",
                    "segment_retreat_state_bucket": "neutral",
                    "circ_mv": 200_000,
                    "scenario_rank": 2,
                    "first_time_detail_bucket": "before_1000",
                },
            ]
        )

        picks = select_e_daily_picks(universe, spec)
        self.assertTrue(picks.empty)

    def test_live_order_uses_scenario_exit_offset(self) -> None:
        engine = object.__new__(CombinedLiveEngine)
        engine.config = {
            "trade_mode": "live",
            "position": {"initial_cash": 500_000},
            "live_trade": {"max_single_order_amount": 0},
        }
        signal = {
            "signal_date": "20260731",
            "ts_code": "300001.SZ",
            "name": "测试股票",
            "limit_close": 10.0,
            "exit_offset": 3,
        }
        order = engine.build_e_buy_order(signal, "20260803")
        self.assertEqual(order["exit_n_days"], 2)


if __name__ == "__main__":
    unittest.main()
