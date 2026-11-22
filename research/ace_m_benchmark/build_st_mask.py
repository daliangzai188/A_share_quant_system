# coding: utf-8
"""ST / delisting-arrangement mask (dates x stocks) from Tushare namechange history.
  python build_st_mask.py PANEL.npz namechange.csv OUT.npz
A name counts as ST if it contains ST, *ST, PT or 退 (src/market_rules.is_st_name); it applies from
its start_date to the day before the next name's start_date (or its own end_date if later records lack).

2026-11-23 从 ~/A_System_复核/2026-11-11_M大法研究/scripts/ 原样移入 git（逻辑一行没改）。"""
import sys
import numpy as np
import pandas as pd

panel, nc, out = sys.argv[1:4]
z = np.load(panel, allow_pickle=True)
dates = np.array([str(d) for d in z['dates']])
codes = [str(c) for c in z['codes']]
cidx = {c: i for i, c in enumerate(codes)}
names = pd.read_csv(nc, encoding='utf-8-sig', dtype=str).fillna('')
names = names.drop_duplicates(['ts_code', 'name', 'start_date']).sort_values(['ts_code', 'start_date'])


def is_st(name):
    t = str(name).upper().replace(' ', '')
    return 'ST' in t or '退' in t or 'PT' in t


st = np.zeros((len(dates), len(codes)), dtype=bool)
n_periods = 0
for code, g in names.groupby('ts_code'):
    j = cidx.get(code)
    if j is None:
        continue
    g = g[g['start_date'] != ''].sort_values('start_date')
    starts = list(g['start_date'])
    for k, (_, r) in enumerate(g.iterrows()):
        if not is_st(r['name']):
            continue
        a = r['start_date']
        nxt = starts[k + 1] if k + 1 < len(starts) else ''
        b = nxt if nxt else '99999999'          # exclusive
        if r['end_date'] and not nxt:
            b = str(int(r['end_date']) + 1)
        st[(dates >= a) & (dates < b), j] = True
        n_periods += 1
np.savez_compressed(out, st=st)
print('st periods', n_periods, 'stock-days', int(st.sum()), 'last day ST count', int(st[-1].sum()))
