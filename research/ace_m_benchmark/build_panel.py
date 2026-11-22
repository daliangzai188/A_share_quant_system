# coding: utf-8
"""Build a dense research panel (dates x stocks, float32 matrices in one .npz) from Tushare-style
per-day CSV folders. Later roots override earlier ones for the same day.

  python build_panel.py OUT.npz ROOT [ROOT ...] [--end YYYYMMDD]
  each ROOT may contain daily/ daily_basic/ adj_factor/ limit_list/ (or limit_list_d/) per-day CSVs.

2026-11-23 从 ~/A_System_复核/2026-11-11_M大法研究/scripts/ 移入 git，只加了 --end（只用这一天及以前的文件；
同步工作区 data/raw 每天在长，不截就会多出新日子）。11-11 建基准面板的命令见 README。
"""
import glob, os, sys
import numpy as np
import pandas as pd

args = sys.argv[1:]
END = None
if '--end' in args:
    k = args.index('--end')
    END = args[k + 1]
    del args[k:k + 2]
out, roots = args[0], args[1:]

DAILY = ['open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount']
BASIC = ['turnover_rate', 'turnover_rate_f', 'volume_ratio', 'pe_ttm', 'pb', 'total_mv', 'circ_mv', 'float_share']
LIMIT = ['fd_amount', 'open_times', 'limit_times', 'first_time']


def day_files(folder_names):
    files = {}
    for root in roots:
        for name in folder_names:
            for path in glob.glob(os.path.join(root, name, '2*.csv')):
                day = os.path.basename(path)[:8]
                if END is None or day <= END:
                    files[day] = path
    return files


daily_files = day_files(['daily'])
dates = sorted(daily_files)
print('days', len(dates), dates[0], dates[-1], flush=True)


def load(files, cols, extra=None):
    frames = []
    for d in dates:
        p = files.get(d)
        if not p:
            continue
        f = pd.read_csv(p, encoding='utf-8-sig', dtype={'trade_date': str, 'first_time': str})
        if f.empty:
            continue
        f['trade_date'] = d
        keep = ['ts_code', 'trade_date'] + [c for c in cols + (extra or []) if c in f.columns]
        frames.append(f[keep])
    return pd.concat(frames, ignore_index=True)


daily = load(daily_files, DAILY)
codes = np.array(sorted(daily['ts_code'].unique()))
cidx = pd.Index(codes)
didx = pd.Index(dates)
print('stocks', len(codes), flush=True)


def to_matrix(frame, col, dtype=np.float32, fill=np.nan):
    m = np.full((len(dates), len(codes)), fill, dtype=dtype)
    f = frame[frame['ts_code'].isin(cidx)]
    r = didx.get_indexer(f['trade_date'])
    c = cidx.get_indexer(f['ts_code'])
    ok = r >= 0
    m[r[ok], c[ok]] = pd.to_numeric(f[col], errors='coerce').values[ok]
    return m


arrays = {c: to_matrix(daily, c) for c in DAILY}
del daily
basic = load(day_files(['daily_basic']), BASIC)
for c in BASIC:
    arrays[c] = to_matrix(basic, c)
del basic
adj = load(day_files(['adj_factor']), ['adj_factor'])
arrays['adj_factor'] = to_matrix(adj, 'adj_factor')
del adj
lim = load(day_files(['limit_list', 'limit_list_d']), LIMIT, extra=['limit'])
lim['first_time'] = pd.to_numeric(lim['first_time'].astype(str).str.replace(':', ''), errors='coerce')
for c in LIMIT:
    arrays['ll_' + c] = to_matrix(lim, c)
# limit status from the official list: 1 = closed at limit-up (U), -1 = limit-down (D), 2 = touched & opened (Z)
lim['code'] = lim['limit'].map({'U': 1, 'D': -1, 'Z': 2})
arrays['ll_status'] = to_matrix(lim, 'code', dtype=np.int8, fill=0)
lim_days = sorted(lim['trade_date'].unique())
np.savez(out, dates=np.array(dates), codes=codes, limit_list_days=np.array(lim_days), **arrays)
print('saved', out, {k: v.shape for k, v in list(arrays.items())[:1]}, 'limit days', len(lim_days), lim_days[0], lim_days[-1])
