"""QMT 一时返回空持仓列表（2026-11-18 实盘问题）。

11-16晚、11-17 09:56、11-18 13:57/14:06/14:16，QMT 的持仓列表一只都没有（短的一轮，长的15:17~15:23约7分钟），账户总资产里却还算着
两只策略持仓（日志：已返回持仓市值=0.00、未解释差额=23800）。以前：账户心跳每段都推“账户快照校验未通过”；
更要紧的是别的流程把空列表当真——15:00:30收盘核对隔2秒两次读到0股会把没卖的到期仓记成已平，14:55平仓会因
“券商无持仓”放弃卖出并打电话。修复后：
- 适配器：持仓一只都没有、账户显示仍持有证券时隔0.5秒重查，还是空就报“结果未知”（不当成空仓）；
- 账户心跳：结果未知按快照不自洽处理（本轮不清仓、不买入、不重连）；连续满10分钟才推送，推过的恢复时再推；
- 14:55平仓碰上结果未知：不打电话，交给看门狗补卖；14:59还读不到才电话报警。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.daemon_testing import patch_daemon  # noqa: F401  先加载守护进程

from src import qmt_adapter as QA
from src.broker_adapter import BrokerConnectionConfig
from src.daemon import close, qmt

HELD = [SimpleNamespace(stock_code="002652.SZ", volume=3000, can_use_volume=3000, open_price=4.43,
                        market_value=12780.0, stock_name="扬子新材"),
        SimpleNamespace(stock_code="301288.SZ", volume=500, can_use_volume=0, open_price=20.92,
                        market_value=11020.0, stock_name="清研环境")]


def asset(market_value=23_800.0, total=88_658.94, cash=64_858.94, frozen=0.0):
    return SimpleNamespace(total_asset=total, available_cash=cash, cash=cash, market_value=market_value,
                           frozen_cash=frozen)


class FakeTrader:
    def __init__(self, position_results, account=None, orders=()):
        self.position_results = list(position_results)
        self.account = account or asset()
        self.orders = list(orders)
        self.position_calls = 0

    def query_stock_positions(self, account):
        self.position_calls += 1
        if len(self.position_results) > 1:
            return self.position_results.pop(0)
        return self.position_results[0]

    def query_stock_asset(self, account):
        return self.account

    def query_stock_orders(self, account):
        return self.orders


def adapter_with(trader):
    adapter = QA.QMTBrokerAdapter(BrokerConnectionConfig("guojin_qmt_inner", "TEST", "STOCK", "", 0))
    adapter.trader = trader
    adapter.account = "TEST"
    return adapter


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        sleeper = patch.object(QA.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_held_positions_come_back_on_recheck(self) -> None:
        trader = FakeTrader([[], HELD])
        positions = adapter_with(trader).query_positions()
        self.assertEqual([p.ts_code for p in positions], ["002652.SZ", "301288.SZ"])
        self.assertEqual(trader.position_calls, 2)
        self.sleep.assert_called_once_with(QA.EMPTY_POSITIONS_RECHECK_SEC)

    def test_empty_list_while_account_holds_securities_is_unknown(self) -> None:
        trader = FakeTrader([[]])
        with self.assertRaises(QA.BrokerPositionsUnknownError) as caught:
            adapter_with(trader).query_positions()
        self.assertIn("结果未知，不等于空仓", str(caught.exception))
        self.assertIn("账户证券市值23800.00元", str(caught.exception))
        self.assertEqual(trader.position_calls, 1 + QA.EMPTY_POSITIONS_RECHECKS)

    def test_truly_empty_account_returns_at_once(self) -> None:
        trader = FakeTrader([[]], account=asset(market_value=0.0, total=64_858.94, cash=64_858.94))
        self.assertEqual(adapter_with(trader).query_positions(), [])
        self.assertEqual(trader.position_calls, 1)
        self.sleep.assert_not_called()

    def test_account_unreadable_keeps_the_old_answer(self) -> None:
        trader = FakeTrader([[]])
        trader.account = None                                       # 账户查询“结果未知”
        self.assertEqual(adapter_with(trader).query_positions(), [])
        self.sleep.assert_not_called()

    def test_money_frozen_by_a_pending_buy_is_not_taken_for_holdings(self) -> None:
        # 早上没有持仓、M 09:20竞价单冻结了1.1万，而 QMT 没给冻结资金字段
        pending = {"order_status": 50, "order_type": 23, "order_volume": 500, "traded_volume": 0}
        trader = FakeTrader([[]], account=asset(market_value=0.0, total=88_658.94, cash=77_658.94), orders=[pending])
        self.assertEqual(adapter_with(trader).query_positions(), [])
        self.sleep.assert_not_called()

    def test_asset_gap_counts_when_no_buy_order_is_pending(self) -> None:
        done = {"order_status": 56, "order_type": 23}
        trader = FakeTrader([[]], account=asset(market_value=0.0), orders=[done])
        with self.assertRaises(QA.BrokerPositionsUnknownError) as caught:
            adapter_with(trader).query_positions()
        self.assertIn("总资产减可用资金和冻结资金23800.00元", str(caught.exception))

    def test_long_outage_never_turns_into_an_empty_account(self) -> None:
        # 11-18 15:17~15:23 一段约7分钟：一直是“结果未知”，不会过几分钟就当成空仓
        adapter = adapter_with(FakeTrader([[]]))
        with patch.object(QA.time, "monotonic", side_effect=[1000.0, 1200.0, 1420.0]):
            for _ in range(3):
                with self.assertRaises(QA.BrokerPositionsUnknownError) as caught:
                    adapter.query_positions()
        self.assertIn("已持续420秒", str(caught.exception))
        adapter.trader.position_results = [HELD]
        self.assertEqual(len(adapter.query_positions()), 2)          # 恢复后计时清零
        self.assertIsNone(adapter._empty_positions_since)


class HeartbeatTests(unittest.TestCase):
    def test_unknown_positions_fail_closed_without_reconnect(self) -> None:
        adapter = SimpleNamespace(query_account=Mock(return_value=asset()),
                                  query_positions=Mock(side_effect=QA.BrokerPositionsUnknownError("空持仓")))
        with self.assertRaises(qmt.BrokerSnapshotInconsistentError):
            qmt._qmt_query_account_positions(adapter)
        self.assertFalse(qmt._is_transient_qmt_read_error(QA.BrokerPositionsUnknownError(
            "QMT持仓查询返回空持仓，但账户证券市值23800.00元（结果未知，不等于空仓）")))

    def test_push_only_after_ten_minutes_and_once_more_on_recovery(self) -> None:
        log = Mock()
        error = qmt.BrokerSnapshotInconsistentError("QMT持仓快照与策略账本/资产不自洽：缺失策略代码=['002652']。本轮禁止")
        clock = [0.0]
        with patch_daemon("_notify", return_value=True) as notify, patch_daemon("write_broker_health"), \
                patch.object(qmt.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(qmt, "_snapshot_bad_since", None), patch.object(qmt, "_snapshot_last_alert", None):
            for second in range(0, 430, 60):                       # 11-18 15:17~15:23 约7分钟一段：不推
                clock[0] = second
                qmt._record_broker_snapshot_inconsistent(error, log, context="账户心跳")
            self.assertEqual(notify.call_count, 0)
            clock[0] = 480.0
            qmt._note_broker_snapshot_ok(log)                       # 没推过：恢复也不推
            self.assertEqual(notify.call_count, 0)

            for second in (1000, 1300, 1600, 1610, 3300, 3410):    # 持续10分钟：推；30分钟内不重复
                clock[0] = second
                qmt._record_broker_snapshot_inconsistent(error, log, context="账户心跳")
            self.assertEqual(notify.call_count, 2)
            title, body = notify.call_args_list[0].args[1:3]
            self.assertEqual(title, "⚠️ 账户快照校验持续未通过")
            self.assertIn("已连续10分钟：QMT持仓快照与策略账本/资产不自洽：缺失策略代码=['002652']", body)
            clock[0] = 3500.0
            qmt._note_broker_snapshot_ok(log)
            self.assertEqual(notify.call_args.args[1], "✅ 账户快照已恢复")
            self.assertIsNone(qmt._snapshot_bad_since)


class CloseTests(unittest.TestCase):
    def test_main_close_hands_over_to_the_watchdog_without_a_phone_call(self) -> None:
        position = {"ts_code": "002652.SZ", "name": "扬子新材", "order_id": "A1", "shares": 3000,
                    "strategy_leg": "E", "planned_exit_date": "20261119"}
        with patch_daemon("_notify") as notify, \
                patch_daemon("_abc_place_sell_order_direct",
                             side_effect=QA.BrokerPositionsUnknownError("QMT持仓查询返回空持仓")), \
                patch_daemon("load_json_config", return_value={"broker": {}, "live_trade": {}}):
            close._do_sell(position, True)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.args[1], "⏳ 平仓暂缓，看门狗补卖")
        self.assertNotIn("call", notify.call_args.kwargs)

    def test_watchdog_calls_once_when_holdings_are_still_unreadable_at_1459(self) -> None:
        import datetime as dt
        due = [{"ts_code": "002652.SZ", "name": "扬子新材", "status": "open", "planned_exit_date": "20261119",
                "shares": 3000, "order_id": "A1"}]
        adapter = SimpleNamespace(query_orders=Mock(return_value=[]),
                                  query_positions=Mock(side_effect=QA.BrokerPositionsUnknownError("空持仓")))
        checkpoints = (dt.time(14, 56), dt.time(14, 57, 5), dt.time(14, 57, 40), dt.time(14, 59))
        fired = set()
        with patch_daemon("_notify") as notify, patch_daemon("load_positions", return_value=due), \
                patch_daemon("_qmt_get", return_value=adapter), patch_daemon("load_json_config", return_value={}), \
                patch.object(close, "_close_unknown_alerted_day", ""):
            close._watchdog_cover_positions(checkpoints, dt.time(14, 57, 10), "20261119", fired, Mock())
            self.assertEqual(notify.call_count, 0)                  # 14:57还会再试，不报
            for second in (0, 10):
                close._watchdog_cover_positions(checkpoints, dt.time(14, 59, second), "20261119", fired, Mock())
        self.assertEqual(notify.call_count, 1)                      # 14:59起一天只打一次
        self.assertEqual(notify.call_args.args[1], "🛑 收盘前仍读不到QMT持仓")
        self.assertIs(notify.call_args.kwargs["call"], True)
        self.assertEqual(fired, set())                              # 没读到就不算核过，15:00前继续重试


if __name__ == "__main__":
    unittest.main()
