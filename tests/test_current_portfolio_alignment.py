from __future__ import annotations

import unittest

from scripts.certify_current_executable_portfolio import (
    EXPECTED_D_DAILY_CANDIDATE_COUNT,
    EXPECTED_CURRENT_MULTIPLE,
    EXPECTED_CURRENT_TRADE_COUNT,
    e_entry_gate_validation,
    load_sources,
    replay,
    summarize,
)


class CurrentPortfolioAlignmentTests(unittest.TestCase):
    """只锁定当前正式组合 D>A>M>E>C，防止退役策略重新混入。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = load_sources()

    def test_current_portfolio_is_reproducible(self) -> None:
        detail = replay(
            self.sources,
            entry_gate_enabled=True,
            m_enabled=True,
        )
        metrics = summarize(detail, "current_d_a_m_e_c")

        self.assertEqual(metrics["executed_trade_count"], EXPECTED_CURRENT_TRADE_COUNT)
        self.assertAlmostEqual(
            metrics["equity_multiple"], EXPECTED_CURRENT_MULTIPLE, places=8
        )
        self.assertEqual(
            set(detail.loc[detail["status"].eq("EXECUTED"), "strategy_leg"]),
            {"D", "A", "M", "E", "C"},
        )

    def test_m_effect_is_measured_on_the_complete_portfolio(self) -> None:
        without_m = summarize(
            replay(self.sources, entry_gate_enabled=True, m_enabled=False),
            "with_e_gate_without_m",
        )
        current = summarize(
            replay(self.sources, entry_gate_enabled=True, m_enabled=True),
            "current_d_a_m_e_c",
        )

        self.assertEqual(without_m["executed_trade_count"], 128)
        self.assertAlmostEqual(without_m["equity_multiple"], 1682.9222043469645, places=8)
        self.assertEqual(without_m["m_trade_count"], 0)
        self.assertEqual(current["m_trade_count"], 28)
        self.assertGreater(current["equity_multiple"], without_m["equity_multiple"])

    def test_e_gate_complete_sample_risk_is_explicit(self) -> None:
        without_gate = summarize(
            replay(self.sources, entry_gate_enabled=False, m_enabled=True),
            "without_e_gate_with_m",
        )
        current = summarize(
            replay(self.sources, entry_gate_enabled=True, m_enabled=True),
            "current_d_a_m_e_c",
        )

        self.assertEqual(without_gate["executed_trade_count"], 152)
        self.assertGreater(current["equity_multiple"], without_gate["equity_multiple"])

        validation = e_entry_gate_validation(self.sources)
        full = validation[validation["split"].eq("全部")].iloc[0]
        self.assertEqual(int(full["base_count"]), 102)
        self.assertEqual(int(full["kept_count"]), 82)
        # E单腿完整样本中，被门禁删除组反而为正；必须持续暴露该过拟合风险。
        self.assertGreater(float(full["removed_avg_return"]), 0)
        self.assertLess(float(full["optimized_vs_base"]), 0)

    def test_current_leg_breakdown_stays_locked(self) -> None:
        metrics = summarize(
            replay(self.sources, entry_gate_enabled=True, m_enabled=True),
            "current_d_a_m_e_c",
        )
        self.assertEqual(metrics["d_trade_count"], 17)
        self.assertEqual(metrics["a_trade_count"], 44)
        self.assertEqual(metrics["m_trade_count"], 28)
        self.assertEqual(metrics["e_trade_count"], 38)
        self.assertEqual(metrics["c_trade_count"], 18)
        self.assertEqual(metrics["d_to_a_trade_count"], 0)
        self.assertEqual(metrics["d_to_c_trade_count"], 0)
        self.assertEqual(metrics["d_to_e_trade_count"], 0)

    def test_d_source_is_the_complete_daily_candidate_ledger(self) -> None:
        self.assertEqual(len(self.sources.strategy_d), EXPECTED_D_DAILY_CANDIDATE_COUNT)
        self.assertEqual(self.sources.strategy_d.index.nunique(), 45)
        self.assertIn("20241129", self.sources.strategy_d.index)
        self.assertIn("20241212", self.sources.strategy_d.index)
        self.assertIn("20250414", self.sources.strategy_d.index)


if __name__ == "__main__":
    unittest.main()
