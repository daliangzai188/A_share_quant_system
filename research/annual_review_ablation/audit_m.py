"""全腿规律性检验（M）：在全部20cm涨停股（创业板/科创板、非ST、上市满60天）里，逐个检验M的条件。
收益=M的口径：T+1开盘买（开盘涨停买不进的不算）、之后第一个收盘没跌停的交易日收盘卖，双边滑点各0.1%。
判定规则与A/C/E相同（先写死）：7年里可计年份至少70%有效且至少4年，无条件、有条件两项都过才算规律。"""
import sys
from pathlib import Path
from _paths import M14 as SCRIPT, out_dir
OUT = out_dir()
head = SCRIPT.read_text(encoding="utf-8").split("\nWIN = [", 1)[0]
sys.argv = [str(SCRIPT), str(OUT)]
ns = {"__file__": str(SCRIPT), "__name__": "m14_head"}
exec(compile(head, str(SCRIPT), "exec"), ns)
np, pd, P, f, T = ns["np"], ns["pd"], ns["P"], ns["f"], ns["T"]
lu, uni, is20, amt20, pct, has_ll, low, adjc, outcome = (ns[k] for k in ("lu", "uni", "is20", "amt20", "pct", "has_ll", "low", "adjc", "outcome"))
rows = []
for t in range(21, T - 2):
    if not has_ll[t]:
        continue
    y = lu[t - 1] & uni[t - 1] & P.traded[t]
    mood = np.nanmean(pct[t][y]) if y.any() else np.nan
    n20 = int((lu[t] & uni[t] & is20).sum())
    for j in np.where(lu[t] & uni[t] & is20)[0]:
        if not P.buyable_open[t + 1, j]:
            continue
        o = outcome(j, t + 1, 0.001, 0.001)
        if o is None:
            continue
        r5 = adjc[t - 1, j] / adjc[t - 6, j] - 1
        fdr = np.nan_to_num(f["ll_fd_amount"][t, j] / (f["circ_mv"][t, j] * 1e4))
        rows.append({"year": P.dates[t][:4], "ret": float(np.clip(o[2], -0.5, 0.5)), "ret5": r5, "amt20": amt20[t, j],
                     "mood": mood, "n20": n20, "one_word": bool(low[t, j] >= P.up[t, j] - 1e-6), "fdr": fdr})
d = pd.DataFrame(rows)
d = d[d.year >= "2020"]
print("20cm涨停（可在T+1开盘买入）样本：%d" % len(d))
COND = {
    "前5日涨幅≥16.7%（动量）": d.ret5 >= 0.16711,
    "近20日日均成交额≥2000万（流动性）": d.amt20 >= 2e4,
    "市场温度：昨日涨停今日平均涨0~2%": (d.mood >= 0) & (d.mood < 0.02),
    "不是一字板": ~d.one_word,
    "封单/流通市值<5%": d.fdr < 0.05,
    "当天20cm涨停<10只": d.n20 < 10,
}
def yearly(inside, base):
    good = n = 0
    det = {}
    for yv in sorted(d.year.unique()):
        yy = (d.year == yv) & base
        a, b = d.loc[yy & inside, "ret"], d.loc[yy & ~inside, "ret"]
        if len(a) < 10 or len(b) < 10:
            continue
        diff = a.mean() - b.mean()
        det[yv] = (round(diff * 100, 2), len(a))
        n += 1
        good += diff > 0
    return good, n, det
ok = lambda g, n: n >= 4 and g >= 0.7 * n
all_true = pd.Series(True, index=d.index)
for name, mask in COND.items():
    others = all_true.copy()
    for other, om in COND.items():
        if other != name:
            others &= om
    u, c = yearly(mask, all_true), yearly(mask, others)
    v = "规律" if ok(*u[:2]) and ok(*c[:2]) else ("弱" if ok(*u[:2]) or ok(*c[:2]) else "不是规律")
    print("%-28s 无条件%d/%d 有条件%d/%d → %s ｜2026无条件%s 有条件%s" % (name, u[0], u[1], c[0], c[1], v, u[2].get("2026"), c[2].get("2026")))
def ladder(col, edges, title, base=None):
    sub = d if base is None else d[base]
    cats = pd.cut(sub[col], edges)
    t = sub.groupby([cats, sub.year], observed=False).ret.mean().unstack() * 100
    n = sub.groupby(cats, observed=False).size()
    print("\n== %s（各档平均收益%%，按年）" % title)
    print(t.round(2).to_string())
    rho = {y: round(pd.Series(range(len(t)), index=t.index, dtype=float).rank().corr(t[y].rank()), 2) for y in t.columns if t[y].notna().sum() >= 3}
    print("档位与收益秩相关：", rho, "｜各档样本：", n.to_dict())
ladder("ret5", [-1, 0, 0.08, 0.1671, 0.3, 0.6, 10], "前5日涨幅")
ladder("mood", [-1, -0.02, 0, 0.02, 0.04, 1], "市场温度（昨日涨停今日平均涨幅）")
ladder("n20", [-1, 5, 10, 20, 40, 1000], "当天20cm涨停只数")
ladder("fdr", [-1, 0.005, 0.01, 0.02, 0.05, 100], "封单/流通市值")
ladder("amt20", [-1, 1e4, 2e4, 5e4, 1e5, 1e12], "近20日日均成交额（千元）")
