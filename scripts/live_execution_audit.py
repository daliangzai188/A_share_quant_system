# -*- coding: utf-8 -*-
"""实盘-回测逐笔执行对账(收盘流水线末步,2026-07-17 用户拍板落地)。

对每笔已平仓实盘持仓,对照回测基准价计算执行损耗:
  买入损耗 = 实际买入均价 / 买入日开盘价 - 1   (回测口径=开盘价买)
  卖出损耗 = 1 - 实际卖出均价 / 卖出日收盘价   (回测口径=收盘价卖)
幂等按 order_id 增量入账;有新平仓即推送损耗;每周五推累计周报
(对照重放模型预期:16万级约0.09%/笔,千万级约0.25%/笔)。
推送(2026-11-19 用户要求说清楚):同一笔交易的分片按股数合并成一行,直接写"比回测好/差x%"
和"比开盘价多花/少花、比收盘价多卖/少卖",不再让人猜正负号;当天平仓但缺卖出价的也列出来。
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


def _px(price: float) -> str:
    """价格:最多3位小数,末尾0去掉但至少留2位(20.924、4.70)。"""
    text = f"{price:.3f}"
    return text[:-1] if text.endswith("0") else text


def _vs_backtest(pct: float) -> str:
    """合计损耗(正=比回测口径差)写成人话。"""
    if abs(pct) < 0.005:
        return "与回测持平"
    return f"比回测{'差' if pct > 0 else '好'}{abs(pct):.2f}%"


def _buy_diff(pct: float) -> str:
    """买入损耗(实际买入均价/开盘价-1):正=多花,负=少花。"""
    if abs(pct) < 0.005:
        return "持平"
    return f"{'多花' if pct > 0 else '少花'}{abs(pct):.2f}%"


def _sell_diff(pct: float) -> str:
    """卖出损耗(1-实际卖出均价/收盘价):正=少卖,负=多卖。"""
    if abs(pct) < 0.005:
        return "持平"
    return f"{'少卖' if pct > 0 else '多卖'}{abs(pct):.2f}%"


def _position_shares(p: dict) -> int:
    """这笔持仓的股数:平仓后shares会归零,优先用entry_shares。"""
    try:
        return max(int(float(p.get("entry_shares") or p.get("shares") or 0)), 0)
    except (TypeError, ValueError):
        return 0


def _leg_name(row: dict) -> str:
    leg = str(row.get("strategy_leg", "") or "")
    name = str(row.get("name", "") or row.get("ts_code", ""))
    return f"{name}（{leg}）" if leg else name


def _merge_trades(rows: list[dict]) -> list[dict]:
    """同一笔交易(股票+腿+买入日+卖出日)的分片按股数加权合并。"""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r["ts_code"], r["strategy_leg"], r["buy_date"], r["sell_date"])
        groups.setdefault(key, []).append(r)
    trades = []
    for parts in groups.values():
        shares = sum(max(int(r.get("_shares", 0) or 0), 0) for r in parts)
        weights = [max(int(r.get("_shares", 0) or 0), 0) for r in parts] if shares > 0 else [1] * len(parts)
        total_w = sum(weights)
        buy = sum(r["buy_price"] * w for r, w in zip(parts, weights)) / total_w
        sell = sum(r["sell_price"] * w for r, w in zip(parts, weights)) / total_w
        first = parts[0]
        buy_slip = (buy / first["bench_open"] - 1) * 100
        sell_slip = (1 - sell / first["bench_close"]) * 100
        trades.append(dict(
            first, shares=shares, buy_vwap=buy, sell_vwap=sell,
            buy_slip=buy_slip, sell_slip=sell_slip, total_slip=buy_slip + sell_slip,
        ))
    return trades


def _trade_text(t: dict, today: str) -> str:
    when = "" if str(t["sell_date"]) == today else f"{str(t['sell_date'])[4:6]}/{str(t['sell_date'])[6:8]}平仓，"
    shares = f"{t['shares']}股" if t["shares"] > 0 else ""
    return (
        f"{_leg_name(t)}{shares}：{when}{_vs_backtest(t['total_slip'])}｜"
        f"买入均价{_px(t['buy_vwap'])}（开盘价{_px(t['bench_open'])}，{_buy_diff(t['buy_slip'])}）｜"
        f"卖出均价{_px(t['sell_vwap'])}（收盘价{_px(t['bench_close'])}，{_sell_diff(t['sell_slip'])}）"
    )


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
    today = datetime.date.today().strftime("%Y%m%d")
    missing_price: dict[tuple, int] = {}
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
        if sp <= 0 and sd == today and bp > 0:
            key = (str(p.get("name", "") or p.get("ts_code", "")), leg)
            missing_price[key] = missing_price.get(key, 0) + _position_shares(p)
            continue
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
            _shares=_position_shares(p),
        ))
    if new_rows:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        new_file = not OUT.exists()
        with open(OUT, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()
            for r in new_rows:
                w.writerow(r)
    trades = _merge_trades(new_rows)
    parts = [_trade_text(t, today) for t in trades]
    parts += [f"{name}（{leg}）{qty}股：缺卖出成交价，暂时对不了账（系统会用QMT当天成交补价，补上后再对；补不上需要补录券商成交）"
              for (name, leg), qty in missing_price.items()]
    if trades or missing_price:
        if len(trades) == 1:
            headline = f"{_leg_name(trades[0])}{_vs_backtest(trades[0]['total_slip'])}"
        elif trades:
            avg = sum(t["total_slip"] for t in trades) / len(trades)
            headline = f"新平仓{len(trades)}笔，平均{_vs_backtest(avg)}"
        else:
            headline = f"{len(missing_price)}笔平仓缺卖出价"
        if trades and missing_price:
            headline += f"，另有{len(missing_price)}笔缺卖出价"
        title = "🧾 执行对账：" + headline
        body = "；".join(parts) + "。"
        if trades:
            body += "（回测按开盘价买、收盘价卖；不含手续费）"
        bark(title, body)
        print(f"新入账{len(new_rows)}条(合并为{len(trades)}笔):", "；".join(parts))
    else:
        print("无新平仓需对账。")
    # 每周五累计周报
    if datetime.date.today().weekday() == 4 and OUT.exists():
        d = pd.read_csv(OUT, encoding="utf-8-sig")
        if not d.empty:
            week_ago = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y%m%d")
            wk = d[d.sell_date.astype(str) >= week_ago]
            week_text = (f"本周平仓{len(wk)}条成交，平均{_vs_backtest(wk.total_slip_pct.mean())}；"
                         if not wk.empty else "本周没有平仓；")
            bark("🧾 执行对账周报",
                 week_text
                 + f"历史累计{len(d)}条：买入平均比开盘价{_buy_diff(d.buy_slip_pct.mean())}，"
                 f"卖出平均比收盘价{_sell_diff(d.sell_slip_pct.mean())}，合计平均{_vs_backtest(d.total_slip_pct.mean())}。"
                 "重放模型预期：16万级约比回测差0.09%，千万级约差0.25%。（回测按开盘价买、收盘价卖；不含手续费）")


if __name__ == "__main__":
    sys.exit(main())
