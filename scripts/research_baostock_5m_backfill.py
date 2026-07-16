# -*- coding: utf-8 -*-
"""用 baostock(免费,5分钟历史约10年)补齐QMT拉不到的两年全样本分钟数据。

QMT免费行情分钟线仅滚动一年(2025-07-16前全empty),非交易所/账户权限限制,
是行情服务商存储深度。baostock 5m 无此限制(无1m;amount单位=元;结束标签,
与QMT同口径)。在Mac直接运行,不依赖Windows/QMT:
    python3 scripts/research_baostock_5m_backfill.py
输出:
    data/processed/research_buy_5m_full.csv   (199笔买入日全天5m)
    data/processed/research_exit_5m_full.csv  (199笔卖出日全天5m)
"""
import csv
import bisect
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent

cal = sorted(set(
    r["cal_date"] for r in csv.DictReader(
        open(PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv", encoding="utf-8-sig"))
    if r["is_open"] == "1"
))


def next_trade(d: str, n: int = 1):
    i = bisect.bisect_right(cal, d)
    return cal[i + n - 1] if i + n - 1 < len(cal) else None


def to_bs(ts_code: str) -> str:
    code, ex = ts_code.split(".")
    return f"{'sh' if ex == 'SH' else 'sz'}.{code}" if ex in ("SH", "SZ") else ""


def main() -> None:
    import baostock as bs

    audit = pd.read_csv(PROJECT_ROOT / "reports" / "current_live_abce2_audit" / "current_live_abce2_detail.csv")
    filled = audit[audit.operation_status == "HISTORICAL_SIM_FILLED"]
    tasks: dict[str, list] = {"buy": [], "exit": []}
    for _, r in filled.iterrows():
        sig = str(int(r.signal_date))
        b = next_trade(sig, 1)
        e = next_trade(sig, 2)
        if b:
            tasks["buy"].append((r.ts_code, b))
        if e:
            tasks["exit"].append((r.ts_code, e))

    lg = bs.login()
    if lg.error_code != "0":
        print("baostock登录失败:", lg.error_msg)
        return

    for kind, pairs in tasks.items():
        pairs = sorted(set(pairs))
        rows, fails = [], 0
        for i, (code, day) in enumerate(pairs, 1):
            bsc = to_bs(code)
            if not bsc:  # 北交所baostock无数据
                fails += 1
                continue
            d = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            try:
                rs = bs.query_history_k_data_plus(
                    bsc, "time,open,high,low,close,volume,amount",
                    start_date=d, end_date=d, frequency="5", adjustflag="3")
                got = 0
                while rs.error_code == "0" and rs.next():
                    t, o, h, lo, c, v, a = rs.get_row_data()
                    rows.append(dict(ts_code=code, trade_date=day, bar_time=t[:14],
                                     open=float(o or 0), high=float(h or 0), low=float(lo or 0),
                                     close=float(c or 0), volume=float(v or 0), amount=float(a or 0)))
                    got += 1
                if got == 0:
                    fails += 1
            except Exception:
                fails += 1
            if i % 40 == 0:
                print(f"[{kind}] {i}/{len(pairs)}", flush=True)
        out = PROJECT_ROOT / "data" / "processed" / f"research_{'buy' if kind == 'buy' else 'exit'}_5m_full.csv"
        pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
        print(f"[{kind}] 完成:{len(rows)}行 → {out} (失败{fails}笔,多为北交所/停牌)", flush=True)
    bs.logout()


if __name__ == "__main__":
    sys.exit(main())
