from __future__ import annotations

import unittest

from scripts.certify_current_executable_portfolio import (
    EXPECTED_BASE_MULTIPLE,
    EXPECTED_BASE_TRADE_COUNT,
    EXPECTED_E2_ONLY_MULTIPLE,
    EXPECTED_E2_ONLY_TRADE_COUNT,
    EXPECTED_OPTIMIZED_MULTIPLE,
    EXPECTED_OPTIMIZED_TRADE_COUNT,
    e2_entry_gate_validation,
    l_chain_expansion_validation,
    load_sources,
    replay,
    summarize,
)


class CurrentPortfolioAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = load_sources()

    def test_locked_current_baseline_is_reproducible(self) -> None:
        detail = replay(
            self.sources,
            entry_gate_enabled=False,
            l_chain_3_8_enabled=False,
        )
        metrics = summarize(detail, "baseline")
        self.assertEqual(metrics["executed_trade_count"], EXPECTED_BASE_TRADE_COUNT)
        self.assertAlmostEqual(metrics["equity_multiple"], EXPECTED_BASE_MULTIPLE, places=8)

    def test_e2_gate_improves_leg_and_total_portfolio(self) -> None:
        base = summarize(
            replay(
                self.sources,
                entry_gate_enabled=False,
                l_chain_3_8_enabled=False,
            ),
            "baseline",
        )
        optimized = summarize(
            replay(
                self.sources,
                entry_gate_enabled=True,
                l_chain_3_8_enabled=False,
            ),
            "e2_only",
        )

        self.assertEqual(
            optimized["executed_trade_count"], EXPECTED_E2_ONLY_TRADE_COUNT
        )
        self.assertAlmostEqual(
            optimized["equity_multiple"], EXPECTED_E2_ONLY_MULTIPLE, places=8
        )
        self.assertGreater(optimized["equity_multiple"], base["equity_multiple"])
        # 浮点容差：同一段回撤的两条曲线末位可能差 ~1e-16。
        self.assertGreaterEqual(
            optimized["max_drawdown"], base["max_drawdown"] - 1e-12
        )

        validation = e2_entry_gate_validation(self.sources)
        self.assertTrue((validation["optimized_vs_base"] > 0).all())
        self.assertTrue((validation["removed_avg_return"] < 0).all())

    def test_l_chain_3_8_expansion_improves_l_leg_and_total(self) -> None:
        before = replay(
            self.sources,
            entry_gate_enabled=True,
            l_chain_3_8_enabled=False,
        )
        after = replay(
            self.sources,
            entry_gate_enabled=True,
            l_chain_3_8_enabled=True,
        )
        metrics = summarize(after, "optimized")

        self.assertEqual(
            metrics["executed_trade_count"], EXPECTED_OPTIMIZED_TRADE_COUNT
        )
        self.assertAlmostEqual(
            metrics["equity_multiple"], EXPECTED_OPTIMIZED_MULTIPLE, places=8
        )
        validation = l_chain_expansion_validation(before, after)
        required = validation[
            validation["split"].isin({"全部", "前半段", "后半段"})
        ]
        self.assertTrue((required["total_change"] > 0).all())
        self.assertTrue((required["l_change"] > 0).all())

    def test_optimized_leg_breakdown_stays_locked(self) -> None:
        """各腿笔数锁定，防止输入漂移后静默改变分布。

        2026-08-07 更新：A/C 改用逐日独立候选（见 load_ac_daily），不再被
        baseline.abc_return 这张作废持仓表锁在90天，A/C 笔数相应上升。
        旧锁定值：A/C被裁口径 a=27 c=9 d=17 d_to_a=2 d_to_c=6 e2=30 l=41；
        仅修A/C口径 a=33 c=13 d=14 d_to_a=1 d_to_c=8 e2=30 l=40。
        再修衔接日D后 a=34 c=16 d=10 d_to_a=0 d_to_c=4 e2=30 l=41；
        D接力全关后 a=34 c=17 d=14 d_to_a=0 d_to_c=0 e2=30 l=40；
        2026-08-07 腿序重排 D>L>A>M>E2>C 后为下方数值（L 大幅上升、E2 下降）。
        """

        optimized = summarize(
            replay(
                self.sources,
                entry_gate_enabled=True,
                l_chain_3_8_enabled=True,
            ),
            "optimized",
        )
        self.assertEqual(optimized["a_trade_count"], 33)
        self.assertEqual(optimized["c_trade_count"], 17)
        self.assertEqual(optimized["d_trade_count"], 14)
        self.assertEqual(optimized["d_to_a_trade_count"], 0)
        self.assertEqual(optimized["d_to_c_trade_count"], 0)
        self.assertEqual(optimized["e2_trade_count"], 19)
        self.assertEqual(optimized["l_trade_count"], 52)


if __name__ == "__main__":
    unittest.main()
