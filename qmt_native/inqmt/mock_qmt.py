# coding: utf-8
"""Offline stand-in for QMT's market data API, fed by the phase-0 QMT exports.

Serves get_stock_list_in_sector / get_market_data_ex (1d and tick) / get_instrument_detail /
get_full_tick / get_trading_dates with the same shapes QMT returns, so the native collector
can be tested outside QMT. Python 3.6 compatible.
"""
import csv
import gzip
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
EXPORTS = [os.path.join(BASE, 'export_a'), os.path.join(BASE, 'export_b')]


class MockMarket(object):
    def __init__(self, today=None):
        frames = [pd.read_csv(os.path.join(e, 'daily_bars.csv'), dtype={'code': str, 'date': str}) for e in EXPORTS]
        bars = pd.concat(frames, ignore_index=True).drop_duplicates(['code', 'date'], keep='last')
        self.bars = dict((code, g.sort_values('date')) for code, g in bars.groupby('code'))
        det = pd.read_csv(os.path.join(EXPORTS[1], 'instrument_details.csv'), dtype={'code': str, 'OpenDate': str})
        self.details = dict((r['code'], r) for r in det.to_dict('records'))
        self.today = today
        self.calls = {'1d': 0, 'tick': 0, 'detail': 0, 'full_tick': 0}

    # ContextInfo API --------------------------------------------------------
    def get_stock_list_in_sector(self, sector):
        if sector == u'沪深A股':
            return sorted(c for c in self.details if not c.endswith('.BJ'))
        if sector == u'京市A股':
            return sorted(c for c in self.details if c.endswith('.BJ'))
        return []

    def get_instrument_detail(self, code):
        self.calls['detail'] += 1
        r = self.details.get(code)
        if r is None:
            return {}
        out = dict(r)
        out['OpenDate'] = str(out.get('OpenDate', ''))
        return out

    def get_market_data_ex(self, fields, codes, period='1d', start_time='', end_time='', count=-1,
                           dividend_type='none', fill_data=False, subscribe=False):
        out = {}
        if period == '1d':
            self.calls['1d'] += 1
            for code in codes:
                g = self.bars.get(code)
                if g is None:
                    continue
                g = g[(g['date'] >= start_time[:8]) & (g['date'] <= end_time[:8])]
                frame = g.set_index('date')[fields] if fields else g.set_index('date')
                out[code] = frame
            return out
        if period == 'tick':
            self.calls['tick'] += 1
            day = start_time[:8]
            for code in codes:
                frame = self._tick_frame(day, code)
                if frame is not None:
                    out[code] = frame
            return out
        raise ValueError('mock period not supported: %s' % period)

    def _tick_path(self, day, code):
        for e in EXPORTS:
            p = os.path.join(e, 'ticks', day, code + '.csv.gz')
            if os.path.exists(p):
                return p
        return None

    def _tick_frame(self, day, code):
        p = self._tick_path(day, code)
        if p is None:
            return None
        rows = list(csv.DictReader(gzip.open(p, 'rt')))
        if not rows:
            return None
        idx = [r['ts'] for r in rows]

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return np.nan
        data = {}
        for col in ('lastPrice', 'open', 'high', 'low', 'lastClose', 'amount', 'volume'):
            data[col] = [num(r.get(col)) for r in rows]
        for name in ('bidPrice', 'bidVol', 'askPrice', 'askVol'):
            data[name] = [[num(r.get('%s%d' % (name, k))) for k in range(1, 6)] for r in rows]
        return pd.DataFrame(data, index=idx)

    def get_full_tick(self, codes):
        self.calls['full_tick'] += 1
        out = {}
        for code in codes:
            frame = self._tick_frame(self.today, code) if self.today else None
            if frame is None:
                continue
            last = frame[[s[8:14] <= '150100' for s in frame.index]].iloc[-1]
            out[code] = {'timetag': '%s %s:%s:%s' % (self.today, '15', '00', '00'), 'lastPrice': last['lastPrice'],
                         'open': last['open'], 'high': last['high'], 'low': last['low'],
                         'lastClose': last['lastClose'], 'volume': last['volume'], 'amount': last['amount']}
        return out


def mock_globals():
    return {'download_history_data': lambda code, period, start, end: None}
