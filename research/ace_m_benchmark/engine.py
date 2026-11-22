# coding: utf-8
"""Research backtest engine for the 2026-11 whole-strategy re-evaluation.

Panel: dates x stocks float32 matrices (build_panel.py). Execution rules (pessimistic, per AGENTS.md):
  * signal at T close, using only data <= T; buy at T+1 open only if the stock trades and the open is
    below the limit-up price (no fill on a limit-up open); sell at an open only if it is above the
    limit-down price, at a close only if the close is above it; otherwise retry next day.
  * suspended day = no daily row -> nothing can be traded; valuation carries the last close.
  * whole lots of 100 shares (STAR: 200 minimum); commission 0.03% min 5 yuan per side, transfer fee
    0.001%, stamp tax on sells 0.1% (0.05% from 20230828), slippage 0.1% per side.
  * corporate actions through adj_factor (position value follows the adjusted price).
"""
import numpy as np
import pandas as pd

COMMISSION, MIN_COMMISSION, TRANSFER, SLIP = 0.0003, 5.0, 0.00001, 0.001


def segment(code):
    prefix, sfx = code.split('.')
    if sfx == 'BJ' or prefix.startswith(('4', '8', '9')):
        return 'bj'
    if prefix.startswith(('688', '689')):
        return 'star'
    if prefix.startswith(('300', '301')):
        return 'chi_next'
    if sfx == 'SH' and prefix.startswith('6'):
        return 'sh_main'
    if sfx == 'SZ' and prefix.startswith(('000', '001', '002', '003')):
        return 'sz_main'
    return 'other'


def stamp_rate(day):
    return 0.001 if day < '20230828' else 0.0005


class Panel(object):
    def __init__(self, path, basic_csv, st_mask_path=None):
        z = np.load(path, allow_pickle=True)
        self.dates = [str(d) for d in z['dates']]
        self.codes = [str(c) for c in z['codes']]
        self.f = {k: z[k] for k in z.files if k not in ('dates', 'codes', 'limit_list_days')}
        self.limit_list_days = set(str(d) for d in z['limit_list_days'])
        n, m = len(self.dates), len(self.codes)
        self.T, self.N = n, m
        self.didx = {d: i for i, d in enumerate(self.dates)}
        self.seg = np.array([segment(c) for c in self.codes], dtype=object)
        basic = pd.read_csv(basic_csv, encoding='utf-8-sig', dtype=str)
        ld = basic.set_index('ts_code')['list_date'].to_dict()
        self.list_date = np.array([ld.get(c, '19000101') or '19000101' for c in self.codes])
        self.st = np.zeros((n, m), dtype=bool)
        if st_mask_path:
            self.st = np.load(st_mask_path)['st']
        self._derive()

    def _derive(self):
        f = self.f
        traded = ~np.isnan(f['close']) & (f['vol'] > 0)
        self.traded = traded
        # forward-filled adjusted close for valuation
        adj_close = f['close'] * f['adj_factor']
        self.adj_close = pd.DataFrame(adj_close).ffill().values
        self.adj = pd.DataFrame(f['adj_factor']).ffill().values
        self.close_ff = pd.DataFrame(f['close']).ffill().values
        # price limits
        dates = np.array(self.dates)
        pct = np.full((self.T, self.N), 0.10, dtype=np.float32)
        chi = self.seg == 'chi_next'
        pct[:, self.seg == 'star'] = 0.20
        pct[:, self.seg == 'bj'] = 0.30
        after = dates >= '20200824'
        pct[np.ix_(after, chi)] = 0.20
        main = np.isin(self.seg, ['sh_main', 'sz_main'])
        st_main = self.st & main[None, :]
        pct[st_main] = 0.05
        pct[self.st & (dates >= '20260706')[:, None] & main[None, :]] = 0.10
        pc = np.round(f['pre_close'].astype(np.float64), 2)
        self.up = np.floor(pc * (1 + pct) * 100 + 0.5 + 1e-6) / 100
        self.down = np.floor(pc * (1 - pct) * 100 + 0.5 + 1e-6) / 100
        # new listings: no price limit for the first 5 trading days; we exclude young stocks anyway
        self.list_idx = np.searchsorted(np.array(self.dates), self.list_date)
        self.age = np.arange(self.T)[:, None] - self.list_idx[None, :]
        o = np.round(f['open'].astype(np.float64), 2)
        c = np.round(f['close'].astype(np.float64), 2)
        self.buyable_open = traded & (o < self.up - 1e-6)
        self.sellable_open = traded & (o > self.down + 1e-6)
        self.sellable_close = traded & (c > self.down + 1e-6)
        self.limit_up_close = traded & (c >= self.up - 1e-6)
        self.limit_down_close = traded & (c <= self.down + 1e-6)

    def base_universe(self, min_age=120, min_amount20=1e4, min_price=2.0, segments=('sh_main', 'sz_main', 'chi_next', 'star')):
        """Eligible at T close: traded today, not ST, listed >= min_age days, 20d mean amount (thousand
        yuan) >= min_amount20, price >= min_price, allowed segment."""
        amt = pd.DataFrame(np.nan_to_num(self.f['amount'], nan=0.0)).rolling(20, min_periods=15).mean().values
        seg_ok = np.isin(self.seg, list(segments))[None, :]
        return (self.traded & ~self.st & (self.age >= min_age) & (amt >= min_amount20)
                & (self.f['close'] >= min_price) & seg_ok)


def _fee_buy(value):
    return max(value * COMMISSION, MIN_COMMISSION) + value * TRANSFER


def _fee_sell(value, day):
    return max(value * COMMISSION, MIN_COMMISSION) + value * TRANSFER + value * stamp_rate(day)


class Result(object):
    pass


def run(panel, score, eligible, mode='rebalance', K=10, R=5, H=2, exit_at='close', buffer=1.0,
        capital=68907.78, start='20190601', end=None, gate=None, stop_loss=None, max_weight=None,
        weight_mode='equal', min_score=None):
    """Simulate a long-only portfolio.

    score/eligible: T x N arrays known at T close (higher score = better); NaN score = not a candidate.
    mode 'rebalance': every R trading days hold the top-K (keep holdings ranked within K*buffer).
    mode 'hold':      every day fill free slots with the best new candidates, each held H trading days
                      after the entry day (sell at the close of entry+H, or the open if exit_at='open').
    gate: optional bool array length T; False at T close = do not open new positions for T+1 (and in
          rebalance mode: go to cash at T+1 open).
    """
    P = panel
    f = P.f
    dates = P.dates
    t0 = P.didx.get(start) if start in P.didx else int(np.searchsorted(np.array(dates), start))
    t1 = (P.didx[end] if end in P.didx else int(np.searchsorted(np.array(dates), end, side='right')) - 1) if end else P.T - 1
    cash = capital
    pos = {}       # j -> dict(shares, entry_t, entry_px, entry_adj, cost, exit_due)
    pending_sell = set()
    trades = []
    equity = np.full(P.T, np.nan)
    attempted = filled = 0
    fees_total = 0.0
    target = None   # set of j to buy at next open (rebalance) / list of j (hold)
    sell_next_open = set()
    last_reb = -10 ** 9

    for t in range(t0, t1 + 1):
        day = dates[t]
        # ---- open: sells
        for j in list(sell_next_open | pending_sell):
            if j not in pos:
                sell_next_open.discard(j); pending_sell.discard(j)
                continue
            if exit_at == 'open' or j in sell_next_open:
                if P.sellable_open[t, j]:
                    p = pos[j]
                    px = f['open'][t, j] * (1 - SLIP)
                    val = p['shares'] * px * f['adj_factor'][t, j] / p['entry_adj']
                    fee = _fee_sell(val, day)
                    cash += val - fee
                    fees_total += fee
                    trades.append((p['entry_t'], t, P.codes[j], p['cost'], val - fee, p.get('tag', '')))
                    del pos[j]
                    sell_next_open.discard(j); pending_sell.discard(j)
        # ---- open: buys
        if target:
            eq_prev = equity[t - 1] if t > t0 and not np.isnan(equity[t - 1]) else cash
            slot = eq_prev / K
            for j in target:
                if len(pos) >= K:
                    break
                if j in pos:
                    continue
                attempted += 1
                if not P.buyable_open[t, j]:
                    continue
                px = f['open'][t, j] * (1 + SLIP)
                lot = 200 if P.seg[j] == 'star' else 100
                budget = min(slot, cash - 10)
                shares = int(budget / (px * 1.0005) / 100) * 100
                if P.seg[j] == 'star' and shares < 200:
                    shares = 0
                if shares <= 0:
                    continue
                val = shares * px
                fee = _fee_buy(val)
                if val + fee > cash:
                    continue
                cash -= val + fee
                fees_total += fee
                filled += 1
                pos[j] = dict(shares=shares, entry_t=t, entry_px=px, entry_adj=f['adj_factor'][t, j],
                              cost=val + fee, exit_due=None)
                if mode == 'hold':
                    pos[j]['exit_due'] = t + H   # index offset in trading days; resolved below
        target = None
        # ---- close: scheduled close-exits (hold mode)
        if mode == 'hold':
            for j in list(pos):
                p = pos[j]
                if p['entry_t'] >= t:
                    continue
                due = p['exit_due'] is not None and t >= p['exit_due']
                stop = stop_loss is not None and (P.adj_close[t, j] / (p['entry_px'] * p['entry_adj']) - 1) <= -stop_loss
                if exit_at == 'close' and (due or stop):
                    if P.sellable_close[t, j]:
                        px = f['close'][t, j] * (1 - SLIP)
                        val = p['shares'] * px * f['adj_factor'][t, j] / p['entry_adj']
                        fee = _fee_sell(val, day)
                        cash += val - fee
                        fees_total += fee
                        trades.append((p['entry_t'], t, P.codes[j], p['cost'], val - fee, ''))
                        del pos[j]
                elif exit_at == 'open' and (due or stop):
                    pending_sell.add(j)    # sold at the next open
        # ---- mark to market
        mv = 0.0
        for j, p in pos.items():
            mv += p['shares'] * P.adj_close[t, j] / p['entry_adj']
        equity[t] = cash + mv
        # ---- after close: decide tomorrow
        if t == t1:
            break
        g = True if gate is None else bool(gate[t])
        sc = score[t]
        el = eligible[t] & ~np.isnan(sc)
        if min_score is not None:
            el &= sc >= min_score
        if mode == 'rebalance':
            if not g:
                sell_next_open |= set(pos)
                last_reb = -10 ** 9
                continue
            if t - last_reb >= R:
                last_reb = t
                cand = np.where(el)[0]
                order = cand[np.argsort(-sc[cand], kind='stable')]
                rank = {j: r for r, j in enumerate(order)}
                keep = set(j for j in pos if j in rank and rank[j] < K * buffer)
                # extra names back-fill slots whose first choice opens at limit-up
                new = [j for j in order if j not in keep][:2 * max(0, K - len(keep))]
                sell_next_open = (sell_next_open | (set(pos) - keep)) - keep
                target = new
        else:
            if not g:
                continue
            free = K - len(pos)
            if free > 0:
                cand = np.where(el)[0]
                cand = [j for j in cand[np.argsort(-sc[cand], kind='stable')] if j not in pos]
                target = cand[:2 * free]
    r = Result()
    r.equity = pd.Series(equity[t0:t1 + 1], index=pd.to_datetime(dates[t0:t1 + 1]))
    r.trades = pd.DataFrame(trades, columns=['entry_t', 'exit_t', 'code', 'cost', 'proceeds', 'tag'])
    if len(r.trades):
        r.trades['ret'] = r.trades['proceeds'] / r.trades['cost'] - 1
        r.trades['entry'] = [dates[i] for i in r.trades['entry_t']]
        r.trades['exit'] = [dates[i] for i in r.trades['exit_t']]
    r.fill_rate = filled / attempted if attempted else np.nan
    r.fees = fees_total
    return r


def stats(eq, trades=None, label=''):
    eq = eq.dropna()
    if len(eq) < 2:
        return {}
    ret = eq.pct_change().fillna(0)
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    dd = eq / eq.cummax() - 1
    monthly = eq.resample('ME').last().pct_change().dropna()
    first_m = eq.resample('ME').last().iloc[0] / eq.iloc[0] - 1
    monthly = pd.concat([pd.Series([first_m], index=[eq.resample('ME').last().index[0]]), monthly])
    out = dict(label=label, start=eq.index[0].strftime('%Y%m%d'), end=eq.index[-1].strftime('%Y%m%d'),
               total=total, cagr=cagr, monthly_mean=monthly.mean(), monthly_median=monthly.median(),
               month_win=(monthly > 0).mean(), maxdd=dd.min(), sharpe=ret.mean() / ret.std() * np.sqrt(244) if ret.std() > 0 else np.nan,
               calmar=cagr / -dd.min() if dd.min() < 0 else np.nan)
    if trades is not None and len(trades):
        t = trades
        t = t[(t['exit'] >= out['start']) & (t['exit'] <= out['end'])]
        if len(t):
            wins, losses = t['ret'][t['ret'] > 0], t['ret'][t['ret'] <= 0]
            streak = mx = 0
            for x in t['ret']:
                streak = streak + 1 if x <= 0 else 0
                mx = max(mx, streak)
            out.update(n_trades=len(t), win=(t['ret'] > 0).mean(), avg_trade=t['ret'].mean(), med_trade=t['ret'].median(),
                       pf=wins.sum() / -losses.sum() if losses.sum() < 0 else np.inf, max_win=t['ret'].max(),
                       max_loss=t['ret'].min(), max_lose_streak=mx,
                       trades_per_month=len(t) / max(1, len(monthly)))
    return out
