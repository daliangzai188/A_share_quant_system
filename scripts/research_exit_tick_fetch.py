# -*- coding: utf-8 -*-
"""研究用:卖出日14:55盘口/分钟数据拉取(诊断版+自动降级)。

先试 tick(五档盘口);tick不可得(权限/深度)则自动降级拉 14:45~15:00 的1分钟K。
在 Windows(QMT在线)运行: py -3.11 scripts\\research_exit_tick_fetch.py
输出: data/processed/research_exit_tick_1455.csv (tick成功时)
      data/processed/research_exit_1m_tail.csv  (降级1m时)
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
        exit_d = next_trade(sig, 2)
        if exit_d:
            pairs.append((r.ts_code, exit_d))
    pairs = sorted(set(pairs), key=lambda x: x[1], reverse=True)  # 最近的先试
    print(f"共 {len(pairs)} 笔卖出日。先用最近3笔诊断tick……")

    # ── tick 诊断(最近3笔) ──
    tick_ok = False
    for code, day in pairs[:3]:
        try:
            xtdata.download_history_data(code, period="tick", start_time=day, end_time=day)
            data = xtdata.get_market_data_ex([], [code], period="tick",
                                             start_time=day + "145400", end_time=day + "145700")
            df = data.get(code)
            if df is not None and not df.empty:
                print(f"tick可用!样本 {code} {day}: {len(df)}行, 列={list(df.columns)[:12]}")
                tick_ok = True
                break
            print(f"tick空: {code} {day} (df={type(df).__name__}, empty={df is None or df.empty})")
        except Exception as e:
            print(f"tick异常: {code} {day}: {type(e).__name__}: {e}")

    if tick_ok:
        rows, fails = [], 0
        for i, (code, day) in enumerate(pairs, 1):
            try:
                xtdata.download_history_data(code, period="tick", start_time=day, end_time=day)
                data = xtdata.get_market_data_ex([], [code], period="tick",
                                                 start_time=day + "145430", end_time=day + "145630")
                df = data.get(code)
                if df is None or df.empty:
                    fails += 1
                    continue
                for ts, bar in df.iterrows():
                    bp, bv = bar.get("bidPrice"), bar.get("bidVol")
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
        print(f"tick完成:{len(rows)} 行 → {out} (失败{fails}笔)")
        return

    # ── 降级:1分钟K 14:45~15:00 ──
    print("tick不可得,降级拉1分钟K(14:45~15:00)……")
    rows, fails = [], 0
    for i, (code, day) in enumerate(pairs, 1):
        try:
            xtdata.download_history_data(code, period="1m", start_time=day, end_time=day)
            data = xtdata.get_market_data_ex(
                ["open", "close", "high", "low", "volume", "amount"], [code],
                period="1m", start_time=day + "144500", end_time=day + "150000")
            df = data.get(code)
            if df is None or df.empty:
                fails += 1
                continue
            for ts, bar in df.iterrows():
                rows.append(dict(ts_code=code, exit_date=day, bar_time=str(ts),
                                 open=bar["open"], close=bar["close"], high=bar["high"],
                                 low=bar["low"], volume=bar["volume"], amount=bar["amount"]))
        except Exception:
            fails += 1
        if i % 40 == 0:
            print(f"{i}/{len(pairs)}")
    out = PROJECT_ROOT / "data" / "processed" / "research_exit_1m_tail.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"1m完成:{len(rows)} 行 → {out} (失败{fails}笔,多为超出QMT历史深度)")


if __name__ == "__main__":
    sys.exit(main())
