"""研究环境：导出研究回放的全部输入和结果，供生产模块逐位核对（只写草稿目录）。"""
import pickle, sys
from pathlib import Path
from _paths import M14 as SCRIPT, out_dir
HERE = out_dir()
head = SCRIPT.read_text(encoding="utf-8").split("\nWIN = [", 1)[0]
sys.argv = [str(SCRIPT), str(HERE)]
ns = {"__file__": str(SCRIPT), "__name__": "m14_head"}
exec(compile(head, str(SCRIPT), "exec"), ns)
P, C, T, outcome, simulate, A = ns["P"], ns["C"], ns["T"], ns["outcome"], ns["simulate"], ns["A"]
days = [d for d in P.dates if "20191202" <= d <= "20261110"]
mc = {}
for t, g in C.groupby("t"):
    if t + 1 < T:
        mc[P.dates[t + 1]] = [P.codes[int(j)] for j in g.sort_values("amount", ascending=False).j]
outcomes = {}
for d, codes in mc.items():
    di = P.didx[d]
    for code in codes:
        j = P.codes.index(code)
        if not P.buyable_open[di, j]:
            outcomes[(code, d)] = None
            continue
        o = outcome(j, di, 0.001, 0.001)
        outcomes[(code, d)] = None if o is None else (P.dates[o[0]], float(o[1]), float(o[2]))
results = {}
for name, kw in (("live", {}), ("ace_only", dict(use_m=False, h_ace=0.0))):
    _em, ec, tr = simulate("20191202", "20261110", **kw)
    results[name] = (list(ec.values), tr.to_dict("records"))
pickle.dump({"days": days, "ace_plans": ns["ace_plans"], "ace_allowed": ns["ace_allowed"], "m_candidates": mc,
             "outcomes": outcomes, "stamp": A.STAMP, "results": results}, open(HERE / "research_inputs.pkl", "wb"))
print("days %d, ace plan days %d, M buy days %d, outcomes %d" % (len(days), len(ns["ace_plans"]), len(mc), len(outcomes)))
