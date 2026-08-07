from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import ModuleType

import pandas as pd

# 纯逻辑测试不读取.env；开发机未安装python-dotenv时注入无副作用最小桩。
if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from scripts.monitor_strategy_d_intraday import calc_shares_below_target_amount
from src.combined_live_engine import CombinedLiveEngine


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_engine(positions: list[dict]) -> CombinedLiveEngine:
    """构造不访问磁盘和券商的组合状态机，用于纯计划层回归测试。"""
    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = PROJECT_ROOT
    engine.config = {
        "trade_mode": "live",
        "position": {"initial_cash": 500_000},
        "live_trade": {"max_single_order_amount": 0},
        "strategy_l": {
            "enabled": True,
            "live_order_enabled": True,
            "position_pct": 0.825,
        },
        "strategy_model3": {
            "enabled": True,
            "live_order_enabled": True,
            "selected_rule_name": "test",
        },
    }
    engine.load_positions = lambda: positions
    engine.load_latest_abc_orders = lambda: (
        Path("test_orders.csv"),
        pd.DataFrame([{
            "strategy_leg": "A",
            "side": "BUY",
            "ts_code": "002800.SZ",
            "name": "测试新候选",
            "planned_order_date": "20260803",
            "reference_price": 10.0,
            "round_lot_shares": 10_000,
            "estimated_shares": 10_000,
        }]),
    )
    engine.load_yesterday_e2_signal = lambda _today: None
    engine.load_today_e2_signal = lambda _today: None
    engine.load_today_l_signal = lambda _today: None
    engine.load_yesterday_l_signal = lambda _today: {
        "signal_date": "20260731",
        "planned_buy_date": "20260803",
        "ts_code": "300001.SZ",
        "name": "测试L候选",
        "limit_close": 10.0,
        "market_segment": "chi_next",
        "segment_retreat_state_bucket": "neutral",
        "market_chain_count_bucket": "15_30",
        "theme_limit_count": 3,
        "first_time_detail_bucket": "before_1430",
    }
    engine.active_strategy_mode = lambda: 3
    engine.active_strategy_name = lambda: "MODEL3"
    engine.is_b_strategy_removed = lambda: True
    return engine


class LegPriorityOrderTests(unittest.TestCase):
    """腿序 D>L>A>M>E2>C 的实盘落地验证（2026-08-07）。

    对应回测口径见 certify_current_executable_portfolio.pick_by_priority，
    两侧必须一致：481信号日 151笔 / 27870.31x / 回撤-23.50%。
    """

    def test_l_无条件优先_不再需要过替换窄门(self) -> None:
        """L 只要过基础规则就抢走 A 的计划，不再要求"创业板∧非尾盘首板"。

        这里刻意给一个**过不了旧窄门**的 L 信号（沪主板 + after_1430），
        旧口径会发 BLOCK_MODEL3_L_REPLACE_GUARD 沿用 A 计划——2026-08-05
        利通电子正是这样被挡掉、当日让位给 C 华之杰的。
        """

        engine = make_engine([])
        engine.load_yesterday_l_signal = lambda _today: {
            "signal_date": "20260731",
            "planned_buy_date": "20260803",
            "ts_code": "603629.SH",
            "name": "沪主板尾盘首板L",
            "limit_close": 10.0,
            "market_segment": "sh_main",          # 旧窄门要求 chi_next
            "segment_retreat_state_bucket": "neutral",
            "market_chain_count_bucket": "15_30",
            "theme_limit_count": 3,
            "first_time_detail_bucket": "after_1430",  # 旧窄门排除尾盘首板
        }

        _state, decisions, orders = engine.build_model3_plan("20260803")
        actions = set(decisions["action"])

        self.assertIn("ALLOW_MODEL3_L_REPLACE", actions)
        self.assertNotIn("BLOCK_MODEL3_L_REPLACE_GUARD", actions)
        buys = orders[orders["side"].astype(str).str.upper().eq("BUY")]
        self.assertEqual(len(buys), 1)
        self.assertEqual(str(buys.iloc[0]["ts_code"]), "603629.SH")

    def test_m_排在e2之前(self) -> None:
        """A/C 无计划时 M 先于 E2 开仓，且当天不再放行 E2。"""

        engine = make_engine([])
        engine.load_latest_abc_orders = lambda: (Path("empty.csv"), pd.DataFrame())
        engine.load_yesterday_e2_signal = lambda _today: {
            "signal_date": "20260731",
            "ts_code": "300002.SZ",
            "name": "测试E2候选",
            "limit_close": 10.0,
            "exit_offset": 2,
        }
        engine.build_m_buy_order_if_any = lambda _today, _codes: {
            "ts_code": "300003.SZ",
            "name": "测试M候选",
            "side": "BUY",
            "round_lot_shares": 5_000,
            "planned_action": "PLAN_BUY_T1_OPEN",
        }

        _state, decisions, orders = engine.build_mode1_plan("20260803")
        actions = set(decisions["action"])

        self.assertIn("ALLOW_M_BUY", actions)
        self.assertIn("BLOCK_E2", actions)
        self.assertNotIn("ALLOW_E2_BUY", actions)
        buys = orders[orders["side"].astype(str).str.upper().eq("BUY")]
        self.assertEqual(len(buys), 1)
        self.assertEqual(str(buys.iloc[0]["ts_code"]), "300003.SZ")

    def test_m无信号时e2正常接棒(self) -> None:
        """M 提前不影响 E2：M 当天无信号时 E2 照常开仓。"""

        engine = make_engine([])
        engine.load_latest_abc_orders = lambda: (Path("empty.csv"), pd.DataFrame())
        engine.load_yesterday_e2_signal = lambda _today: {
            "signal_date": "20260731",
            "ts_code": "300002.SZ",
            "name": "测试E2候选",
            "limit_close": 10.0,
            "exit_offset": 2,
        }
        engine.build_m_buy_order_if_any = lambda _today, _codes: None

        _state, decisions, orders = engine.build_mode1_plan("20260803")
        actions = set(decisions["action"])

        self.assertIn("ALLOW_E2_BUY", actions)
        self.assertNotIn("ALLOW_M_BUY", actions)
        buys = orders[orders["side"].astype(str).str.upper().eq("BUY")]
        self.assertEqual(len(buys), 1)
        self.assertEqual(str(buys.iloc[0]["ts_code"]), "300002.SZ")


class OpeningPositionPolicyTests(unittest.TestCase):
    def test_production_position_configuration_is_82_5_with_85_hard_cap(self) -> None:
        runtime = json.loads((PROJECT_ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        strategy = json.loads((PROJECT_ROOT / "config" / "strategy_config.json").read_text(encoding="utf-8"))

        self.assertEqual(runtime["live_trade"]["max_total_position_pct"], 0.825)
        self.assertEqual(runtime["live_trade"]["max_position_pct"], 0.85)
        self.assertEqual(runtime["live_trade"]["entry_min_acceptable_position_pct"], 0.8)
        self.assertTrue(runtime["live_trade"]["entry_actual_amount_rebalance_enabled"])
        self.assertFalse(runtime["live_trade"]["transition_use_full_available_cash"])
        self.assertEqual(runtime["strategy_d"]["position_pct"], 0.825)
        self.assertEqual(runtime["strategy_l"]["position_pct"], 0.825)
        self.assertEqual(runtime["live_trade"]["capacity_wall_alert_threshold"], 17_000_000)
        self.assertEqual(strategy["position"]["target_position_pct"], 0.825)
        self.assertEqual(strategy["position"]["max_total_position_pct"], 0.825)

    def test_due_non_d_position_blocks_all_new_buy_plans(self) -> None:
        position = {
            "strategy_leg": "A",
            "ts_code": "600000.SH",
            "name": "今日到期旧仓",
            "status": "open",
            "shares": 10_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260803",
        }
        engine = make_engine([position])

        _state, decisions, orders = engine.build_mode1_plan("20260803")

        self.assertFalse(
            not orders.empty and orders["side"].astype(str).str.upper().eq("BUY").any()
        )
        self.assertIn("BLOCK_ABC_BUY", set(decisions["action"]))
        self.assertIn("BLOCK_D_INTRADAY_MONITOR", set(decisions["action"]))

    def test_due_e2_position_keeps_sell_but_drops_new_buy(self) -> None:
        position = {
            "strategy_leg": "E2",
            "ts_code": "000001.SZ",
            "name": "今日到期E2",
            "status": "sell_pending",
            "shares": 5_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260803",
        }
        engine = make_engine([position])

        _state, decisions, orders = engine.build_mode1_plan("20260803")

        self.assertIn("PLAN_SELL_E2", set(decisions["action"]))
        self.assertTrue(orders["side"].astype(str).str.upper().eq("SELL").any())
        self.assertFalse(orders["side"].astype(str).str.upper().eq("BUY").any())

    def test_model3_l_cannot_supplement_while_due_non_l_position_exists(self) -> None:
        position = {
            "strategy_leg": "A",
            "ts_code": "600000.SH",
            "name": "今日到期旧仓",
            "status": "open",
            "shares": 10_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260803",
        }
        engine = make_engine([position])

        _state, decisions, orders = engine.build_model3_plan("20260803")

        self.assertIn("BLOCK_MODEL3_L_BY_HOLDING_POSITION", set(decisions["action"]))
        self.assertFalse(
            not orders.empty and orders["side"].astype(str).str.upper().eq("BUY").any()
        )

    def test_due_l_position_only_generates_sell_in_standalone_mode(self) -> None:
        position = {
            "strategy_leg": "L",
            "ts_code": "300001.SZ",
            "name": "今日到期L",
            "status": "open",
            "shares": 2_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260803",
        }
        engine = make_engine([position])
        engine.active_strategy_mode = lambda: 2
        engine.active_strategy_name = lambda: "L"

        _state, decisions, orders = engine.build_l_mode_plan("20260803")

        self.assertIn("PLAN_SELL_L", set(decisions["action"]))
        self.assertIn("BLOCK_L_BUY_BY_L_POSITION", set(decisions["action"]))
        self.assertTrue(orders["side"].astype(str).str.upper().eq("SELL").all())

    def test_d_relay_disabled_未到期D不卖不接力也不放行新开仓(self) -> None:
        """接力全关（2026-08-07）：D未到期时既不提前卖，也不生成任何接力计划。

        旧口径会发 PLAN_SELL_D_FIRST（daemon 靠它填 force_d_sell_codes 触发
        09:23 接力卖）+ PLAN_D_RELAY_PAIRED_BUY 影子计划。现口径两者都不再出现，
        当天即使有 A/C 候选也一律阻断，等 D 自己 T+2 收盘平仓。
        """

        position = {
            "strategy_leg": "D",
            "ts_code": "600001.SH",
            "name": "D旧仓",
            "status": "open",
            "shares": 3_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260805",   # 未到期
        }
        engine = make_engine([position])

        _state, decisions, orders = engine.build_mode1_plan("20260803")
        actions = set(decisions["action"])

        # 接力链路的两个入口都必须消失
        self.assertNotIn("PLAN_SELL_D_FIRST", actions)
        self.assertNotIn("PLAN_D_RELAY_PAIRED_BUY", actions)
        if not orders.empty and "planned_action" in orders.columns:
            self.assertTrue(
                orders["planned_action"].astype(str).ne("PLAN_D_RELAY_PAIRED_BUY").all()
            )

        # 未到期不发卖出计划，且所有新开仓路径阻断
        self.assertNotIn("PLAN_SELL_D_T2_CLOSE", actions)
        self.assertIn("BLOCK_ABC_BUY", actions)
        self.assertIn("BLOCK_E2_BUY", actions)
        self.assertIn("BLOCK_D_INTRADAY_MONITOR", actions)
        self.assertNotIn("ALLOW_ABC_BUY_PREVIEW", actions)
        self.assertNotIn("ALLOW_E2_BUY", actions)

    def test_d_relay_disabled_到期D只走T2收盘平仓(self) -> None:
        """到期D发 PLAN_SELL_D_T2_CLOSE，等 14:53 收盘平仓，不在09:23竞价卖。"""

        position = {
            "strategy_leg": "D",
            "ts_code": "600001.SH",
            "name": "D旧仓",
            "status": "open",
            "shares": 3_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260803",   # 今日到期
        }
        engine = make_engine([position])

        _state, decisions, orders = engine.build_mode1_plan("20260803")
        actions = set(decisions["action"])

        self.assertIn("PLAN_SELL_D_T2_CLOSE", actions)
        self.assertNotIn("PLAN_SELL_D_FIRST", actions)
        self.assertNotIn("PLAN_D_RELAY_PAIRED_BUY", actions)
        # 卖出计划单也必须是T+2收盘口径
        sell = orders[orders["side"].astype(str).str.upper().eq("SELL")]
        self.assertTrue(
            sell["planned_action"].astype(str).eq("PLAN_SELL_D_T2_CLOSE").all()
        )
        # 平仓当天不开新仓
        self.assertIn("BLOCK_ABC_BUY", actions)

    def test_model3_d持仓期间L拿不到资金_且无影子计划(self) -> None:
        position = {
            "strategy_leg": "D",
            "ts_code": "600001.SH",
            "name": "D旧仓",
            "status": "open",
            "shares": 3_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260805",
        }
        engine = make_engine([position])

        _state, decisions, orders = engine.build_model3_plan("20260803")

        self.assertIn("BLOCK_MODEL3_L_BY_HOLDING_POSITION", set(decisions["action"]))
        self.assertNotIn("ALLOW_MODEL3_L_REPLACE", set(decisions["action"]))
        # 接力全关后不再有影子计划；D持仓期间 L 一样拿不到资金。
        if not orders.empty and "planned_action" in orders.columns:
            self.assertTrue(
                orders["planned_action"].astype(str).ne("PLAN_D_RELAY_PAIRED_BUY").all()
            )

    def test_d_does_not_relay_for_l_only_candidate(self) -> None:
        position = {
            "strategy_leg": "D",
            "ts_code": "600001.SH",
            "name": "D旧仓",
            "status": "open",
            "shares": 3_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260805",
        }
        engine = make_engine([position])
        engine.load_latest_abc_orders = lambda: (Path("empty.csv"), pd.DataFrame())

        _state, decisions, orders = engine.build_model3_plan("20260803")

        self.assertNotIn("PLAN_SELL_D_FIRST", set(decisions["action"]))
        self.assertIn("BLOCK_MODEL3_L_BY_HOLDING_POSITION", set(decisions["action"]))
        self.assertFalse(
            not orders.empty and orders["side"].astype(str).str.upper().eq("BUY").any()
        )

    def test_d_target_amount_rounds_down_without_exceeding_82_5_pct(self) -> None:
        shares = calc_shares_below_target_amount(825_000.0, 10.0)
        self.assertEqual(shares, 82_400)
        self.assertLess(shares * 10.0, 825_000.0)


if __name__ == "__main__":
    unittest.main()
