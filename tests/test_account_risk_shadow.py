from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

import pandas as pd

from src.account_risk_shadow import (
    AccountRiskShadowError,
    update_account_risk_shadow,
    validate_shadow_policy,
)


class AccountRiskShadowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = {
            "schema_version": 1,
            "status": "SHADOW",
            "policy_id": "test-shadow",
            "enforce_live_gate": False,
            "observation_start_date": "20260811",
            "max_daily_realized_loss_pct": 0.03,
            "max_account_drawdown_pct": 0.15,
            "max_consecutive_losses": 3,
            "suggested_cooldown_trade_days": 5,
            "minimum_complete_trades_for_activation_review": 20,
            "state_path": "reports/shadow/state.json",
            "latest_status_path": "reports/shadow/latest.json",
            "bootstrap_equity_path": "reports/equity.json",
        }

    @staticmethod
    def trades(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
        return pd.DataFrame(rows, columns=["trade_key", "exit_date", "net_pnl"])

    def update(
        self,
        root: Path,
        trades: pd.DataFrame,
        *,
        date: str = "20260811",
        bootstrap: float = 100.0,
    ) -> dict:
        return update_account_risk_shadow(
            state_path=root / "state.json",
            latest_status_path=root / "latest.json",
            policy=self.policy,
            complete_trades=trades,
            bootstrap_equity=bootstrap,
            as_of_date=date,
        )

    def test_bootstrap_does_not_double_count_existing_trades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.update(
                root,
                self.trades([("old", "20260810", 10.0)]),
                bootstrap=100.0,
            )
            self.assertEqual(result["status"], "BOOTSTRAPPED")
            self.assertEqual(result["current_equity"], 100.0)
            again = self.update(
                root,
                self.trades([("old", "20260810", 10.0)]),
                bootstrap=999.0,
            )
            self.assertEqual(again["new_complete_trade_count"], 0)
            self.assertEqual(again["current_equity"], 100.0)

    def test_daily_loss_trigger_is_shadow_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.update(root, self.trades([]), date="20260811")
            result = self.update(
                root,
                self.trades([("loss", "20260812", -4.0)]),
                date="20260812",
            )
            self.assertIn("DAILY_REALIZED_LOSS", result["triggers"])
            self.assertEqual(
                result["suggested_action"],
                "HYPOTHETICAL_PAUSE_NEW_ENTRIES_UNTIL_NEXT_TRADE_DAY",
            )
            self.assertFalse(result["enforce_live_gate"])
            self.assertFalse(result["changes_live_orders"])

    def test_drawdown_and_loss_streak_suggest_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.update(root, self.trades([]), date="20260811")
            rows = [
                ("loss1", "20260812", -6.0),
                ("loss2", "20260813", -6.0),
                ("loss3", "20260814", -6.0),
            ]
            result = self.update(root, self.trades(rows), date="20260814")
            self.assertIn("ACCOUNT_DRAWDOWN", result["triggers"])
            self.assertIn("CONSECUTIVE_LOSSES", result["triggers"])
            self.assertEqual(
                result["suggested_action"],
                "HYPOTHETICAL_PAUSE_NEW_ENTRIES_5_TRADE_DAYS",
            )

    def test_new_trades_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.update(root, self.trades([]), date="20260811")
            rows = self.trades([("win", "20260812", 5.0)])
            first = self.update(root, rows, date="20260812")
            second = self.update(root, rows, date="20260812")
            self.assertEqual(first["current_equity"], 105.0)
            self.assertEqual(second["current_equity"], 105.0)
            self.assertEqual(second["new_complete_trade_count"], 0)

    def test_live_enforcement_cannot_be_enabled_in_shadow_policy(self) -> None:
        invalid = dict(self.policy)
        invalid["enforce_live_gate"] = True
        with self.assertRaisesRegex(AccountRiskShadowError, "禁止设置"):
            validate_shadow_policy(invalid)

    def test_daily_loss_trigger_can_be_disabled(self) -> None:
        policy = dict(self.policy)
        policy["max_daily_realized_loss_pct"] = None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            update_account_risk_shadow(
                state_path=root / "state.json",
                latest_status_path=root / "latest.json",
                policy=policy,
                complete_trades=self.trades([]),
                bootstrap_equity=100.0,
                as_of_date="20260811",
            )
            result = update_account_risk_shadow(
                state_path=root / "state.json",
                latest_status_path=root / "latest.json",
                policy=policy,
                complete_trades=self.trades([("loss", "20260812", -4.0)]),
                bootstrap_equity=100.0,
                as_of_date="20260812",
            )
            self.assertNotIn("DAILY_REALIZED_LOSS", result["triggers"])
            self.assertIsNone(result["daily_realized_loss_limit"])

    def test_empty_shadow_state_can_migrate_to_fixed_candidate_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.update(root, self.trades([]), date="20260811")
            fixed = dict(self.policy)
            fixed["policy_id"] = "test-shadow-v2"
            fixed["supersedes_policy_id"] = "test-shadow"
            fixed["max_daily_realized_loss_pct"] = None
            fixed["max_account_drawdown_pct"] = 0.18
            fixed["max_consecutive_losses"] = 2
            result = update_account_risk_shadow(
                state_path=root / "state.json",
                latest_status_path=root / "latest.json",
                policy=fixed,
                complete_trades=self.trades([]),
                bootstrap_equity=100.0,
                as_of_date="20260811",
            )
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(result["policy_id"], "test-shadow-v2")
            self.assertEqual(state["policy_id"], "test-shadow-v2")
            self.assertEqual(len(state["policy_transitions"]), 1)

    def test_nonempty_shadow_state_refuses_silent_policy_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.update(root, self.trades([]), date="20260811")
            self.update(
                root,
                self.trades([("win", "20260812", 1.0)]),
                date="20260812",
            )
            fixed = dict(self.policy)
            fixed["policy_id"] = "test-shadow-v2"
            fixed["supersedes_policy_id"] = "test-shadow"
            with self.assertRaisesRegex(AccountRiskShadowError, "policy_id不一致"):
                update_account_risk_shadow(
                    state_path=root / "state.json",
                    latest_status_path=root / "latest.json",
                    policy=fixed,
                    complete_trades=self.trades([("win", "20260812", 1.0)]),
                    bootstrap_equity=100.0,
                    as_of_date="20260812",
                )


if __name__ == "__main__":
    unittest.main()
