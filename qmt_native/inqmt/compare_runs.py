"""Compare an in-memory run (run_local.py output) with the reference replay workspace.

Byte-identical files pass outright. Other CSVs are compared as tables: same rows,
same columns, strings equal, numbers equal within 1e-9 relative. JSON files are
compared without wall-clock fields (generated_at/updated_at).

Usage: venv/bin/python compare_runs.py REF_WS RUN_OUT DAYS_FILE
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CLOCK_KEYS = {'generated_at', 'updated_at', 'run_at', 'finished_at', 'started_at'}


def strip_clock(obj):
    if isinstance(obj, dict):
        return {k: strip_clock(v) for k, v in obj.items() if k not in CLOCK_KEYS}
    if isinstance(obj, list):
        return [strip_clock(v) for v in obj]
    return obj


def frame_diff(a_path, b_path):
    a = pd.read_csv(a_path, dtype=str, keep_default_na=False)
    b = pd.read_csv(b_path, dtype=str, keep_default_na=False)
    if list(a.columns) != list(b.columns):
        extra = [c for c in b.columns if c not in a.columns]
        missing = [c for c in a.columns if c not in b.columns]
        if set(a.columns) == set(b.columns):
            return 'column order differs'
        return 'columns differ: missing %s extra %s' % (missing[:8], extra[:8])
    if len(a) != len(b):
        return 'rows %d vs %d' % (len(a), len(b))
    bad = []
    for c in a.columns:
        x, y = a[c], b[c]
        if (x == y).all():
            continue
        xn = pd.to_numeric(x, errors='coerce')
        yn = pd.to_numeric(y, errors='coerce')
        both = xn.notna() & yn.notna()
        same = (x == y) | (both & np.isclose(xn.fillna(0), yn.fillna(0), rtol=1e-9, atol=1e-12))
        # textual forms like "1" vs "1.0" or "True" vs "true" count as equal values
        same |= both & (xn == yn)
        if not same.all():
            i = int(np.flatnonzero(~same.values)[0])
            bad.append('%s[%d]: %r vs %r (%d rows)' % (c, i, x.iloc[i], y.iloc[i], int((~same).sum())))
        else:
            bad.append('%s: same values, different text form' % c)
    if not bad:
        return None
    if all(b.endswith('different text form') for b in bad):
        return 'TEXT ' + '; '.join(bad[:6])
    return 'VALUES ' + '; '.join(bad[:6])


def compare_file(ref, got):
    if not got.exists():
        return 'MISSING in run'
    if ref.read_bytes() == got.read_bytes():
        return None
    if ref.suffix == '.json':
        a = strip_clock(json.loads(ref.read_text(encoding='utf-8-sig')))
        b = strip_clock(json.loads(got.read_text(encoding='utf-8-sig')))
        return None if a == b else 'JSON differs'
    if ref.suffix == '.csv':
        return frame_diff(ref, got)
    if ref.suffix == '.md':
        return 'markdown text differs (report only)'
    return 'bytes differ'


def main():
    ref, run = Path(sys.argv[1]), Path(sys.argv[2])
    days = open(sys.argv[3]).read().split()
    vfs = run / 'vfs'
    pairs = []
    for day in days:
        for f in sorted((ref / 'snapshots' / day).glob('*')):
            pairs.append(('snapshot', day, f, run / 'snapshots' / day / f.name))
        for f in sorted((ref / 'reports/paper_trade/ab_filtered_daily_ops').glob('*_%s*' % day)):
            pairs.append(('ac_report', day, f, vfs / f.relative_to(ref)))
        for f in sorted((ref / 'reports/strategy_e').glob('e_signal_%s*' % day)):
            pairs.append(('e_report', day, f, vfs / f.relative_to(ref)))
    for f in sorted((ref / 'data/processed/daily_merged_by_date').glob('*.csv')):
        pairs.append(('partition', f.stem, f, vfs / f.relative_to(ref)))
    summary = {}
    problems = []
    for kind, day, a, b in pairs:
        res = compare_file(a, b)
        key = kind
        s = summary.setdefault(key, {'files': 0, 'identical_bytes': 0, 'equal_values': 0, 'text_form_only': 0,
                                     'report_text_only': 0, 'DIFF': 0})
        s['files'] += 1
        if res is None:
            if b.exists() and a.read_bytes() == b.read_bytes():
                s['identical_bytes'] += 1
            else:
                s['equal_values'] += 1
        elif res.startswith('TEXT'):
            s['text_form_only'] += 1
            problems.append((kind, day, a.name, res))
        elif res.startswith('markdown'):
            s['report_text_only'] += 1
            problems.append((kind, day, a.name, res))
        else:
            s['DIFF'] += 1
            problems.append((kind, day, a.name, res))
    for k, v in summary.items():
        print('%-10s %s' % (k, v))
    for p in problems[:60]:
        print(' | '.join(p))
    print('TOTAL value differences:', sum(v['DIFF'] for v in summary.values()))


if __name__ == '__main__':
    main()
