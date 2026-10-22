"""Can the daily limit-up list (who closed at limit-up) and limit_times be derived
from daily bars plus the project's own market rules, without Tushare limit_list?

Daily bars here are Tushare's, standing in for QMT 1d bars (phase 0 separately
checks QMT daily == Tushare daily). Names and list dates come from the current
stock_basic snapshot, the same information QMT's instrument detail provides.

Usage: venv/bin/python validate_membership.py START END
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path('/Users/eass/Desktop/A_System')
sys.path.insert(0, str(REPO))
from src.market_rules import limit_up_price, price_limit_pct  # noqa: E402

RAW = REPO / 'data/raw'
OUT = Path(__file__).resolve().parent / 'membership_report'


def calendar():
    cal = pd.read_csv(RAW / 'trade_calendar.csv', dtype=str)
    return sorted(cal.loc[cal['is_open'].astype(str).isin(['1', '1.0']), 'cal_date'].unique())


def main():
    start, end = sys.argv[1], sys.argv[2]
    OUT.mkdir(exist_ok=True)
    days = calendar()
    window = [d for d in days if start <= d <= end]
    basic = pd.read_csv(RAW / 'stock_basic/stock_basic_all.csv', dtype=str)
    names = dict(zip(basic['ts_code'], basic['name']))
    list_dates = dict(zip(basic['ts_code'], basic['list_date']))
    day_index = {d: i for i, d in enumerate(days)}

    derived = {}
    for d in window:
        bars = pd.read_csv(RAW / 'daily' / ('%s.csv' % d), dtype={'ts_code': str})
        hits = set()
        for r in bars.itertuples():
            ld = list_dates.get(r.ts_code)
            n = None
            if isinstance(ld, str) and ld in day_index and d in day_index:
                n = day_index[d] - day_index[ld] + 1
            pct = price_limit_pct(r.ts_code, name=names.get(r.ts_code, ''), trade_date=d, listing_day_number=n)
            limit = limit_up_price(r.pre_close, pct) if pct is not None else None
            if limit is not None and round(r.close * 100) == round(limit * 100):
                hits.add(r.ts_code)
        derived[d] = hits

    rows, streak = [], {}
    total = Counter = {'tp': 0, 'fp': 0, 'fn': 0, 'lt_ok': 0, 'lt_n': 0}
    for d in window:
        truth = pd.read_csv(RAW / 'limit_list' / ('%s.csv' % d), dtype={'ts_code': str})
        truth = truth[truth['limit'] == 'U']
        tset = set(truth['ts_code'])
        dset = derived[d]
        new_streak = {}
        for code in dset:
            new_streak[code] = streak.get(code, 0) + 1
        streak = new_streak
        total['tp'] += len(tset & dset)
        total['fp'] += len(dset - tset)
        total['fn'] += len(tset - dset)
        for code in dset - tset:
            rows.append(dict(trade_date=d, ts_code=code, kind='derived_only', name=names.get(code, '')))
        for code in tset - dset:
            rows.append(dict(trade_date=d, ts_code=code, kind='tushare_only', name=names.get(code, '')))
        if d >= window[min(len(window) - 1, 15)]:  # allow streak history to warm up
            for r in truth.itertuples():
                if r.ts_code in streak:
                    total['lt_n'] += 1
                    ok = int(r.limit_times) == streak[r.ts_code]
                    total['lt_ok'] += ok
                    if not ok:
                        rows.append(dict(trade_date=d, ts_code=r.ts_code, kind='limit_times',
                                         name='expected %s derived %s' % (r.limit_times, streak[r.ts_code])))
    pd.DataFrame(rows).to_csv(OUT / 'membership_mismatches.csv', index=False)
    tp, fp, fn = total['tp'], total['fp'], total['fn']
    print('窗口 %s-%s 共 %d 个交易日' % (window[0], window[-1], len(window)))
    print('Tushare 涨停 %d 次 | 规则推导命中 %d | 多判 %d | 漏判 %d' % (tp + fn, tp, fp, fn))
    print('精确率 %.3f%%  召回率 %.3f%%' % (100.0 * tp / max(1, tp + fp), 100.0 * tp / max(1, tp + fn)))
    print('连板数一致 %d / %d = %.2f%%（预热 15 天后统计）' % (total['lt_ok'], total['lt_n'], 100.0 * total['lt_ok'] / max(1, total['lt_n'])))
    print('差异明细: %s' % (OUT / 'membership_mismatches.csv'))


if __name__ == '__main__':
    main()
