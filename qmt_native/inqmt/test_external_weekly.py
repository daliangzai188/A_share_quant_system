# coding: utf-8
"""vm_scripts/qmt_external_checks.py: the daily result file and the weekly factor-health push.

Runs the real program against a stub A_System root (stub production modules record pushes), so it
needs no data and no network:
  1. a mid-week day: result file written, no weekly push, run marked done;
  2. the week's last open day with a failing push: exit 2, not marked done (retried in 30 min);
  3. the same day again: exactly one weekly push, acknowledged A leg shown with its values and
     recheck date, healthy C leg, alignment disabled; a later rerun does not push again;
  4. the message text for an unacknowledged breach, alignment issues and a failed check.

Usage: python3 test_external_weekly.py   (Mac Python 3)
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKS = os.path.join(os.path.dirname(HERE), 'vm_scripts', 'qmt_external_checks.py')
HEALTH = {'A': {'value': 0.00052, 'samples': 207, 'as_of': '202611', 'line': 0.00539, 'months_below': 4,
                'need_months': 2, 'triggered': True},
          'C': {'value': 0.01666, 'samples': 192, 'as_of': '202609', 'line': -0.00474, 'months_below': 0,
                'need_months': 2, 'triggered': False}}
STUBS = {
    'src/__init__.py': '',
    'src/utils/__init__.py': '',
    'src/utils/config.py': 'import json\ndef load_json_config(p):\n    return json.load(open(p, encoding="utf-8"))\n',
    'src/equity_curve_stop.py': '''
import datetime as dt
def open_dates_from_calendar(p):
    return [l.split(",")[1] for l in open(p).read().split()[1:] if l.split(",")[2] == "1"]
def allowed_flags(*a, **k): raise AssertionError("alignment disabled in this test")
def gated_replay(*a, **k): raise AssertionError("alignment disabled in this test")
def _iso_week(d):
    return dt.date(int(d[:4]), int(d[4:6]), int(d[6:])).isocalendar()[:2]
def is_week_last_open_day(open_dates, signal_date):
    dates = sorted(open_dates); later = [d for d in dates if d > signal_date]
    return signal_date in dates and (not later or _iso_week(later[0]) != _iso_week(signal_date))
''',
    'src/review_triggers.py': '''
from types import SimpleNamespace
def active_acknowledgements(entries, today):
    return {e["key"]: "ack" for e in entries or [] if e["recheck_by"].replace("-", "") > today}
def alignment_triggers(*a, **k): return []
def check_failure_trigger(name, err): return SimpleNamespace(key="check_failed:" + name, detail=str(err))
def factor_health_triggers(h):
    return [SimpleNamespace(key="factor_health:" + l, detail=l) for l, i in sorted(h.items()) if i["triggered"]]
def plan_push(triggers, state, **k): return SimpleNamespace(kind=None, title="", body="", state=dict(state))
''',
    'scripts/__init__.py': '',
    'scripts/update_equity_curve_stop.py': '''
import dataclasses, json, os
from pathlib import Path
ROOT = Path(os.environ["A_SYSTEM_ROOT"])
@dataclasses.dataclass
class S:
    work_dir: Path; history_start: str = "20190101"; ma_window: int = 60; lag_trading_days: int = 6
def load_settings(config, root): return S(work_dir=root / "w")
def build_dataset(settings, day): return settings.work_dir
def compute_factor_health_checked(config, dataset, calendar):
    return json.load(open(ROOT / "health.json")), ""
def notify(title, body, level="active"):
    if (ROOT / "fail_push").exists(): return False
    with open(ROOT / "pushes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"title": title, "body": body}, ensure_ascii=False) + "\\n")
    return True
''',
}


def setup(root):
    for rel, text in STUBS.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w', encoding='utf-8').write(text)
    os.makedirs(os.path.join(root, 'config'))
    json.dump({'review_triggers': {'alignment_start_date': '', 'remind_every_open_days': 5, 'acknowledged': [
        {'key': 'factor_health:A', 'reviewed_on': '2026-09-30', 'conclusion': '维持', 'recheck_by': '2027-01-02'},
        {'key': 'shadow_3m', 'reviewed_on': '2026-11-11', 'conclusion': '维持', 'recheck_by': '2026-12-11'}]}},
        open(os.path.join(root, 'config', 'config.json'), 'w'))
    os.makedirs(os.path.join(root, 'data', 'raw'))
    rows = ['exchange,cal_date,is_open'] + ['SSE,%s,1' % d for d in ('20261109', '20261110', '20261111', '20261112', '20261113', '20261116')]
    open(os.path.join(root, 'data', 'raw', 'trade_calendar.csv'), 'w').write('\n'.join(rows) + '\n')
    json.dump(HEALTH, open(os.path.join(root, 'health.json'), 'w'))


def run(root, local, day):
    env = dict(os.environ, A_SYSTEM_ROOT=root, LOCALAPPDATA=local, PYTHONIOENCODING='utf-8')
    p = subprocess.run([sys.executable, CHECKS, '--signal-date', day, '--skip-collect'], env=env,
                       capture_output=True, text=True, encoding='utf-8')
    return p.returncode, p.stdout + p.stderr


def pushes(root):
    path = os.path.join(root, 'pushes.jsonl')
    return [json.loads(l) for l in open(path, encoding='utf-8')] if os.path.exists(path) else []


def main():
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        root, local = os.path.join(tmp, 'A_System'), os.path.join(tmp, 'local')
        setup(root)
        work = os.path.join(local, 'A_System', 'qmt_native', 'external_checks')
        code, out = run(root, local, '20261111')
        latest = json.load(open(os.path.join(work, 'factor_health_latest.json'), encoding='utf-8'))
        c1 = (code == 0 and os.path.exists(os.path.join(work, 'done_20261111.json')) and not pushes(root)
              and abs(latest['factor_health']['A']['value'] - 0.00052) < 1e-12 and 'FACTOR_HEALTH 20261111' in out)
        print('1. mid-week: exit %s, done %s, pushes %d, result file A %s -> %s' % (
            code, os.path.exists(os.path.join(work, 'done_20261111.json')), len(pushes(root)),
            latest['factor_health']['A']['value'], 'ok' if c1 else 'FAIL'))
        ok &= c1
        open(os.path.join(root, 'fail_push'), 'w').close()
        code, out = run(root, local, '20261113')
        c2 = code == 2 and not os.path.exists(os.path.join(work, 'done_20261113.json')) and 'FACTOR_HEALTH_WEEKLY_PUSH_FAILED' in out
        print('2. week end, push fails: exit %s, done %s -> %s' % (
            code, os.path.exists(os.path.join(work, 'done_20261113.json')), 'ok' if c2 else 'FAIL'))
        ok &= c2
        os.remove(os.path.join(root, 'fail_push'))
        code, out = run(root, local, '20261113')
        got = pushes(root)
        body = got[0]['body'] if got else ''
        c3 = (code == 0 and len(got) == 1 and got[0]['title'] == '📊 方案甲因子健康周报 20261113'
              and 'A腿：滚动12个月优势+0.05%，冻结线+0.54%（207个样本，截至202611），连续4个月在线下，已复核登记，2027-01-02复查' in body
              and 'C腿：滚动12个月优势+1.67%，冻结线-0.47%（192个样本，截至202609），健康' in body
              and '逐笔对账：未启用' in body)
        os.remove(os.path.join(work, 'done_20261113.json'))
        code2, _ = run(root, local, '20261113')
        c3 = c3 and code2 == 0 and len(pushes(root)) == 1
        print('3. week end again: exit %s, weekly pushes %d, rerun pushes %d -> %s' % (code, len(got), len(pushes(root)), 'ok' if c3 else 'FAIL'))
        print('   ' + body.replace('\n', '\n   '))
        ok &= c3
    spec = importlib.util.spec_from_file_location('xc', CHECKS)
    os.environ['A_SYSTEM_ROOT'] = tempfile.gettempdir()
    xc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(xc)
    _t, b = xc.weekly_health_message('20270105', HEALTH, '', [{'key': 'factor_health:A', 'recheck_by': '2027-01-02'}],
                                     [object(), object()], '')
    _t, b2 = xc.weekly_health_message('20261113', {}, 'boom', [], None, 'ValueError: x')
    c4 = ('连续4个月在线下，需临时复核' in b and '2处不一致' in b and '没算成（boom）' in b2 and '对账：没算成（ValueError: x）' in b2)
    print('4. expired acknowledgement -> 需临时复核, 2 alignment issues, failed checks -> %s' % ('ok' if c4 else 'FAIL'))
    ok &= c4
    print('RESULT', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
