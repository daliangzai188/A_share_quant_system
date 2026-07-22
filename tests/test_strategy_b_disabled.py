from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from scripts import trading_daemon
from scripts.generate_live_limit_pool_daily_ops import select_candidates
from scripts.run_paper_ab_filtered_observation_window import (
    is_b_strategy_enabled,
    reject_strategy_risk_mask,
)
from scripts.trading_daemon import _position_manual_exit_only
from src.combined_live_engine import CombinedLiveEngine


class _FakeGateway:
    def __init__(self, orders: pd.DataFrame) -> None:
        self.orders = orders

    def load_planned_orders(self, _path: str) -> tuple[Path, pd.DataFrame]:
        return Path("legacy_planned_orders.csv"), self.orders.copy()


class StrategyBDisabledTests(unittest.TestCase):
    def test_disabled_b_is_skipped_and_c_can_fill(self) -> None:
        candidates = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "allow_buy_reliable": True,
                    "is_fill_score_reliable": True,
                    "segment_emotion_state_bucket": "warming",
                    "market_chain_count_bucket": "15_30",
                    "segment_limit_up_count_bucket": "40_80",
                    "fill_probability": 1.0,
                    "amount": 100_000_000.0,
                    "turnover_rate": 8.0,
                }
            ]
        )
        config = {
            "candidate_filters": {
                "conditions": [{"column": "market_chain_count_bucket", "operator": "==", "value": "8_15"}],
                "exclude_conditions": [],
                "exclude_rules": [],
            },
            "paper_ab_filtered_strategy": {
                "b_strategy": {
                    "enabled": False,
                    "removed": True,
                    "new_entries_disabled": True,
                    "auto_exit_enabled": False,
                    "manual_exit_only": True,
                },
                "c_strategy": {
                    "enabled": True,
                    "conditions": [
                        {"column": "market_chain_count_bucket", "operator": "==", "value": "15_30"},
                        {"column": "segment_limit_up_count_bucket", "operator": "==", "value": "40_80"},
                    ],
                },
            },
            "ranking": {"columns": ["fill_probability"], "ascending": [False], "score_rules": []},
        }

        strategy_leg, selected = select_candidates(candidates, config, top_n=1)

        self.assertEqual(strategy_leg, "LIVE_LIMIT_POOL_C")
        self.assertEqual(selected.iloc[0]["ts_code"], "000001.SZ")
        self.assertFalse(is_b_strategy_enabled(config))

    def test_combined_engine_rejects_all_legacy_b_orders_after_removal(self) -> None:
        orders = pd.DataFrame(
            [
                {"strategy_leg": "B", "side": "BUY", "ts_code": "000001.SZ"},
                {"strategy_leg": "B", "side": "SELL", "ts_code": "000002.SZ"},
                {"strategy_leg": "A", "side": "BUY", "ts_code": "000003.SZ"},
            ]
        )
        engine = object.__new__(CombinedLiveEngine)
        engine.gateway = _FakeGateway(orders)
        engine.is_b_new_entry_enabled = lambda: False
        engine.is_b_strategy_removed = lambda: True

        _, filtered = engine.load_latest_abc_orders()

        remaining = set(zip(filtered["strategy_leg"], filtered["side"], filtered["ts_code"]))
        self.assertNotIn(("B", "BUY", "000001.SZ"), remaining)
        self.assertNotIn(("B", "SELL", "000002.SZ"), remaining)
        self.assertIn(("A", "BUY", "000003.SZ"), remaining)

    def test_unreadable_b_config_fails_closed(self) -> None:
        engine = object.__new__(CombinedLiveEngine)
        engine.project_root = Path("/path/that/does/not/exist")

        self.assertTrue(engine.is_b_strategy_removed())
        self.assertFalse(engine.is_b_new_entry_enabled())

    def test_removed_b_position_is_manual_exit_only(self) -> None:
        position = {
            "strategy_leg": "B",
            "manual_exit_only": True,
            "auto_exit_disabled": True,
        }
        self.assertTrue(_position_manual_exit_only(position))
        engine = object.__new__(CombinedLiveEngine)
        engine.is_b_strategy_removed = lambda: True
        self.assertTrue(engine.is_manual_exit_only_position(position))

    def test_manual_b_position_cannot_reach_direct_or_watchdog_sell(self) -> None:
        position = {
            "order_id": "B-RETIRED-1",
            "strategy_leg": "B",
            "ts_code": "000048.SZ",
            "name": "京基智农",
            "shares": 6_900,
            "status": "open",
            "manual_exit_only": True,
            "auto_exit_disabled": True,
        }
        fake_log = MagicMock()
        with patch.object(trading_daemon, "logger", return_value=fake_log), patch.object(
            trading_daemon, "_abc_place_sell_order_direct"
        ) as direct_sell, patch.object(trading_daemon, "mark_position_closed") as mark_closed:
            trading_daemon._do_sell(position, qmt_enabled=True)
            trading_daemon._do_sell(position, qmt_enabled=False)

        direct_sell.assert_not_called()
        mark_closed.assert_not_called()

        with patch.object(trading_daemon, "_qmt_get") as qmt_get:
            submitted = trading_daemon._watchdog_rescue_sell(position, fake_log)
        self.assertEqual(submitted, [])
        qmt_get.assert_not_called()

    def test_c_uses_its_own_risk_rules_after_b_removal(self) -> None:
        candidates = pd.DataFrame([{"risk_flags": "LOSS_OVERLAY_WATCH", "limit_times": 1, "open_times": 1}])
        config = {
            "paper_ab_filtered_strategy": {
                "b_strategy": {"removed": True},
                "c_strategy": {
                    "risk_reject_rules": [
                        {"risk_flags_contains_any": ["LOSS_OVERLAY_WATCH"]},
                    ]
                },
            }
        }
        self.assertTrue(bool(reject_strategy_risk_mask(candidates, config, "c_strategy").iloc[0]))


if __name__ == "__main__":
    unittest.main()
