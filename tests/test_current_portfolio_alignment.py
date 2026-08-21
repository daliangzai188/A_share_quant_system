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
    """锁定四腿身份回放D>A>E>C；严格发布认证仍单独fail-closed。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = load_sources()

    def test_current_portfolio_is_reproducible(self) -> None:
        detail = replay(self.sources, entry_gate_enabled=True)
        metrics = summarize(detail, "current_d_a_e_c")

        self.assertEqual(metrics["executed_trade_count"], EXPECTED_CURRENT_TRADE_COUNT)
        self.assertAlmostEqual(
            metrics["equity_multiple"], EXPECTED_CURRENT_MULTIPLE, places=8
        )
        self.assertEqual(
            set(detail.loc[detail["status"].eq("EXECUTED"), "strategy_leg"]),
            {"D", "A", "E", "C"},
        )

    def test_e_gate_complete_sample_risk_is_explicit(self) -> None:
        without_gate = summarize(
            replay(self.sources, entry_gate_enabled=False),
            "without_e_gate",
        )
        current = summarize(
            replay(self.sources, entry_gate_enabled=True),
            "current_d_a_e_c",
        )

        self.assertEqual(without_gate["executed_trade_count"], 136)
        self.assertGreater(current["equity_multiple"], without_gate["equity_multiple"])

        validation = e_entry_gate_validation(self.sources)
        full = validation[validation["split"].eq("全部")].iloc[0]
        self.assertEqual(int(full["base_count"]), 102)
        self.assertEqual(int(full["kept_count"]), 82)
        # E单腿完整样本中，被门禁删除组反而为正；必须持续暴露该过拟合风险。
        self.assertGreater(float(full["removed_avg_return"]), 0)
        self.assertLess(float(full["optimized_vs_base"]), 0)

    def test_current_leg_breakdown_stays_locked(self) -> None:
        detail = replay(self.sources, entry_gate_enabled=True)
        metrics = summarize(
            detail,
            "current_d_a_e_c",
        )
        self.assertEqual(metrics["d_trade_count"], 18)
        self.assertEqual(metrics["a_trade_count"], 45)
        self.assertEqual(metrics["e_trade_count"], 47)
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
