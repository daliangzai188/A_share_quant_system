"""ACE+M+L 新框架（2026-11-26）实盘执行：L 定仓与下单、M 给 L 让钱、连亏降仓、资金族、组合状态机。

现行框架（portfolio_framework 缺省 = ace_m）下 L 一律不执行、M 与 ACE 的数字与原来逐位相同；
新框架下的仓位数字与 src/ace_m_replay.py 的 replay_ace_m_l 同一口径。
"""
from __future__ import annotations

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pandas as pd

if "dotenv" not in sys.modules:
    stub = ModuleType("dotenv")
    stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = stub

from scripts import trading_daemon as td
from tests.daemon_testing import patch_daemon
from src import portfolio_ace_m as PM
from src import strategy_l as SL
from src import strategy_m as SM
from src.rolling_signal_store import save_recent_signal
from tests.test_ace_m_portfolio import FakeAdapter
from tests.test_equity_curve_stop import make_engine

NEW = {"portfolio_framework": "ace_m_l"}


class FrameworkRuleTests(unittest.TestCase):
    def test_fund_family(self) -> None:
        self.assertEqual([PM.fund_family(x) for x in ("A", "c", "E", "D", "M", "l", "B", "")],
                         ["ACE", "ACE", "ACE", "ACE", "M", "L", "ACE", "ACE"])

    def test_m_budget_new_framework_matches_replay(self) -> None:
        # 回放：weight = min(82.5%, 99.5% − L已用) × 连亏倍数；L 已用 18%、连亏 ×0.3 → 0.815×0.3
        b = PM.m_budget(total_asset=100_000, available_cash=100_000, ace_active_today=False, ace_value=0.0,
                        ace_unfilled_target=0.0, cash_buffer=0.0, framework=PM.FRAMEWORK_ACE_M_L,
                        l_value=18_000.0, streak_mult=0.3)
        self.assertAlmostEqual(b["pct"], (0.995 - 0.18) * 0.3)
        # ACE 今日有计划或持仓：新框架 M 不开
        b = PM.m_budget(total_asset=100_000, available_cash=100_000, ace_active_today=True, ace_value=0.0,
                        ace_unfilled_target=0.0, cash_buffer=0.0, framework=PM.FRAMEWORK_ACE_M_L)
        self.assertEqual(b["amount"], 0.0)
        # L 今日目标尚未成交：总仓位和现金都给 L 留着（已冻结的部分不重复扣现金）
        b = PM.m_budget(total_asset=100_000, available_cash=76_000, ace_active_today=False, ace_value=0.0,
                        ace_unfilled_target=0.0, cash_buffer=1000.0, framework=PM.FRAMEWORK_ACE_M_L,
                        l_unfilled_target=25_000.0, l_frozen_pending=24_000.0)
        self.assertAlmostEqual(b["by_total_cap"], 74_500.0)
        self.assertAlmostEqual(b["by_cash"], 76_000 - 1000 - 1000)

    def test_current_framework_m_budget_unchanged(self) -> None:
        kw = dict(total_asset=123_456.0, available_cash=50_000.0, ace_active_today=True, ace_value=60_000.0,
                  ace_unfilled_target=1_000.0, cash_buffer=1000.0, ace_frozen_pending=500.0)
        self.assertEqual(PM.m_budget(**kw), PM.m_budget(**kw, framework=PM.FRAMEWORK_ACE_M, l_value=0.0,
                                                        l_unfilled_target=0.0, l_frozen_pending=0.0, streak_mult=1.0))

    def test_l_target_pct(self) -> None:
        self.assertAlmostEqual(PM.l_target_pct(ace_mode=PM.GATE_FULL, l_self_allowed=True), 0.25)
        self.assertAlmostEqual(PM.l_target_pct(ace_mode=PM.GATE_REDUCED, l_self_allowed=True), 0.60)
        self.assertAlmostEqual(PM.l_target_pct(ace_mode=PM.GATE_FULL, l_self_allowed=False), 0.60 * 20 / 82.5)
        self.assertAlmostEqual(PM.l_target_pct(ace_mode=PM.GATE_REDUCED, l_self_allowed=True, streak_mult=0.3), 0.18)


class ClosedTradeReturnTests(unittest.TestCase):
    def _pos(self, leg, code, buy_date, sell_date, qty, buy, sell, oid, status="closed"):
        return {"strategy_leg": leg, "ts_code": code, "buy_date": buy_date, "sell_date": sell_date, "shares": 0,
                "entry_shares": qty, "buy_price": buy, "sell_price": sell, "order_id": oid, "status": status}

    def test_split_fills_are_one_trade_and_order_follows_replay(self) -> None:
        positions = [
            self._pos("A", "000001.SZ", "20260901", "20260903", 1000, 10.0, 9.0, "a1"),   # 竞价分片
            self._pos("A", "000001.SZ", "20260901", "20260903", 500, 10.2, 9.0, "a2"),    # POV 分片
            self._pos("M", "300001.SZ", "20260902", "20260903", 100, 20.0, 22.0, "m1"),
            self._pos("L", "300002.SZ", "20260902", "20260903", 100, 30.0, 29.0, "l1"),
            self._pos("C", "000002.SZ", "20260904", "", 100, 10.0, 0.0, "c1", status="open"),  # 未平仓不算
            self._pos("E", "000003.SZ", "20260905", "20260908", 100, 10.0, 11.0, "e1"),  # 卖出日不早于当天不算
        ]
        with patch_daemon("load_positions", lambda: positions):
            out = td.closed_trade_net_returns("20260908", commission_rate=0.0003, transfer_rate=0.00001,
                                              minimum_commission=5.0)
        self.assertEqual(len(out), 3)
        self.assertLess(out[0], 0)          # A（合并 1500 股）先，L 次之（同卖出日、L 买入日更晚）……
        self.assertLess(out[1], 0)          # 买入日 0902：ACE→L→M，L 在 M 前
        self.assertGreater(out[2], 0)
        g_buy, g_sell = 1000 * 10.0 + 500 * 10.2, 1500 * 9.0
        cost = g_buy + max(5.0, g_buy * 0.0003) + g_buy * 0.00001
        proceeds = g_sell - max(5.0, g_sell * 0.0003) - g_sell * 0.00001 - g_sell * 0.0005
        self.assertAlmostEqual(out[0], proceeds / cost - 1)

    def test_streak_multiplier_only_in_new_framework(self) -> None:
        positions = [self._pos("A", "000001.SZ", "20260901", "20260902", 100, 10.0, 9.0, "a1"),
                     self._pos("M", "300001.SZ", "20260903", "20260904", 100, 10.0, 9.0, "m1")]
        with patch_daemon("load_positions", lambda: positions):
            self.assertEqual(td.loss_streak_multiplier_for("20260907", {}), 1.0)
            self.assertEqual(td.loss_streak_multiplier_for("20260907", dict(NEW)), PM.LOSS_STREAK_MULTIPLIER)
            self.assertEqual(td.loss_streak_multiplier_for("20260904", dict(NEW)), 1.0)   # 卖出日当天还不算


class LExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.l_state = Path(self.tmp.name) / "l_execution_state.json"
        self.m_state = Path(self.tmp.name) / "m_execution_state.json"
        self.signal = {"signal_date": "20260918", "planned_buy_date": "20260921", "status": "SIGNAL_READY",
                       "strategy_leg": "L", "l_version": SL.L_VERSION, "ts_code": "300100.SZ", "name": "龙一",
                       "l_allowed": True}
        self.m_signal = {"signal_date": "20260918", "planned_buy_date": "20260921", "status": "SIGNAL_READY",
                         "m_version": SM.M_VERSION,
                         "candidates": [{"rank": 1, "ts_code": "300100.SZ", "name": "龙一"},
                                        {"rank": 2, "ts_code": "300200.SZ", "name": "测试二"}]}
        self.recorded: list[dict] = []
        self.config = {"trade_mode": "live", "broker_adapter_enabled": True, "qmt_enabled": True, "broker": {},
                       "live_trade": {"cash_buffer_amount": 1000, "pov_chase_cap": 0.02, "pov_participation": 0.10},
                       **NEW}
        self.mode = PM.GATE_FULL
        self.streak = 1.0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patches(self, adapter, *, now, decisions=None, positions=None):
        tz = td.BEIJING_TZ
        return [
            patch_daemon("L_EXEC_STATE_PATH", self.l_state),
            patch_daemon("M_EXEC_STATE_PATH", self.m_state),
            patch_daemon("load_json_config", lambda _p: self.config),
            patch_daemon("_qmt_get", lambda _cfg: adapter),
            patch_daemon("_l_signal_for_today", lambda _d: self.signal),
            patch_daemon("_m_signal_for_today", lambda _d: self.m_signal),
            patch_daemon("read_cached_combined_decisions",
                         lambda: (decisions, None) if decisions is not None else None),
            patch_daemon("load_positions", lambda: list(positions or [])),
            patch_daemon("load_pending_buys", lambda: []),
            patch_daemon("now_beijing", lambda: now.replace(tzinfo=tz)),
            patch_daemon("today_beijing", lambda: now.date()),
            patch_daemon("_ace_gate_mode_for", lambda _d="": self.mode),
            patch_daemon("loss_streak_multiplier_for", lambda _d, _c=None: self.streak),
            patch_daemon("_l_exit_date", lambda _d: "20260922"),
            patch_daemon("_m_exit_date", lambda _d: "20260922"),
            patch_daemon("_notify", lambda *a, **k: None),
            patch_daemon("_track_execution", lambda *a, **k: None),
            patch_daemon("_record_live_buy", lambda **kw: self.recorded.append(kw)),
            patch_daemon("_l_start_pov_worker", lambda: None),
            patch_daemon("_m_start_pov_worker", lambda: None),
            patch("src.live_order_gateway.LiveOrderGateway.assert_real_order_allowed", lambda *a, **k: None),
        ]

    def _run(self, fn, adapter, **kw):
        ps = self._patches(adapter, **kw)
        for p in ps:
            p.start()
        try:
            return fn()
        finally:
            for p in reversed(ps):
                p.stop()

    @staticmethod
    def _quote(*, open_price=0.0, last=0.0, upper=12.0):
        return SimpleNamespace(open_price=open_price, last_price=last, upper_limit=upper, pre_close=10.0,
                               suspended=False, amount=5e7, ask_prices=[last] if last else None)

    def _adapter(self, **quote):
        return FakeAdapter({"300100.SZ": self._quote(**quote), "300200.SZ": self._quote()}, {})

    def _premarket(self, adapter, **kw):
        self._run(td.job_l_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 30), **kw)

    def test_current_framework_never_runs_l(self) -> None:
        self.config.pop("portfolio_framework")
        adapter = self._adapter()
        self._premarket(adapter)
        self._run(td.job_l_after_auction, adapter, now=datetime.datetime(2026, 9, 21, 9, 26, 5))
        self.assertEqual(adapter.orders, [])
        self.assertFalse(self.l_state.exists())

    def test_ace_normal_uses_25pct_at_limit_minus_one_tick(self) -> None:
        adapter = self._adapter()
        self._premarket(adapter)
        self.assertEqual(len(adapter.orders), 1)
        order = adapter.orders[0]
        self.assertEqual((order.ts_code, order.strategy_leg, order.price), ("300100.SZ", "L", 11.99))
        self.assertEqual(order.quantity, 2000)                        # 25,000 / 11.99 → 2000 股
        self.assertEqual(order.metadata["planned_exit_date"], "20260922")

    def test_ace_halted_60pct_self_stop_and_streak(self) -> None:
        cases = [(PM.GATE_REDUCED, True, 1.0, 5000),                  # 60,000 / 11.99 → 5000 股
                 (PM.GATE_FULL, False, 1.0, 1200),                    # 25% 与 60%×20/82.5=14.5% 取小 → 1200 股
                 (PM.GATE_REDUCED, True, 0.3, 1500)]                  # 60%×0.3=18,000 → 1500 股
        for mode, allowed, streak, qty in cases:
            with self.subTest(mode=mode, allowed=allowed, streak=streak):
                self.l_state = Path(self.tmp.name) / ("l_state_%s_%s_%s.json" % (mode, allowed, streak))
                self.mode, self.signal["l_allowed"], self.streak = mode, allowed, streak
                adapter = self._adapter()
                self._premarket(adapter)
                self.assertEqual(adapter.orders[0].quantity, qty)

    def test_ace_same_stock_or_l_holding_means_no_l_today(self) -> None:
        decisions = pd.DataFrame([{"action": "ALLOW_ABC_BUY_PREVIEW", "strategy_leg": "A", "side": "BUY",
                                   "ts_code": "300100.SZ"}])
        adapter = self._adapter()
        self._premarket(adapter, decisions=decisions)
        self.assertEqual(adapter.orders, [])                          # 首选与 ACE 同票，不递补
        held = [{"strategy_leg": "L", "status": "open", "ts_code": "300009.SZ", "buy_date": "20260918",
                 "shares": 100, "buy_price": 10.0}]
        self._premarket(adapter, positions=held)
        self.assertEqual(adapter.orders, [])

    def test_ace_plan_reserves_cash_for_ace_first(self) -> None:
        decisions = pd.DataFrame([{"action": "ALLOW_ABC_BUY_PREVIEW", "strategy_leg": "A", "side": "BUY",
                                   "ts_code": "000001.SZ"}])
        adapter = self._adapter()
        self._premarket(adapter, decisions=decisions)
        # ACE 82.5% 目标先留着：现金 100,000 − 缓冲 1,000 − 82,500 = 16,500（比 99.5%−82.5%=17,000 小）→ 1300 股
        self.assertEqual(adapter.orders[0].quantity, 1300)

    def test_auction_fill_recorded_as_l_with_next_day_close(self) -> None:
        adapter = self._adapter(open_price=10.5, last=10.5)
        adapter.fills = {"oid1": [SimpleNamespace(filled_qty=2000, avg_price=10.5, is_terminal=True)]}
        self._premarket(adapter)
        self._run(td.job_l_after_auction, adapter, now=datetime.datetime(2026, 9, 21, 9, 26, 5))
        self.assertEqual(len(self.recorded), 1)
        rec = self.recorded[0]
        self.assertEqual((rec["strategy_leg"], rec["shares"], rec["planned_exit_date_override"]),
                         ("L", 2000, "20260922"))
        self.assertEqual(adapter.cancelled, [])

    def test_open_limit_up_means_no_buy_and_no_substitute(self) -> None:
        adapter = self._adapter(open_price=12.0, last=12.0)
        self._premarket(adapter)
        self._run(td.job_l_after_auction, adapter, now=datetime.datetime(2026, 9, 21, 9, 26, 5))
        self.assertIn("oid1", adapter.cancelled)
        ps = self._patches(adapter, now=datetime.datetime(2026, 9, 21, 9, 30, 30))
        for p in ps:
            p.start()
        try:
            st = td._l_load_state()
            td._l_pov_round(st, {}, self.config, "20260921")
        finally:
            for p in reversed(ps):
                p.stop()
        self.assertTrue(st["items"][0]["done"])
        self.assertIn("不递补", st["items"][0]["done_reason"])
        self.assertEqual(len(adapter.orders), 1)                     # 只有那张被撤的竞价单

    def test_l_commitment_and_m_yields_to_l(self) -> None:
        adapter = self._adapter()
        self._premarket(adapter)
        cm = self._run(lambda: td.l_commitment_for_m("20260921", []), adapter,
                       now=datetime.datetime(2026, 9, 21, 9, 20, 40))
        self.assertAlmostEqual(cm["unfilled"], 25_000.0)
        self.assertAlmostEqual(cm["frozen"], 2000 * 11.99)
        self.assertIn("300100.SZ", cm["codes"])
        self._run(td.job_m_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 45))
        m_order = adapter.orders[-1]
        self.assertEqual((m_order.ts_code, m_order.strategy_leg), ("300200.SZ", "M"))   # L 的票 M 不买
        self.assertEqual(m_order.quantity, 6200)                     # min(82.5%, 99.5%−25%)=74,500 → 6200 股
        m_item = json.loads(self.m_state.read_text(encoding="utf-8"))["items"][0]
        self.assertAlmostEqual(m_item["l_reserved"], 25_000.0)
        # L 收口没买到：M 把预留拿回来，目标按 82.5% 重算
        st = json.loads(self.l_state.read_text(encoding="utf-8"))
        st["items"][0].update({"done": True, "finalized": True})
        self.l_state.write_text(json.dumps(st), encoding="utf-8")
        ps = self._patches(adapter, now=datetime.datetime(2026, 9, 21, 9, 35, 10))
        for p in ps:
            p.start()
        try:
            td._m_refresh_target_after_l(m_item, {}, self.config, "20260921")
        finally:
            for p in reversed(ps):
                p.stop()
        self.assertAlmostEqual(m_item["target_amount"], 82_500.0)
        self.assertEqual(m_item["l_reserved"], 0.0)

    def test_m_does_not_open_beside_ace_in_new_framework(self) -> None:
        decisions = pd.DataFrame([{"action": "ALLOW_ABC_BUY_PREVIEW", "strategy_leg": "A", "side": "BUY",
                                   "ts_code": "000001.SZ"}])
        self.signal = None
        adapter = self._adapter()
        self._run(td.job_m_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 45), decisions=decisions)
        self.assertEqual(adapter.orders, [])
        # 现行框架同样情形 M 仍用闲置资金开仓（与原来相同）
        self.config.pop("portfolio_framework")
        self._run(td.job_m_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 45), decisions=decisions)
        self.assertEqual(len(adapter.orders), 1)

    def test_scheduler_order_ace_then_l_then_m(self) -> None:
        calls: list[str] = []
        with patch_daemon("is_trade_day", lambda _d: True), \
                patch_daemon("job_morning", lambda: calls.append("morning")), \
                patch_daemon("job_premarket_buy", lambda: calls.append("ACE")), \
                patch_daemon("job_l_premarket_buy", lambda: calls.append("L")), \
                patch_daemon("job_m_premarket_buy", lambda: calls.append("M")), \
                patch_daemon("job_premarket_position_sync", lambda: calls.append("sync")), \
                patch_daemon("job_l_after_auction", lambda: calls.append("L26")), \
                patch_daemon("job_m_after_auction", lambda: calls.append("M26")):
            td.run_job(td.SCHED_MORNING_REVIEW)
            td.run_job(td.SCHED_PREMARKET_SYNC)
        self.assertEqual(calls, ["morning", "ACE", "L", "M", "sync", "L26", "M26"])


class CombinedEngineLTests(unittest.TestCase):
    def _engine(self, *, positions=None, ac_leg=None, framework="ace_m_l"):
        engine = make_engine(allowed=True, positions=positions, ac_leg=ac_leg)
        engine.equity_curve_stop_check = lambda _today: SimpleNamespace(
            allowed=True, reason="正常开仓", source="test", halt_valid=False)
        engine.has_m_plan_today = lambda _today: False
        if framework:
            engine.config["portfolio_framework"] = framework
        return engine

    def test_l_plan_blocks_d_only_in_new_framework(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_recent_signal(root / "reports" / "strategy_l" / "l_signals_recent.json",
                               {"signal_date": "20260918", "planned_buy_date": "20260921", "status": "SIGNAL_READY",
                                "ts_code": "300100.SZ"}, strategy_leg="L")
            engine = self._engine()
            engine.project_root = root
            _s, decisions, _o = engine.build_mode1_plan("20260921")
            self.assertNotIn("ALLOW_D_INTRADAY_MONITOR", set(decisions["action"]))
            self.assertTrue(any("M/L" in str(r) for r in decisions["reason"]))
            engine = self._engine(framework=None)
            engine.project_root = root
            _s, decisions, _o = engine.build_mode1_plan("20260921")
            self.assertIn("ALLOW_D_INTRADAY_MONITOR", set(decisions["action"]))

    def test_l_position_does_not_block_ace_but_blocks_d(self) -> None:
        l_pos = {"strategy_leg": "L", "status": "open", "ts_code": "300001.SZ", "planned_exit_date": "20260921",
                 "shares": 100}
        engine = self._engine(positions=[l_pos], ac_leg="A")
        engine.has_l_plan_today = lambda _today: False
        _s, decisions, _o = engine.build_mode1_plan("20260921")
        self.assertIn("ALLOW_ABC_BUY_PREVIEW", set(decisions["action"]))
        engine = self._engine(positions=[l_pos])
        engine.has_l_plan_today = lambda _today: False
        _s, decisions, _o = engine.build_mode1_plan("20260921")
        self.assertNotIn("ALLOW_D_INTRADAY_MONITOR", set(decisions["action"]))


if __name__ == "__main__":
    unittest.main()
