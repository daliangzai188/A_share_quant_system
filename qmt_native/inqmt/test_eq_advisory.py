# coding: utf-8
"""Step 13 inside the bundle with its pushes going to the QMT-log outbox:
  * 10-20 (production seed): decision equal to production, review check with no trigger and
    the same review state production wrote that day;
  * 10-09 (a Friday, 10-08 seed): the weekly report is pushed.

Usage: python test_eq_advisory.py CORE
"""
import ast, json, os, re, shutil, sys, tempfile, urllib.parse
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
REPO = '/Users/eass/Desktop/A_System'


def run(seed, day):
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
    src = open(os.path.join(REPO, 'src/execution_completion_tracker.py'), encoding='utf-8').read()
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
    return decision, state, lines, pushes


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
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
