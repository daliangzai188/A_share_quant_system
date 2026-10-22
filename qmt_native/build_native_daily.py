"""Build Tushare-format daily and daily_basic CSVs from QMT exports only.

daily: open/high/low/close/pre_close from QMT 1d bars (dividend_type none), vol in
lots, amount converted from CNY to Tushare's thousand-CNY. Zero-volume rows are
dropped (Tushare omits suspended days).
daily_basic: turnover_rate = vol*100/FloatVolume*100, volume_ratio = vol / mean of
previous 5 trading days' vol, float/total share in 10k shares, circ_mv/total_mv in
10k CNY. free_share and turnover_rate_f are left empty: QMT has no free float and
the selection kernel does not use them (only the Tushare collector's completeness gate).

Replay caveat: FloatVolume is today's value, so historical turnover/circ_mv can differ
where shares unlocked inside the window. Live use takes the same-day value.

Usage: venv/bin/python build_native_daily.py OUT_RAW_DIR DATE...
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
EXPORTS = [HERE / 'export_a', HERE / 'export_b']


def main():
    out = Path(sys.argv[1])
    days = sys.argv[2:]
    bars = pd.concat([pd.read_csv(e / 'daily_bars.csv', dtype={'code': str, 'date': str}) for e in EXPORTS],
                     ignore_index=True).drop_duplicates(['code', 'date'], keep='last')
    bars = bars[bars['volume'] > 0].sort_values(['code', 'date']).copy()
    det = pd.read_csv(HERE / 'export_b' / 'instrument_details.csv', dtype={'code': str}).set_index('code')
    bars['vol_prev5'] = bars.groupby('code')['volume'].transform(lambda s: s.shift(1).rolling(5).mean())
    bars['float_vol'] = bars['code'].map(det['FloatVolume'])
    bars['total_vol'] = bars['code'].map(det['TotalVolume'])
    (out / 'daily').mkdir(parents=True, exist_ok=True)
    (out / 'daily_basic').mkdir(parents=True, exist_ok=True)
    for day in days:
        d = bars[bars['date'] == day]
        if d.empty:
            print(day, 'no bars'); continue
        daily = pd.DataFrame({
            'ts_code': d['code'], 'trade_date': day, 'open': d['open'], 'high': d['high'], 'low': d['low'],
            'close': d['close'], 'pre_close': d['preClose'], 'change': (d['close'] - d['preClose']).round(4),
            'pct_chg': ((d['close'] / d['preClose'] - 1) * 100).round(4), 'vol': d['volume'],
            'amount': (d['amount'] / 1000.0).round(4)})
        daily.to_csv(out / 'daily' / ('%s.csv' % day), index=False)
        fs = d['float_vol'] / 1e4
        ts = d['total_vol'] / 1e4
        basic = pd.DataFrame({
            'ts_code': d['code'], 'trade_date': day,
            'turnover_rate': (d['volume'] * 100 / d['float_vol'] * 100).round(4),
            'turnover_rate_f': np.nan,
            'volume_ratio': (d['volume'] / d['vol_prev5']).round(2),
            'pe': np.nan, 'pe_ttm': np.nan, 'pb': np.nan, 'ps': np.nan, 'ps_ttm': np.nan,
            'dv_ratio': np.nan, 'dv_ttm': np.nan,
            'total_share': ts.round(4), 'float_share': fs.round(4), 'free_share': np.nan,
            'total_mv': (d['close'] * ts).round(4), 'circ_mv': (d['close'] * fs).round(4)})
        basic.to_csv(out / 'daily_basic' / ('%s.csv' % day), index=False)
        print(day, 'daily', len(daily), 'basic', len(basic))


if __name__ == '__main__':
    main()
