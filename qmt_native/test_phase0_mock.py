"""Run the generated QMT phase-0 script end to end against a fake QMT API.

Catches runtime errors before the user spends time in QMT. Exercises: probe,
chunked daily export, tick export with list-valued depth columns, the async
first-pass-empty retry path, a hard failure, resume-after-restart and the
trading-hours pause.
"""
import gzip
import os
import shutil
import sys
import tempfile

import numpy as np
import pandas as pd

SCRIPT = sys.argv[1]
OUT = tempfile.mkdtemp(prefix='phase0_mock_')
DOWNLOADED = set()


def tick_frame(day, n=40, limit=10.0):
    stamps = ['%s%s' % (day, t) for t in
              [('0925%02d' % s) for s in range(0, 60, 3)][:n // 2] +
              [('0930%02d' % s) for s in range(0, 60, 3)][:n - n // 2]]
    rng = np.random.default_rng(0)
    frame = pd.DataFrame(index=stamps)
    frame['lastPrice'] = np.round(limit - rng.integers(0, 3, len(stamps)) * 0.01, 2)
    for col in ('open', 'high', 'low', 'lastClose'):
        frame[col] = limit - 0.5
    frame['amount'] = np.arange(len(stamps)) * 1000.0
    frame['volume'] = np.arange(len(stamps)) * 10
    frame['pvolume'] = frame['volume'] * 100
    frame['stockStatus'] = 3
    frame['transactionNum'] = np.arange(len(stamps))
    frame['bidPrice'] = [[limit, limit - 0.01, limit - 0.02, limit - 0.03, limit - 0.04]] * len(stamps)
    frame['bidVol'] = [[1000, 200, 300, 0, 5]] * len(stamps)
    frame['askPrice'] = [[0.0, 0.0, 0.0, 0.0, 0.0]] * len(stamps)
    frame['askVol'] = [[0, 0, 0, 0, 0]] * len(stamps)
    return frame


class FakeContext:
    do_back_test = False

    def __init__(self, empty_once, always_empty, explode):
        self.timers = []
        self.empty_once = set(empty_once)
        self.always_empty = set(always_empty)
        self.explode = set(explode)

    def run_time(self, name, period, start):
        self.timers.append((name, period, start))

    def get_stock_list_in_sector(self, name):
        return ['600000.SH', '000001.SZ', '920627.BJ'] * 1 + ['%06d.SZ' % i for i in range(1, 650)]

    def get_instrument_detail(self, code):
        return {'InstrumentName': u'\u6d4b\u8bd5,A', 'OpenDate': '20100101',
                'FloatVolume': 1e9, 'TotalVolume': 2e9, 'UpStopPrice': 11.0,
                'DownStopPrice': 9.0, 'PreClose': 10.0}

    def get_market_data_ex(self, fields, codes, period='follow', start_time='', end_time='',
                           count=-1, dividend_type='none', fill_data=False, subscribe=False):
        if period == '1d':
            out = {}
            for code in codes:
                if code.endswith('7.SZ') and (code, '1d') not in DOWNLOADED:
                    out[code] = pd.DataFrame()
                    continue
                idx = ['20260826', '20260827', '20261008']
                out[code] = pd.DataFrame({f: [1.0, 2.0, 3.0] for f in fields}, index=idx)
            return out
        code = codes[0]
        day = start_time[:8]
        key = (day, code)
        if key in self.explode:
            raise ValueError('boom')
        if key in self.always_empty:
            return {code: pd.DataFrame()}
        if key in self.empty_once:
            self.empty_once.discard(key)
            return {code: pd.DataFrame()}
        if period in ('limitupperformance', 'l2quote', 'l2transaction', 'l2order'):
            raise RuntimeError('unsupported period: ' + period)
        return {code: tick_frame(day)}


def load_module(ctx_downloads):
    source = open(SCRIPT, encoding='ascii').read()
    source = source.replace(r"OUT_DIR = r'C:\A_System\qmt_native_phase0\out'", 'OUT_DIR = %r' % OUT)
    ns = {'__name__': 'phase0'}
    ns['download_history_data'] = lambda *a: (ctx_downloads.append(a), DOWNLOADED.add((a[0], a[1])))
    ns['get_sector_list'] = lambda: [u'\u6caa\u6df1A\u80a1', u'\u6982\u5ff5\u677f\u5757']
    exec(compile(source, SCRIPT, 'exec'), ns)
    return ns


def main():
    downloads = []
    ns = load_module(downloads)
    events = ns['EVENTS']
    first, second, third = events[0], events[1], events[2]
    ctx = FakeContext(empty_once=[first], always_empty=[second], explode=[third])

    ns['_paused'] = lambda: True
    ns['init'](ctx)
    ns['phase0_pump'](ctx)
    assert ns['S']['phase'] == 'probe', 'pause must block all work'
    ns['_paused'] = lambda: False

    # Stop half way through the ticks to test resume.
    for _ in range(40):
        ns['phase0_pump'](ctx)
    done_before_restart = ns['S']['i']
    assert ns['S']['phase'] == 'ticks' and done_before_restart > 0

    ns2 = load_module(downloads)
    ns2['_paused'] = lambda: False
    ns2['init'](ctx)
    guard = 0
    while not ns2['S']['done'] and guard < 5000:
        ns2['phase0_pump'](ctx)
        guard += 1
    S = ns2['S']
    assert S['done'], 'did not finish'

    probe = open(os.path.join(OUT, 'probe.json')).read()
    assert 'depth_tick' in probe and 'limitup_600825.SH' in probe and 'sector_count' in probe
    bars = open(os.path.join(OUT, 'daily_bars.csv')).read().splitlines()
    details = open(os.path.join(OUT, 'instrument_details.csv'), encoding='utf-8').read().splitlines()
    index = open(os.path.join(OUT, 'ticks_index.csv')).read().splitlines()
    errors = os.path.exists(os.path.join(OUT, 'errors.log')) and open(os.path.join(OUT, 'errors.log')).read()
    sample = os.path.join(OUT, 'ticks', first[0], first[1] + '.csv.gz')
    with gzip.open(sample, 'rt') as handle:
        rows = handle.read().splitlines()
    header = rows[0].split(',')
    values = dict(zip(header, rows[1].split(',')))

    print('timers               ', ctx.timers)
    print('pumps after restart  ', guard, '| events done before restart', done_before_restart)
    print('counts (2nd process) ', S['counts'])
    print('daily rows           ', len(bars) - 1, '| header', bars[0])
    print('details rows         ', len(details) - 1, '| sample', details[1])
    print('index lines          ', len(index), '| EMPTY', sum(1 for l in index if l.endswith('EMPTY')),
          '| FAIL', sum(1 for l in index if ',FAIL ' in l))
    print('tick file cols       ', len(header), '| first row', {k: values[k] for k in ('ts', 'lastPrice', 'bidPrice1', 'bidVol1', 'askPrice1')})
    print('errors.log           ', repr(errors)[:300])
    ok_files = sum(len(fs) for _, _, fs in os.walk(os.path.join(OUT, 'ticks')))
    print('tick files on disk   ', ok_files, 'of', len(events))
    assert len(header) == 1 + 10 + 20
    assert values['bidPrice1'] and values['bidVol1'] == '1000'
    assert any(l.startswith('%s,%s,' % second) and l.endswith('EMPTY') for l in index)
    assert any(l.startswith('%s,%s,' % third) and ',FAIL ' in l for l in index)
    assert os.path.exists(sample), 'empty-once event must succeed on retry'
    assert not errors, 'unexpected errors'
    meta = open(os.path.join(OUT, 'daily_meta.txt')).read()
    assert 'missing_first_read=' in meta and 'still_missing=0' in meta and 'still_missing=1' not in meta, meta
    print('daily fallback       ', [l for l in meta.splitlines() if 'missing' in l][:2])
    print('MOCK RUN PASSED')


if __name__ == '__main__':
    try:
        main()
    finally:
        shutil.rmtree(OUT, ignore_errors=True)
