# -*- coding: utf-8 -*-
"""实盘-回测逐笔执行对账(收盘流水线末步,2026-07-17 用户拍板落地)。

对每笔已平仓实盘持仓,对照回测基准价计算执行损耗:
  买入损耗 = 实际买入均价 / 买入日开盘价 - 1   (回测口径=开盘价买)
  卖出损耗 = 1 - 实际卖出均价 / 卖出日收盘价   (回测口径=收盘价卖)
幂等按 order_id 增量入账;有新平仓即推送逐笔损耗;每周五推累计周报
(对照重放模型预期:16万级约0.09%/笔,千万级约0.25%/笔)。
D 腿口径不同(9:23竞价卖≈开盘价),暂不纳入,标注跳过。
输出: reports/live_execution_audit.csv
"""
from __future__ import annotations

import csv
import datetime
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POSITIONS = PROJECT_ROOT / "data" / "processed" / "positions.json"
OUT = PROJECT_ROOT / "reports" / "live_execution_audit.csv"
FIELDS = ["order_id", "ts_code", "name", "strategy_leg", "buy_date", "sell_date",
          "buy_price", "bench_open", "buy_slip_pct", "sell_price", "bench_close",
          "sell_slip_pct", "total_slip_pct", "recorded_at"]

_daily_cache: dict = {}


def daily(dd: str):
    if dd not in _daily_cache:
        p = PROJECT_ROOT / "data" / "raw" / "daily" / f"{dd}.csv"
        _daily_cache[dd] = (
            pd.read_csv(p).drop_duplicates("ts_code").set_index("ts_code") if p.exists() else None
        )
    return _daily_cache[dd]


def bark(title: str, body: str) -> None:
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.notify import notify
        notify("sell_result", title, body, level="timeSensitive")
    except Exception as e:
        print(f"[bark失败] {e}")


def main() -> None:
    if not POSITIONS.exists():
        print("无持仓文件,跳过。")
        return
    positions = json.loads(POSITIONS.read_text(encoding="utf-8"))
    done: set[str] = set()
    if OUT.exists():
        try:
            done = set(pd.read_csv(OUT, encoding="utf-8-sig").order_id.astype(str))
        except Exception:
            done = set()
    new_rows = []
    for p in positions:
        if str(p.get("status", "")).lower() != "closed":
            continue
        oid = str(p.get("order_id", ""))
        if not oid or oid in done:
            continue
        leg = str(p.get("strategy_leg", "")).upper()
        if leg == "D":
            continue  # D=9:23竞价卖开盘价口径,基准不同,暂不纳入
        bp = float(p.get("buy_price", 0) or 0)
        sp = float(p.get("sell_price", 0) or 0)
        bd = str(p.get("buy_date", ""))
        sd = str(p.get("sell_date", ""))
        if bp <= 0 or sp <= 0 or not bd or not sd:
            continue
        ts = str(p.get("ts_code", ""))
        db, ds = daily(bd), daily(sd)
        if db is None or ds is None or ts not in db.index or ts not in ds.index:
            continue  # 卖出日日线未入库(当晚流水线先采集再跑本步,正常应已就绪)
        bench_o = float(db.loc[ts]["open"])
        bench_c = float(ds.loc[ts]["close"])
        if bench_o <= 0 or bench_c <= 0:
            continue
        buy_slip = bp / bench_o - 1
        sell_slip = 1 - sp / bench_c
        new_rows.append(dict(
            order_id=oid, ts_code=ts, name=str(p.get("name", "")), strategy_leg=leg,
            buy_date=bd, sell_date=sd,
            buy_price=round(bp, 4), bench_open=round(bench_o, 4), buy_slip_pct=round(buy_slip * 100, 4),
            sell_price=round(sp, 4), bench_close=round(bench_c, 4), sell_slip_pct=round(sell_slip * 100, 4),
            total_slip_pct=round((buy_slip + sell_slip) * 100, 4),
            recorded_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
    if new_rows:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        new_file = not OUT.exists()
        with open(OUT, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            for r in new_rows:
                w.writerow(r)
        parts = [f"{r['ts_code']} {r['name']} 买{r['buy_slip_pct']:+.2f}%/卖{r['sell_slip_pct']:+.2f}%"
                 f"=合计{r['total_slip_pct']:+.2f}%" for r in new_rows]
        bark("🧾 执行对账:新平仓损耗", "；".join(parts) + "。(正=比回测口径差)")
        print(f"新入账{len(new_rows)}笔:", "；".join(parts))
    else:
        print("无新平仓需对账。")
    # 每周五累计周报
    if datetime.date.today().weekday() == 4 and OUT.exists():
        d = pd.read_csv(OUT, encoding="utf-8-sig")
        if not d.empty:
            week_ago = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y%m%d")
            wk = d[d.sell_date.astype(str) >= week_ago]
            bark("🧾 执行对账周报",
                 f"本周平仓{len(wk)}笔(均损耗{wk.total_slip_pct.mean():+.2f}%),"
                 f"历史累计{len(d)}笔:买入均{d.buy_slip_pct.mean():+.2f}%/卖出均{d.sell_slip_pct.mean():+.2f}%"
                 f"/合计均{d.total_slip_pct.mean():+.2f}%。重放模型预期:16万级约0.09%,千万级约0.25%。")


if __name__ == "__main__":
    sys.exit(main())
