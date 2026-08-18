"""生成 A/C 每日独立候选，供 certify_current_executable_portfolio 使用。

为什么需要它：certify 旧口径用 `baseline.abc_return != 0` 当 A/C 的门槛，
而 baseline 是 A/B/C 三腿单独回放的产物，带着那次回放自己的持仓序列
（A/C 明细481天里108天是 POSITION_OCCUPIED_SKIP，当年被已删除的B等占掉，
连 ts_code 都没落盘）。当前组合还含D/E/M，那张持仓表早已作废，却仍在
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
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.paper_candidate_generator import PaperCandidateGenerator  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402
from scripts.run_paper_ab_filtered_daily_ops import (  # noqa: E402
    condition_strategy_config,
    configured_c_conditions,
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


def day(d: str):
    if d not in _dc:
        p = DAILY / f"{d}.csv"
        _dc[d] = pd.read_csv(p, dtype={"ts_code": str}).set_index("ts_code") if p.exists() else pd.DataFrame()
    return None if _dc[d].empty else _dc[d]


def limit_cap(code: str, pre: float) -> float:
    pct = 0.20 if code[:3] in {"300", "301", "688"} else 0.10
    return round(pre * (1 + pct), 2)


def trade_return(sig: str, code: str, hold: int):
    """返回 (状态, 买日, 卖日, 个股净收益)。hold=2 → T+2收盘；hold=3 → T+3收盘。"""
    i = DIDX.get(sig)
    if i is None or i + hold >= len(DATES):
        return "NO_CALENDAR", "", "", None
    d1 = DATES[i + 1]
    f1 = day(d1)
    if f1 is None or code not in f1.index:
        return "NO_PRICE", "", "", None
    r1 = f1.loc[code]
    o = float(r1["open"])
    pre = float(r1.get("pre_close", 0) or 0)
    if o <= 0:
        return "BAD_PRICE", "", "", None
    if pre > 0:
        cap = limit_cap(code, pre)
        if o >= cap - 1e-6 and float(r1["low"]) >= cap - 1e-6:
            return "LIMIT_UP_UNBUYABLE", d1, "", None
    buy = o * 1.001
    # 卖出日跌停顺延
    for k in range(hold, hold + 4):
        if i + k >= len(DATES):
            break
        dk = DATES[i + k]
        fk = day(dk)
        if fk is None or code not in fk.index:
            continue
        rk = fk.loc[code]
        pk = float(rk.get("pre_close", 0) or 0)
        if pk > 0:
            pct = 0.20 if code[:3] in {"300", "301", "688"} else 0.10
            floor = round(pk * (1 - pct), 2)
            if float(rk["high"]) <= floor + 1e-6:
                continue          # 跌停卖不出，顺延
        return "OK", d1, dk, float(rk["close"]) * 0.999 / buy - 1.0
    return "SELL_UNRESOLVED", d1, "", None


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
    gc = make(configured_c_conditions(cfg), "C")
    allc = ga.load_all_candidates()

    fa = ga.apply_strategy_filters(allc)
    fc = gc.apply_strategy_filters(allc)
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
        st, bd, ed, ret = trade_return(d, code, hold)
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
