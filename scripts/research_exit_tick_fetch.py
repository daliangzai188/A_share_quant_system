# -*- coding: utf-8 -*-
"""研究用:拉取历史卖出日 14:54:30~14:56:30 的 tick(含五档盘口),
实测 14:55 挂跌停一把梭的瞬时订单簿承接能力(用户质疑:500万单吃几个档?)。

在 Windows(QMT在线)运行: py -3.11 scripts\\research_exit_tick_fetch.py
输出: data/processed/research_exit_tick_1455.csv
每行=一个tick快照: lastPrice + 买一~买五的价格/挂单量。
"""
import csv
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    from xtquant import xtdata

    audit = pd.read_csv(PROJECT_ROOT / "reports" / "current_live_abce2_audit" / "current_live_abce2_detail.csv")
    filled = audit[audit.operation_status == "HISTORICAL_SIM_FILLED"]

    cal = sorted(set(
        r["cal_date"] for r in csv.DictReader(
            open(PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv", encoding="utf-8-sig"))
        if r["is_open"] == "1"
    ))
    import bisect

    def next_trade(d: str, n: int = 1):
        i = bisect.bisect_right(cal, d)
        return cal[i + n - 1] if i + n - 1 < len(cal) else None

    pairs = []
    for _, r in filled.iterrows():
        sig = str(int(r.signal_date))
        exit_d = next_trade(sig, 2)  # 与research_exit_5m_fetch同款:信号次日买,买入次日收盘卖
        if exit_d:
            pairs.append((r.ts_code, exit_d))
    pairs = sorted(set(pairs))
    print(f"待下载 {len(pairs)} 笔卖出日的tick盘口……")

    rows, fails = [], 0
    for i, (code, day) in enumerate(pairs, 1):
        try:
            xtdata.download_history_data(code, period="tick", start_time=day, end_time=day)
            data = xtdata.get_market_data_ex(
                [], [code], period="tick",
                start_time=day + "145430", end_time=day + "145630",
            )
            df = data.get(code)
            if df is None or df.empty:
                fails += 1
                continue
            for ts, bar in df.iterrows():
                bp = bar.get("bidPrice")
                bv = bar.get("bidVol")
                if bp is None or bv is None:
                    continue
                row = dict(ts_code=code, exit_date=day, tick_time=str(ts),
                           last=bar.get("lastPrice", 0.0))
                for k in range(5):
                    row[f"bid{k+1}"] = bp[k] if hasattr(bp, "__len__") and len(bp) > k else 0.0
                    row[f"bidv{k+1}"] = bv[k] if hasattr(bv, "__len__") and len(bv) > k else 0.0
                rows.append(row)
        except Exception:
            fails += 1
        if i % 40 == 0:
            print(f"{i}/{len(pairs)}")

    out = PROJECT_ROOT / "data" / "processed" / "research_exit_tick_1455.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"完成:{len(rows)} 行 → {out} (失败{fails}笔,多为超出QMT tick深度)")


if __name__ == "__main__":
    sys.exit(main())
