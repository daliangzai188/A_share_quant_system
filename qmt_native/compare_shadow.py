"""Compare the QMT shadow export with production outputs for one signal date.

Usage: venv/bin/python compare_shadow.py COMPARE_DIR DAY
COMPARE_DIR is the unpacked as_compare_<DAY>.zip (production/ and shadow/ subfolders).
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def rd(path):
    try:
        return pd.read_csv(path, dtype={'ts_code': str, 'trade_date': str, 'first_time': str, 'last_time': str},
                           low_memory=False)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return None


def cmp_table(name, a, b, key, cols):
    if a is None or b is None:
        return '%-34s production=%s shadow=%s' % (name, 'missing' if a is None else len(a), 'missing' if b is None else len(b))
    ka, kb = set(map(tuple, a[key].astype(str).values)), set(map(tuple, b[key].astype(str).values))
    m = a.merge(b, on=key, suffixes=('_p', '_s'))
    diffs = []
    for c in cols:
        if c + '_p' not in m.columns or c + '_s' not in m.columns:
            continue
        x, y = m[c + '_p'], m[c + '_s']
        xn, yn = pd.to_numeric(x, errors='coerce'), pd.to_numeric(y, errors='coerce')
        same = (x.astype(str) == y.astype(str)) | (xn.notna() & yn.notna() & np.isclose(xn, yn, rtol=1e-6, atol=1e-9)) | (x.isna() & y.isna())
        if not same.all():
            diffs.append('%s:%d' % (c, int((~same).sum())))
    return '%-34s rows prod=%d shadow=%d only_prod=%d only_shadow=%d field_diffs=%s' % (
        name, len(a), len(b), len(ka - kb), len(kb - ka), ', '.join(diffs) or 'none')


def main():
    base, day = Path(sys.argv[1]), sys.argv[2]
    p, s = base / 'production', base / 'shadow'
    print('== raw limit list')
    print(cmp_table('limit_list', rd(p / ('data/raw/limit_list/%s.csv' % day)), rd(s / ('data/raw/limit_list/%s.csv' % day)),
                    ['ts_code'], ['first_time', 'last_time', 'open_times', 'fd_amount', 'limit_times', 'close']))
    print('== live processed')
    for name in ('live_limit_up_merged.csv', 'live_limit_up_fill_scored.csv', 'live_theme_heat_features.csv'):
        a, b = rd(p / 'data/processed' / name), rd(s / 'data/processed' / name)
        if a is not None:
            a = a[a['trade_date'] == day]
        if b is not None:
            b = b[b['trade_date'] == day]
        cols = [c for c in (a.columns if a is not None else []) if c not in ('trade_date', 'ts_code', 'limit_data_source')]
        print(cmp_table(name, a, b, ['ts_code'], cols))
    print('== A/C reports')
    for f in sorted((p / 'reports/paper_trade/ab_filtered_daily_ops').glob('*_%s_*.csv' % day)):
        a, b = rd(f), rd(s / 'reports/paper_trade/ab_filtered_daily_ops' / f.name)
        key = ['ts_code'] if a is not None and 'ts_code' in a.columns else list(a.columns[:1]) if a is not None else ['ts_code']
        cols = [c for c in (a.columns if a is not None else []) if c not in key][:60]
        print(cmp_table(f.name.replace('a_strict_plus_c_hold3_', ''), a, b, key, cols))
    print('== E')
    f = ('reports/strategy_e/e_signal_%s_candidates.csv' % day)
    a, b = rd(p / f), rd(s / f)
    print(cmp_table('e_candidates', a, b, ['ts_code'] if a is not None and 'ts_code' in a.columns else ['trade_date'],
                    [c for c in (a.columns if a is not None else []) if c != 'ts_code'][:60]))
    print('== equity-curve stop decision')
    for who, root in (('production', p), ('shadow', s)):
        path = root / 'data/state/equity_curve_stop_decision.json'
        if path.exists():
            d = json.loads(path.read_text(encoding='utf-8-sig'))
            print('%-10s action=%s allowed=%s lag_nav=%s lag_ma=%s last_nav=%s' % (
                who, d.get('action_date'), d.get('allowed'), d.get('lag_nav'), d.get('lag_ma'), d.get('shadow_last_nav')))
        else:
            print('%-10s missing' % who)


if __name__ == '__main__':
    main()
