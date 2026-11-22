"""逐个去掉条件检验：分析与判定（规则在看结果前写死）。
主判据：这条腿单独账户2020~2026每年收益，“去掉后”比“现行”差的年数H：H≥5有用；H≤2拟合嫌疑；其余不确定。
（两边当年都没有交易的年份不计。）佐证：单笔收益中位数各年比较、整个ACE+M（现行仓位与停手）的全期、样本内/外月均、回撤。"""
import json, pickle, sys
from pathlib import Path
from _paths import REPO, out_dir
sys.path.insert(0, str(REPO))
import numpy as np
import pandas as pd
from src.ace_m_replay import ace_plans_by_day, replay_ace_m
from src.equity_curve_stop import allowed_flags
HERE = out_dir()
res = pickle.load(open(HERE / "research_inputs.pkl", "rb"))
ace = pickle.load(open(HERE / "ablate_ace.pkl", "rb"))["results"]
mab = pickle.load(open(HERE / "ablate_m.pkl", "rb"))
DAYS, STAMP = res["days"], res["stamp"]
YEARS = [str(y) for y in range(2020, 2027)]
OOS, IS = [("20191202", "20230731"), ("20260801", "20261110")], ("20230801", "20260731")

def window(series, a, b):
    s = series[(series.index >= a) & (series.index <= b)]
    prev = series[series.index < a]
    base = prev.iloc[-1] if len(prev) else (500000.0 if series.iloc[0] > 1000 else 1.0)
    return float(s.iloc[-1] / base), len(s)

def monthly(series, spans):
    x, n = 1.0, 0
    for a, b in spans:
        r, k = window(series, a, b)
        x, n = x * r, n + k
    return x ** (21.0 / n) - 1

def yearly(series):
    return {y: window(series, y + "0101", y + "1231")[0] - 1 for y in YEARS}

def tb(s):
    return s.astype(str).str.lower().isin(["true", "1", "1.0"])

def medians_from_plans(p):
    if p is None or len(p) == 0:
        return {}
    p = p[tb(p["position_opened"]) & tb(p["outcome_observable"]) & p["status"].astype(str).eq("OK")]
    yr = p["action_date"].astype(str).str[:4]
    return {y: (float(g.median()), len(g)) for y, g in p["stock_return_before_fees"].astype(float).groupby(yr)}

def medians_from_trades(tr):
    if tr is None or len(tr) == 0:
        return {}
    return {y: (float(g.ret.median()), len(g)) for y, g in tr.groupby(tr.action_date.astype(str).str[:4])}

def system(ace_plans, shadow_nav, m_cands, m_out):
    flags = allowed_flags(shadow_nav.to_numpy(), ma_window=60, lag=6)
    allowed = {str(d): bool(ok) for d, ok in zip(shadow_nav.index, flags)}
    eq, tr = replay_ace_m(DAYS, ace_plans, allowed, m_cands, lambda c, d: m_out.get((c, d)), stamp_schedule=STAMP)
    return {"全期": eq.iloc[-1] / 500000.0, "样本内月均": monthly(eq, [IS]), "样本外月均": monthly(eq, OOS),
            "回撤": float((eq / eq.cummax() - 1).min())}

def compare(cur_y, var_y, cur_med, var_med):
    worse = counted = 0
    for y in YEARS:
        a, b = cur_y.get(y, 0.0), var_y.get(y, 0.0)
        if abs(a) < 1e-12 and abs(b) < 1e-12:
            continue
        counted += 1
        worse += b < a - 1e-12
    mw = mc = 0
    for y in YEARS:
        if y in cur_med and y in var_med and cur_med[y][1] >= 5 and var_med[y][1] >= 5:
            mc += 1
            mw += var_med[y][0] < cur_med[y][0]
    verdict = "有用" if worse >= 5 else ("拟合嫌疑" if worse <= 2 else "不确定")
    return worse, counted, mw, mc, verdict

rows = []
base_plans = {leg: ace["%s-现行" % leg]["plans"] for leg in ("A", "C", "E")}
cur_shadow = ace["A-现行"]["shadow_nav"]
res_m, res_out = res["m_candidates"], res["outcomes"]
cur_sys = system(ace_plans_by_day(base_plans), cur_shadow, res_m, res_out)
rows.append({"方案": "现行（基准）", **cur_sys})
for vid, item in ace.items():
    if vid.endswith("-现行") or item.get("error"):
        if item.get("error"):
            rows.append({"方案": vid, "判定": "没跑成：" + item["error"]})
        continue
    leg = item["leg"]
    cur = ace["%s-现行" % leg]
    w, c, mw, mc, v = compare(yearly(cur["alone_nav"]), yearly(item["alone_nav"]),
                              medians_from_plans(cur["plans"]), medians_from_plans(item["plans"]))
    legs = dict(base_plans); legs[leg] = item["plans"]
    sysm = system(ace_plans_by_day(legs), item["shadow_nav"], res_m, res_out)
    rows.append({"方案": vid, "去掉后变差的年数": "%d/%d" % (w, c), "中位数变差的年数": "%d/%d" % (mw, mc), "判定": v,
                 "单独账户全期(去掉后/现行)": "%.1f / %.1f" % (item["alone_nav"].iloc[-1], cur["alone_nav"].iloc[-1]), **sysm})
m_out = mab["outcomes"]
def m_alone(cands):
    eq, tr = replay_ace_m(DAYS, {}, {}, cands, lambda c, d: m_out.get((c, d)), stamp_schedule=STAMP)
    return eq / 500000.0, tr
cur_m_eq, cur_m_tr = m_alone(mab["variants"]["M-现行"])
for vid, cands in mab["variants"].items():
    if vid == "M-现行":
        continue
    eq, tr = m_alone(cands)
    w, c, mw, mc, v = compare(yearly(cur_m_eq), yearly(eq), medians_from_trades(cur_m_tr), medians_from_trades(tr))
    sysm = system(ace_plans_by_day(base_plans), cur_shadow, cands, m_out)
    rows.append({"方案": vid, "去掉后变差的年数": "%d/%d" % (w, c), "中位数变差的年数": "%d/%d" % (mw, mc), "判定": v,
                 "单独账户全期(去掉后/现行)": "%.1f / %.1f" % (eq.iloc[-1], cur_m_eq.iloc[-1]), **sysm})
t = pd.DataFrame(rows)
for col in ("样本内月均", "样本外月均", "回撤"):
    t[col] = t[col].map(lambda x: "%+.1f%%" % (x * 100) if x == x else "")
t["全期"] = t["全期"].map(lambda x: "{:,.0f}".format(x) if x == x else "")
pd.set_option("display.width", 320); pd.set_option("display.max_colwidth", 40)
cols = ["方案", "去掉后变差的年数", "中位数变差的年数", "判定", "单独账户全期(去掉后/现行)", "全期", "样本内月均", "样本外月均", "回撤"]
print(t[[c for c in cols if c in t.columns]].fillna("").to_string(index=False))
t.to_json(HERE / "analyze.json", orient="records", force_ascii=False, indent=1)
