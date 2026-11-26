# coding: utf-8
"""Step 13 inside the bundle with its pushes going to the QMT-log outbox:
  * 10-20 (production seed): decision equal to production, review check with no trigger and
    the same review state production wrote that day;
  * 10-09 (a Friday, 10-08 seed): the weekly report is pushed;
  * ACE+M+L from its activation day: previous snapshots stay intact and the separate
    incremental NAV agrees with the audited full replay for every action date.

Usage: python test_eq_advisory.py CORE
"""
import ast, csv, io, json, math, os, re, sys, urllib.parse
from pathlib import Path
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
from qn_paths import A_SYSTEM_REPO

OLD_EQ = 'qn_state/equity_stop'
NEW_EQ = 'qn_state/equity_stop_ace_m_l'
SHADOW_NAV = 'reports/equity_curve_stop/shadow_nav_latest.csv'


def run(seed, day, new_seed=None, replay_dates=()):
    ws = os.path.join(BASE, 'vm_raw_latest_unz')
    ns = {'__name__': 'qn_core'}
    for name in ('qn_runtime.py', sys.argv[1]):
        exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
    vfs = ns['QN_VFS']
    for rel, text in ns['QN_STATIC_FILES'].items():
        vfs.put_text(rel, text)
    for sub in ('daily', 'daily_basic', 'limit_list'):
        d = os.path.join(ws, 'data/raw', sub)
        for f in sorted(os.listdir(d)):
            if '20260901' <= f[:8] <= '20261231':
                vfs.put_bytes('data/raw/%s/%s' % (sub, f), open(os.path.join(d, f), 'rb').read())
    vfs.put_bytes('data/raw/trade_calendar.csv', open(os.path.join(ws, 'data/raw/trade_calendar.csv'), 'rb').read())
    for root, _, files in os.walk(seed):
        if 'tmp_replay' in root:
            continue
        for f in files:
            full = os.path.join(root, f)
            vfs.put_bytes(os.path.relpath(full, seed), open(full, 'rb').read())
    if new_seed:
        for f in os.listdir(os.path.join(new_seed, OLD_EQ)):
            vfs.put_bytes(NEW_EQ + '/' + f, open(os.path.join(new_seed, OLD_EQ, f), 'rb').read())
        vfs.put_bytes(NEW_EQ + '/shadow_nav.csv', open(os.path.join(new_seed, SHADOW_NAV), 'rb').read())
        for date in replay_dates:
            rel = 'daily/' + date + '.csv'
            # Static outcomes query actual daily bars, independently of the compact features.
            vfs.put_bytes('data/raw/' + rel, (Path(new_seed) / 'fixtures' / rel).read_bytes())
    old_state = vfs.get_text(OLD_EQ + '/state.json')
    src = open(os.path.join(A_SYSTEM_REPO, 'src/execution_completion_tracker.py'), encoding='utf-8').read()
    fields = ast.literal_eval(re.search(r'^SUMMARY_FIELDS = (\[.*?\])\n', src, re.S | re.M).group(1))
    vfs.put_text('reports/execution_tracking/trade_completion_summary.csv', ','.join(fields) + '\n')
    ns['QN_ENVIRON']['BARK_URL'] = ns['QN_NOTIFY_BASE']
    ns['QN_ENVIRON'].pop('A_SYSTEM_DISABLE_NOTIFICATIONS', None)
    ns['QN_NOTIFY_MODE'][0] = 'live'
    ns['QnLoader'](ns['QnCodeBase'](ns['QN_MODULES'])).run_main('scripts.update_equity_curve_stop', ['--signal-date', day])
    pushes = []
    for line in ns['QN_PRINT_LINES']:
        if line.startswith('[A_SYSTEM_NOTIFY live]'):
            parts = urllib.parse.urlsplit(line.split(' ', 3)[3])
            pushes.append([urllib.parse.unquote(x) for x in parts.path.strip('/').split('/', 1)])
    lines = [l for l in ns['QN_PRINT_LINES'] if l.startswith(('REVIEW_TRIGGERS', 'EQUITY_CURVE_STOP'))]
    decision = json.loads(vfs.get_text('data/state/equity_curve_stop_decision.json'))
    state = json.loads(vfs.get_text('data/state/review_trigger_state.json')) if vfs.exists(
        'data/state/review_trigger_state.json') else None
    if new_seed:
        return decision, vfs.get_text(SHADOW_NAV), lines, old_state == vfs.get_text(OLD_EQ + '/state.json')
    return decision, state, lines, pushes


def nav_rows(text):
    return [(row['action_date'], float(row['shadow_nav']))
            for row in csv.DictReader(io.StringIO(text.lstrip(u'\ufeff')))]


def new_framework_check():
    repo = Path(A_SYSTEM_REPO)
    config = json.loads((repo / 'config' / 'config.json').read_text(encoding='utf-8'))
    if config.get('portfolio_framework') != 'ace_m_l':
        return True
    seed = os.environ.get('QN_EQ_SEED_ACE_M_L')
    if not seed:
        print('ACE+M+L seed is required by the active configuration')
        return False
    seed_state = json.load(open(os.path.join(seed, OLD_EQ, 'state.json'), encoding='utf-8-sig'))
    expected = nav_rows(open(os.path.join(seed, SHADOW_NAV), encoding='utf-8-sig').read())
    signal_date = seed_state['seed_until']
    with (repo / 'data' / 'raw' / 'trade_calendar.csv').open(encoding='utf-8-sig') as calendar:
        open_dates = sorted(row['cal_date'] for row in csv.DictReader(calendar)
                            if float(row['is_open']) == 1)
    action_date = min(date for date in open_dates if date > signal_date)
    replay_dates = [date for date in open_dates if seed_state['replay_start'] <= date <= signal_date]
    assert action_date >= config['review_triggers']['ace_m_l_start_date']
    decision, actual_text, _lines, legacy_unchanged = run(
        os.path.join(BASE, 'eqseed_prod_full'), signal_date, new_seed=seed, replay_dates=replay_dates)
    actual = nav_rows(actual_text)
    same_dates = [d for d, _ in expected] == [d for d, _ in actual]
    same_nav = same_dates and all(math.isclose(x[1], y[1], rel_tol=1e-11, abs_tol=1e-11)
                                  for x, y in zip(expected, actual))
    nums = [n for _, n in expected]
    stop_settings = config.get('equity_curve_stop') or {}
    ma_window = int(stop_settings.get('ma_window', 60))
    lag = int(stop_settings.get('lag_trading_days', 6))
    lag_index = len(nums) - lag
    expected_ma = sum(nums[lag_index - ma_window + 1:lag_index + 1]) / ma_window
    state_ok = (decision.get('action_date') == action_date
                and decision.get('lag_date') == expected[lag_index][0]
                and math.isclose(decision.get('lag_nav', 0), nums[lag_index], rel_tol=1e-11)
                and math.isclose(decision.get('lag_ma', 0), expected_ma, rel_tol=1e-11)
                and decision.get('allowed') == (nums[lag_index] >= expected_ma)
                and decision.get('qmt_native_incremental', {}).get('framework') == 'ace_m_l')
    print('ACE+M+L seed NAV dates equal:', same_dates, '| NAV equal:', same_nav,
          '| decision equal:', state_ok, '| legacy state unchanged:', legacy_unchanged)
    if not same_nav or not state_ok:
        print('ACE+M+L first NAV mismatch:', next(((x, y) for x, y in zip(expected, actual)
              if x[0] != y[0] or not math.isclose(x[1], y[1], rel_tol=1e-11)), None))
        print('ACE+M+L expected / actual lag:', expected[lag_index], expected_ma,
              decision.get('lag_date'), decision.get('lag_nav'), decision.get('lag_ma'))
    return same_nav and state_ok and legacy_unchanged


def main():
    ok = True
    prod = json.load(open(os.path.join(BASE, 'eqseed_prod_full/data/state/equity_curve_stop_decision.json'),
                          encoding='utf-8-sig'))
    prod_state = json.load(open(os.path.join(BASE, 'vm_state/files/data/state/review_trigger_state.json'),
                                encoding='utf-8-sig'))
    d, state, lines, pushes = run(os.path.join(BASE, 'eqseed_prod_full'), '20261020')
    same = all(d.get(k) == prod.get(k) for k in ('signal_date', 'action_date', 'allowed', 'lag_nav', 'lag_ma', 'shadow_last_nav'))
    print('10-20 decision equal to production:', same, '| review state equal:', state == prod_state, '| pushes:', len(pushes))
    for l in lines:
        print('  ', l[:120])
    ok &= same and state == prod_state and not pushes
    d, state, lines, pushes = run(os.path.join(BASE, 'eqseed_1008'), '20261009')
    weekly = [p for p in pushes if p[0].startswith(u'\U0001F4C4 方案甲周报 20261009')]
    print('10-09 weekly pushed:', bool(weekly), '| other pushes:', len(pushes) - len(weekly))
    for l in lines:
        print('  ', l[:120])
    if weekly:
        print('   ', weekly[0][0], '|', weekly[0][1][:80], '...')
    ok &= bool(weekly) and any(l.startswith('EQUITY_CURVE_STOP_WEEKLY 20261009') for l in lines)
    ok &= new_framework_check()
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
