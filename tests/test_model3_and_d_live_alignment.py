from __future__ import annotations

import json
import logging
from pathlib import Path
from types import ModuleType
import sys
import unittest
from unittest.mock import patch

import pandas as pd

# 纯规则测试不读取.env；开发机缺少python-dotenv时使用无副作用桩。
if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from scripts.monitor_strategy_d_intraday import StockState, StrategyDMonitor
from scripts.trading_daemon import _model3_l_base_rule_pass_for_log
from src.combined_live_engine import CombinedLiveEngine
from src.strategy_model3_policy import (
    model3_l_base_rule_pass,
    model3_l_replace_guard_pass,
)
from src.strategy_d_spec import (
    historical_candidate_mask,
    intraday_history_is_complete,
)


class Model3PolicyAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "strategy_model3": {
                "base_l_rule": {
                    "market_segment": "not_star",
                    "segment_retreat_state_bucket": ["neutral", "warming_2day"],
                    "market_chain_count_bucket": [
                        "3_8",
                        "8_15",
                        "15_30",
                        "gte_30",
                    ],
                },
                "replace_guard": {
                    "market_segment": "chi_next",
                    "theme_limit_count_min": 2,
                    "first_time_detail_bucket_exclude": ["after_1430"],
                },
            }
        }

    def test_chain_3_8_expansion_uses_only_signal_day_fields(self) -> None:
        signal = {
            "market_segment": "chi_next",
            "segment_retreat_state_bucket": "neutral",
            "market_chain_count_bucket": "3_8",
        }
        passed, _reason = model3_l_base_rule_pass(signal, self.config)
        self.assertTrue(passed)

        old_config = {
            "strategy_model3": {
                **self.config["strategy_model3"],
                "base_l_rule": {
                    **self.config["strategy_model3"]["base_l_rule"],
                    "market_chain_count_bucket": ["8_15", "15_30", "gte_30"],
                },
            }
        }
        old_passed, _reason = model3_l_base_rule_pass(signal, old_config)
        self.assertFalse(old_passed)

    def test_base_rule_missing_field_fails_closed(self) -> None:
        passed, reason = model3_l_base_rule_pass(
            {
                "market_segment": "chi_next",
                "segment_retreat_state_bucket": "neutral",
            },
            self.config,
        )
        self.assertFalse(passed)
        self.assertIn("missing", reason)

        missing_segment, segment_reason = model3_l_base_rule_pass(
            {
                "segment_retreat_state_bucket": "neutral",
                "market_chain_count_bucket": "3_8",
            },
            self.config,
        )
        self.assertFalse(missing_segment)
        self.assertIn("market_segment=missing", segment_reason)

    def test_replace_guard_remains_unchanged(self) -> None:
        passed, _reason = model3_l_replace_guard_pass(
            {
                "market_segment": "chi_next",
                "theme_limit_count": 2,
                "first_time_detail_bucket": "1330_1430",
            },
            self.config,
        )
        self.assertTrue(passed)
        blocked, _reason = model3_l_replace_guard_pass(
            {
                "market_segment": "chi_next",
                "theme_limit_count": 2,
                "first_time_detail_bucket": "after_1430",
            },
            self.config,
        )
        self.assertFalse(blocked)

    def test_production_config_live_engine_and_daemon_share_same_rule(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        production_config = json.loads(
            (project_root / "config" / "config.json").read_text(encoding="utf-8")
        )
        self.assertIn(
            "3_8",
            production_config["strategy_model3"]["base_l_rule"][
                "market_chain_count_bucket"
            ],
        )
        signal = {
            "market_segment": "chi_next",
            "segment_retreat_state_bucket": "neutral",
            "market_chain_count_bucket": "3_8",
        }

        shared_result = model3_l_base_rule_pass(signal, production_config)
        engine = CombinedLiveEngine.__new__(CombinedLiveEngine)
        engine.config = production_config
        engine_result = engine.model3_l_base_rule_pass(signal)
        daemon_result = _model3_l_base_rule_pass_for_log(signal, production_config)

        self.assertEqual(shared_result, engine_result)
        self.assertEqual(shared_result, daemon_result)
        self.assertTrue(shared_result[0])


class StrategyDLiveAlignmentTests(unittest.TestCase):
    def make_monitor(self) -> StrategyDMonitor:
        config = {
            "strategy_d": {
                "preferred_open_times": 2,
                "max_open_times": 3,
            }
        }
        return StrategyDMonitor(
            broker=None,
            live_order=False,
            logger=logging.getLogger("test_strategy_d_alignment"),
            signal_csv=Path("reports/strategy_d/test_alignment.csv"),
            config=config,
        )

    def test_d_ranking_prefers_two_opens_before_fd_ratio(self) -> None:
        monitor = self.make_monitor()
        preferred = StockState(
            ts_code="300001.SZ",
            upper_limit=10.0,
            open_times_today=2,
            bid_vol=100_000,
            circ_mv=100_000,
        )
        higher_fd_but_not_preferred = StockState(
            ts_code="300002.SZ",
            upper_limit=10.0,
            open_times_today=3,
            bid_vol=300_000,
            circ_mv=100_000,
        )
        ranked = sorted(
            [higher_fd_but_not_preferred, preferred],
            key=monitor._rank_key,
            reverse=True,
        )
        self.assertEqual(ranked[0].ts_code, preferred.ts_code)

    def test_d_rejects_more_than_three_opens(self) -> None:
        monitor = self.make_monitor()
        target = StockState(
            ts_code="300001.SZ",
            was_sealed=True,
            open_times_today=4,
        )
        self.assertFalse(monitor._passes_base_filters(target.ts_code, target))

    def test_d_rejects_one_open_t_board_like_yesterday_live_trade(self) -> None:
        monitor = self.make_monitor()
        target = StockState(
            ts_code="600815.SH",
            was_sealed=True,
            open_times_today=1,
            first_seal_hhmm=1030,
            last_seal_hhmm=1405,
        )
        self.assertFalse(monitor._passes_base_filters(target.ts_code, target))

    def test_d_watch_cannot_bypass_tail_reseal_gate(self) -> None:
        monitor = self.make_monitor()
        monitor.sentiment_current_min = 1
        monitor.sentiment_current_max = 2
        target = StockState(
            ts_code="300001.SZ",
            market_segment="chi_next",
            upper_limit=10.0,
            last_price=10.0,
            was_sealed=True,
            open_times_today=2,
            first_seal_hhmm=1030,
            last_seal_hhmm=1359,
            watch_alerted=True,
            bid_vol=100_000,
            circ_mv=100_000,
        )
        monitor.states[target.ts_code] = target
        with patch(
            "scripts.monitor_strategy_d_intraday.now_hhmm", return_value=1405
        ):
            passed, reason = monitor._validate_buy_candidate(target)
        self.assertFalse(passed)
        self.assertIn("早于回测下限", reason)

    def test_d_live_fill_gate_uses_same_80_percent_reliable_threshold(self) -> None:
        class FakeEstimator:
            def __init__(self, probability: float, source: str = "exact") -> None:
                self.probability = probability
                self.source = source

            def estimate(self, **_kwargs):
                return {
                    "fill_probability": self.probability,
                    "matched_source": self.source,
                }

        monitor = self.make_monitor()
        monitor.segment_stock_counts = {"chi_next": 100}
        target = StockState(
            ts_code="300001.SZ",
            market_segment="chi_next",
            upper_limit=10.0,
            last_price=10.0,
            was_sealed=True,
            open_times_today=2,
            first_seal_hhmm=1030,
            last_seal_hhmm=1405,
            bid_vol=100_000,
            circ_mv=100_000,
        )
        monitor.states[target.ts_code] = target
        monitor.sentiment_current_min = 1
        monitor.sentiment_current_max = 2
        monitor.fill_model_ready = True
        monitor.fill_estimator = FakeEstimator(0.79)
        rejected, reason = monitor._refresh_fill_gate(target)
        self.assertFalse(rejected)
        self.assertIn("低于回测阈值", reason)

        monitor.fill_estimator = FakeEstimator(0.80)
        passed, _reason = monitor._refresh_fill_gate(target)
        self.assertTrue(passed)
        self.assertTrue(target.fill_reliable)

        monitor.fill_estimator = FakeEstimator(1.0, source="none")
        reliable, reason = monitor._refresh_fill_gate(target)
        self.assertFalse(reliable)
        self.assertIn("没有可靠历史匹配", reason)

    def test_d_exact_historical_boundaries_pass_live_validation(self) -> None:
        class ExactEstimator:
            @staticmethod
            def estimate(**_kwargs):
                return {"fill_probability": 0.8, "matched_source": "exact"}

        monitor = self.make_monitor()
        monitor.segment_stock_counts = {"sh_main": 100}
        monitor.sentiment_current_min = 1
        monitor.sentiment_current_max = 1
        monitor.fill_model_ready = True
        monitor.fill_estimator = ExactEstimator()
        target = StockState(
            ts_code="600001.SH",
            market_segment="sh_main",
            upper_limit=10.0,
            last_price=10.0,
            was_sealed=True,
            open_times_today=2,
            first_seal_hhmm=1000,
            last_seal_hhmm=1400,
            bid_vol=100_000,
            circ_mv=100_000,
        )
        monitor.states[target.ts_code] = target
        with patch(
            "scripts.monitor_strategy_d_intraday.now_hhmm", return_value=1400
        ):
            passed, reason = monitor._validate_buy_candidate(target)
        self.assertTrue(passed, reason)

        extra = StockState(ts_code="600002.SH", was_sealed=True)
        monitor.states[extra.ts_code] = extra
        with patch(
            "scripts.monitor_strategy_d_intraday.now_hhmm", return_value=1400
        ):
            blocked, reason = monitor._validate_buy_candidate(target)
        self.assertFalse(blocked)
        self.assertIn("不在回测strong代理区间", reason)

    def test_d_historical_mask_and_live_common_rules_share_boundaries(self) -> None:
        rows = pd.DataFrame(
            [
                {
                    "limit_times": 1,
                    "is_st": False,
                    "market_sentiment_level": "strong",
                    "board_type": "multi_open",
                    "open_times": 2,
                    "first_time_bucket": "midday",
                    "last_time": 140000,
                    "fill_probability": 0.8,
                    "is_fill_score_reliable": True,
                    "market_segment": "sh_main",
                },
                {
                    "limit_times": 1,
                    "is_st": False,
                    "market_sentiment_level": "strong",
                    "board_type": "t_board",
                    "open_times": 1,
                    "first_time_bucket": "midday",
                    "last_time": 140000,
                    "fill_probability": 1.0,
                    "is_fill_score_reliable": True,
                    "market_segment": "sh_main",
                },
                {
                    "limit_times": 1,
                    "is_st": False,
                    "market_sentiment_level": "very_strong",
                    "board_type": "multi_open",
                    "open_times": 2,
                    "first_time_bucket": "midday",
                    "last_time": 140000,
                    "fill_probability": 1.0,
                    "is_fill_score_reliable": True,
                    "market_segment": "sh_main",
                },
            ]
        )
        mask = historical_candidate_mask(rows, allowed_segments={"sh_main"})
        self.assertEqual(mask.tolist(), [True, False, False])

    def test_d_explicit_config_drift_fails_instead_of_silently_widening(self) -> None:
        config = {
            "strategy_d": {
                "min_open_times": 1,
                "max_open_times": 3,
                "preferred_open_times": 2,
            }
        }
        with self.assertRaisesRegex(ValueError, "偏离D回测值"):
            StrategyDMonitor(
                broker=None,
                live_order=False,
                logger=logging.getLogger("test_strategy_d_config_drift"),
                signal_csv=Path("reports/strategy_d/test_alignment.csv"),
                config=config,
            )

    def test_d_late_restart_fails_closed(self) -> None:
        self.assertTrue(intraday_history_is_complete(930))
        self.assertFalse(intraday_history_is_complete(931))
        self.assertFalse(intraday_history_is_complete(936))
        self.assertFalse(intraday_history_is_complete(1324))

    def test_d_sentiment_proxy_config_drift_fails_closed(self) -> None:
        config = {
            "strategy_d": {
                "sentiment_current_sealed_min": 88,
                "sentiment_current_sealed_max": 200,
            }
        }
        with self.assertRaisesRegex(ValueError, "偏离D认证值"):
            StrategyDMonitor(
                broker=None,
                live_order=False,
                logger=logging.getLogger("test_strategy_d_sentiment_drift"),
                signal_csv=Path("reports/strategy_d/test_sentiment_drift.csv"),
                config=config,
            )


if __name__ == "__main__":
    unittest.main()
