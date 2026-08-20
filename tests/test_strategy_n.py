from __future__ import annotations

import json
import datetime as dt
import tempfile
from pathlib import Path
import unittest

import pandas as pd

from scripts.research_strategy_n_v2 import available_rankers
from src.strategy_n import load_n_spec, n_live_entry_block_reason, select_n_daily_picks


ROOT = Path(__file__).resolve().parents[1]


def row(**overrides):
    value = {
        "trade_date": "20260818", "ts_code": "300001.SZ", "name": "测试",
        "limit_close": 10.0, "market_segment": "chi_next",
        "allow_buy_reliable": True, "is_fill_score_reliable": True,
        "is_fd_amount_abnormal": False, "strategy_compatible": True,
        "fill_probability": 0.8, "segment_limit_max_height_bucket": "1",
        "segment_retreat_state_bucket": "retreat_weak",
        "market_chain_count_bucket": "8_15", "market_emotion_state_bucket": "retreat",
        "first_time_minutes": 600, "amount": 100000, "circ_mv": 10000,
        "volume_ratio_bucket": "4_8",
    }
    value.update(overrides)
    return value


class StrategyNTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        cls.spec = load_n_spec(cls.config)

    def test_exact_condition_and_rank_order(self) -> None:
        frame = pd.DataFrame([
            row(ts_code="300003.SZ", first_time_minutes=610, circ_mv=5000),
            row(ts_code="300002.SZ", first_time_minutes=590, circ_mv=9000),
            row(ts_code="300001.SZ", first_time_minutes=590, circ_mv=8000),
            row(ts_code="300004.SZ", segment_limit_max_height_bucket="2", first_time_minutes=500),
        ])
        pick = select_n_daily_picks(frame, self.spec, signal_date="20260818")
        self.assertEqual(len(pick), 1)
        self.assertEqual(str(pick.iloc[0]["ts_code"]), "300001.SZ")

    def test_reliability_and_fill_probability_are_fail_closed(self) -> None:
        frame = pd.DataFrame([
            row(allow_buy_reliable=False),
            row(ts_code="300002.SZ", fill_probability=0.59),
        ])
        self.assertTrue(
            select_n_daily_picks(frame, self.spec, signal_date="20260818").empty
        )

    def test_current_branch_has_priority_over_supplement(self) -> None:
        frame = pd.DataFrame([
            row(ts_code="300001.SZ"),
            row(
                ts_code="300002.SZ",
                segment_limit_max_height_bucket="2",
                segment_retreat_state_bucket="neutral",
                market_chain_count_bucket="3_8",
                market_emotion_state_bucket="mixed",
                amount=999999,
            ),
        ])
        pick = select_n_daily_picks(frame, self.spec, signal_date="20260818")
        self.assertEqual(str(pick.iloc[0]["ts_code"]), "300001.SZ")
        self.assertEqual(str(pick.iloc[0]["n_branch"]), "CURRENT")

    def test_supplement_uses_amount_desc_when_current_is_empty(self) -> None:
        frame = pd.DataFrame([
            row(
                ts_code="300001.SZ",
                segment_limit_max_height_bucket="2",
                segment_retreat_state_bucket="neutral",
                market_chain_count_bucket="3_8",
                market_emotion_state_bucket="mixed",
                amount=100000,
            ),
            row(
                ts_code="300002.SZ",
                segment_limit_max_height_bucket="2",
                segment_retreat_state_bucket="neutral",
                market_chain_count_bucket="3_8",
                market_emotion_state_bucket="mixed",
                amount=200000,
            ),
        ])
        pick = select_n_daily_picks(frame, self.spec, signal_date="20260818")
        self.assertEqual(str(pick.iloc[0]["ts_code"]), "300002.SZ")
        self.assertEqual(str(pick.iloc[0]["n_branch"]), "SUPPLEMENT")

    def test_current_post_filter_failure_skips_day_without_supplement_fallback(self) -> None:
        frame = pd.DataFrame([
            row(ts_code="300001.SZ", volume_ratio_bucket="2_4"),
            row(
                ts_code="300002.SZ",
                segment_limit_max_height_bucket="2",
                segment_retreat_state_bucket="neutral",
                market_chain_count_bucket="3_8",
                market_emotion_state_bucket="mixed",
                amount=999999,
            ),
        ])
        pick = select_n_daily_picks(frame, self.spec, signal_date="20260818")
        self.assertTrue(pick.empty)

    def test_current_post_filter_accepts_locked_volume_buckets(self) -> None:
        frame = pd.DataFrame([
            row(ts_code="300001.SZ", volume_ratio_bucket="lt_1"),
            row(ts_code="300002.SZ", volume_ratio_bucket="2_4", first_time_minutes=500),
        ])
        pick = select_n_daily_picks(frame, self.spec, signal_date="20260818")
        # 排名第一的300002未过门禁，不能回补300001。
        self.assertTrue(pick.empty)

    def test_legacy_v2_candidate_ledger_is_preserved(self) -> None:
        locked = pd.read_csv(
            ROOT / "reports" / "strategy_n" / "n_backtest_candidates.csv",
            dtype={"trade_date": str},
            low_memory=False,
        )
        self.assertEqual(len(locked), 106)
        self.assertEqual(locked["trade_date"].nunique(), 106)
        self.assertTrue(locked["execution_status"].eq("OK").all())
        self.assertTrue(locked["sample_scope"].eq("COMPLETE_DAILY_CANDIDATES").all())

    def test_corrected_v3_candidate_ledger_uses_asof_and_real_fill_status(self) -> None:
        corrected = pd.read_csv(
            ROOT / "reports" / "strategy_n_v3" / "n_backtest_candidates.csv",
            dtype={"trade_date": str},
            low_memory=False,
        )
        expected = int(self.config["strategy_n"]["execution_v3_audit"]["candidate_count"])
        self.assertEqual(len(corrected), expected)
        self.assertEqual(corrected["trade_date"].nunique(), expected)
        self.assertTrue(
            corrected["fill_probability_method"].eq("asof_turnover_space_proxy_v2").all()
        )
        self.assertTrue(
            set(corrected["execution_status"]).issubset({
                "OK", "LIMIT_UP_UNBUYABLE", "SELL_UNRESOLVED", "NO_PRICE",
                "NO_CALENDAR", "BAD_PRICE", "NO_ADJUSTED_PRICE",
            })
        )
        self.assertGreater(int(corrected["execution_status"].eq("OK").sum()), 0)

    def test_current_v4_candidate_ledger_enforces_volume_post_filter(self) -> None:
        current = pd.read_csv(
            ROOT / "reports" / "strategy_n_v4" / "n_backtest_candidates.csv",
            dtype={"trade_date": str},
            low_memory=False,
        )
        audit = self.config["strategy_n"]["execution_v4_audit"]
        self.assertEqual(len(current), int(audit["candidate_count"]))
        self.assertEqual(
            int(current["execution_status"].eq("OK").sum()),
            int(audit["executable_candidate_count"]),
        )
        self.assertTrue(
            current["fill_probability_method"].eq("asof_turnover_space_proxy_v2").all()
        )
        first_branch = current[current["n_branch"].eq("CURRENT")]
        self.assertGreater(len(first_branch), 0)
        self.assertEqual(
            set(first_branch["volume_ratio_bucket"].astype(str)),
            {"4_8", "lt_1"},
        )

    def test_n_entry_pause_and_health_gate_only_block_new_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "health.json"
            base = {"strategy_n": dict(self.config["strategy_n"])}
            base["strategy_n"].update({
                "health_gate_enabled": True,
                "health_gate_status_path": str(status),
                "health_gate_max_age_hours": 72,
            })
            # 没有真实完整交易状态时允许继续收集样本。
            self.assertEqual(n_live_entry_block_reason(root, base), "")

            now = dt.datetime(2026, 8, 20, 4, 0, tzinfo=dt.timezone.utc)
            status.write_text(json.dumps({
                "N": "RED",
                "_details": {"N": {"level": "RED", "updated_at": now.isoformat()}},
            }), encoding="utf-8")
            self.assertIn("健康等级=RED", n_live_entry_block_reason(root, base, now=now))

            status.write_text(json.dumps({
                "N": "GREEN",
                "_details": {"N": {
                    "level": "GREEN",
                    "updated_at": (now - dt.timedelta(hours=73)).isoformat(),
                }},
            }), encoding="utf-8")
            self.assertIn("已过期", n_live_entry_block_reason(root, base, now=now))

            base["strategy_n"]["entry_pause"] = True
            self.assertIn("entry_pause=true", n_live_entry_block_reason(root, base, now=now))

    def test_research_only_generates_rankers_available_in_asof_pool(self) -> None:
        pool = pd.DataFrame({
            "amount": [1.0],
            "circ_mv": [1.0],
            "ts_code": ["300001.SZ"],
        })
        rankers = available_rankers(pool)
        self.assertIn("amount_desc", rankers)
        self.assertNotIn("segment_leader_rank_asc", rankers)


if __name__ == "__main__":
    unittest.main()
