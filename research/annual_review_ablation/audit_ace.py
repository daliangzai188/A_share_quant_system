"""全腿规律性检验（A、C、E）：每个条件按事先写死的规则判定是不是规律。只读研究池，不改任何东西。

判定规则（先写死，后看结果）：
- 年份：2020~2026共7年；某年“有效”=该年满足条件的样本≥10只、且均值高于不满足的（排除类条件则是被排除的那档更差）。
- 无条件检验：在全部涨停股里，7年中有效的年份数；有条件检验：在这条腿其余条件都满足的股票里，有效年份数。
- 规律：两项都达到“可计年份中至少70%有效、且至少4年”；弱：只有一项达到；不是规律：都达不到或样本不够。
- 收益口径与该腿持有期一致：A、E为T+1开盘买、T+2收盘卖；C为T+1开盘买、T+3收盘卖；单笔截在±50%。
"""
import json, os, sys
from pathlib import Path
from _paths import DS, FROZEN, enter_frozen, out_dir
OUT = out_dir()
enter_frozen()
import numpy as np
import pandas as pd
from src.factor_health import attach_forward_returns

YEARS = [str(y) for y in range(2020, 2027)]
cal = pd.read_csv(FROZEN / "data/raw/trade_calendar.csv", dtype={"cal_date": str})
dates = sorted(cal[pd.to_numeric(cal["is_open"], errors="coerce").eq(1)]["cal_date"].astype(str))

def frame_for(hold):
    path = OUT / ("forward_hold%d.pkl" % hold)
    if path.exists():
        return pd.read_pickle(path)
    pool = pd.read_csv(DS / "strict_feature_pool.csv", dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    f = attach_forward_returns(pool, calendar_dates=dates, daily_dir=FROZEN / "data/raw/daily",
                               adj_dir=FROZEN / "data/raw/adj_factor", hold=hold)
    f = f[f["trade_date"] >= "20200101"].copy()
    f["year"] = f["trade_date"].str[:4]
    f["ret"] = f["ret"].clip(-0.5, 0.5)
    f.to_pickle(path)
    return f

def cond_mask(f, conds):
    m = pd.Series(True, index=f.index)
    for col, vals in conds:
        m &= f[col].astype(str).isin([str(v) for v in vals])
    return m

def yearly(f, inside, base, exclude=False):
    """返回 (有效年数, 可计年数, {年: 差}, 2026差)。"""
    good = n = 0
    detail = {}
    for y in YEARS:
        yy = f["year"].eq(y) & base
        a, b = f.loc[yy & inside, "ret"], f.loc[yy & ~inside, "ret"]
        if len(a) < 10 or len(b) < 10:
            continue
        d = a.mean() - b.mean()
        detail[y] = (round(d * 100, 2), len(a))
        n += 1
        good += (d < 0) if exclude else (d > 0)
    return good, n, detail

def verdict(u, c):
    ok = lambda g, n: n >= 4 and g >= 0.7 * n
    if ok(*u) and ok(*c):
        return "规律"
    if ok(*u) or ok(*c):
        return "弱"
    return "不是规律"

rows = []
def test(leg, name, f, cond, others, exclude=False, base_extra=None):
    base_all = pd.Series(True, index=f.index) if base_extra is None else base_extra
    inside = cond_mask(f, [cond])
    u = yearly(f, inside, base_all, exclude)
    base = cond_mask(f, others) & base_all
    c = yearly(f, inside, base, exclude)
    rows.append({"leg": leg, "condition": name, "exclude": exclude,
                 "uncond": "%d/%d" % (u[0], u[1]), "cond": "%d/%d" % (c[0], c[1]),
                 "y2026_uncond": u[2].get("2026"), "y2026_cond": c[2].get("2026"),
                 "verdict": verdict(u[:2], c[:2]), "cond_detail": c[2]})

f2, f3 = frame_for(2), frame_for(3)
# ---------- A ----------
A_CORE = [("segment_limit_up_count_bucket", ["lt_5"]), ("market_chain_count_bucket", ["8_15"]),
          ("fd_ratio_bucket", ["0_1pct_0_3pct", "0_3pct_0_5pct", "0_5pct_1pct"])]
for i, cond in enumerate(A_CORE):
    test("A", "%s=%s" % (cond[0], "/".join(cond[1])), f2, cond, A_CORE[:i] + A_CORE[i + 1:])
test("A", "chain上限：避开15_30/gte_30（过热）", f2, ("market_chain_count_bucket", ["15_30", "gte_30"]),
     A_CORE[:1] + A_CORE[2:], exclude=True)
test("A", "chain下限：避开lt_3/3_8", f2, ("market_chain_count_bucket", ["lt_3", "3_8"]), A_CORE[:1] + A_CORE[2:], exclude=True)
for b in ("0_1pct_0_3pct", "0_3pct_0_5pct", "0_5pct_1pct"):
    test("A", "封单细分=%s（相对A内其他封单档）" % b, f2, ("fd_ratio_bucket", [b]), A_CORE[:2] + [A_CORE[2]])
test("A", "排除：成交额倍率0_8_1_2", f2, ("amount_ratio_bucket", ["0_8_1_2"]), A_CORE, exclude=True)
test("A", "排除：科创板", f2, ("market_segment", ["star"]), A_CORE, exclude=True)
test("A", "排序加分：非退潮（retreat_state=neutral）", f2, ("retreat_state_bucket", ["neutral"]), A_CORE)
# ---------- C ----------
C_PROFILES = {
    "C1": [("market_chain_count_bucket", ["15_30"]), ("segment_limit_up_count_bucket", ["40_80"]),
           ("first_time_detail_bucket", ["1100_1330"]), ("board_type", ["multi_open"])],
    "C2": [("limit_up_count_bucket", ["50_80"]), ("market_leader_rank_bucket", ["rank_4_10"]), ("fd_ratio_bucket", ["0_1pct_0_3pct"])],
    "C3-5": [("limit_up_count_bucket", ["50_80"]), ("market_leader_rank_bucket", ["rank_2_3"]), ("fd_ratio_bucket", ["0_1pct_0_3pct"]),
             ("market_limit_down_count_bucket", ["lt_5", "5_15", "15_30"])],
}
for pid, conds in C_PROFILES.items():
    for i, cond in enumerate(conds):
        test("C", "%s %s=%s" % (pid, cond[0], "/".join(cond[1])), f3, cond, conds[:i] + conds[i + 1:])
# ---------- E ----------
E_CORE = [("segment_limit_up_count_bucket", ["lt_5"]), ("market_chain_count_bucket", ["8_15"]), ("fd_ratio_bucket", ["0_5pct_1pct"])]
for i, cond in enumerate(E_CORE):
    test("E", "核心 %s=%s" % (cond[0], "/".join(cond[1])), f2, cond, E_CORE[:i] + E_CORE[i + 1:])
E_EXTRA = [("segment_chain_count_bucket", ["lt_1"]), ("segment_limit_down_ratio_bucket", ["lt_0_1pct"]),
           ("segment_emotion_state_bucket", ["ice_point"]), ("limit_height_rank_bucket", ["rank_2_3"]),
           ("volume_ratio_bucket", ["1_2"]), ("segment_limit_down_count_bucket", ["lt_1"]),
           ("market_leader_rank_bucket", ["rank_gt_30"]), ("segment_market_sentiment_level", ["weak"]),
           ("segment_limit_height_rank_bucket", ["rank_1"]), ("market_emotion_state_bucket", ["mixed"]),
           ("segment_retreat_state_bucket", ["neutral"])]
for cond in E_EXTRA:
    if cond[0] in f2.columns:
        test("E", "附加 %s=%s" % (cond[0], "/".join(cond[1])), f2, cond, E_CORE)
for cond in [("first_time_detail_bucket", ["1330_1430"]), ("market_leader_rank_bucket", ["rank_11_30"]),
             ("limit_up_count_bucket", ["120_180"]), ("limit_up_count_bucket", ["lt_30"]), ("fd_ratio_bucket", ["2pct_5pct"])]:
    test("E", "排除门 %s=%s" % (cond[0], "/".join(cond[1])), f2, cond, E_CORE, exclude=True)
both = cond_mask(f2, [("amount_ratio_bucket", ["1_2_2"])]) & cond_mask(f2, [("open_times_bucket", ["2_3"])])
f2["_and_gate"] = np.where(both, "hit", "miss")
test("E", "排除门 成交额倍率1_2_2且炸板2_3", f2, ("_and_gate", ["hit"]), E_CORE, exclude=True)

table = pd.DataFrame(rows)
pd.set_option("display.width", 260); pd.set_option("display.max_colwidth", 70)
print(table[["leg", "condition", "uncond", "cond", "y2026_uncond", "y2026_cond", "verdict"]].to_string(index=False))
table.to_json(OUT / "audit_ace.json", orient="records", force_ascii=False, indent=1)
