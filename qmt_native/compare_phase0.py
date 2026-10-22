"""Compare the QMT phase-0 export with the Tushare data the strategy uses today.

Usage: python3 compare_phase0.py [--out EXPORT_DIR] [--raw TUSHARE_RAW_DIR] [--report REPORT_DIR]

Runs on partial exports. Writes summary.md plus mismatch CSVs so every
difference can be inspected, not just counted.
"""
import argparse
import gzip
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from native_limit_reconstruct import VARIANTS, reconstruct  # noqa: E402

DEFAULT_OUT = Path('/Users/eass/Desktop/A_System/qmt_native_phase0/out')
DEFAULT_RAW = Path('/Users/eass/Desktop/A_System/data/raw')


def load_ticks(path):
    with gzip.open(path, 'rt') as handle:
        return list(csv.DictReader(handle))


def tushare_limit(raw, dates):
    frames = []
    for day in dates:
        path = raw / 'limit_list' / ('%s.csv' % day)
        if path.exists():
            frames.append(pd.read_csv(path, dtype={'trade_date': str, 'ts_code': str,
                                                   'first_time': str, 'last_time': str}))
    data = pd.concat(frames, ignore_index=True)
    data = data[data['limit'] == 'U'].copy()
    for col in ('first_time', 'last_time'):
        data[col] = data[col].fillna('').astype(str).str.split('.').str[0].str.zfill(6)
    return data


def pct_bucket(rel):
    if rel is None:
        return 'missing'
    rel = abs(rel)
    if rel == 0:
        return 'exact'
    if rel < 1e-4:
        return '<0.01%'
    if rel < 1e-3:
        return '<0.1%'
    if rel < 1e-2:
        return '<1%'
    return '>=1%'


def compare_limits(out, truth, report):
    index_path = out / 'ticks_index.csv'
    rows = []
    variants_summary = {}
    for variant in VARIANTS:
        stats = Counter()
        fd_dist = Counter()
        for rec in truth.itertuples():
            path = out / 'ticks' / rec.trade_date / ('%s.csv.gz' % rec.ts_code)
            if not path.exists():
                stats['no_ticks'] += 1
                continue
            ticks = load_ticks(path)
            got = reconstruct(ticks, rec.close, variant)
            stats['compared'] += 1
            ok_first = got['first_time'] == rec.first_time
            ok_last = got['last_time'] == rec.last_time
            ok_open = got['open_times'] == int(rec.open_times)
            stats['first_ok'] += ok_first
            stats['last_ok'] += ok_last
            stats['open_ok'] += ok_open
            stats['all_three_ok'] += ok_first and ok_last and ok_open
            stats['closed_sealed'] += got['closed_sealed']
            rel = None
            if got['fd_amount'] is not None and rec.fd_amount and rec.fd_amount == rec.fd_amount:
                rel = (got['fd_amount'] - float(rec.fd_amount)) / float(rec.fd_amount)
            fd_dist[pct_bucket(rel)] += 1
            if not (ok_first and ok_last and ok_open) or pct_bucket(rel) not in ('exact', '<0.01%'):
                rows.append(dict(variant=variant, trade_date=rec.trade_date, ts_code=rec.ts_code,
                                 exp_first=rec.first_time, got_first=got['first_time'],
                                 exp_last=rec.last_time, got_last=got['last_time'],
                                 exp_open=int(rec.open_times), got_open=got['open_times'],
                                 exp_fd=rec.fd_amount, got_fd=got['fd_amount'],
                                 snapshots=got['snapshots'], unknown=got['unknown']))
        variants_summary[variant] = (stats, fd_dist)
    pd.DataFrame(rows).to_csv(report / 'limit_mismatches.csv', index=False)
    return variants_summary, index_path.exists()


def compare_daily(out, raw, dates, report):
    path = out / 'daily_bars.csv'
    if not path.exists():
        return None
    qmt = pd.read_csv(path, dtype={'code': str, 'date': str})
    ts = pd.concat([pd.read_csv(raw / 'daily' / ('%s.csv' % d), dtype={'ts_code': str, 'trade_date': str})
                    for d in dates if (raw / 'daily' / ('%s.csv' % d)).exists()], ignore_index=True)
    merged = qmt.merge(ts, left_on=['code', 'date'], right_on=['ts_code', 'trade_date'], how='inner',
                       suffixes=('_q', '_t'))
    result = {'qmt_rows': len(qmt), 'tushare_rows': len(ts), 'matched_rows': len(merged)}
    for q, t in (('open_q', 'open_t'), ('high_q', 'high_t'), ('low_q', 'low_t'), ('close_q', 'close_t'),
                 ('preClose', 'pre_close')):
        result[t.replace('_t', '') + '_cent_equal'] = float(((merged[q] * 100).round() == (merged[t] * 100).round()).mean())
    result['vol_equal_lots'] = float((merged['volume'].round() == merged['vol'].round()).mean())
    result['amount_rel_lt_1e-4'] = float(((merged['amount_q'] - merged['amount_t'] * 1000).abs()
                                          <= merged['amount_t'].abs() * 1000 * 1e-4).mean())
    bad = merged[(merged['close_q'] * 100).round() != (merged['close_t'] * 100).round()]
    bad.head(500).to_csv(report / 'daily_close_mismatches.csv', index=False)
    return result, qmt


def compare_basic(out, raw, dates, qmt_daily, report):
    path = out / 'instrument_details.csv'
    if not path.exists() or qmt_daily is None:
        return None
    detail = pd.read_csv(path, dtype={'code': str})
    last = dates[-1]
    basic = pd.read_csv(raw / 'daily_basic' / ('%s.csv' % last), dtype={'ts_code': str})
    merged = detail.merge(basic, left_on='code', right_on='ts_code', how='inner')
    result = {'details': len(detail), 'matched': len(merged)}
    float_ratio = merged['FloatVolume'] / (merged['float_share'] * 1e4)
    result['float_share_within_0.1%'] = float((float_ratio - 1).abs().lt(1e-3).mean())

    # Turnover and volume ratio rebuilt from QMT bars with Tushare definitions.
    bars = qmt_daily.sort_values(['code', 'date']).copy()
    bars['vol_prev5_mean'] = bars.groupby('code')['volume'].transform(lambda s: s.shift(1).rolling(5).mean())
    bars['vr_native'] = bars['volume'] / bars['vol_prev5_mean']
    allb = pd.concat([pd.read_csv(raw / 'daily_basic' / ('%s.csv' % d), dtype={'ts_code': str, 'trade_date': str})
                      for d in dates if (raw / 'daily_basic' / ('%s.csv' % d)).exists()], ignore_index=True)
    vm = bars.merge(allb, left_on=['code', 'date'], right_on=['ts_code', 'trade_date'], how='inner')
    vm = vm.dropna(subset=['vr_native', 'volume_ratio'])
    result['volume_ratio_rows'] = len(vm)
    result['volume_ratio_2dp_equal'] = float((vm['vr_native'].round(2) == vm['volume_ratio'].round(2)).mean()) if len(vm) else None
    fs = vm.merge(detail[['code', 'FloatVolume']], on='code', how='left')
    fs['tr_native'] = fs['volume'] * 100 / fs['FloatVolume'] * 100
    result['turnover_4dp_equal_using_current_float'] = float((fs['tr_native'].round(4) == fs['turnover_rate'].round(4)).mean()) if len(fs) else None
    vm[['code', 'date', 'volume', 'vol_prev5_mean', 'vr_native', 'volume_ratio']].head(2000).to_csv(
        report / 'volume_ratio_sample.csv', index=False)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--raw', type=Path, default=DEFAULT_RAW)
    parser.add_argument('--report', type=Path, default=HERE / 'phase0_report')
    parser.add_argument('--events', default='phase0_events.json')
    args = parser.parse_args()
    args.report.mkdir(parents=True, exist_ok=True)
    events = json.load(open(HERE / args.events))
    dates = events['dates']

    lines = ['# 阶段0 对比报告', '']
    progress = args.out / 'progress.json'
    if progress.exists():
        lines += ['导出进度：`%s`' % progress.read_text().replace('\n', ' '), '']
    probe = args.out / 'probe.json'
    if probe.exists():
        p = json.load(open(probe))
        lines += ['## QMT 能力探测', '',
                  '- 逐笔历史深度：%s' % p.get('depth_tick'),
                  '- 1 分钟历史深度：%s' % p.get('depth_1m'),
                  '- 涨停特色数据：%s / %s' % (p.get('limitup_600825.SH'), p.get('limitup_002058.SZ')),
                  '- L2：%s' % {k: v for k, v in p.items() if k.startswith('l2_')},
                  '- 板块数量：%s' % p.get('sector_count'), '']

    truth = tushare_limit(args.raw, dates)
    summary, _ = compare_limits(args.out, truth, args.report)
    lines += ['## 涨停字段重建（Tushare 涨停池 %d 个事件）' % len(truth), '',
              '| 规则 | 有逐笔 | 首封一致 | 最后封板一致 | 炸板次数一致 | 三项全一致 | 收盘仍封住 | 封单金额 精确/≤0.01%/≤0.1%/≤1%/更大 |',
              '|---|---|---|---|---|---|---|---|']
    for variant, (s, fd) in summary.items():
        n = s['compared'] or 1
        lines.append('| %s | %d | %.1f%% | %.1f%% | %.1f%% | **%.1f%%** | %.1f%% | %d / %d / %d / %d / %d |' % (
            variant, s['compared'], 100 * s['first_ok'] / n, 100 * s['last_ok'] / n,
            100 * s['open_ok'] / n, 100 * s['all_three_ok'] / n, 100 * s['closed_sealed'] / n,
            fd['exact'], fd['<0.01%'], fd['<0.1%'], fd['<1%'], fd['>=1%']))
    lines += ['', '逐条差异：`limit_mismatches.csv`', '']

    probe_events = events.get('extra_membership_probe', [])
    if probe_events:
        lines += ['## 收盘价=涨停价但 Tushare 未计入涨停的事件', '',
                  '推测：收盘时买一没有封单，Tushare 只把「收盘封住」算作涨停。', '',
                  '| 日期 | 代码 | 收盘仍封住 | 收盘买一量 | 判定 |', '|---|---|---|---|---|']
        daily_raw = {}
        for day, code in probe_events:
            path = args.out / 'ticks' / day / ('%s.csv.gz' % code)
            if not path.exists():
                lines.append('| %s | %s | 无逐笔 | | |' % (day, code))
                continue
            if day not in daily_raw:
                daily_raw[day] = pd.read_csv(args.raw / 'daily' / ('%s.csv' % day), dtype={'ts_code': str})
            close = float(daily_raw[day].loc[daily_raw[day].ts_code == code, 'close'].iloc[0])
            ticks = load_ticks(path)
            got = reconstruct(ticks, close)
            last = ticks[-1] if ticks else {}
            lines.append('| %s | %s | %s | %s | %s |' % (
                day, code, got['closed_sealed'], last.get('bidVol1', ''),
                '推测成立' if not got['closed_sealed'] else '推测不成立，需另查'))
        lines.append('')

    daily = compare_daily(args.out, args.raw, dates, args.report)
    if daily:
        res, qmt_daily = daily
        lines += ['## 日线', ''] + ['- %s: %s' % kv for kv in res.items()] + ['']
        basic = compare_basic(args.out, args.raw, dates, qmt_daily, args.report)
        if basic:
            lines += ['## 股本 / 换手率 / 量比', ''] + ['- %s: %s' % kv for kv in basic.items()] + ['']
    (args.report / 'summary.md').write_text('\n'.join(lines), encoding='utf-8')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
