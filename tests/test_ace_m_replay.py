"""ACE+M规则回放（三级警告的“整体组合”“M”判定用）：仓位、停手、M的回避与占用、费用、M的买卖结果。

与研究回放（m14_verify.py 的 simulate）逐位一致的核对在草稿目录用研究数据做过（2026-11-13，
全期295,345.586176倍、547笔逐笔相同）；这里用小数据锁住每条规则。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.ace_m_replay import RawPrices, ace_plans_by_day, make_m_outcome, replay_ace_m

DAYS = ["20261102", "20261103", "20261104", "20261105", "20261106", "20261109"]
STAMP = [{"start_date": "19000101", "end_date": "99991231", "rate": 0.0005}]


def plan(code, exit_date, ret=0.0, *, leg="A", opened=True, observable=True, until=""):
    return {"strategy_leg": leg, "ts_code": code, "position_opened": opened, "outcome_observable": observable,
            "status": "OK" if observable else "OUTCOME_NOT_OBSERVABLE", "entry_price": 10.0,
            "stock_return_before_fees": ret, "exit_date": exit_date, "position_open_until": until or exit_date}


def run(plans, allowed=None, candidates=None, outcomes=None, **kwargs):
    outcomes = outcomes or {}
    return replay_ace_m(DAYS, plans, allowed or {}, candidates or {}, lambda code, day: outcomes.get((code, day)),
                        stamp_schedule=STAMP, **kwargs)


class SizingTests(unittest.TestCase):
    def test_ace_full_and_halted_weights(self) -> None:
        _eq, trades = run({"20261102": [plan("000001.SZ", "20261103")]})
        self.assertAlmostEqual(trades.iloc[0]["weight"], 0.825, delta=0.002)
        _eq, trades = run({"20261102": [plan("000001.SZ", "20261103")]}, allowed={"20261102": False})
        self.assertAlmostEqual(trades.iloc[0]["weight"], 0.20, delta=0.002)
        _eq, trades = run({"20261102": [plan("000001.SZ", "20261103")]}, allowed={"20261102": False},
                          use_m=False, ace_halted_weight=0.0)
        self.assertTrue(trades.empty, "只用ACE（甲v1）停手日不开")

    def test_first_plan_wins_and_occupied_account_skips(self) -> None:
        plans = ace_plans_by_day({
            "C": pd.DataFrame([plan("000002.SZ", "20261104", leg="C") | {"action_date": "20261102"}]),
            "A": pd.DataFrame([plan("000001.SZ", "20261104") | {"action_date": "20261102"},
                               plan("000003.SZ", "20261105") | {"action_date": "20261103"}]),
        })
        self.assertEqual([p["strategy_leg"] for p in plans["20261102"]], ["A", "C"])
        _eq, trades = run(plans)
        self.assertEqual(list(trades.ts_code), ["000001.SZ"], "A先于C；ACE持仓到卖出日都占着账户")

    def test_unobservable_plan_still_occupies_until_its_end(self) -> None:
        plans = {"20261102": [plan("000001.SZ", "", observable=False, until="20261104")],
                 "20261104": [plan("000002.SZ", "20261105")], "20261105": [plan("000003.SZ", "20261106")]}
        _eq, trades = run(plans)
        self.assertEqual(list(trades.ts_code), ["000003.SZ"])

    def test_m_weights_beside_and_without_ace(self) -> None:
        outcomes = {("300001.SZ", "20261102"): ("20261103", 10.0, 0.05),
                    ("300002.SZ", "20261104"): ("20261105", 10.0, 0.05)}
        _eq, trades = run({"20261104": [plan("000001.SZ", "20261105")]},
                          candidates={"20261102": ["300001.SZ"], "20261104": ["300002.SZ"]}, outcomes=outcomes)
        weights = dict(zip(trades.ts_code, trades.weight))
        self.assertAlmostEqual(weights["300001.SZ"], 0.825, delta=0.002)
        self.assertAlmostEqual(weights["000001.SZ"], 0.825, delta=0.002)
        # ACE当天买了82.5%：M最多17.5%，同时合计不超过99.5%，所以只剩约17%
        self.assertAlmostEqual(weights["300002.SZ"], 0.995 - weights["000001.SZ"], delta=0.002)

    def test_ace_capped_by_total_when_m_is_held(self) -> None:
        outcomes = {("300001.SZ", "20261102"): ("20261104", 10.0, 0.0)}
        _eq, trades = run({"20261103": [plan("000001.SZ", "20261104")]},
                          candidates={"20261102": ["300001.SZ"]}, outcomes=outcomes)
        weights = dict(zip(trades.ts_code, trades.weight))
        self.assertLessEqual(weights["000001.SZ"], 0.995 - 0.825 + 1e-3, "ACE不超过99.5%−M占用")

    def test_m_skips_ace_stock_unbuyable_and_one_at_a_time(self) -> None:
        outcomes = {("300002.SZ", "20261102"): ("20261104", 10.0, 0.01),
                    ("300003.SZ", "20261103"): ("20261104", 10.0, 0.01)}
        _eq, trades = run({"20261102": [plan("300001.SZ", "20261103")]},
                          candidates={"20261102": ["300001.SZ", "300009.SZ", "300002.SZ"],
                                      "20261103": ["300003.SZ"]}, outcomes=outcomes)
        self.assertEqual(list(trades[trades.family == "M"].ts_code), ["300002.SZ"],
                         "跳过ACE当天买的股票和买不进的候选；上一笔M没卖完不买新的")

    def test_fees_and_stamp_tax(self) -> None:
        equity, trades = run({"20261102": [plan("000001.SZ", "20261103", ret=0.0)]})
        gross = 41200 * 10.0
        cost = gross + gross * 0.0003 + gross * 0.00001
        proceeds = gross - gross * 0.0003 - gross * 0.00001 - gross * 0.0005
        self.assertAlmostEqual(trades.iloc[0]["ret"], proceeds / cost - 1, places=12)
        self.assertAlmostEqual(equity.iloc[-1], 500000.0 - cost + proceeds, places=6)


class MOutcomeTests(unittest.TestCase):
    def test_open_limit_up_unbuyable_and_limit_down_exit_postponed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            (raw / "daily").mkdir()
            (raw / "adj_factor").mkdir()
            bars = {
                "20261102": [("300001.SZ", 10.5, 11.0, 10.0, 100.0), ("300002.SZ", 12.0, 12.0, 10.0, 100.0)],
                "20261103": [("300001.SZ", 9.0, 8.8, 11.0, 100.0)],          # 收盘跌停（11×0.8=8.8），不能卖
                "20261104": [("300001.SZ", 8.8, 9.2, 8.8, 100.0)],
            }
            for day, rows in bars.items():
                pd.DataFrame(rows, columns=["ts_code", "open", "close", "pre_close", "vol"]).to_csv(
                    raw / "daily" / ("%s.csv" % day), index=False)
                pd.DataFrame({"ts_code": [r[0] for r in rows], "adj_factor": 1.0}).to_csv(
                    raw / "adj_factor" / ("%s.csv" % day), index=False)
            outcome = make_m_outcome(RawPrices(raw), DAYS[:3], slippage=0.001)
            self.assertIsNone(outcome("300002.SZ", "20261102"), "开盘就涨停买不进")
            exit_date, entry, ret = outcome("300001.SZ", "20261102")
            self.assertEqual(exit_date, "20261104")
            self.assertAlmostEqual(entry, 10.5 * 1.001)
            self.assertAlmostEqual(ret, 9.2 * 0.999 / (10.5 * 1.001) - 1)


if __name__ == "__main__":
    unittest.main()
