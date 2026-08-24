from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import trading_daemon


class QmtPovPriorityTests(unittest.TestCase):
    def tearDown(self) -> None:
        # 失败用例也不能把模块级优先状态泄漏给同进程内后续测试。
        with trading_daemon._buy_pov_qmt_priority_condition:
            trading_daemon._buy_pov_qmt_priority_waiters = 0
            trading_daemon._buy_pov_qmt_priority_active = 0
            trading_daemon._buy_pov_qmt_priority_condition.notify_all()

    def test_d_market_batch_waits_until_buy_pov_releases_priority(self) -> None:
        adapter = SimpleNamespace(get_full_tick=MagicMock(return_value={"000001.SZ": 1}))
        proxy = trading_daemon.SharedQMTBrokerProxy({"adapter": "qmt"})
        finished = threading.Event()
        result: list[dict] = []

        def run_d_batch() -> None:
            result.append(proxy.get_full_tick(["000001.SZ"]))
            finished.set()

        with (
            patch.object(trading_daemon, "_qmt_get", return_value=adapter),
            patch.object(trading_daemon, "logger", return_value=MagicMock()),
        ):
            with trading_daemon._qmt_access("测试POV查询"):
                thread = threading.Thread(target=run_d_batch)
                thread.start()
                time.sleep(0.05)
                self.assertFalse(finished.is_set())
                adapter.get_full_tick.assert_not_called()

            thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual(result, [{"000001.SZ": 1}])
        adapter.get_full_tick.assert_called_once_with(["000001.SZ"])

    def test_d_market_batch_runs_normally_without_buy_pov(self) -> None:
        adapter = SimpleNamespace(get_full_tick=MagicMock(return_value={"000001.SZ": 1}))
        proxy = trading_daemon.SharedQMTBrokerProxy({"adapter": "qmt"})

        with (
            patch.object(trading_daemon, "_qmt_get", return_value=adapter),
            patch.object(trading_daemon, "logger", return_value=MagicMock()),
        ):
            result = proxy.get_full_tick(["000001.SZ"])

        self.assertEqual(result, {"000001.SZ": 1})
        adapter.get_full_tick.assert_called_once_with(["000001.SZ"])

    def test_pov_overtakes_d_already_waiting_for_qmt_lock(self) -> None:
        """D先等锁、POV后登记时，D拿到锁也必须复核并再次让路。"""

        call_order: list[str] = []
        adapter = SimpleNamespace(
            get_full_tick=MagicMock(
                side_effect=lambda _codes: call_order.append("D") or {}
            )
        )
        proxy = trading_daemon.SharedQMTBrokerProxy({"adapter": "qmt"})
        d_done = threading.Event()
        pov_done = threading.Event()

        def run_d_batch() -> None:
            proxy.get_full_tick(["000001.SZ"])
            d_done.set()

        def run_pov_call() -> None:
            with trading_daemon._qmt_access("测试POV插队"):
                call_order.append("POV")
            pov_done.set()

        with (
            patch.object(trading_daemon, "_qmt_get", return_value=adapter),
            patch.object(trading_daemon, "logger", return_value=MagicMock()),
        ):
            trading_daemon._qmt_lock.acquire()
            try:
                d_thread = threading.Thread(target=run_d_batch)
                d_thread.start()
                time.sleep(0.03)
                pov_thread = threading.Thread(target=run_pov_call)
                pov_thread.start()

                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    with trading_daemon._buy_pov_qmt_priority_condition:
                        if trading_daemon._buy_pov_qmt_priority_waiters > 0:
                            break
                    time.sleep(0.005)
                else:
                    self.fail("POV未在超时前登记优先权")
            finally:
                trading_daemon._qmt_lock.release()

            pov_thread.join(timeout=1.0)
            d_thread.join(timeout=1.0)

        self.assertTrue(pov_done.is_set())
        self.assertTrue(d_done.is_set())
        self.assertEqual(call_order, ["POV", "D"])

    def test_nested_buy_pov_priorities_do_not_release_early(self) -> None:
        with patch.object(trading_daemon, "logger", return_value=MagicMock()):
            self.assertFalse(trading_daemon._buy_pov_qmt_priority_pending())
            with trading_daemon._qmt_access("POV外层"):
                self.assertTrue(trading_daemon._buy_pov_qmt_priority_pending())
                with trading_daemon._qmt_access("POV内层"):
                    self.assertTrue(trading_daemon._buy_pov_qmt_priority_pending())
                self.assertTrue(trading_daemon._buy_pov_qmt_priority_pending())
            self.assertFalse(trading_daemon._buy_pov_qmt_priority_pending())


if __name__ == "__main__":
    unittest.main()
