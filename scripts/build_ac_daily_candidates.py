"""生成 A/C 每日独立候选，供 certify_current_executable_portfolio 使用。

为什么需要它：certify 旧口径用 `baseline.abc_return != 0` 当 A/C 的门槛，
而 baseline 是 A/B/C 三腿单独回放的产物，带着那次回放自己的持仓序列
（A/C 明细481天里108天是 POSITION_OCCUPIED_SKIP，当年被已删除的B等占掉，
连 ts_code 都没落盘）。当前组合还含D/E，那张持仓表早已作废，却仍在
把 A/C 锁死在90天。实盘从不受此限制——三个实盘文件都不读 baseline。

输出：reports/ac_daily_candidates/ac_daily_candidates.csv（被 .gitignore 挡，
需要时用本脚本重建）。

运行：
    python scripts/build_ac_daily_candidates.py

口径与实盘/认证一致：
  A: candidate_filters + ranking(顶层)，T+1开盘×1.001买、T+2收盘×0.999卖
  C: paper_ab_filtered_strategy.c_strategy.conditions，T+3收盘卖
     C只在A当天无候选时才生成（only_when_a_no_candidate）
  T+1开盘一字涨停视为排队买不到；卖出日跌停顺延到可卖日。

校验：现有 abc 明细里 operation_status=HISTORICAL_SIM_FILLED 的90天，
      重建结果必须选出同一只 ts_code。对不上就报出来。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.paper_candidate_generator import PaperCandidateGenerator  # noqa: E402
from src.adjusted_returns import linked_forward_adjusted_return  # noqa: E402
from src.market_rules import (  # noqa: E402
    fixed_close_sell_executable,
    fixed_open_buy_executable,
    limit_up_price,
    listing_trade_day_number,
    price_limit_pct,
)
from src.utils.config import load_json_config  # noqa: E402
from src.strategy_c_factor_rules import FACTOR_UNION_MODE as C_FACTOR_UNION_MODE  # noqa: E402
from scripts.run_paper_ab_filtered_daily_ops import (  # noqa: E402
    build_c_factor_filtered_pool,
    condition_strategy_config,
    reject_strategy_risk_mask,
)
import scripts.certify_current_executable_portfolio as cert  # noqa: E402

HERE = ROOT / "reports" / "ac_daily_candidates"
HERE.mkdir(parents=True, exist_ok=True)
STRAT = ROOT / "config" / "strategy_config.json"
SRC = ROOT / "data" / "processed" / "next_day_premium_trades_2y.csv"
DAILY = ROOT / "data" / "raw" / "daily"

cal = pd.read_csv(ROOT / "data" / "raw" / "trade_calendar.csv", dtype=str)
ccol = "cal_date" if "cal_date" in cal.columns else cal.columns[0]
if "is_open" in cal.columns:
    cal = cal[cal["is_open"].astype(str).isin({"1", "1.0", "True"})]
DATES = sorted(cal[ccol].tolist())
DIDX = {d: i for i, d in enumerate(DATES)}
_dc: dict[str, pd.DataFrame] = {}

STOCK_BASIC_PATH = ROOT / "data" / "raw" / "stock_basic" / "stock_basic_all.csv"
if STOCK_BASIC_PATH.exists():
    _stock_basic = pd.read_csv(
        STOCK_BASIC_PATH,
        dtype={"ts_code": str, "list_date": str, "name": str},
        low_memory=False,
    ).drop_duplicates("ts_code", keep="last").set_index("ts_code")
else:
    _stock_basic = pd.DataFrame()


def day(d: str):
    if d not in _dc:
        p = DAILY / f"{d}.csv"
        _dc[d] = pd.read_csv(p, dtype={"ts_code": str}).set_index("ts_code") if p.exists() else pd.DataFrame()
    return None if _dc[d].empty else _dc[d]


def stock_meta(code: str, name: str = "") -> tuple[str, str]:
    resolved_name = str(name or "")
    list_date = ""
    if not _stock_basic.empty and code in _stock_basic.index:
        row = _stock_basic.loc[code]
        if not resolved_name:
            resolved_name = str(row.get("name", "") or "")
        list_date = str(row.get("list_date", "") or "").replace(".0", "")
    return resolved_name, list_date


def limit_cap(code: str, pre: float, *, name: str = "", trade_date: str = "") -> float | None:
    resolved_name, list_date = stock_meta(code, name)
    listing_day = listing_trade_day_number(list_date, trade_date, DATES)
    pct = price_limit_pct(
        code,
        name=resolved_name,
        trade_date=trade_date,
        listing_day_number=listing_day,
    )
    return limit_up_price(pre, pct)


@dataclass(frozen=True)
class TradeReturnResult:
    status: str
    buy_date: str
    exit_date: str
    stock_return: float | None
    exit_rule: str = ""


def trade_return_details(
    sig: str,
    code: str,
    hold: int,
    *,
    name: str = "",
    use_intraday_takeprofit: bool = False,
    takeprofit_offset: float = 0.01,
    sell_delay_max: int = 4,
) -> TradeReturnResult:
    """按固定开盘买/固定收盘卖规则计算可执行的前复权收益。"""

    i = DIDX.get(sig)
    if i is None or i + hold >= len(DATES):
        return TradeReturnResult("NO_CALENDAR", "", "", None)
    d1 = DATES[i + 1]
    f1 = day(d1)
    if f1 is None or code not in f1.index:
        return TradeReturnResult("NO_PRICE", "", "", None)
    r1 = f1.loc[code]
    open_price = float(r1["open"])
    pre_close = float(r1.get("pre_close", 0) or 0)
    if open_price <= 0:
        return TradeReturnResult("BAD_PRICE", "", "", None)

    resolved_name, list_date = stock_meta(code, name)
    listing_day = listing_trade_day_number(list_date, d1, DATES)
    buy_limit_pct = price_limit_pct(
        code,
        name=resolved_name,
        trade_date=d1,
        listing_day_number=listing_day,
    )
    if pre_close > 0 and not fixed_open_buy_executable(
        pre_close=pre_close,
        open_price=open_price,
        limit_pct=buy_limit_pct,
    ):
        return TradeReturnResult("LIMIT_UP_UNBUYABLE", d1, "", None)

    buy_price = open_price * 1.001
    for k in range(hold, hold + max(int(sell_delay_max), 1)):
        if i + k >= len(DATES):
            break
        exit_date = DATES[i + k]
        frame = day(exit_date)
        if frame is None or code not in frame.index:
            continue
        exit_row = frame.loc[code]
        exit_pre_close = float(exit_row.get("pre_close", 0) or 0)
        exit_close = float(exit_row.get("close", 0) or 0)
        exit_high = float(exit_row.get("high", 0) or 0)
        exit_listing_day = listing_trade_day_number(list_date, exit_date, DATES)
        exit_limit_pct = price_limit_pct(
            code,
            name=resolved_name,
            trade_date=exit_date,
            listing_day_number=exit_listing_day,
        )

        sell_price: float | None = None
        exit_rule = ""
        cap = limit_up_price(exit_pre_close, exit_limit_pct) if exit_pre_close > 0 else None
        if (
            use_intraday_takeprofit
            # 实盘止盈监控只在原计划退出日运行。若计划日跌停/停牌导致延期，
            # 后续交易日按延期卖出的固定收盘口径处理，不能回测出实盘不存在的止盈。
            and k == hold
            and cap is not None
            and exit_high >= cap - float(takeprofit_offset) - 1e-9
        ):
            sell_price = cap - float(takeprofit_offset)
            exit_rule = "INTRADAY_LIMIT_UP_MINUS_OFFSET"
        elif exit_pre_close > 0 and fixed_close_sell_executable(
            pre_close=exit_pre_close,
            close_price=exit_close,
            limit_pct=exit_limit_pct,
        ):
            sell_price = exit_close * 0.999
            exit_rule = "FIXED_CLOSE"
        elif exit_pre_close <= 0 and exit_close > 0:
            sell_price = exit_close * 0.999
            exit_rule = "FIXED_CLOSE_NO_LIMIT_REFERENCE"

        if sell_price is None or sell_price <= 0:
            continue
        try:
            stock_return = linked_forward_adjusted_return(
                ts_code=code,
                buy_date=d1,
                buy_price=buy_price,
                sell_date=exit_date,
                sell_price=sell_price,
                trade_dates=DATES,
                daily_loader=day,
            )
        except ValueError:
            return TradeReturnResult("NO_ADJUSTED_PRICE", d1, exit_date, None, exit_rule)
        return TradeReturnResult("OK", d1, exit_date, stock_return, exit_rule)
    return TradeReturnResult("SELL_UNRESOLVED", d1, "", None)


def trade_return(
    sig: str,
    code: str,
    hold: int,
    *,
    name: str = "",
    use_intraday_takeprofit: bool = False,
    takeprofit_offset: float = 0.01,
):
    """兼容旧调用，返回 ``(状态, 买日, 卖日, 前复权个股收益)``。"""

    result = trade_return_details(
        sig,
        code,
        hold,
        name=name,
        use_intraday_takeprofit=use_intraday_takeprofit,
        takeprofit_offset=takeprofit_offset,
    )
    return result.status, result.buy_date, result.exit_date, result.stock_return


def main() -> None:
    cfg = load_json_config(STRAT)

    def make(conditions, label):
        c = condition_strategy_config(cfg, conditions, label) if conditions else cfg
        g = PaperCandidateGenerator(STRAT, input_trades_path=SRC)
        g.config = c
        g.paper_config = c.get("paper_candidate", {})
        g.risk_thresholds = g.paper_config.get("risk_thresholds", {})
        return g

    ga = make(None, "A")                                  # A用顶层conditions
    allc = ga.load_all_candidates()

    fa = ga.apply_strategy_filters(allc)
    _, gc, fc, c_release = build_c_factor_filtered_pool(
        STRAT, cfg, allc, include_match_ids=False
    )
    win = (str(cert.load_sources().baseline["date"].min()), str(cert.load_sources().baseline["date"].max()))
    print("窗口", win)
    fa = fa[(fa.trade_date >= win[0]) & (fa.trade_date <= win[1])]
    fc = fc[(fc.trade_date >= win[0]) & (fc.trade_date <= win[1])]
    print("A过滤后 %d行/%d日   C过滤后 %d行/%d日"
          % (len(fa), fa.trade_date.nunique(), len(fc), fc.trade_date.nunique()))

    a_by = {d: p for d, p in fa.groupby("trade_date")}
    c_by = {d: p for d, p in fc.groupby("trade_date")}

    rows = []
    for d in DATES:
        if d < win[0] or d > win[1]:
            continue
        leg, pick = "", None
        if d in a_by:
            r = ga.rank_candidates(a_by[d].copy()).reset_index(drop=True)
            if len(r):
                leg, pick = "A", r.iloc[0]
        if pick is None and d in c_by:
            r = gc.rank_candidates(c_by[d].copy()).reset_index(drop=True)
            if str(c_release["strategy_mode"]) == C_FACTOR_UNION_MODE:
                r = r.head(1).copy()
            try:
                m = reject_strategy_risk_mask(r, cfg, "c_strategy")
                r = r[~pd.Series(m.values, index=r.index)]
            except Exception:
                pass
            if len(r):
                leg, pick = "C", r.iloc[0]
        if pick is None:
            rows.append({"signal_date": d, "leg": "", "ts_code": "", "name": "",
                         "status": "NO_CANDIDATE", "buy_date": "", "exit_date": "", "stock_return": None})
            continue
        code = str(pick["ts_code"])
        hold = 2 if leg == "A" else 3
        st, bd, ed, ret = trade_return(d, code, hold, name=str(pick.get("name", "")))
        rows.append({"signal_date": d, "leg": leg, "ts_code": code, "name": pick.get("name", ""),
                     "status": st, "buy_date": bd, "exit_date": ed, "stock_return": ret})

    out = pd.DataFrame(rows)
    out.to_csv(HERE / "ac_daily_candidates.csv", index=False, encoding="utf-8-sig")
    print()
    print("重建结果:", out["status"].value_counts().to_dict())
    print("有候选天数:", int(out["leg"].ne("").sum()), " (A %d / C %d)"
          % (int(out.leg.eq("A").sum()), int(out.leg.eq("C").sum())))

    # ---- 校验：对上已知90天成交 ----
    s = cert.load_sources()
    abc = s.abc.reset_index() if s.abc.index.name else s.abc.copy()
    filled = abc[abc["operation_status"].astype(str).eq("HISTORICAL_SIM_FILLED")].copy()
    filled["signal_date"] = filled["signal_date"].astype(str)
    oi = out.set_index("signal_date")
    same = diff = missing = 0
    bad = []
    for _, r in filled.iterrows():
        d = str(r["signal_date"])
        if d not in oi.index:
            missing += 1
            continue
        got = str(oi.loc[d, "ts_code"])
        exp = str(r["ts_code"])
        if exp in ("", "nan"):
            continue
        if got == exp:
            same += 1
        else:
            diff += 1
            if len(bad) < 12:
                bad.append((d, str(r["strategy_leg"]), exp, str(r.get("name", "")),
                            got, str(oi.loc[d, "name"]), str(oi.loc[d, "leg"])))
    print()
    print("=== 校验:已知成交日 vs 重建 ===")
    print(f"一致 {same} / 不一致 {diff} / 缺失 {missing}")
    if bad:
        print("不一致样例(日期 原腿 原票 → 重建票 重建腿):")
        for x in bad:
            print(f"  {x[0]} {x[1]:8s} {x[2]} {x[3][:6]:7s} → {x[4]} {x[5][:6]:7s} [{x[6]}]")


if __name__ == "__main__":
    main()
