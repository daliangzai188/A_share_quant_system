# coding: utf-8
"""Track 1 capital-layer variants of the core A>C>E architecture.

S slots of weight w each; every action date, free slots take that day's frozen plans in the fixed
order A(rank0..k-1) > C(rank0..) > E(rank0..). A plan that cannot be bought still uses its decision
(as in the formal single-account rule: a higher leg's unfilled plan never hands the day to a lower
leg). A slot stays occupied through the exit day. Fees, lots and stamp tax follow the formal cash
replay (src/acde_rolling_framework.replay_action_date_cash_portfolio). With S=1, k=1, w=0.825 the
entry-booked NAV equals the formal shadow NAV.

Gate modes: none | official (方案甲 flags from the formal shadow NAV) | own (same rule on this
variant's own ungated NAV) | own_half (own rule, but a closed gate halves w instead of halting).
"""
import os, sys
import numpy as np
import pandas as pd

ROOT = None


def setup(root):
    global ROOT, orderable_buy_quantity, market_segment, stamp_tax_rate_for_date, allowed_flags, STAMP
    ROOT = root
    sys.path.insert(0, str(root))
    from src.market_rules import orderable_buy_quantity, market_segment  # noqa
    from src.trading_fees import stamp_tax_rate_for_date  # noqa
    from src.equity_curve_stop import allowed_flags  # noqa
    import json
    cfg = json.load(open(os.path.join(str(root), 'config/config.json'), encoding='utf-8'))
    STAMP = cfg['analysis'].get('stamp_tax_schedule')
    globals().update(orderable_buy_quantity=orderable_buy_quantity, market_segment=market_segment,
                     stamp_tax_rate_for_date=stamp_tax_rate_for_date, allowed_flags=allowed_flags)


def load_plans(topk_dir, k):
    frames = []
    for order, leg in enumerate('ACE'):
        p = pd.read_csv(os.path.join(topk_dir, 'topk_%s.csv' % leg), dtype={'signal_date': str, 'action_date': str,
                                                                          'exit_date': str, 'position_open_until': str})
        p = p[p['leg_rank'] < k].copy()
        p['leg_order'] = order
        frames.append(p)
    plans = pd.concat(frames, ignore_index=True)
    plans = plans.sort_values(['action_date', 'leg_order', 'leg_rank'])
    return {d: g.to_dict('records') for d, g in plans.groupby('action_date')}


def replay(by_day, dates, S=1, w=0.825, gate=None, gate_mode='none', cash0=500000.0,
           commission_rate=0.0003, minimum_commission=5.0, transfer=0.00001):
    def commission(g):
        return max(minimum_commission, g * commission_rate)

    def truthy(x):
        return str(x).strip().lower() in ('true', '1', '1.0')

    cash = cash0
    open_pos = []          # dicts: until, cost, proceeds, code
    trades = []
    eq_exit, eq_entry = [], []
    for i, d in enumerate(dates):
        # release slots whose exit day has passed
        keep = []
        for p in open_pos:
            if d > p['until']:
                cash += p['proceeds']
            else:
                keep.append(p)
        open_pos = keep
        allowed = True if gate is None else bool(gate[i])
        weight = w
        if not allowed and gate_mode == 'own_half':
            allowed, weight = True, w * 0.5
        free = S - len(open_pos)
        if allowed and free > 0:
            held = set(p['code'] for p in open_pos)
            equity_cost = cash + sum(p['cost'] for p in open_pos)
            for plan in by_day.get(d, []):
                if free <= 0:
                    break
                code = str(plan['ts_code'])
                if code in held:
                    continue
                free -= 1                       # this decision uses the slot for today
                if not truthy(plan.get('position_opened')):
                    continue
                until = str(plan.get('position_open_until') or '') or dates[-1]
                if not truthy(plan.get('outcome_observable')) or str(plan.get('status')) != 'OK':
                    open_pos.append(dict(until=until, cost=0.0, proceeds=0.0, code=code))
                    continue
                entry, sr = float(plan['entry_price']), float(plan['stock_return_before_fees'])
                qty = orderable_buy_quantity(ts_code=code, available_amount=min(equity_cost * weight, cash), execution_price=entry)
                step = 1 if market_segment(code) in ('star', 'bj') else 100
                while qty > 0:
                    g = qty * entry
                    if g + commission(g) + g * transfer <= cash + 1e-9:
                        break
                    qty -= step
                if qty <= 0:
                    free += 1
                    continue
                g = qty * entry
                cost = g + commission(g) + g * transfer
                sell = g * (1 + sr)
                proceeds = sell - commission(sell) - sell * transfer - sell * stamp_tax_rate_for_date(str(plan['exit_date']), STAMP)
                cash -= cost
                open_pos.append(dict(until=until, cost=cost, proceeds=proceeds, code=code))
                held.add(code)
                trades.append(dict(action_date=d, exit_date=str(plan['exit_date']), leg=plan['strategy_leg'], rank=int(plan['leg_rank']),
                                   code=code, cost=cost, proceeds=proceeds, ret=proceeds / cost - 1, weight=cost / equity_cost))
        eq_exit.append(cash + sum(p['cost'] for p in open_pos))
        eq_entry.append(cash + sum(p['proceeds'] for p in open_pos))
    idx = pd.to_datetime(pd.Index(dates))
    return pd.Series(eq_exit, index=idx), pd.Series(eq_entry, index=idx), pd.DataFrame(trades)
