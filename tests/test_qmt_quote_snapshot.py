from __future__ import annotations

import unittest

import pandas as pd

from src.broker_adapter import BrokerConnectionConfig
from src.qmt_adapter import QMTBrokerAdapter


class _FakeXtData:
    def __init__(
        self,
        ticks: dict[str, dict[str, object]],
        minute_bars: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        self._ticks = ticks
        self._minute_bars = minute_bars or {}
        self.subscription_results: dict[str, int] = {}
        self.unsubscribe_failures: set[int] = set()
        self.subscribe_calls: list[dict[str, object]] = []
        self.unsubscribe_calls: list[int] = []
        self.market_data_calls: list[dict[str, object]] = []
        self._next_subscription_id = 1

    def subscribe_quote(
        self,
        stock_code: str,
        *,
        period: str,
        start_time: str,
        end_time: str,
        count: int,
        callback: object,
    ) -> int:
        self.subscribe_calls.append(
            {
                "stock_code": stock_code,
                "period": period,
                "start_time": start_time,
                "end_time": end_time,
                "count": count,
                "callback": callback,
            }
        )
        if stock_code in self.subscription_results:
            return self.subscription_results[stock_code]
        result = self._next_subscription_id
        self._next_subscription_id += 1
        return result

    def unsubscribe_quote(self, subscription_id: int) -> None:
        self.unsubscribe_calls.append(subscription_id)
        if subscription_id in self.unsubscribe_failures:
            raise RuntimeError(f"cannot unsubscribe {subscription_id}")

    def get_full_tick(self, broker_codes: list[str]) -> dict[str, dict[str, object]]:
        return {code: self._ticks.get(code, {}) for code in broker_codes}

    def get_market_data_ex(
        self,
        _fields: list[str],
        broker_codes: list[str],
        **kwargs: object,
    ) -> dict[str, pd.DataFrame]:
        self.market_data_calls.append(
            {"broker_codes": list(broker_codes), **kwargs}
        )
        return {
            code: self._minute_bars.get(code, pd.DataFrame())
            for code in broker_codes
        }


def _adapter_with_ticks(
    ticks: dict[str, dict[str, object]],
    minute_bars: dict[str, pd.DataFrame] | None = None,
) -> QMTBrokerAdapter:
    adapter = QMTBrokerAdapter(
        BrokerConnectionConfig(
            broker_name="qmt",
            account_id="TEST",
            account_type="STOCK",
            qmt_path="",
            session_id=1001,
        )
    )
    adapter.xtdata_module = _FakeXtData(ticks, minute_bars)
    # 设置任意非空值，使懒加载判断直接返回，测试不依赖本机安装 xtquant。
    adapter.xttrader_module = object()
    return adapter


class QMTQuoteSnapshotAmountTest(unittest.TestCase):
    def test_maps_standard_amount_as_cumulative_yuan_and_keeps_raw(self) -> None:
        raw = {
            "lastPrice": 13.66,
            "amount": 12_345_678.9,
            "volume": 912_300,
            "vendorExtra": "kept-for-audit",
        }
        adapter = _adapter_with_ticks({"002800.SZ": raw})

        quote = adapter.get_full_tick(["002800.SZ"])["002800.SZ"]

        self.assertEqual(quote.amount, 12_345_678.9)
        self.assertEqual(quote.last_price, 13.66)
        self.assertEqual(quote.raw, raw)

    def test_maps_common_turnover_alias_when_amount_is_missing(self) -> None:
        adapter = _adapter_with_ticks(
            {
                "600000.SH": {
                    "lastPrice": 10.25,
                    "turnover": "9876543.21",
                }
            }
        )

        quote = adapter.get_full_tick(["600000.SH"])["600000.SH"]

        self.assertEqual(quote.amount, 9_876_543.21)

    def test_prefers_canonical_amount_and_does_not_infer_from_volume(self) -> None:
        adapter = _adapter_with_ticks(
            {
                "000001.SZ": {
                    "lastPrice": 10.0,
                    "amount": 0,
                    "turnover": 999.0,
                    "volume": 100_000,
                },
                "000002.SZ": {
                    "lastPrice": 20.0,
                    "volume": 200_000,
                },
            }
        )

        quotes = adapter.get_full_tick(["000001.SZ", "000002.SZ"])

        self.assertEqual(quotes["000001.SZ"].amount, 0.0)
        self.assertEqual(quotes["000002.SZ"].amount, 0.0)

    def test_normalizes_qmt_minute_bars_without_shifting_research_label(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "open": 196.39,
                    "high": 196.81,
                    "low": 190.30,
                    "close": 193.28,
                    "volume": 10_116,
                    "amount": 198_738_852.0,
                }
            ],
            index=["20260826093100"],
        )
        adapter = _adapter_with_ticks({}, {"301630.SZ": frame})

        bars = adapter.get_minute_bars(
            ["301630.SZ"],
            start_time="20260826093000",
            end_time="20260826093238",
        )["301630.SZ"]

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["hhmm"], 931)
        self.assertEqual(bars[0]["close"], 193.28)
        self.assertEqual(bars[0]["low"], 190.30)

    def test_minute_query_subscribes_once_reuses_ids_and_releases_stale_codes(self) -> None:
        adapter = _adapter_with_ticks({})
        xtdata = adapter.xtdata_module

        adapter.get_minute_bars(
            ["000001.SZ", "000002.SZ"],
            start_time="20260831093000",
            end_time="20260831102700",
        )
        adapter.get_minute_bars(
            ["000001.SZ", "000002.SZ"],
            start_time="20260831093000",
            end_time="20260831102800",
        )

        self.assertEqual(len(xtdata.subscribe_calls), 2)
        self.assertEqual(xtdata.subscribe_calls[0]["period"], "1m")
        self.assertEqual(
            xtdata.subscribe_calls[0]["start_time"], "20260831093000"
        )
        self.assertEqual(xtdata.subscribe_calls[0]["count"], -1)
        self.assertEqual(xtdata.market_data_calls[-1]["fill_data"], True)

        adapter.get_minute_bars(
            ["000002.SZ", "000003.SZ"],
            start_time="20260831093000",
            end_time="20260831102900",
        )

        self.assertEqual(len(xtdata.subscribe_calls), 3)
        self.assertEqual(xtdata.subscribe_calls[-1]["stock_code"], "000003.SZ")
        self.assertEqual(xtdata.unsubscribe_calls, [1])
        self.assertEqual(
            set(adapter._minute_quote_subscriptions),
            {"000002.SZ", "000003.SZ"},
        )

    def test_failed_subscription_rolls_back_new_ids_and_does_not_query(self) -> None:
        adapter = _adapter_with_ticks({})
        xtdata = adapter.xtdata_module
        xtdata.subscription_results["000002.SZ"] = -1

        with self.assertRaisesRegex(RuntimeError, "订阅失败"):
            adapter.get_minute_bars(
                ["000001.SZ", "000002.SZ"],
                start_time="20260831093000",
                end_time="20260831102700",
            )

        self.assertEqual(xtdata.unsubscribe_calls, [1])
        self.assertEqual(adapter._minute_quote_subscriptions, {})
        self.assertEqual(xtdata.market_data_calls, [])

    def test_more_than_fifty_minute_codes_fails_before_partial_subscription(self) -> None:
        adapter = _adapter_with_ticks({})
        xtdata = adapter.xtdata_module
        codes = [f"{index:06d}.SZ" for index in range(51)]

        with self.assertRaisesRegex(RuntimeError, "超过安全上限"):
            adapter.get_minute_bars(
                codes,
                start_time="20260831093000",
                end_time="20260831102700",
            )

        self.assertEqual(xtdata.subscribe_calls, [])
        self.assertEqual(adapter._minute_quote_subscriptions, {})

    def test_missing_unsubscribe_api_fails_before_creating_subscription(self) -> None:
        adapter = _adapter_with_ticks({})
        xtdata = adapter.xtdata_module
        xtdata.unsubscribe_quote = None

        with self.assertRaisesRegex(RuntimeError, "无法释放"):
            adapter.get_minute_bars(
                ["000001.SZ"],
                start_time="20260831093000",
                end_time="20260831102700",
            )

        self.assertEqual(xtdata.subscribe_calls, [])
        self.assertEqual(adapter._minute_quote_subscriptions, {})

    def test_disconnect_releases_all_subscriptions_even_if_one_release_fails(self) -> None:
        adapter = _adapter_with_ticks({})
        xtdata = adapter.xtdata_module
        adapter.get_minute_bars(
            ["000001.SZ", "000002.SZ"],
            start_time="20260831093000",
            end_time="20260831102700",
        )
        xtdata.unsubscribe_failures.add(1)

        class _Trader:
            stopped = False

            def stop(self) -> None:
                self.stopped = True

        trader = _Trader()
        adapter.trader = trader
        adapter.disconnect()

        self.assertEqual(xtdata.unsubscribe_calls, [1, 2])
        self.assertTrue(trader.stopped)
        self.assertEqual(adapter._minute_quote_subscriptions, {"000001.SZ": 1})


if __name__ == "__main__":
    unittest.main()
