"""Extract per-day selections from a replay workspace and diff two workspaces.

Usage:
  python3 selections.py WORKSPACE                 -> print and save selections.json
  python3 selections.py BASE_WS OTHER_WS          -> per-day diff table
"""
import json
import sys
from pathlib import Path

import pandas as pd

PREFIX = 'a_strict_plus_c_hold3_'


def _codes(path, column='ts_code'):
    if not path.exists() or path.stat().st_size < 5:
        return []
    try:
        frame = pd.read_csv(path, dtype=str)
    except pd.errors.EmptyDataError:
        return []
    return sorted(frame[column].dropna().tolist()) if column in frame.columns else []


def extract(ws):
    ws = Path(ws)
    ops = ws / 'reports/paper_trade/ab_filtered_daily_ops'
    status = json.load(open(ws / 'replay_status.json'))
    out = {}
    for day in sorted(status):
        check = ops / (PREFIX + day + '_checklist.csv')
        leg, op_status = '', ''
        if check.exists():
            row = pd.read_csv(check, dtype=str).iloc[0]
            leg, op_status = row.get('strategy_leg', ''), row.get('operation_status', '')
        orders = []
        po = ops / (PREFIX + day + '_planned_orders.csv')
        if po.exists() and po.stat().st_size > 5:
            for r in pd.read_csv(po, dtype=str).itertuples():
                orders.append('%s %s %s' % (getattr(r, 'side', ''), r.ts_code, getattr(r, 'planned_position_pct', '')))
        out[day] = {
            'ac_leg': leg, 'ac_status': op_status,
            'a_candidates': _codes(ops / (PREFIX + day + '_a_candidates.csv')),
            'c_candidates': _codes(ops / (PREFIX + day + '_c_candidates.csv')),
            'selected': _codes(ops / (PREFIX + day + '_selected.csv')),
            'planned_orders': sorted(orders),
            'e_candidates': _codes(ws / 'reports/strategy_e' / ('e_signal_%s_candidates.csv' % day)),
        }
    json.dump(out, open(ws / 'selections.json', 'w'), ensure_ascii=False, indent=1)
    return out


def diff(base, other):
    a, b = extract(base), extract(other)
    keys = ('planned_orders', 'selected', 'a_candidates', 'c_candidates', 'e_candidates')
    lines = ['| 信号日 | 计划单一致 | 选中一致 | A候选 | C候选 | E候选 |', '|---|---|---|---|---|---|']
    same_orders = 0
    for day in sorted(set(a) | set(b)):
        x, y = a.get(day, {}), b.get(day, {})
        cells = []
        for k in keys:
            ok = x.get(k) == y.get(k)
            cells.append('✅' if ok else '❌ %s → %s' % (x.get(k), y.get(k)))
        same_orders += x.get('planned_orders') == y.get('planned_orders')
        lines.append('| %s | %s |' % (day, ' | '.join(cells)))
    lines.append('')
    lines.append('计划单完全一致：%d / %d 天' % (same_orders, len(set(a) | set(b))))
    return '\n'.join(lines)


if __name__ == '__main__':
    if len(sys.argv) == 2:
        sel = extract(sys.argv[1])
        for day, s in sel.items():
            print(day, s['ac_leg'] or '-', 'orders=%s' % s['planned_orders'], 'A=%d C=%d E=%d' % (
                len(s['a_candidates']), len(s['c_candidates']), len(s['e_candidates'])))
    else:
        print(diff(sys.argv[1], sys.argv[2]))
