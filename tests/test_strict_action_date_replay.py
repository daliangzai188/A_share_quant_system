from __future__ import annotations

import unittest

import pandas as pd

from scripts.validate_other_live_strategies_strict import replay_by_action_date


def candidate(
    leg: str,
    signal_date: str,
    action_date: str,
    exit_date: str,
    account_return: float,
) -> dict[str, object]:
    row: dict[str, object] = {
        "status": "OK",
        "strategy_leg": leg,
        "signal_date": signal_date,
        "exit_date": exit_date,
        "account_return": account_return,
        "ts_code": f"{leg}00001.SZ",
        "name": f"测试{leg}",
    }
    if leg != "D":
        row["buy_date"] = action_date
    return row


class StrictActionDateReplayTests(unittest.TestCase):
    def test_previous_close_plans_are_ranked_a_c_e_before_intraday_d(self) -> None:
        legs = {
            "A": pd.DataFrame([candidate("A", "20240701", "20240702", "20240703", 0.01)]),
            "C": pd.DataFrame([candidate("C", "20240701", "20240702", "20240703", 0.02)]),
            "E": pd.DataFrame([candidate("E", "20240701", "20240702", "20240703", 0.03)]),
            "D": pd.DataFrame([candidate("D", "20240702", "20240702", "20240703", 0.04)]),
        }

        detail = replay_by_action_date(
            legs,
            ("A", "C", "E", "D"),
            action_dates=["20240702", "20240703"],
        )

        executed = detail[detail["status"].eq("EXECUTED")]
        self.assertEqual(executed["strategy_leg"].tolist(), ["A"])
        self.assertEqual(executed["action_date"].tolist(), ["20240702"])

    def test_c_blocks_e_and_d_when_a_has_no_plan(self) -> None:
        legs = {
            "A": pd.DataFrame(columns=["status", "signal_date", "buy_date"]),
            "C": pd.DataFrame([candidate("C", "20240701", "20240702", "20240703", 0.02)]),
            "E": pd.DataFrame([candidate("E", "20240701", "20240702", "20240703", 0.03)]),
            "D": pd.DataFrame([candidate("D", "20240702", "20240702", "20240703", 0.04)]),
        }

        detail = replay_by_action_date(
            legs,
            ("A", "C", "E", "D"),
            action_dates=["20240702"],
        )

        self.assertEqual(detail.iloc[0]["strategy_leg"], "C")

    def test_d_runs_only_when_static_plans_absent_and_exit_day_stays_occupied(self) -> None:
        empty = pd.DataFrame(columns=["status", "signal_date", "buy_date"])
        legs = {
            "A": empty.copy(),
            "C": empty.copy(),
            "E": empty.copy(),
            "D": pd.DataFrame(
                [
                    candidate("D", "20240702", "20240702", "20240703", 0.04),
                    candidate("D", "20240703", "20240703", "20240704", 0.05),
                    candidate("D", "20240704", "20240704", "20240705", 0.06),
                ]
            ),
        }

        detail = replay_by_action_date(
            legs,
            ("A", "C", "E", "D"),
            action_dates=["20240702", "20240703", "20240704"],
        )

        self.assertEqual(detail["status"].tolist(), ["EXECUTED", "SKIP_OCCUPIED", "EXECUTED"])
        self.assertEqual(
            detail[detail["status"].eq("EXECUTED")]["signal_date"].tolist(),
            ["20240702", "20240704"],
        )


if __name__ == "__main__":
    unittest.main()
