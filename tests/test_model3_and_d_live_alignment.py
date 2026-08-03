from __future__ import annotations

import json
import logging
from pathlib import Path
from types import ModuleType
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
