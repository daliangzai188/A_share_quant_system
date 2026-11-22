# coding: utf-8
"""Round 14: independent re-check of ACE + M (M = 20cm strong limit-up relay, no one-word board, seal/float < 5%,
fewer than 10 20cm limit-ups that day). Candidates are rebuilt from the raw panel with fresh code, the account is
replayed by a fresh simulator that marks open positions to market at every close (drawdown on market value).
  python m14_verify.py OUT_DIR

ACE+M 上线基准（docs/equity_curve_stop.md 第九节）的回放脚本。2026-11-22 从
~/A_System_复核/2026-11-11_M大法研究/scripts/ 移入 git，只改了下面两处路径，逻辑不变：
  - 冻结数据（面板、ACE计划、影子净值、f9_rows）仍在 git 外：M_RESEARCH_DATA，默认 ~/A_System_复核/2026-11-11_M大法研究
  - 交易规则函数（下单股数、板块、印花税、停手判定）用本仓库的 src/：ACE_M_CODE_ROOT 可改，默认本仓库根目录
"""
import os, sys, pickle
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E
import m_plans as MP
import acde_slots as A

out = Path(sys.argv[1])
HERE = Path(os.path.expanduser(os.environ.get('M_RESEARCH_DATA', '~/A_System_复核/2026-11-11_M大法研究'))).resolve()
A.setup(Path(os.path.expanduser(os.environ.get('ACE_M_CODE_ROOT', str(Path(__file__).resolve().parents[2])))).resolve())
P = E.Panel(str(HERE / 'panel/panel_20261110.npz'), str(HERE / 'fetched_20261110/stock_basic_all.csv'), str(HERE / 'panel/st_20261110.npz'))
X = MP.build(P)
f = P.f
T, N = P.T, P.N
dates = np.array(P.dates)
RET5_EDGE = 0.16711   # in-sample top-quintile edge of the prior 5-day return among limit-up stocks (from F9)

# ---------- A. rebuild M candidates from raw data
close, openp, low = f['close'], f['open'], np.round(f['low'].astype(np.float64), 2)
adjc = P.adj_close
lu = P.limit_up_close
uni = P.traded & ~P.st & (P.age >= 60) & np.isin(P.seg, ['sh_main', 'sz_main', 'chi_next', 'star'])[None, :]
is20 = np.isin(P.seg, ['chi_next', 'star'])
amt = np.nan_to_num(f['amount'], nan=0.0)
amt20 = pd.DataFrame(amt).rolling(20, min_periods=15).mean().values
has_ll = np.array([d in P.limit_list_days for d in P.dates])
pct = np.where(P.traded, close / f['pre_close'] - 1, np.nan)
cand = []
for t in range(21, T - 1):
    if not has_ll[t]:
        continue
    y = lu[t - 1] & uni[t - 1] & P.traded[t]
    mood = np.nanmean(pct[t][y]) if y.any() else np.nan            # yesterday's limit-ups, today's average change
    if not (0.0 <= mood < 0.02):
        continue
    n20 = int((lu[t] & uni[t] & is20).sum())                       # 20cm limit-ups today
    if n20 >= 10:
        continue
    for j in np.where(lu[t] & uni[t] & is20 & (amt20[t] >= 2e4))[0]:
        r5 = adjc[t - 1, j] / adjc[t - 6, j] - 1
        if not (r5 >= RET5_EDGE):
            continue
        if low[t, j] >= P.up[t, j] - 1e-6:                          # one-word board
            continue
        fdr = np.nan_to_num(f['ll_fd_amount'][t, j] / (f['circ_mv'][t, j] * 1e4))
        if fdr >= 0.05:
            continue
        cand.append((t, j, amt[t, j]))
C = pd.DataFrame(cand, columns=['t', 'j', 'amount'])
D = pd.read_pickle(HERE / 'out/m7/f9_rows.pkl')
old = D[(D.pool == 'P1') & (D.f2 == 1) & (D.f10 == 4) & (D.f18 == 2) & (D.f3 != 0) & (D.f6 != 6) & (D.f15 <= 1)]
a_set, b_set = set(zip(C.t, C.j)), set(zip(old.t, old.j))
print('A. M candidates rebuilt: %d, research version: %d, identical: %s (only new %d, only old %d)' % (
    len(a_set), len(b_set), a_set == b_set, len(a_set - b_set), len(b_set - a_set)))

# ---------- trade outcome of a buy at e open (sell at the first sellable close from e+1)
def outcome(j, e, slip_buy, slip_sell):
    d = e + 1
    while d < T and not P.sellable_close[d, j]:
        d += 1
    if d >= T:
        return None
    ent = openp[e, j] * (1 + slip_buy)
    sr = close[d, j] * f['adj_factor'][d, j] * (1 - slip_sell) / (openp[e, j] * f['adj_factor'][e, j] * (1 + slip_buy)) - 1
    return d, ent, sr


cidx = {c: i for i, c in enumerate(P.codes)}
PL = pickle.load(open(HERE / 'ace/plans.pkl', 'rb'))
ace_plans = {}
for order, leg in enumerate('ACE'):
    for r in PL[(leg, '%s_CURRENT' % leg)].to_dict('records'):
        ace_plans.setdefault(str(r['action_date']), []).append((order, r))
ace_plans = {d: [r for _o, r in sorted(v, key=lambda x: x[0])] for d, v in ace_plans.items()}
nav = pd.read_csv(HERE / 'ace/shadow_nav.csv', dtype={0: str}).set_axis(['d', 'v'], axis=1).set_index('d').v
ace_allowed = dict(zip(nav.index, (bool(x) for x in A.allowed_flags(nav.to_numpy(), ma_window=60, lag=6))))


def comm(g):
    return max(5.0, g * 0.0003)


def simulate(start, end, use_m=True, h_ace=0.2, m_slip=0.001, cash0=500000.0):
    days = [d for d in P.dates if start <= d <= end]
    mc = {}
    if use_m:
        for t, g in C.groupby('t'):
            if P.dates[t + 1] if t + 1 < T else None:
                mc[P.dates[t + 1]] = [int(j) for j in g.sort_values('amount', ascending=False).j]
    cash, pos, trades, eq_mtm, eq_cost = cash0, [], [], [], []
    for d in days:
        di = P.didx[d]
        pos2 = []
        for p in pos:
            if d > p['until']:
                cash += p['proceeds']
            else:
                pos2.append(p)
        pos = pos2
        base = cash + sum(p['cost'] for p in pos)

        def buy(fam, code, entry, sr, exit_date, w, j=None, e=None):
            nonlocal cash
            qty = A.orderable_buy_quantity(ts_code=code, available_amount=min(base * w, cash), execution_price=entry)
            step = 1 if A.market_segment(code) in ('star', 'bj') else 100
            while qty > 0 and qty * entry + comm(qty * entry) + qty * entry * 0.00001 > cash + 1e-9:
                qty -= step
            if qty <= 0:
                return False
            g = qty * entry
            cost = g + comm(g) + g * 0.00001
            sell = g * (1 + sr)
            proceeds = sell - comm(sell) - sell * 0.00001 - sell * A.stamp_tax_rate_for_date(exit_date, A.STAMP)
            cash -= cost
            pos.append(dict(fam=fam, code=code, until=exit_date, cost=cost, proceeds=proceeds, qty=qty, j=j if j is not None else cidx.get(code),
                            e=e if e is not None else di, entry=entry))
            trades.append(dict(action_date=d, exit_date=exit_date, fam=fam, code=code, ret=proceeds / cost - 1, weight=cost / base))
            return True
        used = lambda fam: sum(p['cost'] for p in pos if p['fam'] == fam) / base
        has = lambda fam: any(p['fam'] == fam for p in pos)
        ace_today = False
        plans = ace_plans.get(d, [])
        if plans:
            w = 0.825 if ace_allowed.get(d, True) else h_ace
            if w > 0:
                ace_today = True
                p0 = plans[0]
                if not has('ACE') and str(p0.get('position_opened')).lower() in ('true', '1', '1.0'):
                    if str(p0.get('outcome_observable')).lower() in ('true', '1', '1.0') and str(p0.get('status')) == 'OK':
                        buy('ACE', str(p0['ts_code']), float(p0['entry_price']), float(p0['stock_return_before_fees']), str(p0['exit_date']),
                            min(w, 0.995 - used('M')))
                    else:
                        pos.append(dict(fam='ACE', code='', until=str(p0.get('position_open_until') or days[-1]), cost=0.0, proceeds=0.0, qty=0, j=None, e=di, entry=0))
        if use_m and not has('M') and d in mc:
            held = set(p['code'] for p in pos if p['fam'] == 'ACE')
            w = 0.825 if (not ace_today and not has('ACE')) else min(0.175, 0.995 - used('ACE'))
            if w > 0.001:
                for j in mc[d]:
                    code = P.codes[j]
                    if code in held or not P.buyable_open[di, j]:
                        continue
                    o = outcome(j, di, m_slip, m_slip)
                    if o is None:
                        continue
                    xd, ent, sr = o
                    if buy('M', code, ent, sr, P.dates[xd], w, j=j, e=di):
                        break
        # mark to market at today's close
        mv = 0.0
        for p in pos:
            if p['qty'] == 0:
                continue
            if d == p['until']:
                mv += p['proceeds']
            else:
                j = p['j']; px = adjc[di, j] / (f['adj_factor'][p['e'], j])        # adjusted close in entry-day price units
                mv += p['qty'] * px
        eq_mtm.append(cash + mv)
        eq_cost.append(cash + sum(p['cost'] for p in pos))
    idx = pd.to_datetime(pd.Index(days))
    return pd.Series(eq_mtm, index=idx), pd.Series(eq_cost, index=idx), pd.DataFrame(trades)


WIN = [('样本外前段', '20191202', '20230731'), ('样本内', '20230801', '20260731'), ('样本外后段', '20260801', '20261110'), ('全期', '20191202', '20261110')]


def window_stats(eq, a, b):
    e = eq[(eq.index >= pd.Timestamp(a)) & (eq.index <= pd.Timestamp(b))]
    prev = eq[eq.index < pd.Timestamp(a)]
    base = prev.iloc[-1] if len(prev) else e.iloc[0]
    s = pd.concat([pd.Series([base]), e.reset_index(drop=True)])
    return e.iloc[-1] / base, float((s / s.cummax() - 1).min())


rows = []
for name, kw in (('只用ACE', dict(use_m=False, h_ace=0.0)), ('ACE+M', dict()),
                 ('ACE+M 真实资金6.9万', dict(cash0=68907.78)), ('ACE+M M滑点0.2%', dict(m_slip=0.002)), ('ACE+M M滑点0.3%', dict(m_slip=0.003))):
    em, ec, tr = simulate('20191202', '20261110', **kw)
    tr.to_csv(out / ('m14_%s.csv' % name.replace(' ', '_')), index=False)
    r = dict(name=name, n=len(tr), nM=int((tr.fam == 'M').sum()) if len(tr) else 0)
    for pn, a_, b_ in WIN:
        xm, ddm = window_stats(em, a_, b_)
        xc, ddc = window_stats(ec, a_, b_)
        t2 = tr[(tr.action_date >= a_) & (tr.action_date <= b_)]
        r[pn + '_n'] = len(t2); r[pn + '_x'] = xc; r[pn + '_dd成本'] = ddc; r[pn + '_dd市值'] = ddm
    rows.append(r)
R = pd.DataFrame(rows)
R.to_csv(out / 'm14_verify.csv', index=False)
pd.set_option('display.width', 320)
for pn, _, _ in WIN:
    print('==', pn)
    print(R[['name', pn + '_n', pn + '_x', pn + '_dd成本', pn + '_dd市值']].round(4).to_string(index=False))
# M recent performance (standalone trades inside ACE+M)
tr = pd.read_csv(out / 'm14_ACE+M.csv', dtype={'action_date': str})
m = tr[tr.fam == 'M']
print('M by year (n, mean):', m.groupby(m.action_date.str[:4]).ret.agg(['size', 'mean']).round(4).to_dict('index'))
print('M 2026 by month:', m[m.action_date >= '20260101'].groupby(m.action_date.str[:6]).ret.agg(['size', 'mean']).round(4).to_dict('index'))
print('ACE 2026 by month:', tr[(tr.fam == 'ACE') & (tr.action_date >= '20260101')].groupby(tr.action_date.str[:6]).ret.agg(['size', 'mean']).round(4).to_dict('index'))
