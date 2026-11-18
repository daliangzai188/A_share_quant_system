"""ACE 和 M 同一天早上开仓（2026-11-18 实盘问题）。

回测里 ACE 和 M 资金分开，同一天各自在开盘买入。11-18 实盘：M 09:20 先挂竞价单，E 09:24 的竞价单被统一执行通道
“已有一笔未成交买单”拒绝（ACTIVE_BUY_INTENT_EXISTS:M），E 全部改由 09:35 的 POV 买入。修复后：
- 执行通道只在同一资金族（ACE / M）内互斥（tests/test_broker_execution_service.py）；
- E 09:24 竞价单按涨停价冻结，不超过此刻可用资金（M 已冻结的扣掉），多出的交给开盘后 POV，不再整单被券商拒绝；
- M 按现金定仓时，ACE 今天已挂单冻结的钱不再重复预留（否则方案甲允许开仓、A/C 09:20 竞价单冻结后 M 一分钱也分不到）。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import portfolio_ace_m as PM
from src.daemon import buy

ROW = {"ts_code": "002652.SZ", "name": "扬子新材", "round_lot_shares": 3000, "reference_price": 4.40,
       "live_target_amount": 13625.49, "live_min_acceptable_amount": 13212.6, "live_hard_cap_amount": 15328.67,
       "live_equity_snapshot": 68127.0, "live_available_cash_snapshot": 68127.0, "signal_date": "20261117"}


class MBudgetTests(unittest.TestCase):
    def test_frozen_ace_auction_money_is_not_reserved_twice(self) -> None:
        # 方案甲允许开仓：A/C 09:20 竞价单按涨停价冻结了 85%，可用资金只剩 15%
        args = dict(total_asset=100_000, available_cash=15_000, ace_active_today=True, ace_value=0.0,
                    ace_unfilled_target=82_500, cash_buffer=1_000)
        self.assertEqual(PM.m_budget(**args)["amount"], 0.0)                      # 旧算法：M 分不到钱
        fixed = PM.m_budget(**args, ace_frozen_pending=85_000)
        self.assertEqual(fixed["amount"], 14_000)                                  # 可用 15% 扣掉缓冲
        self.assertEqual(fixed["by_total_cap"], 17_000)                            # 总仓位上限照旧给 ACE 留足 82.5%

    def test_nothing_changes_when_ace_has_no_pending_order(self) -> None:
        args = dict(total_asset=68_127, available_cash=68_127, ace_active_today=True, ace_value=0.0,
                    ace_unfilled_target=13_625.49, cash_buffer=1_000)
        self.assertEqual(PM.m_budget(**args), PM.m_budget(**args, ace_frozen_pending=0.0))


class EAuctionCashTests(unittest.TestCase):
    def run_one(self, free_cash):
        quote = SimpleNamespace(amount=0.0, volume=0.0, last_price=4.40)
        adapter = SimpleNamespace(get_full_tick=Mock(return_value={"002652.SZ": quote}))
        with patch.object(buy, "_qmt_get", return_value=adapter), \
                patch.object(buy, "_premarket_buy_price", return_value=(4.84, "涨停价")), \
                patch.object(buy, "_track_execution"), \
                patch.object(buy, "st_open_forbidden", return_value=False), \
                patch.object(buy, "_e_auction_submit") as submit:
            buy._e_auction_buy_one({}, "20261118", False, False, None, dict(ROW), 50_000, [], Mock(), [],
                                   0.10, [], [], free_cash=free_cash)
        return submit.call_args.args[7] if submit.called else None

    def test_auction_order_fits_the_cash_free_at_0924(self) -> None:
        self.assertEqual(self.run_one(None), 3000)        # 11-18：本该整单 3000 股挂进竞价
        self.assertEqual(self.run_one(60_000.0), 3000)    # 钱够：不变
        self.assertEqual(self.run_one(9_000.0), 1800)     # 钱不够：按涨停价只挂得起 1800 股，其余开盘后 POV 补


if __name__ == "__main__":
    unittest.main()
