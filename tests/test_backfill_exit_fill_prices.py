from __future__ import annotations

import copy
import unittest

from scripts.backfill_exit_fill_prices import (
    apply_backfill,
    build_backfill_plan,
    parse_fill_line,
)


class ExitFillBackfillTest(unittest.TestCase):
    def test_parse_pov_and_watchdog_lines(self) -> None:
        pov = parse_fill_line(
            "2026-08-03 14:49:13 | [卖出POV] 300894.SZ 挂300股@11.17 成300股@11.19 剩19700股"
        )
        watchdog = parse_fill_line(
            "2026-08-04 15:00:31 | WARNING | [平仓看门狗] 300996.SZ "
            "补挂确认成交2500股 均价18.03；按broker_order_id幂等回写0股"
        )
        self.assertIsNotNone(pov)
        self.assertIsNotNone(watchdog)
        self.assertEqual((pov.trade_date, pov.quantity, pov.price), ("20260803", 300, 11.19))
        self.assertEqual(
            (watchdog.ts_code, watchdog.quantity, watchdog.price),
            ("300996.SZ", 2_500, 18.03),
        )

    def test_only_exact_quantity_match_is_ready(self) -> None:
        positions = [
            {
                "order_id": "LOCAL-1",
                "ts_code": "300996.SZ",
                "name": "普联软件",
                "entry_shares": 2_500,
                "shares": 2_500,
                "status": "closed",
                "sell_date": "20260804",
                "sell_price": 0,
            }
        ]
        event = parse_fill_line(
            "2026-08-04 15:00:31 | WARNING | [平仓看门狗] 300996.SZ "
            "补挂确认成交2500股 均价18.03"
        )
        ready, unresolved = build_backfill_plan(positions, [event])
        self.assertEqual(len(ready), 1)
        self.assertEqual(unresolved, [])

        mismatched = copy.deepcopy(event)
        object.__setattr__(mismatched, "quantity", 2_400)
        ready, unresolved = build_backfill_plan(positions, [mismatched])
        self.assertEqual(ready, [])
        self.assertIn("股数不一致", unresolved[0]["reason"])

    def test_apply_sets_zero_remaining_and_weighted_ledger(self) -> None:
        positions = [
            {
                "order_id": "LOCAL-1",
                "ts_code": "603400.SH",
                "entry_shares": 4_800,
                "shares": 4_800,
                "status": "closed",
                "sell_date": "20260807",
                "sell_price": 0,
            },
            {
                "order_id": "LOCAL-2",
                "ts_code": "603400.SH",
                "entry_shares": 200,
                "shares": 200,
                "status": "closed",
                "sell_date": "20260807",
                "sell_price": 0,
            },
        ]
        events = [
            parse_fill_line(
                "2026-08-07 15:00:31 | [平仓看门狗] 603400.SH 补挂确认成交4800股 均价43.48"
            ),
            parse_fill_line(
                "2026-08-07 15:00:31 | [平仓看门狗] 603400.SH 补挂确认成交200股 均价43.48"
            ),
        ]
        ready, unresolved = build_backfill_plan(positions, events)
        self.assertEqual(unresolved, [])
        apply_backfill(positions, ready, applied_at="2026-08-10T10:00:00+08:00")
        self.assertEqual(sum(row["shares"] for row in positions), 0)
        self.assertTrue(all(row["sell_price"] == 43.48 for row in positions))
        self.assertEqual(
            sum(row["exit_fills_by_date"]["20260807"]["qty"] for row in positions),
            5_000,
        )


if __name__ == "__main__":
    unittest.main()
