from __future__ import annotations

import unittest
from unittest.mock import patch

import scripts.run_strategy_m_signal as m_signal
from scripts.run_strategy_l_signal import parse_hhmmss_to_minutes
from tests.test_opening_position_policy import make_engine


class StrategyLLiveDisabledTests(unittest.TestCase):
    def test_shadow_l_uses_real_hhmmss_minutes(self) -> None:
        self.assertAlmostEqual(parse_hhmmss_to_minutes(93839), 578 + 39 / 60)
        self.assertAlmostEqual(parse_hhmmss_to_minutes(143001), 870 + 1 / 60)

    def test_disabled_l_keeps_mode1_buy_and_never_replaces_it(self) -> None:
        engine = make_engine([])
        engine.config["strategy_model3"]["l_participation_enabled"] = False

        _state, decisions, orders = engine.build_model3_plan("20260803")

        actions = set(decisions["action"].astype(str))
        self.assertIn("BLOCK_MODEL3_L_INVALIDATED", actions)
        self.assertNotIn("ALLOW_MODEL3_L_REPLACE", actions)
        self.assertNotIn("ALLOW_MODEL3_L_SUPPLEMENT", actions)
        buys = orders[orders["side"].astype(str).str.upper().eq("BUY")]
        self.assertEqual(len(buys), 1)
        self.assertEqual(str(buys.iloc[0]["ts_code"]), "002800.SZ")

    def test_disabled_l_does_not_block_existing_l_exit(self) -> None:
        position = {
            "strategy_leg": "L",
            "ts_code": "300001.SZ",
            "name": "到期L持仓",
            "status": "open",
            "shares": 2_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260803",
        }
        engine = make_engine([position])
        engine.config["strategy_model3"]["l_participation_enabled"] = False

        _state, decisions, orders = engine.build_model3_plan("20260803")

        self.assertIn("PLAN_SELL_L", set(decisions["action"].astype(str)))
        self.assertTrue(orders["side"].astype(str).str.upper().eq("SELL").all())

    def test_disabled_shadow_l_signal_does_not_block_m(self) -> None:
        with patch.object(m_signal, "has_ac_planned_order", return_value=False), patch.object(
            m_signal,
            "load_config",
            return_value={"strategy_model3": {"l_participation_enabled": False}},
        ), patch.object(
            m_signal,
            "signal_by_signal_date",
            return_value={"ts_code": "300750.SZ"},
        ):
            busy, reason = m_signal.higher_priority_leg_has_signal("20260803")

        self.assertFalse(busy)
        self.assertIn("L实盘参与已关闭", reason)


if __name__ == "__main__":
    unittest.main()
