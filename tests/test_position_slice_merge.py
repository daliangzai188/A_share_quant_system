"""分片持仓合并的回归测试。

2026-08-05 实盘暴露：同一只票有 09:20 竞价种子单 + 09:30 POV 补单两条记录时，
账户播报按 alias 直接覆盖 map，后一片顶掉前一片，导致用单片买价去算整笔收益。
华之杰实例：200股@46.38 顶掉 4800股@45.47，收益显示 -4.44%，实际 -2.61%。

这不只是显示问题——同一个 buy_price 喂给浮亏告警（-5/-10/-15/-20/-30%），
-15% 及以下会触发 critical 级别加电话呼叫，误报和漏报都可能发生。
"""
from __future__ import annotations

import unittest


def merge_slices(local_positions: list[dict]) -> dict[str, dict]:
    """复刻 trading_daemon 中的分片合并逻辑，用于锁定行为。"""
    merged_by_code: dict[str, dict] = {}
    for lp in local_positions:
        if str(lp.get("status", "")).lower() not in {"open", "sell_pending"}:
            continue
        code = str(lp.get("ts_code", ""))
        try:
            shares = int(float(lp.get("shares", 0) or 0))
            price = float(lp.get("buy_price", 0) or 0)
        except (TypeError, ValueError):
            shares, price = 0, 0.0
        order_id = str(lp.get("order_id", ""))
        if code not in merged_by_code:
            merged = dict(lp)
            merged["_total_shares"] = shares
            merged["_total_cost"] = shares * price
            merged["_slices"] = 1
            merged["_slice_order_ids"] = [order_id] if order_id else []
            merged["notified_loss_thresholds"] = list(
                lp.get("notified_loss_thresholds") or []
            )
            merged_by_code[code] = merged
        else:
            merged = merged_by_code[code]
            merged["_total_shares"] += shares
            merged["_total_cost"] += shares * price
            merged["_slices"] += 1
            if order_id:
                merged["_slice_order_ids"].append(order_id)
            merged["notified_loss_thresholds"] = sorted(
                {
                    str(x)
                    for x in (
                        list(merged.get("notified_loss_thresholds") or [])
                        + list(lp.get("notified_loss_thresholds") or [])
                    )
                },
                key=lambda x: float(x),
                reverse=True,
            )
    for merged in merged_by_code.values():
        if merged["_total_shares"] > 0 and merged["_total_cost"] > 0:
            merged["shares"] = merged["_total_shares"]
            merged["buy_price"] = merged["_total_cost"] / merged["_total_shares"]
    return merged_by_code


class PositionSliceMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        # 2026-08-05 华之杰真实数据
        self.slices = [
            {
                "order_id": "1082178519",
                "ts_code": "603400.SH",
                "name": "华之杰",
                "shares": 4800,
                "buy_price": 45.47,
                "status": "open",
            },
            {
                "order_id": "pov-20260805-603400.SH",
                "ts_code": "603400.SH",
                "name": "华之杰",
                "shares": 200,
                "buy_price": 46.38,
                "status": "open",
            },
        ]

    def test_shares_are_summed(self) -> None:
        merged = merge_slices(self.slices)["603400.SH"]
        self.assertEqual(merged["shares"], 5000)
        self.assertEqual(merged["_slices"], 2)

    def test_cost_is_weighted_not_last_slice(self) -> None:
        """核心回归：必须是加权成本，不能是最后一片的价格。"""
        merged = merge_slices(self.slices)["603400.SH"]
        self.assertAlmostEqual(merged["buy_price"], 45.5064, places=4)
        self.assertNotAlmostEqual(merged["buy_price"], 46.38, places=2)

    def test_cost_is_not_simple_average(self) -> None:
        """分片股数差异大时，简单平均会显著高估成本。"""
        merged = merge_slices(self.slices)["603400.SH"]
        simple_average = (45.47 + 46.38) / 2
        self.assertLess(merged["buy_price"], simple_average - 0.3)

    def test_return_matches_weighted_cost(self) -> None:
        merged = merge_slices(self.slices)["603400.SH"]
        current_price = 44.32
        pnl = current_price / merged["buy_price"] - 1
        self.assertAlmostEqual(pnl, -0.0261, places=4)
        # 旧实现（取最后一片）会算成 -4.44%，误差超过1.8个百分点
        wrong = current_price / 46.38 - 1
        self.assertLess(wrong, pnl - 0.017)

    def test_all_slice_order_ids_kept(self) -> None:
        """告警去重要写回全部分片，只写第一片会让其余片重复推送。"""
        merged = merge_slices(self.slices)["603400.SH"]
        self.assertEqual(
            merged["_slice_order_ids"],
            ["1082178519", "pov-20260805-603400.SH"],
        )

    def test_notified_thresholds_are_unioned(self) -> None:
        """任一分片推送过的档位都算已推送，避免合并后重复告警。"""
        self.slices[0]["notified_loss_thresholds"] = ["-5"]
        self.slices[1]["notified_loss_thresholds"] = ["-10"]
        merged = merge_slices(self.slices)["603400.SH"]
        self.assertEqual(set(merged["notified_loss_thresholds"]), {"-5", "-10"})

    def test_closed_slices_excluded(self) -> None:
        self.slices[1]["status"] = "closed"
        merged = merge_slices(self.slices)["603400.SH"]
        self.assertEqual(merged["shares"], 4800)
        self.assertAlmostEqual(merged["buy_price"], 45.47, places=2)

    def test_single_slice_unchanged(self) -> None:
        merged = merge_slices([self.slices[0]])["603400.SH"]
        self.assertEqual(merged["shares"], 4800)
        self.assertAlmostEqual(merged["buy_price"], 45.47, places=2)
        self.assertEqual(merged["_slices"], 1)


if __name__ == "__main__":
    unittest.main()
