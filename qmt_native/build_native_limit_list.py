"""Build Tushare-format limit_list CSVs from QMT data only, for the selection replay.

Membership (who closed limit-up) uses QMT daily bars plus the project's own
src/market_rules.py, excluding ST names like Tushare limit_list_d, and requires
the seal to still stand at the close (validated: close==limit without a closing
seal is not counted by Tushare). Fields come from native_limit_reconstruct with the
chosen rule. limit_times counts consecutive native limit-up days.

Usage: venv/bin/python build_native_limit_list.py OUT_RAW_LIMIT_DIR DATE...
Reads QMT exports from export_a/ and export_b/.
"""
import csv
import gzip
import sys
from pathlib import Path

import pandas as pd

REPO = Path('/Users/eass/Desktop/A_System')
sys.path.insert(0, str(REPO))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from src.market_rules import is_st_name, limit_up_price, price_limit_pct  # noqa: E402
from native_limit_reconstruct import reconstruct  # noqa: E402

RULE = 'bid_raw_merge_minute'
EXPORTS = [HERE / 'export_a', HERE / 'export_b']


def _tick_close(path):
    rows = list(csv.DictReader(gzip.open(path, 'rt')))
    last, pre = None, None
    for r in rows:
        stamp = str(r.get('ts', '')).split('.')[0]
        if stamp[8:14] <= '150500':
            if r.get('lastPrice'):
                last = float(r['lastPrice'])
            if r.get('lastClose'):
                pre = float(r['lastClose'])
    return last, pre


def load_daily():
    frames = [pd.read_csv(e / 'daily_bars.csv', dtype={'code': str, 'date': str}) for e in EXPORTS]
    bars = pd.concat(frames, ignore_index=True).drop_duplicates(['code', 'date'], keep='last')
    # QMT's local daily cache was stale for some names in the second export (0.69% rows).
    # Where a QMT tick file exists for a missing (code, day), take close and preClose from it.
    have = set(zip(bars.code, bars.date))
    extra = []
    for e in EXPORTS:
        for day_dir in sorted((e / 'ticks').glob('2026*')):
            for f in day_dir.glob('*.csv.gz'):
                code = f.name[:-len('.csv.gz')]
                if (code, day_dir.name) in have:
                    continue
                close, pre = _tick_close(f)
                if close and pre:
                    extra.append(dict(code=code, date=day_dir.name, close=close, preClose=pre))
                    have.add((code, day_dir.name))
    if extra:
        print('daily gaps patched from QMT ticks:', len(extra), sorted((x['date'], x['code']) for x in extra))
        bars = pd.concat([bars, pd.DataFrame(extra)], ignore_index=True)
    return bars.sort_values(['code', 'date'])


def load_details():
    d = pd.read_csv(HERE / 'export_b' / 'instrument_details.csv', dtype={'code': str, 'OpenDate': str})
    return d.set_index('code')


def calendar():
    cal = pd.read_csv(HERE / 'vm_raw/data/raw/trade_calendar.csv', dtype=str)
    return sorted(cal.loc[cal['is_open'].astype(str).isin(['1', '1.0']), 'cal_date'].unique())


def tick_path(day, code):
    for e in EXPORTS:
        p = e / 'ticks' / day / (code + '.csv.gz')
        if p.exists():
            return p
    return None


def main():
    out_dir = Path(sys.argv[1])
    days = sys.argv[2:]
    out_dir.mkdir(parents=True, exist_ok=True)
    bars = load_daily()
    details = load_details()
    cal = calendar()
    idx = {d: i for i, d in enumerate(cal)}

    # Daily-bar membership (close at limit, non-ST) for the whole exported window.
    candidates = {}
    for r in bars.itertuples():
        if r.date not in idx:
            continue
        name = str(details['InstrumentName'].get(r.code, '')) if r.code in details.index else ''
        if is_st_name(name):
            continue
        od = str(details['OpenDate'].get(r.code, '')) if r.code in details.index else ''
        n = idx[r.date] - idx[od] + 1 if od in idx else None
        pct = price_limit_pct(r.code, name=name, trade_date=r.date, listing_day_number=n)
        lim = limit_up_price(r.preClose, pct) if pct is not None else None
        if lim is not None and round(r.close * 100) == round(lim * 100):
            candidates.setdefault(r.date, {})[r.code] = (r, lim, name)

    stats = {}
    streak = {}
    for day in [d for d in cal if d in candidates and d <= max(days)]:
        rows = []
        today = {}
        for code, (r, lim, name) in candidates[day].items():
            p = tick_path(day, code)
            if p is None:
                # No ticks: only allowed for days outside the replay window (streak history).
                if day in days:
                    # Inside the replay window every Tushare limit-up has ticks, so a candidate
                    # without ticks is a close==limit name Tushare did not count; its closing seal
                    # cannot be checked, so it is excluded from the list and from the streak.
                    stats.setdefault(day, {}).setdefault('no_ticks_excluded', 0)
                    stats[day]['no_ticks_excluded'] += 1
                else:
                    today[code] = True  # history days: daily-bar membership only
                continue
            g = reconstruct(list(csv.DictReader(gzip.open(p, 'rt'))), lim, RULE)
            if not g['closed_sealed']:
                stats.setdefault(day, {}).setdefault('unsealed_excluded', 0)
                stats[day]['unsealed_excluded'] += 1
                continue
            today[code] = True
            rows.append(dict(trade_date=day, ts_code=code, name=name, close=r.close,
                             pct_chg=round((r.close / r.preClose - 1) * 100, 4),
                             fd_amount=g['fd_amount'], first_time=g['first_time'], last_time=g['last_time'],
                             open_times=g['open_times'], limit='U', limit_amount='',
                             limit_times=streak.get(code, 0) + 1, limit_data_source='qmt_native',
                             limit_data_quality='full', strategy_compatible=True))
        streak = {c: streak.get(c, 0) + 1 for c in today}
        if day in days:
            cols = ['trade_date', 'ts_code', 'name', 'close', 'pct_chg', 'fd_amount', 'first_time', 'last_time',
                    'open_times', 'limit', 'limit_amount', 'limit_times', 'limit_data_source',
                    'limit_data_quality', 'strategy_compatible']
            pd.DataFrame(rows, columns=cols).to_csv(out_dir / ('%s.csv' % day), index=False)
            stats.setdefault(day, {})['written'] = len(rows)
    for day in days:
        print(day, stats.get(day))


if __name__ == '__main__':
    main()
