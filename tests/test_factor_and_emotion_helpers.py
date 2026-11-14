"""研究池特征用到的计算（src/factors.py、src/market_emotion.py）：首封时间、涨跌幅档、回撤、连亏、市场情绪状态。

研究池每天由QMT外检查重建，影子账、停手判定都读它。2026-11-14 优化第1步“安全网”补的测试，把每个分档边界钉住。
"""
from __future__ import annotations

import math
import unittest

import pandas as pd

from src.factors import FactorAnalyzer, NextDayPremiumAnalyzer
from src.market_emotion import MarketEmotionBuilder


class DrawdownAndLossStreakTests(unittest.TestCase):
    def test_max_drawdown(self):
        self.assertEqual(NextDayPremiumAnalyzer.calculate_max_drawdown(pd.Series(dtype=float)), 0.0)
        self.assertEqual(NextDayPremiumAnalyzer.calculate_max_drawdown(pd.Series([1.0, 1.1, 1.2])), 0.0)
        self.assertAlmostEqual(NextDayPremiumAnalyzer.calculate_max_drawdown(pd.Series([1.0, 1.2, 0.9, 1.5, 1.2])), 0.25)

    def test_consecutive_losses_count_zero_as_loss(self):
        self.assertEqual(NextDayPremiumAnalyzer.max_consecutive_losses(pd.Series([0.1, -0.1, 0.0, -0.2, 0.3, -0.1])), 3)
        self.assertEqual(NextDayPremiumAnalyzer.max_consecutive_losses(pd.Series(dtype=float)), 0)


class FactorBucketTests(unittest.TestCase):
    def test_parse_time_to_minutes(self):
        cases = {"09:35": 575, "14:30:00": 870, "093500": 575, 93500: 575, "  145700 ": 897, "0930": 9}
        for value, minutes in cases.items():  # 纯数字一律按HHMMSS理解，所以"0930"是00:09:30
            self.assertEqual(FactorAnalyzer.parse_time_to_minutes(value), minutes, value)
        for value in (None, float("nan"), "", "925", "ab"):
            self.assertTrue(math.isnan(FactorAnalyzer.parse_time_to_minutes(value)), value)

    def test_limit_pct_bucket_edges(self):
        trades = pd.DataFrame({"limit_pct": [0.05, 0.051, 0.0511, 0.1, 0.101, 0.2, 0.201, 0.3, 0.301, 0.31, None, "x"]})
        self.assertEqual(FactorAnalyzer.build_limit_pct_bucket(trades).tolist(),
                         ["5cm", "5cm", "10cm", "10cm", "10cm", "20cm", "20cm", "30cm", "30cm", "other", "unknown", "unknown"])
        self.assertEqual(FactorAnalyzer.build_limit_pct_bucket(pd.DataFrame({"x": [1]})).tolist(), ["unknown"])

    def test_cut_or_unknown(self):
        data = pd.DataFrame({"v": [0.5, 1.5, 2.5, None, "bad"]})
        self.assertEqual(FactorAnalyzer.cut_or_unknown(data, "v", [0, 1, 2, 3], ["low", "mid", "high"]).tolist(),
                         ["low", "mid", "high", "unknown", "unknown"])
        self.assertEqual(FactorAnalyzer.cut_or_unknown(data, "w", [0, 1], ["a"]).tolist(), ["unknown"] * 5)

    def test_summarize_factor_group(self):
        row = FactorAnalyzer.summarize_factor_group("f", "v", pd.DataFrame({"net_return": [0.1, -0.05, None, 0.2]}))
        self.assertEqual((row["sample_count"], row["max_consecutive_losses"], row["is_candidate"]), (3, 1, False))
        self.assertAlmostEqual(row["win_rate"], 2 / 3)
        self.assertAlmostEqual(row["total_compound_return"], 1.1 * 0.95 * 1.2 - 1)
        self.assertAlmostEqual(row["profit_loss_ratio"], 0.15 / 0.05)
        self.assertAlmostEqual(row["max_drawdown"], 0.05)


class EmotionStateTests(unittest.TestCase):
    def state(self, **kwargs):
        args = dict(limit_count=50, limit_ratio=0.01, down_ratio=0.0, max_height=3, chain_count=2, prev1=50, prev2=50)
        args.update(kwargs)
        return MarketEmotionBuilder.classify_state(**args)

    def test_each_state_and_priority(self):
        self.assertEqual(self.state(prev1=float("nan")), "unknown")
        self.assertEqual(self.state(down_ratio=0.015), "ice_point")
        self.assertEqual(self.state(limit_ratio=0.0049), "ice_point")
        self.assertEqual(self.state(limit_count=3), "ice_point")
        self.assertEqual(self.state(limit_count=40, prev1=45, prev2=50), "retreat")
        self.assertEqual(self.state(limit_count=60, prev1=55, prev2=50, chain_count=3), "warming")
        self.assertEqual(self.state(limit_count=60, prev1=55, prev2=50, chain_count=2), "mixed")
        self.assertEqual(self.state(limit_ratio=0.03, max_height=5), "climax")
        self.assertEqual(self.state(limit_ratio=0.015, chain_count=3), "main_rise")
        self.assertEqual(self.state(limit_count=40, prev1=45, prev2=50, limit_ratio=0.03, max_height=5), "retreat",
                         "退潮优先于高潮")
        self.assertEqual(self.state(), "mixed")

    def test_scores(self):
        segment = pd.Series({"segment_limit_up_ratio_emotion": 0.02, "segment_chain_count": 4,
                             "segment_limit_max_height": 5, "segment_limit_down_ratio": 0.01})
        self.assertAlmostEqual(MarketEmotionBuilder.calculate_segment_score(segment), 2 + 2 + 4 - 1.2)
        market = pd.Series({"market_limit_up_ratio": 0.02, "market_chain_count": 10, "market_limit_max_height": 5,
                            "market_limit_down_ratio": 0.01})
        self.assertAlmostEqual(MarketEmotionBuilder.calculate_market_score(market), 2 + 2 + 4 - 1.2)

    def test_limit_down_count_and_full_limit_data(self):
        daily = pd.DataFrame({"pct_chg": [-9.5, -9.4, -19.5, -4.5, None], "limit_pct": [0.10, 0.10, 0.20, 0.05, 0.10]})
        self.assertEqual(MarketEmotionBuilder.count_limit_down(daily), 3)
        self.assertEqual(MarketEmotionBuilder.count_limit_down(pd.DataFrame({"pct_chg": [-9.5, -9.4]})), 1, "缺涨跌幅限制按10%")
        self.assertEqual(MarketEmotionBuilder.count_limit_down(pd.DataFrame()), 0)
        self.assertFalse(MarketEmotionBuilder.has_full_limit_data(pd.DataFrame()))
        self.assertTrue(MarketEmotionBuilder.has_full_limit_data(pd.DataFrame({"ts_code": ["a"]})))
        self.assertFalse(MarketEmotionBuilder.has_full_limit_data(pd.DataFrame({"limit_data_quality": ["full", "partial"]})))
        self.assertFalse(MarketEmotionBuilder.has_full_limit_data(pd.DataFrame({"strategy_compatible": ["true", "false"]})))


if __name__ == "__main__":
    unittest.main()
