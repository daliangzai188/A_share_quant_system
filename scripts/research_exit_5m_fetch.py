# -*- coding: utf-8 -*-
"""研究用：下载实盘审计199笔交易卖出日的5分钟K线（QMT xtdata行情接口）。

用途：验证"任意盘中时点平仓 vs 收盘平仓"(5分钟粒度,可验证10:15/13:15等)的收益差异。
在 Windows（QMT客户端在线）上运行：
    python scripts/research_exit_30m_fetch.py
只用 xtdata 行情接口，不碰交易 session，对 daemon 无影响，盘后运行即可。
输出：data/processed/research_exit_5m.csv（Mac端直接分析）。
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
        sig = str(int(r.signal_date))
        exit_d = next_trade(sig, 2)  # 全腿规则：信号次日买，买入次日收盘卖
        if exit_d:
            pairs.append((r.ts_code, exit_d, r.strategy_leg))
    print(f"待下载 {len(pairs)} 笔卖出日的5分钟K……")

    rows = []
    fails = []
    for i, (code, day, leg) in enumerate(pairs, 1):
        try:
            xtdata.download_history_data(code, period="5m", start_time=day, end_time=day)
            data = xtdata.get_market_data_ex(
                ["open", "close", "high", "low"], [code],
                period="5m", start_time=day, end_time=day + "150000",
            )
            df = data.get(code)
            if df is None or df.empty:
                fails.append((code, day, "empty"))
                continue
            for ts, bar in df.iterrows():
                rows.append(dict(ts_code=code, exit_date=day, leg=leg, bar_time=str(ts),
                                 open=bar["open"], close=bar["close"],
                                 high=bar["high"], low=bar["low"]))
        except Exception as e:  # noqa: BLE001
            fails.append((code, day, str(e)[:60]))
        if i % 20 == 0:
            print(f"{i}/{len(pairs)}")

    out = PROJECT_ROOT / "data" / "processed" / "research_exit_5m.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"完成：{len(rows)} 根K线 → {out}")
    if fails:
        print(f"失败 {len(fails)} 笔：")
        for f in fails[:10]:
            print(" ", f)


if __name__ == "__main__":
    sys.exit(main())
