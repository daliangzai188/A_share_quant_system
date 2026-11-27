"""2026-11-27 实盘第一天发现的两处问题：
1. ACE+M+L 下 M 信号版本是 M_VERSION_RET5，M 下单和开仓推送原来固定拿旧版本号比，新框架的 M 信号全被当成“版本不符”；
2. 开仓推送的 L 一行只有规则比例，没有像 A 一样的预估金额；
3. L 开盘后补仓（POV）收口时，竞价成交但补仓一股没买的情况不推送，也看不出为什么没补、还差多少（用户要求像 A 一样写清楚）。"""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import trading_daemon as td
from src import portfolio_ace_m as PM
from src.daemon import buy_l
from src import strategy_l as SL
from src import strategy_m as SM
from src.open_plan_notification import l_plan_notice, m_plan_notice
from tests.daemon_testing import patch_daemon
from tests import test_ace_m_l_execution as LX
from tests.test_ace_m_portfolio import FakeAdapter

NEW = {"portfolio_framework": "ace_m_l"}


class MSignalVersionTests(unittest.TestCase):
    def _signal_for(self, version: str, config: dict):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text(json.dumps({"signals": [{"signal_date": "20261126", "planned_buy_date": "20261127",
                                                     "status": "SIGNAL_READY", "m_version": version,
                                                     "candidates": [{"rank": 1, "ts_code": "300100.SZ"}]}]}),
                            encoding="utf-8")
            with patch_daemon("M_SIGNAL_RECENT_PATH", path), patch_daemon("load_json_config", lambda _p: config):
                return td._m_signal_for_today("20261127")

    def test_new_framework_accepts_ret5_version(self) -> None:
        self.assertIsNotNone(self._signal_for(SM.M_VERSION_RET5, dict(NEW)))
        self.assertIsNone(self._signal_for(SM.M_VERSION, dict(NEW)))

    def test_current_framework_unchanged(self) -> None:
        self.assertIsNotNone(self._signal_for(SM.M_VERSION, {}))
        self.assertIsNone(self._signal_for(SM.M_VERSION_RET5, {}))

    def test_plan_notice_uses_configured_version(self) -> None:
        signal = {"planned_buy_date": "20261127", "status": "SIGNAL_READY", "m_version": SM.M_VERSION_RET5,
                  "candidates": [{"rank": 1, "ts_code": "300100.SZ", "name": "测试"}]}
        text, _ = m_plan_notice(signal, action_date="20261127", ace_active=False, plan={}, positions=[], config=dict(NEW),
                                gate_mode=PM.GATE_FULL, has_m_position=False, framework=PM.FRAMEWORK_ACE_M_L)
        self.assertNotIn("版本不符", text)
        text, _ = m_plan_notice(signal, action_date="20261127", ace_active=False, plan={}, positions=[], config={},
                                gate_mode=PM.GATE_FULL, has_m_position=False)
        self.assertIn("版本不符", text)


class LPlanAmountTests(unittest.TestCase):
    SIGNAL = {"planned_buy_date": "20261127", "l_version": SL.L_VERSION, "status": "SIGNAL_READY",
              "ts_code": "300469.SZ", "name": "信息发展", "l_allowed": True}

    def test_amount_shown_like_ace(self) -> None:
        # 2026-11-27 早盘推送的账户快照：总资产、可用资金 17.05 万，方案甲停手期，连亏降仓 ×0.3，A 有计划（目标 6%）
        plan = {"action_date": "20261127", "final_buy": {"strategy": "A", "name": "旋极信息", "ts_code": "300324.SZ"},
                "live_sizing": {"total_asset": 170500.0, "available_cash": 170500.0, "ace_gate_mode": PM.GATE_REDUCED,
                                "ace_market_value": 0.0, "m_market_value": 0.0, "shares": 2000}}
        text, first = l_plan_notice(self.SIGNAL, occasion="早盘", action_date="20261127", gate_mode=PM.GATE_REDUCED,
                                    has_l_position=False, streak_mult=0.3, ace_codes={"300324.SZ"}, l_version=SL.L_VERSION,
                                    plan=plan, positions=[], config={"live_trade": {"cash_buffer_amount": 1000}})
        self.assertEqual(first, "信息发展")
        self.assertIn("预估开仓约3.07万（占总资产18%，执行前重算）", text)

    def test_no_snapshot_says_pending(self) -> None:
        text, _ = l_plan_notice(self.SIGNAL, occasion="早盘", action_date="20261127", gate_mode=PM.GATE_FULL,
                                has_l_position=False, streak_mult=1.0, ace_codes=set(), l_version=SL.L_VERSION,
                                plan={}, positions=[], config={})
        self.assertIn("预估金额待执行前定仓", text)


class LFillSummaryTests(unittest.TestCase):
    def test_today_case_explains_gap_and_each_slice(self) -> None:
        # 2026-11-27 信息发展：目标 3.07 万、竞价 900 股 @25.87，第一片现价 26.50 超过开盘价 +2% 的上限 26.39
        item = {"ts_code": "300469.SZ", "name": "信息发展", "target_amount": 30690.0, "total_asset": 170500.0,
                "auction_filled_qty": 900, "auction_cost": 900 * 25.87, "filled_qty": 0, "cost_amt": 0.0,
                "open_price": 25.87, "stage": "pov",
                "slices": [{"t": "09:30", "result": "over_chase", "last": 26.50, "cap": 26.39, "pct": 0.02,
                            "limit_up": False},
                           {"t": "09:35", "result": "over_chase", "last": 26.62, "cap": 26.39, "pct": 0.02,
                            "limit_up": False},
                           {"t": "09:40", "result": "small_flow", "flow": 3000.0, "pct": 0.10}]}
        text = buy_l._l_fill_summary(item, "10:30收口")
        self.assertIn("目标3.07万/18.0%", text)
        self.assertIn("竞价成交900股2.33万", text)
        self.assertIn("开盘后补仓0股", text)
        self.assertIn("应买1100股，还差200股约0.52万", text)
        self.assertIn("09:30、09:35现价超过开盘价+2%的上限26.39元（最高26.62元），不追", text)
        self.assertIn("09:40这5分钟成交太少，按成交额10%参与买不到一手", text)
        self.assertTrue(text.endswith("收口：10:30收口。"))

    def test_full_or_no_target(self) -> None:
        item = {"ts_code": "300469.SZ", "name": "信息发展", "target_amount": 30690.0, "total_asset": 170500.0,
                "auction_filled_qty": 1100, "auction_cost": 1100 * 25.87, "open_price": 25.87, "stage": "pov"}
        text = buy_l._l_fill_summary(item, "剩余目标不足最小申报量")
        self.assertIn("已买足", text)
        self.assertNotIn("没补上的原因", text)
        self.assertEqual(buy_l._l_fill_summary({"stage": "select"}, "L首选今日买不进（开盘涨停），不递补"),
                         "L今日未开仓：L首选今日买不进（开盘涨停），不递补。")


class LPovCloseNoticeTests(unittest.TestCase):
    """走一遍：竞价部分成交 → 第一片成交太少 → 第二片超过上限不追 → 第三片部分成交 → 10:30 收口，一定推送且写清楚。"""
    setUp, tearDown = LX.LExecutionTests.setUp, LX.LExecutionTests.tearDown
    _patches, _run = LX.LExecutionTests._patches, LX.LExecutionTests._run
    _quote = staticmethod(LX.LExecutionTests._quote)

    def _at(self, adapter, hh, mm, fn, notes):
        extra = [patch_daemon("_notify", lambda kind, title, body, *a, **k: notes.append((title, body))),
                 patch_daemon("_confirm_fill", lambda *a, **k: SimpleNamespace(filled_qty=300, avg_price=10.1,
                                                                                  is_terminal=True))]
        ps = self._patches(adapter, now=datetime.datetime(2026, 9, 21, hh, mm, 30)) + extra
        for p in ps:
            p.start()
        try:
            return fn()
        finally:
            for p in reversed(ps):
                p.stop()

    def _quote_at(self, last, amount):
        q = self._quote(open_price=10.0, last=last)
        q.amount = amount
        return q

    def test_partial_fill_close_notice(self) -> None:
        adapter = FakeAdapter({"300100.SZ": self._quote()}, {})
        adapter.fills = {"oid1": [SimpleNamespace(filled_qty=1400, avg_price=10.0, is_terminal=True)]}
        notes: list = []
        self._at(adapter, 9, 20, td.job_l_premarket_buy, notes)              # 25% × 10 万 = 2.5 万，限价 11.99
        adapter.quotes["300100.SZ"] = self._quote_at(10.0, 1e6)
        self._at(adapter, 9, 26, td.job_l_after_auction, notes)              # 竞价成交 1400 股 @10.00

        def one_round():
            st = td._l_load_state()
            td._l_pov_round(st, {}, self.config, "20260921")
            td._l_save_state(st)

        for hh, mm, last, amount in ((9, 30, 10.0, 5000), (9, 35, 10.3, 2e6), (9, 40, 10.1, 5e7)):
            adapter.quotes["300100.SZ"] = self._quote_at(last, amount)
            self._at(adapter, hh, mm, one_round, notes)

        def close():
            st = td._l_load_state()
            td._l_pov_finalize(st, "10:30收口")
            td._l_save_state(st)

        notes.clear()
        self._at(adapter, 10, 30, close, notes)
        self.assertEqual(len(notes), 1)
        title, body = notes[0]
        self.assertEqual(title, "✅ L开仓完成")
        self.assertIn("目标2.50万/25.0%", body)
        self.assertIn("竞价成交1400股1.40万", body)
        self.assertIn("开盘后补仓成交300股0.30万", body)
        self.assertIn("累计1700股1.70万/17.0%", body)
        self.assertIn("应买2500股，还差800股约0.80万", body)
        self.assertIn("共3片", body)
        self.assertIn("09:35现价超过开盘价+2%的上限10.20元（最高10.30元），不追", body)
        self.assertIn("09:30这5分钟成交太少，按成交额10%参与买不到一手", body)
        self.assertIn("成交的片：09:40 300股@10.10", body)
        self.assertIn("收口：10:30收口。", body)

    def test_auction_only_still_notifies(self) -> None:
        adapter = FakeAdapter({"300100.SZ": self._quote()}, {})
        adapter.fills = {"oid1": [SimpleNamespace(filled_qty=1400, avg_price=10.0, is_terminal=True)]}
        notes: list = []
        self._at(adapter, 9, 20, td.job_l_premarket_buy, notes)
        adapter.quotes["300100.SZ"] = self._quote_at(10.0, 1e6)
        self._at(adapter, 9, 26, td.job_l_after_auction, notes)

        def close():
            st = td._l_load_state()
            td._l_pov_finalize(st, "10:30收口")
            td._l_save_state(st)

        notes.clear()
        self._at(adapter, 10, 30, close, notes)
        self.assertEqual([t for t, _ in notes], ["ℹ️ L开仓金额校准结束"])
        self.assertIn("还差1100股约1.10万", notes[0][1])
        self.assertIn("开盘后补仓一片都没执行到", notes[0][1])


if __name__ == "__main__":
    unittest.main()
