from __future__ import annotations

import unittest
import sys
from types import ModuleType
from types import SimpleNamespace

if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from src.broker_adapter import BrokerConnectionConfig
from src.qmt_adapter import QMTBrokerAdapter


class _FakeTrader:
    def __init__(
        self,
        *,
        account=(),
        positions=(),
        orders=(),
        trades=(),
    ) -> None:
        self.account_result = account
        self.positions_result = positions
        self.orders_result = orders
        self.trades_result = trades

    def query_stock_asset(self, _account):
        return self.account_result

    def query_stock_positions(self, _account):
        return self.positions_result

    def query_stock_orders(self, _account):
        return self.orders_result

    def query_stock_trades(self, _account):
        return self.trades_result


class _FakeXtData:
    def __init__(self, result) -> None:
        self.result = result

    def get_full_tick(self, _codes):
        return self.result


def _adapter(trader: _FakeTrader) -> QMTBrokerAdapter:
    result = QMTBrokerAdapter(
        BrokerConnectionConfig(
            broker_name="qmt",
            account_id="TEST",
            account_type="STOCK",
            qmt_path="",
            session_id=1001,
        )
    )
    result.trader = trader
    result.account = object()
    return result


class QMTQueryResultSemanticsTest(unittest.TestCase):
    def test_none_position_result_is_error_not_empty_position(self) -> None:
        adapter = _adapter(_FakeTrader(positions=None))

        with self.assertRaisesRegex(RuntimeError, "不等于空仓"):
            adapter.query_positions()

    def test_explicit_empty_position_list_remains_valid_empty_position(self) -> None:
        adapter = _adapter(_FakeTrader(positions=[]))

        self.assertEqual(adapter.query_positions(), [])

    def test_none_account_result_is_error_not_zero_asset_account(self) -> None:
        adapter = _adapter(_FakeTrader(account=None))

        with self.assertRaisesRegex(RuntimeError, "结果未知"):
            adapter.query_account()

    def test_valid_account_object_is_still_normalized(self) -> None:
        raw = SimpleNamespace(
            cash=50_000.0,
            available_cash=49_000.0,
            total_asset=280_000.0,
            market_value=230_000.0,
        )
        adapter = _adapter(_FakeTrader(account=raw))

        account = adapter.query_account()

        self.assertEqual(account.available_cash, 49_000.0)
        self.assertEqual(account.total_asset, 280_000.0)

    def test_none_order_and_trade_results_are_unknown_not_empty(self) -> None:
        adapter = _adapter(_FakeTrader(orders=None, trades=None))

        with self.assertRaisesRegex(RuntimeError, "不等于无委托"):
            adapter.query_orders()
        with self.assertRaisesRegex(RuntimeError, "不等于无成交"):
            adapter.query_trades()

    def test_none_quote_result_is_unknown_not_empty_quote(self) -> None:
        adapter = _adapter(_FakeTrader())
        adapter.xttrader_module = object()
        adapter.xtdata_module = _FakeXtData(None)

        with self.assertRaisesRegex(RuntimeError, "不等于无行情"):
            adapter.get_full_tick(["603118.SH"])


if __name__ == "__main__":
    unittest.main()
