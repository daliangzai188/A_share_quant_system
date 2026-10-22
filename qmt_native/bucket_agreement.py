"""Exact and bucket-level agreement of rebuilt limit fields vs Tushare, per rule variant.
Bucket edges copied from src/factors.py (open_times_bucket, first_time_detail_bucket)."""
import gzip, csv, sys, json
from pathlib import Path
import pandas as pd
from native_limit_reconstruct import VARIANTS, reconstruct

def open_bucket(n):
    return '0' if n <= 0 else '1' if n <= 1 else '2_3' if n <= 3 else 'gte_4'

def first_bucket(t):
    if not t: return 'nan'
    t = str(t).split('.')[0].zfill(6); m = int(t[:2]) * 60 + int(t[2:4])
    for edge, lab in ((570, 'open_auction'), (600, 'before_1000'), (660, '1000_1100'),
                      (810, '1100_1330'), (870, '1330_1430')):
        if m <= edge: return lab
    return 'after_1430'

def main(export, raw, dates, out_csv):
    truth = []
    for d in dates:
        p = Path(raw) / 'limit_list' / (d + '.csv')
        if p.exists():
            t = pd.read_csv(p, dtype={'ts_code': str, 'first_time': str, 'last_time': str})
            truth.append(t[t['limit'] == 'U'])
    truth = pd.concat(truth, ignore_index=True)
    for c in ('first_time', 'last_time'):
        truth[c] = truth[c].fillna('').astype(str).str.split('.').str[0].str.zfill(6)
    ticks = {}
    for r in truth.itertuples():
        p = Path(export) / 'ticks' / str(r.trade_date) / (r.ts_code + '.csv.gz')
        if p.exists():
            ticks[(str(r.trade_date), r.ts_code)] = (list(csv.DictReader(gzip.open(p, 'rt'))), r)
    rows = []
    for v in VARIANTS:
        s = dict(n=0, first=0, last=0, open=0, all3=0, ob=0, fb=0, both_b=0)
        for (d, code), (tk, r) in ticks.items():
            g = reconstruct(tk, r.close, v)
            eo = int(r.open_times)
            s['n'] += 1
            s['first'] += g['first_time'] == r.first_time
            s['last'] += g['last_time'] == r.last_time
            s['open'] += g['open_times'] == eo
            s['all3'] += g['first_time'] == r.first_time and g['last_time'] == r.last_time and g['open_times'] == eo
            ob = open_bucket(g['open_times']) == open_bucket(eo)
            fb = first_bucket(g['first_time']) == first_bucket(r.first_time)
            s['ob'] += ob; s['fb'] += fb; s['both_b'] += ob and fb
        n = s['n'] or 1
        rows.append([v, s['n']] + ['%.1f%%' % (100.0 * s[k] / n) for k in ('first', 'last', 'open', 'all3', 'ob', 'fb', 'both_b')])
    df = pd.DataFrame(rows, columns=['规则', '样本', '首封精确', '末封精确', '炸板精确', '三项精确', '炸板分档', '首封分档', '两项分档都对'])
    df.to_csv(out_csv, index=False)
    print(df.to_string(index=False))

if __name__ == '__main__':
    ev = json.load(open(sys.argv[3]))
    main(sys.argv[1], sys.argv[2], ev['dates'], sys.argv[4])
