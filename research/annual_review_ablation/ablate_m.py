"""逐个去掉条件检验（M）：在研究面板上按M的规则重建候选，每次只去掉一个条件（或把按成交额排序换成按代码排序），
其余不变；输出每个方案的每日候选（排序后）与每笔买卖结果（T+1开盘买，开盘涨停买不进；之后第一个收盘没跌停的交易日卖，双边滑点0.1%）。"""
import pickle, sys
from pathlib import Path
from _paths import M14 as SCRIPT, out_dir
OUT = out_dir()
head = SCRIPT.read_text(encoding="utf-8").split("\nWIN = [", 1)[0]
sys.argv = [str(SCRIPT), str(OUT)]
ns = {"__file__": str(SCRIPT), "__name__": "m14_head"}
exec(compile(head, str(SCRIPT), "exec"), ns)
np, pd, P, f, T = ns["np"], ns["pd"], ns["P"], ns["f"], ns["T"]
lu, uni, is20, amt20, pct, has_ll, low, adjc, outcome, RET5 = (ns[k] for k in ("lu", "uni", "is20", "amt20", "pct", "has_ll", "low", "adjc", "outcome", "RET5_EDGE"))
amt = np.nan_to_num(f["amount"], nan=0.0)

def build(skip=None, rank="amount"):
    cands = {}
    for t in range(21, T - 1):
        if not has_ll[t]:
            continue
        y = lu[t - 1] & uni[t - 1] & P.traded[t]
        mood = np.nanmean(pct[t][y]) if y.any() else np.nan
        if skip != "temperature" and not (0.0 <= mood < 0.02):
            continue
        if skip != "n20" and int((lu[t] & uni[t] & is20).sum()) >= 10:
            continue
        rows = []
        base = lu[t] & uni[t] & is20
        if skip != "liquidity":
            base = base & (amt20[t] >= 2e4)
        for j in np.where(base)[0]:
            if skip != "momentum" and not (adjc[t - 1, j] / adjc[t - 6, j] - 1 >= RET5):
                continue
            if skip != "one_word" and low[t, j] >= P.up[t, j] - 1e-6:
                continue
            if skip != "seal" and np.nan_to_num(f["ll_fd_amount"][t, j] / (f["circ_mv"][t, j] * 1e4)) >= 0.05:
                continue
            rows.append((-amt[t, j] if rank == "amount" else 0, P.codes[j]))
        if rows and t + 1 < T:
            cands[P.dates[t + 1]] = [code for _key, code in sorted(rows)]
    return cands

variants = {"M-现行": build(), "M-动量（前5日涨幅≥16.7%）": build("momentum"), "M-流动性（20日均成交≥2000万）": build("liquidity"),
            "M-市场温度（昨日涨停今日0~2%）": build("temperature"), "M-当天20cm涨停<10只": build("n20"),
            "M-非一字板": build("one_word"), "M-封单<5%": build("seal"), "M-排序（按成交额）": build(rank="code")}
outcomes = {}
for cands in variants.values():
    for day, codes in cands.items():
        di = P.didx[day]
        for code in codes:
            if (code, day) in outcomes:
                continue
            j = P.codes.index(code)
            if not P.buyable_open[di, j]:
                outcomes[(code, day)] = None
                continue
            o = outcome(j, di, 0.001, 0.001)
            outcomes[(code, day)] = None if o is None else (P.dates[o[0]], float(o[1]), float(o[2]))
pickle.dump({"variants": variants, "outcomes": outcomes}, open(OUT / "ablate_m.pkl", "wb"))
print({k: len(v) for k, v in variants.items()})
