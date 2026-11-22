# coding: utf-8
"""M 大法 candidate plans (pre-registered 2026-11-11, see M大法_规则设计与预注册_2026-11-11.md).
For every signal day T: the ordered list of M candidates that are buyable at T+1 open (9:25 rule),
modules in priority M1 > M2 > M3 > M4, each with its outcome under exit X1 (first non-limit-up close
from T+2, max 5 holding days, limit-down postpones) and X2 (fixed T+2 close, limit-down postpones).
Output rows follow the ACE plan columns used by the cash replay (entry_price raw incl. 0.1% slippage,
stock_return_before_fees on adjusted prices incl. both slippages, position_open_until = exit day).
"""
import os, sys
import numpy as np
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E

SLIP = 0.001


def build(P):
    f = P.f
    T, N = P.T, P.N
    lu = P.limit_up_close
    ld = P.limit_down_close
    low = np.round(f['low'].astype(np.float64), 2)
    one_word = lu & (low >= P.up - 1e-6)
    seg_ok = np.isin(P.seg, ['sh_main', 'sz_main', 'chi_next', 'star'])[None, :]
    uni = P.traded & ~P.st & (P.age >= 60) & seg_ok
    boards = np.zeros((T, N), dtype=np.int16)
    for t in range(T):
        boards[t] = np.where(lu[t], (boards[t - 1] if t else 0) + 1, 0)
    maxh = np.where(uni, boards, 0).max(axis=1)
    pct = np.where(P.traded, f['close'] / f['pre_close'] - 1, np.nan)
    prev_perf = np.full(T, np.nan)
    for t in range(1, T):
        m = lu[t - 1] & uni[t - 1] & P.traded[t]
        if m.sum():
            prev_perf[t] = np.nanmean(pct[t][m])
    dt_cnt = (ld & uni).sum(axis=1)
    gate = (np.nan_to_num(prev_perf, nan=-1) >= 0) & (dt_cnt < 20)
    amount = np.nan_to_num(f['amount'], nan=0.0)
    st = f['ll_status']
    ot = f['ll_open_times']
    ft = f['ll_first_time']
    fdr = f['ll_fd_amount'] / (f['circ_mv'] * 1e4)
    gap = np.full((T, N), np.nan)
    gap[1:] = f['open'][1:] / f['pre_close'][1:] - 1          # gap[t+1] known at 9:25 of t+1
    maxb10 = np.zeros((T, N), dtype=np.int16)
    for t in range(1, T):
        maxb10[t] = boards[max(0, t - 10):t].max(axis=0)       # T-10 .. T-1
    return dict(lu=lu, one_word=one_word, uni=uni, boards=boards, maxh=maxh, prev_perf=prev_perf, dt_cnt=dt_cnt,
                gate=gate, amount=amount, st=st, ot=ot, ft=ft, fdr=fdr, gap=gap, pct=pct, maxb10=maxb10)


def exit_day(P, X, j, e, rule):
    """Index of the selling day for an entry on day e (bought at the open), or None if not observable."""
    T = P.T
    if rule == 'X2':
        d = e + 1
        while d < T:
            if P.sellable_close[d, j]:
                return d
            d += 1
        return None
    last = e + 5
    d = e + 1
    while d < T:
        if P.traded[d, j]:
            if X['lu'][d, j] and d < last:
                d += 1
                continue
            if P.sellable_close[d, j]:
                return d
        d += 1
    return None


def candidates(P, X, t):
    """Ordered M candidates for signal day t (buy at t+1 open)."""
    if t + 1 >= P.T:
        return []
    uni, lu, boards = X['uni'][t], X['lu'], X['boards']
    out = []
    g = X['gap'][t + 1]
    buy = P.buyable_open[t + 1]
    # M1 空间龙头接力
    if X['maxh'][t] >= 3:
        c = np.where(uni & (boards[t] == X['maxh'][t]))[0]
        c = c[np.argsort(-X['amount'][t][c], kind='stable')]
        out += [('M1', j) for j in c if buy[j] and g[j] >= -0.03]
    # M2 龙头首阴
    if t >= 1:
        c = np.where(uni & lu[t - 1] & ~lu[t] & (X['pct'][t] >= -0.09) & (X['pct'][t] <= 0.05) & (X['maxb10'][t] >= 4))[0]
        c = sorted(c, key=lambda j: (-X['maxb10'][t][j], -X['amount'][t][j]))
        out += [('M2', j) for j in c if buy[j]]
    # M3 分歧弱转强
    if t >= 1:
        bad = (X['st'][t] == 2) | (lu[t] & (np.nan_to_num(X['ot'][t], nan=0) >= 3))
        c = np.where(uni & (boards[t - 1] >= 1) & bad)[0]
        c = sorted(c, key=lambda j: (-boards[t - 1][j], -X['amount'][t][j]))
        out += [('M3', j) for j in c if buy[j] and g[j] >= 0.02]
    # M4 一进二竞价
    c = np.where(uni & (boards[t] == 1) & ~X['one_word'][t] & (X['st'][t] == 1)
                 & (np.nan_to_num(X['ft'][t], nan=999999) <= 100000) & (np.nan_to_num(X['ot'][t], nan=9) == 0))[0]
    c = c[np.argsort(-np.nan_to_num(X['fdr'][t][c], nan=-1), kind='stable')]
    out += [('M4', j) for j in c if buy[j] and 0.01 <= g[j] <= 0.06]
    return out


def plans(P, X, modules, rule, use_gate, start='20200101'):
    rows = []
    dates = P.dates
    seen = set()
    t0 = int(np.searchsorted(np.array(dates), start))
    for t in range(max(t0 - 1, 1), P.T - 1):
        if use_gate and not X['gate'][t]:
            continue
        rank = 0
        for mod, j in candidates(P, X, t):
            if mod not in modules or (t, j) in seen and False:
                continue
            e = t + 1
            d = exit_day(P, X, j, e, rule)
            entry = float(P.f['open'][e, j]) * (1 + SLIP)
            base = dict(signal_date=dates[t], action_date=dates[e], buy_date=dates[e], strategy_leg='M', module=mod,
                        ts_code=P.codes[j], leg_rank=rank, entry_price=entry, entry_filled=True, position_opened=True)
            if d is None:
                rows.append(dict(base, status='OUTCOME_NOT_OBSERVABLE_AT_UPDATE', outcome_observable=False, exit_date='',
                                 position_open_until=dates[-1], stock_return_before_fees=np.nan))
            else:
                ent_adj = P.f['open'][e, j] * P.f['adj_factor'][e, j] * (1 + SLIP)
                ex_adj = P.f['close'][d, j] * P.f['adj_factor'][d, j] * (1 - SLIP)
                rows.append(dict(base, status='OK', outcome_observable=True, exit_date=dates[d], position_open_until=dates[d],
                                 exit_price=float(P.f['close'][d, j]) * (1 - SLIP), hold_days=d - e,
                                 stock_return_before_fees=float(ex_adj / ent_adj - 1)))
            rank += 1
    return pd.DataFrame(rows)
