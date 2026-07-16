# -*- coding: utf-8 -*-
"""研究用:拉取历史交易【买入日】买入日09:30-10:30五分钟数据,重放两段式平滑段真实成交价。

目的:ABC 流动性限仓系数不拍脑袋——竞价额 ≈ 09:31bar成交额 - 后续分钟中位数,
     得到"竞价占日成交额"真实分布,取保守分位 × 参与率上限(10%) = 限仓系数。
在 Windows(QMT在线)运行: py -3.11 scripts\\research_buy_smooth_fetch.py
输出: data/processed/research_buy_smooth_5m.csv
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


def main() -> None:
    from xtquant import xtdata

    audit = pd.read_csv(PROJECT_ROOT / "reports" / "current_live_abce2_audit" / "current_live_abce2_detail.csv")
    filled = audit[audit.operation_status == "HISTORICAL_SIM_FILLED"]
    pairs = []
    for _, r in filled.iterrows():
        buy_d = next_trade(str(int(r.signal_date)), 1)
        if buy_d:
            pairs.append((r.ts_code, buy_d, r.strategy_leg))
    print(f"待下载 {len(pairs)} 笔买入日的开盘1分钟数据……")

    rows, fails = [], 0
    for i, (code, day, leg) in enumerate(pairs, 1):
        try:
            xtdata.download_history_data(code, period="5m", start_time=day, end_time=day)
            data = xtdata.get_market_data_ex(
                ["open", "close", "volume", "amount"], [code],
                period="5m", start_time=day + "093000", end_time=day + "103000",
            )
            df = data.get(code)
            if df is None or df.empty:
                fails += 1
                continue
            for ts, bar in df.iterrows():
                rows.append(dict(ts_code=code, buy_date=day, leg=leg, bar_time=str(ts),
                                 amount=bar["amount"], volume=bar["volume"], close=bar["close"]))
        except Exception:
            fails += 1
        if i % 40 == 0:
            print(f"{i}/{len(pairs)}")

    out = PROJECT_ROOT / "data" / "processed" / "research_buy_smooth_5m.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"完成:{len(rows)} 行 → {out} (失败{fails}笔,多为超出QMT历史深度)")


if __name__ == "__main__":
    sys.exit(main())
