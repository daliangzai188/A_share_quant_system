# -*- coding: utf-8 -*-
"""实盘-规则逐笔执行对账(收盘流水线末步,2026-07-17 用户拍板落地)。

对每笔已平仓实盘持仓,对照"按规则应得的买卖价"计算执行损耗:
  买入损耗 = 实际买入均价 / 买入日开盘价 - 1      (规则=开盘价买)
  卖出损耗 = 1 - 实际卖出均价 / 规则卖出价         (规则卖出价见下)
规则卖出价(2026-11-19 用户要求改为含止盈):实盘计划卖出日09:20挂"涨停价-0.01"的止盈单
(src/daemon/takeprofit.py),所以计划卖出日最高价摸到"涨停价-0.01"时,规则卖出价=止盈价
(与回测引擎 src/fixed_rule_execution.py 的 use_intraday_takeprofit 同一判定:只在原计划卖出日,
跌停顺延后的日子和提前卖掉的不算);否则=实际卖出日收盘价。止盈本身多卖的部分不再算成"执行做得好",
只单独写出来;滑点报警、实盘统计(读成交汇总的 benchmark_close)因此只看执行成本。
CSV(reports/live_execution_audit.csv)每次按全部已平仓持仓重算:bench_close=规则卖出价,
sell_bench_rule=止盈价/收盘价,exit_close=卖出日收盘价;本次缺行情算不出的保留上次的结果。
新平仓才推送(同一笔交易的分片按股数合并成一行,写"比规则好/差x%";当天平仓但缺卖出价的也列出来),
每周五推累计周报(对照重放模型预期:16万级约0.09%/笔,千万级约0.25%/笔)。
D 腿口径不同(9:23竞价卖≈开盘价),暂不纳入,标注跳过。
"""
from __future__ import annotations

import csv
import datetime
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.market_rules import limit_up_price, price_limit_pct  # noqa: E402

POSITIONS = PROJECT_ROOT / "data" / "processed" / "positions.json"
OUT = PROJECT_ROOT / "reports" / "live_execution_audit.csv"
FIELDS = ["order_id", "ts_code", "name", "strategy_leg", "buy_date", "sell_date",
          "buy_price", "bench_open", "buy_slip_pct", "sell_price", "bench_close",
          "sell_slip_pct", "total_slip_pct", "recorded_at", "sell_bench_rule", "exit_close"]
RULE_TAKEPROFIT = "止盈价"
RULE_CLOSE = "收盘价"
RULE_TEXT = "（规则：开盘价买、收盘价卖，计划卖出日最高价摸到涨停价−0.01时按止盈价卖；不含手续费）"

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
        from src.notify import notify
        notify("sell_result", title, body, level="timeSensitive")
    except Exception as e:
        print(f"[bark失败] {e}")


def _takeprofit_settings() -> tuple[bool, float]:
    """实盘止盈开关和让价(config.json live_trade,与止盈线程同一默认值)。"""
    try:
        from src.utils.config import load_json_config
        live = load_json_config(PROJECT_ROOT / "config" / "config.json").get("live_trade", {}) or {}
        return bool(live.get("intraday_takeprofit_enabled", True)), float(live.get("intraday_takeprofit_offset", 0.01))
    except Exception as e:
        print(f"[读取止盈配置失败,按开启、让价0.01对账] {e}")
        return True, 0.01


def _number(row: Any, column: str) -> float:
    try:
        value = float(row.get(column, 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if value == value else 0.0


def _manual_exit_only(p: dict) -> bool:
    """人工退出的仓不挂止盈(与 src/daemon/state._position_manual_exit_only 同一判定;B腿已删除)。"""
    return (bool(p.get("manual_exit_only", False)) or bool(p.get("auto_exit_disabled", False))
            or str(p.get("strategy_leg", "")).upper() == "B")


def rule_sell_price(p: dict, sell_date: str, enabled: bool, offset: float) -> tuple[float, str, float] | None:
    """按规则的卖出价:(规则卖出价, 止盈价/收盘价, 卖出日收盘价);行情还没有时返回None(下次再算)。"""
    ts = str(p.get("ts_code", ""))
    ds = daily(sell_date)
    if ds is None or ts not in ds.index:
        return None
    close = _number(ds.loc[ts], "close")
    if close <= 0:
        return None
    planned = str(p.get("planned_exit_date", "") or "").replace("-", "")[:8]
    if enabled and len(planned) == 8 and sell_date >= planned and not _manual_exit_only(p):
        dp = daily(planned)
        if dp is None:
            return None                       # 计划卖出日行情还没入库,不能判断止盈
        if ts in dp.index:                    # 计划卖出日停牌就挂不了止盈
            row = dp.loc[ts]
            pre, high = _number(row, "pre_close"), _number(row, "high")
            limit_pct = price_limit_pct(ts, name=str(p.get("name", "")), trade_date=planned)
            cap = limit_up_price(pre, limit_pct) if pre > 0 else None
            if cap is not None and high > 0 and high >= cap - offset - 1e-9:
                return round(cap - offset, 2), RULE_TAKEPROFIT, close
    return close, RULE_CLOSE, close


def _px(price: float) -> str:
    """价格:最多3位小数,末尾0去掉但至少留2位(20.924、4.70)。"""
    text = f"{price:.3f}"
    return text[:-1] if text.endswith("0") else text


def _vs_rule(pct: float) -> str:
    """合计损耗(正=比规则差)写成人话。"""
    if abs(pct) < 0.005:
        return "与规则持平"
    return f"比规则{'差' if pct > 0 else '好'}{abs(pct):.2f}%"


def _buy_diff(pct: float) -> str:
    """买入损耗(实际买入均价/开盘价-1):正=多花,负=少花。"""
    if abs(pct) < 0.005:
        return "持平"
    return f"{'多花' if pct > 0 else '少花'}{abs(pct):.2f}%"


def _sell_diff(pct: float) -> str:
    """卖出损耗(1-实际卖出均价/规则卖出价):正=少卖,负=多卖。"""
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


def _takeprofit_gain(t: dict) -> float:
    """止盈价比收盘价多卖的百分比(规则本身的收益,不是执行)。"""
    close = float(t.get("exit_close") or 0)
    return (float(t["bench_close"]) / close - 1) * 100 if close > 0 else 0.0


def _trade_text(t: dict, today: str) -> str:
    when = "" if str(t["sell_date"]) == today else f"{str(t['sell_date'])[4:6]}/{str(t['sell_date'])[6:8]}平仓，"
    shares = f"{t['shares']}股" if t["shares"] > 0 else ""
    if t.get("sell_bench_rule") == RULE_TAKEPROFIT:
        gain = _takeprofit_gain(t)
        sell = (f"卖出均价{_px(t['sell_vwap'])}（止盈价{_px(t['bench_close'])}，{_sell_diff(t['sell_slip'])}；"
                f"收盘价{_px(float(t['exit_close']))}，止盈比收盘{'多' if gain >= 0 else '少'}卖{abs(gain):.2f}%）")
    else:
        sell = f"卖出均价{_px(t['sell_vwap'])}（收盘价{_px(t['bench_close'])}，{_sell_diff(t['sell_slip'])}）"
    return (
        f"{_leg_name(t)}{shares}：{when}{_vs_rule(t['total_slip'])}｜"
        f"买入均价{_px(t['buy_vwap'])}（开盘价{_px(t['bench_open'])}，{_buy_diff(t['buy_slip'])}）｜{sell}"
    )


def _read_previous() -> dict[str, dict]:
    if not OUT.exists():
        return {}
    try:
        with open(OUT, newline="", encoding="utf-8-sig") as fh:
            return {str(r.get("order_id", "")): dict(r) for r in csv.DictReader(fh) if r.get("order_id")}
    except Exception as e:
        print(f"[读取旧对账表失败,按全部重算] {e}")
        return {}


def _audit_row(p: dict, enabled: bool, offset: float, now_text: str) -> dict | None:
    """一笔已平仓持仓的对账行;价格或行情还没有时返回None。"""
    bp = float(p.get("buy_price", 0) or 0)
    sp = float(p.get("sell_price", 0) or 0)
    bd = str(p.get("buy_date", ""))
    sd = str(p.get("sell_date", ""))
    if bp <= 0 or sp <= 0 or not bd or not sd:
        return None
    ts = str(p.get("ts_code", ""))
    db = daily(bd)
    if db is None or ts not in db.index:
        return None  # 买入日日线未入库(当晚流水线先采集再跑本步,正常应已就绪)
    bench_o = _number(db.loc[ts], "open")
    rule = rule_sell_price(p, sd, enabled, offset)
    if bench_o <= 0 or rule is None:
        return None
    bench_s, rule_name, exit_close = rule
    buy_slip = bp / bench_o - 1
    sell_slip = 1 - sp / bench_s
    return dict(
        order_id=str(p.get("order_id", "")), ts_code=ts, name=str(p.get("name", "")),
        strategy_leg=str(p.get("strategy_leg", "")).upper(), buy_date=bd, sell_date=sd,
        buy_price=round(bp, 4), bench_open=round(bench_o, 4), buy_slip_pct=round(buy_slip * 100, 4),
        sell_price=round(sp, 4), bench_close=round(bench_s, 4), sell_slip_pct=round(sell_slip * 100, 4),
        total_slip_pct=round((buy_slip + sell_slip) * 100, 4), recorded_at=now_text,
        sell_bench_rule=rule_name, exit_close=round(exit_close, 4), _shares=_position_shares(p),
    )


def _push_new(new_rows: list[dict], missing_price: dict[tuple, int], today: str) -> None:
    trades = _merge_trades(new_rows)
    parts = [_trade_text(t, today) for t in trades]
    parts += [f"{name}（{leg}）{qty}股：缺卖出成交价，暂时对不了账（系统会用QMT当天成交补价，补上后再对；补不上需要补录券商成交）"
              for (name, leg), qty in missing_price.items()]
    if not (trades or missing_price):
        print("无新平仓需对账。")
        return
    if len(trades) == 1:
        headline = f"{_leg_name(trades[0])}{_vs_rule(trades[0]['total_slip'])}"
        if trades[0].get("sell_bench_rule") == RULE_TAKEPROFIT:
            gain = _takeprofit_gain(trades[0])
            headline += f"（止盈成交，比收盘{'多' if gain >= 0 else '少'}卖{abs(gain):.2f}%）"
    elif trades:
        avg = sum(t["total_slip"] for t in trades) / len(trades)
        headline = f"新平仓{len(trades)}笔，平均{_vs_rule(avg)}"
    else:
        headline = f"{len(missing_price)}笔平仓缺卖出价"
    if trades and missing_price:
        headline += f"，另有{len(missing_price)}笔缺卖出价"
    body = "；".join(parts) + "。"
    if trades:
        body += RULE_TEXT
    bark("🧾 执行对账：" + headline, body)
    print(f"新入账{len(new_rows)}条(合并为{len(trades)}笔):", "；".join(parts))


def main() -> None:
    if not POSITIONS.exists():
        print("无持仓文件,跳过。")
        return
    positions = json.loads(POSITIONS.read_text(encoding="utf-8"))
    previous = _read_previous()
    enabled, offset = _takeprofit_settings()
    today = datetime.date.today().strftime("%Y%m%d")
    now_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows: dict[str, dict] = dict(previous)
    new_rows: list[dict] = []
    missing_price: dict[tuple, int] = {}
    for p in positions:
        if str(p.get("status", "")).lower() != "closed":
            continue
        oid = str(p.get("order_id", ""))
        if not oid:
            continue
        leg = str(p.get("strategy_leg", "")).upper()
        if leg == "D":
            continue  # D=9:23竞价卖开盘价口径,基准不同,暂不纳入
        if (float(p.get("sell_price", 0) or 0) <= 0 and str(p.get("sell_date", "")) == today
                and float(p.get("buy_price", 0) or 0) > 0 and oid not in previous):
            key = (str(p.get("name", "") or p.get("ts_code", "")), leg)
            missing_price[key] = missing_price.get(key, 0) + _position_shares(p)
            continue
        row = _audit_row(p, enabled, offset, now_text)
        if row is None:
            continue  # 算不出来的保留上次结果(没有就等下次)
        if oid in previous:
            row["recorded_at"] = previous[oid].get("recorded_at") or now_text
        else:
            new_rows.append(row)
        rows[oid] = row
    if rows:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in rows.values():
                w.writerow(r)
    _push_new(new_rows, missing_price, today)
    # 每周五累计周报
    if datetime.date.today().weekday() == 4 and OUT.exists():
        d = pd.read_csv(OUT, encoding="utf-8-sig")
        if not d.empty:
            week_ago = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y%m%d")
            wk = d[d.sell_date.astype(str) >= week_ago]
            week_text = (f"本周平仓{len(wk)}条成交，平均{_vs_rule(wk.total_slip_pct.mean())}；"
                         if not wk.empty else "本周没有平仓；")
            bark("🧾 执行对账周报",
                 week_text
                 + f"历史累计{len(d)}条：买入平均比开盘价{_buy_diff(d.buy_slip_pct.mean())}，"
                 f"卖出平均比规则卖出价{_sell_diff(d.sell_slip_pct.mean())}，合计平均{_vs_rule(d.total_slip_pct.mean())}。"
                 "重放模型预期：16万级约比规则差0.09%，千万级约差0.25%。" + RULE_TEXT)


if __name__ == "__main__":
    sys.exit(main())
