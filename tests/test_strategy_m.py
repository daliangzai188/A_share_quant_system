from __future__ import annotations

import json
from pathlib import Path
import unittest

import pandas as pd

from src.strategy_m import (
    DEFAULT_SPEC,
    apply_base_filters,
    build_m_candidate,
    drawdown_guard_passed,
    load_m_spec,
    resolve_exit_offset,
    select_m_candidate,
    sentiment_gate_passed,
)

PROJECT_ROOT = Path(__file__).absolute().parents[1]


def make_pool(**overrides) -> pd.DataFrame:
    base = {
        "trade_date": ["20260804", "20260804", "20260804"],
        "ts_code": ["300001.SZ", "300002.SZ", "300003.SZ"],
        "name": ["甲公司", "乙公司", "丙公司"],
        "circ_mv": [500000.0, 120000.0, 300000.0],
        "sz_main_market_sentiment_level": ["weak", "weak", "weak"],
        "limit_data_quality": ["full", "full", "full"],
        "strategy_compatible": ["true", "true", "true"],
        "allow_buy_reliable": ["true", "true", "true"],
        "is_fill_score_reliable": ["true", "true", "true"],
        "is_st": ["false", "false", "false"],
    }
    base.update(overrides)
    return pd.DataFrame(base)


class StrategyMRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = dict(DEFAULT_SPEC)

    def test_picks_smallest_circ_mv(self) -> None:
        picked = select_m_candidate(make_pool(), self.spec)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked.iloc[0]["ts_code"], "300002.SZ")

    def test_sentiment_gate_requires_weak(self) -> None:
        ok, _ = sentiment_gate_passed(make_pool(), self.spec)
        self.assertTrue(ok)
        strong = make_pool(sz_main_market_sentiment_level=["strong"] * 3)
        ok, reason = sentiment_gate_passed(strong, self.spec)
        self.assertFalse(ok)
        self.assertIn("strong", reason)

    def test_sentiment_missing_field_fails_closed(self) -> None:
        pool = make_pool().drop(columns=["sz_main_market_sentiment_level"])
        ok, reason = sentiment_gate_passed(pool, self.spec)
        self.assertFalse(ok)
        self.assertIn("按安全口径拒绝", reason)

    def test_base_filters_exclude_unreliable_and_st(self) -> None:
        pool = make_pool(
            is_st=["false", "true", "false"],
            allow_buy_reliable=["true", "true", "false"],
        )
        kept = apply_base_filters(pool)
        self.assertEqual(kept["ts_code"].tolist(), ["300001.SZ"])

    def test_base_filters_exclude_name_with_st_or_delisting(self) -> None:
        pool = make_pool(name=["ST甲", "乙公司", "丙退"])
        kept = apply_base_filters(pool)
        self.assertEqual(kept["ts_code"].tolist(), ["300002.SZ"])

    def test_build_candidate_end_to_end(self) -> None:
        picked, reason = build_m_candidate(make_pool(), self.spec)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked.iloc[0]["ts_code"], "300002.SZ")
        self.assertIn("weak", reason)

    def test_build_candidate_blocked_when_not_weak(self) -> None:
        pool = make_pool(sz_main_market_sentiment_level=["neutral"] * 3)
        picked, reason = build_m_candidate(pool, self.spec)
        self.assertTrue(picked.empty)
        self.assertIn("neutral", reason)

    def test_drawdown_guard(self) -> None:
        ok, _ = drawdown_guard_passed(100.0, 100.0, self.spec)
        self.assertTrue(ok)
        ok, _ = drawdown_guard_passed(95.0, 100.0, self.spec)
        self.assertTrue(ok)
        ok, reason = drawdown_guard_passed(89.0, 100.0, self.spec)
        self.assertFalse(ok)
        self.assertIn("暂停", reason)

    def test_drawdown_guard_fails_closed_on_missing_equity(self) -> None:
        ok, reason = drawdown_guard_passed(0.0, 0.0, self.spec)
        self.assertFalse(ok)
        self.assertIn("安全口径", reason)

    def test_exit_offset_is_t2(self) -> None:
        self.assertEqual(resolve_exit_offset(self.spec), 2)


class StrategyMConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(
            (PROJECT_ROOT / "config" / "config.json").read_text(encoding="utf-8")
        )

    def test_config_section_exists(self) -> None:
        self.assertIn("strategy_m", self.config)
        m = self.config["strategy_m"]
        self.assertIsInstance(m["enabled"], bool)
        self.assertIsInstance(m["live_order_enabled"], bool)

    def test_live_order_requires_enabled(self) -> None:
        """不允许出现 enabled=false 但 live_order_enabled=true 的矛盾配置。"""
        m = self.config["strategy_m"]
        if m["live_order_enabled"]:
            self.assertTrue(
                m["enabled"],
                "live_order_enabled=true 时 enabled 必须也为 true，否则信号不会生成却允许下单",
            )

    def test_live_config_is_complete_when_enabled(self) -> None:
        """M 一旦放开实盘，风控参数必须齐全且在合理区间。"""
        m = self.config["strategy_m"]
        if not m["enabled"]:
            self.skipTest("M 未启用")
        self.assertEqual(m["sentiment_required"], "weak")
        self.assertEqual(m["rank_column"], "circ_mv")
        self.assertEqual(m["exit_hold_offset"], 2)
        self.assertGreater(m["drawdown_guard_pct"], 0, "启用实盘时回撤保护不得关闭")
        self.assertLessEqual(m["drawdown_guard_pct"], 0.2)
        self.assertAlmostEqual(m["position_pct"], 0.825)
        self.assertTrue(m.get("require_all_legs_idle"), "M 必须保持兜底腿定位")

    def test_spec_loads_from_full_config(self) -> None:
        spec = load_m_spec(self.config)
        self.assertEqual(spec["sentiment_required"], "weak")
        self.assertEqual(spec["rank_column"], "circ_mv")
        self.assertEqual(spec["exit_hold_offset"], 2)
        self.assertAlmostEqual(spec["drawdown_guard_pct"], 0.10)

    def test_m_included_in_exit_pov_legs(self) -> None:
        """M 必须走与 A/C/E2/L 相同的容量型卖出 POV，否则大仓位会砸盘。"""
        live = self.config["live_trade"]
        self.assertIn("M", live["exit_pov_strategy_legs"])
        self.assertIn("M", live["exit_pov_large_force_strategy_legs"])

    def test_m_in_t2_close_legs(self) -> None:
        """M 必须在 T+2 收盘卖白名单里，否则会在盘中被提前卖出。"""
        from scripts.trading_daemon import T2_CLOSE_STRATEGY_LEGS

        self.assertIn("M", T2_CLOSE_STRATEGY_LEGS)


if __name__ == "__main__":
    unittest.main()
