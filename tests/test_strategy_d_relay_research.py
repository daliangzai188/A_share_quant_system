from __future__ import annotations

import unittest

import pandas as pd

from scripts.research_strategy_d_relay_capacity import (
    build_capacity_replay,
    build_capacity_replay_from_proxy,
    build_impact_sensitivity,
    infer_book_volume_unit,
    load_portfolio,
    validate_proxy_inputs,
    validate_inputs,
)
from scripts.research_strategy_d_relay_fetch import (
    EXPECTED_RELAY_COUNT,
    complete_one_minute_keys,
    complete_tick_keys,
    load_relay_targets,
    normalize_ticks,
)
from scripts.research_strategy_d_relay_tushare_fetch import (
    build_auction_proxy,
    complete_auction_proxy_keys,
    normalize_tushare_minute,
)


@unittest.skipIf(
    EXPECTED_RELAY_COUNT == 0,
    "D接力已于2026-08-07全关（见 combined_live_engine 顶部「腿序与接力口径」），"
    "组合中不再产生 D→A/C/E2，本研究工具无研究对象。若将来重开接力，"
    "把 research_strategy_d_relay_fetch.EXPECTED_RELAY_COUNT 改回实际笔数，"
    "本测试类会自动恢复。",
)
class StrategyDRelayResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.targets = load_relay_targets()
        cls.portfolio = load_portfolio()

    def test_locked_portfolio_contains_expected_relay_count(self) -> None:
        self.assertEqual(len(self.targets), EXPECTED_RELAY_COUNT)
        self.assertEqual(
            self.targets["strategy_leg"].value_counts().to_dict(),
            {"D→C": 4},
        )
        same_stock = self.targets[
            self.targets["d_ts_code"].eq(self.targets["next_ts_code"])
        ]
        # 2026-08-07 A/C候选修正后同票接力 2→1。
        # 同票接力：20241216 创新医疗 D与接手腿是同一只。
        self.assertEqual(len(same_stock), 1)
        self.assertTrue(self.targets["d_t1_account_return"].notna().all())
        self.assertTrue(self.targets["next_account_return"].notna().all())

    def test_tick_normalizer_expands_virtual_auction_book(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "lastPrice": 10.0,
                    "lastClose": 9.9,
                    "amount": 0.0,
                    "volume": 0,
                    "bidPrice": [10.0, 9.99, 9.98, 9.97, 9.96],
                    "askPrice": [10.0, 10.01, 10.02, 10.03, 10.04],
                    "bidVol": [100_000, 20_000, 0, 0, 0],
                    "askVol": [100_000, 5_000, 0, 0, 0],
                }
            ],
            index=["20240927092300000"],
        )

        normalized = normalize_ticks(
            raw,
            signal_date="20240926",
            relay_date="20240927",
            role="D",
            ts_code="002976.SZ",
        )

        self.assertEqual(normalized.iloc[0]["hhmm"], "0923")
        self.assertEqual(float(normalized.iloc[0]["bid_price_1"]), 10.0)
        self.assertEqual(float(normalized.iloc[0]["ask_volume_2"]), 5_000)

    def test_tick_normalizer_accepts_flat_qmt_fields_and_epoch_time(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    # 2024-09-27 09:23:00 Asia/Shanghai
                    "time": 1_727_400_180_000,
                    "lastPrice": 10.0,
                    "lastClose": 9.9,
                    "volume": 100,
                    "pvolume": 10_000,
                    "bidPrice1": 10.0,
                    "askPrice1": 10.0,
                    "bidVol1": 1_000,
                    "askVol1": 1_000,
                    "bidVol2": 100,
                    "askVol2": 0,
                }
            ]
        )

        normalized = normalize_ticks(
            raw,
            signal_date="20240926",
            relay_date="20240927",
            role="D",
            ts_code="002976.SZ",
        )

        self.assertEqual(normalized.iloc[0]["hhmm"], "0923")
        self.assertEqual(float(normalized.iloc[0]["bid_price_1"]), 10.0)
        self.assertEqual(float(normalized.iloc[0]["pvolume"]), 10_000)
        self.assertEqual(infer_book_volume_unit(normalized), 100)

    def test_book_volume_unit_refuses_ambiguous_ratio(self) -> None:
        with self.assertRaisesRegex(ValueError, "禁止猜测"):
            infer_book_volume_unit(
                pd.DataFrame({"volume": [100], "pvolume": [2_500]})
            )

    def make_complete_inputs(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        tick_rows = []
        one_rows = []
        one_times = pd.date_range("09:30", periods=60, freq="1min").strftime("%H%M")
        for target in self.targets.itertuples(index=False):
            for role, code in (
                ("D", target.d_ts_code),
                ("NEXT", target.next_ts_code),
            ):
                tick_rows.append(
                    {
                        "signal_date": target.signal_date,
                        "relay_date": target.relay_date,
                        "role": role,
                        "ts_code": code,
                        "bar_time": target.relay_date + "092300000",
                        "hhmm": "0923",
                        "bid_price_1": 10.0,
                        "ask_price_1": 10.0,
                        "bid_volume_1": 1_000_000,
                        "ask_volume_1": 1_000_000,
                        "bid_volume_2": 100_000,
                        "ask_volume_2": 0,
                        "pre_close": 10.0,
                    }
                )
                for hhmm in one_times:
                    one_rows.append(
                        {
                            "signal_date": target.signal_date,
                            "relay_date": target.relay_date,
                            "role": role,
                            "ts_code": code,
                            "bar_time": target.relay_date + hhmm + "00000",
                            "hhmm": hhmm,
                            "open": 10.0,
                            "close": 10.0,
                            "high": 10.0,
                            "low": 10.0,
                            "volume": 1_000_000,
                            "amount": 10_000_000.0,
                        }
                    )
        return pd.DataFrame(tick_rows), pd.DataFrame(one_rows)

    def test_complete_data_gate_requires_all_relay_roles(self) -> None:
        # 每笔接力两个角色(D腿+接手腿)，4笔=8。
        tick, one = self.make_complete_inputs()
        self.assertEqual(len(complete_tick_keys(tick)), 8)
        self.assertEqual(len(complete_one_minute_keys(one)), 8)
        validate_inputs(self.targets, tick, one)

        with self.assertRaisesRegex(ValueError, "1分钟数据不完整"):
            validate_inputs(self.targets, tick, one[one["signal_date"] != "20241216"])

    def test_tushare_minute_builds_final_auction_proxy(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "ts_code": "002976.SZ",
                    "trade_time": "2024-09-27 09:30:00",
                    "open": 10.0,
                    "close": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    # 刻意模拟 QMT 常见的“手”口径；代理成交股数必须由成交额/价格推导，
                    # 不能直接把原始 volume 当成股数，否则会少算 100 倍。
                    "vol": 5_000,
                    "amount": 5_000_000.0,
                }
            ]
        )
        normalized = normalize_tushare_minute(
            raw,
            signal_date="20240926",
            relay_date="20240927",
            role="D",
            ts_code="002976.SZ",
        )
        one = pd.concat(
            [
                normalized.assign(
                    signal_date=str(target.signal_date),
                    relay_date=str(target.relay_date),
                    ts_code=str(target.d_ts_code),
                )
                for target in self.targets.itertuples(index=False)
            ],
            ignore_index=True,
        )
        proxy = build_auction_proxy(self.targets, one)

        self.assertEqual(len(complete_auction_proxy_keys(proxy)), 4)
        self.assertTrue(proxy["single_price_proxy"].all())
        self.assertTrue(proxy["unmatched_volume_available"].eq(False).all())
        self.assertTrue(proxy["matched_qty"].eq(500_000).all())
        self.assertTrue(proxy["shares_to_raw_volume_ratio"].eq(100.0).all())

    def test_proxy_gate_and_capacity_replay_are_explicitly_conservative(self) -> None:
        _tick, one = self.make_complete_inputs()
        proxy = pd.DataFrame(
            [
                {
                    "signal_date": target.signal_date,
                    "auction_reference_price": 10.0,
                    "matched_qty": 1_000_000,
                    "matched_amount": 10_000_000.0,
                    "single_price_proxy": True,
                }
                for target in self.targets.itertuples(index=False)
            ]
        )
        validate_proxy_inputs(self.targets, proxy, one)
        detail, _summary = build_capacity_replay_from_proxy(
            self.targets,
            proxy,
            position_amounts=(25_000.0, 5_000_000.0),
            max_auction_participation=0.025,
        )

        self.assertTrue(
            detail["auction_snapshot_source"].eq("TUSHARE_0930_FINAL_PROXY").all()
        )
        self.assertTrue(detail["unmatched_volume_available"].eq(False).all())
        self.assertTrue(
            detail[detail["position_amount"].eq(5_000_000.0)][
                "recommended_action"
            ].eq("PAIRED_POV_REQUIRED").all()
        )

    def test_capacity_replay_keeps_small_and_routes_large_to_paired_pov(self) -> None:
        tick, _one = self.make_complete_inputs()
        detail, summary = build_capacity_replay(
            self.targets,
            tick,
            position_amounts=(25_000.0, 5_000_000.0),
            book_volume_unit=1,
            max_auction_participation=0.05,
            max_sell_unmatched_ratio=0.05,
        )

        small = detail[detail["position_amount"].eq(25_000.0)]
        large = detail[detail["position_amount"].eq(5_000_000.0)]
        self.assertTrue(small["recommended_action"].eq("FULL_AUCTION_RELAY").all())
        self.assertTrue(large["recommended_action"].eq("PAIRED_POV_REQUIRED").all())
        self.assertTrue(small["paired_pov_sell_qty"].eq(0).all())
        self.assertTrue(large["auction_sell_qty"].eq(50_000).all())
        self.assertTrue(large["paired_pov_sell_qty"].gt(0).all())
        self.assertEqual(len(summary), 2)

    def test_large_sell_imbalance_cancels_relay(self) -> None:
        tick, _one = self.make_complete_inputs()
        tick.loc[tick["role"].eq("D"), "ask_volume_2"] = 100_000
        detail, _summary = build_capacity_replay(
            self.targets,
            tick,
            position_amounts=(25_000.0,),
            book_volume_unit=1,
            max_auction_participation=0.05,
            max_sell_unmatched_ratio=0.05,
        )

        self.assertTrue(detail["recommended_action"].eq("CANCEL_RELAY").all())
        self.assertTrue(detail["auction_sell_qty"].eq(0).all())

    def test_one_percent_self_impact_reduces_full_portfolio(self) -> None:
        sensitivity = build_impact_sensitivity(self.targets, self.portfolio)
        one_percent = sensitivity[
            sensitivity["additional_d_price_impact"].eq(0.01)
        ].iloc[0]

        self.assertAlmostEqual(float(one_percent["portfolio_multiple"]), 8087.27, places=1)
        self.assertAlmostEqual(float(one_percent["portfolio_change"]), -0.0315, places=3)


if __name__ == "__main__":
    unittest.main()
