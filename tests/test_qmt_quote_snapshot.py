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

    def get_full_tick(self, broker_codes: list[str]) -> dict[str, dict[str, object]]:
        return {code: self._ticks.get(code, {}) for code in broker_codes}

    def get_market_data_ex(
        self,
        _fields: list[str],
        broker_codes: list[str],
        **_kwargs: object,
    ) -> dict[str, pd.DataFrame]:
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


if __name__ == "__main__":
    unittest.main()
