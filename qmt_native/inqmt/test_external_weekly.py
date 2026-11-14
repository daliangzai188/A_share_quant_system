# coding: utf-8
"""vm_scripts/qmt_external_checks.py: one strategy daily report per trading day (2026-11-14 on).

Runs the real program against a stub A_System root (stub production modules record pushes and
calls), so it needs no data and no network:
  1. a normal day: exactly one push, the daily report (acknowledged items only, so level ok); the
     result files and daily_report_latest.json are written and copied into C:\\A_System\\reports\\qmt_native
     (synced to the Mac), the run is marked done; a rerun of the same day does not push again;
  2. a failing push: exit 2, not marked done, no state written (retried in 30 min); the retry pushes once;
  3. Friday: still exactly one push, no weekly factor-health push any more;
  4. the values handed to the report: factor health, alignment disabled, acknowledged items with recheck dates;
  5. alignment enabled (ACE+M): QMT's exported real buys, the ACE+M start date and the raw data folder reach
     run_alignment, M and A/C/E slippage and the pool coverage get their configured windows and lines, the
     problems go into the one daily push (no separate review push), and the first-seen date of a
     condition is kept from one day to the next;
  6. three-tier strategy warnings enabled: the shadow, factor health, raw data folder and cache file
     reach run_strategy_warnings, escalation runs on the state, the report level follows the final
     warning, and both terminal files are written;
  7. the shadow 3/6-month lines, the normal-market stall and the live loss streak (pushed inside QMT as
     before) are computed here too and reach the report.

Usage: python3 test_external_weekly.py   (Mac Python 3)
"""
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
import json, os
from pathlib import Path
from types import SimpleNamespace
def open_dates_from_calendar(p):
    return [l.split(",")[1] for l in open(p).read().split()[1:] if l.split(",")[2] == "1"]
def load_weekly_report_settings(config):
    return SimpleNamespace(window_3m=1, window_6m=2, shadow_3m_fail=-0.241, shadow_6m_fail=-0.194,
                           worst_stop_missed_gain=0.269)
def current_stop_episode(labels, values, **kwargs):
    json.dump({"labels": labels, "kwargs": kwargs}, open(Path(os.environ["A_SYSTEM_ROOT"]) / "episode_call.json", "w"))
    return None
''',
    'src/review_triggers.py': '''
import json, os
from pathlib import Path
from types import SimpleNamespace
FINAL_STANDARD = "FINAL"
ROOT = Path(os.environ["A_SYSTEM_ROOT"])
def active_acknowledgements(entries, today):
    return {e["key"]: "ack" for e in entries or [] if e["recheck_by"].replace("-", "") > today}
def check_failure_trigger(name, err): return SimpleNamespace(key="check_failed:" + name, detail=str(err), tier=1)
def factor_health_triggers(h):
    return [SimpleNamespace(key="factor_health:" + l, detail=l, tier=1) for l, i in sorted(h.items()) if i["triggered"]]
def m_slippage_stats(live, **kwargs):
    kwargs["live_codes"] = sorted(live["ts_code"])
    json.dump(kwargs, open(ROOT / "slippage_call.json", "w"))
    return {"completed": 10, "trades": kwargs["trades"], "limit_pct": kwargs["limit_pct"], "average_pct": 2.0,
            "window": [], "triggered": True}
def m_slippage_triggers(stats):
    return [SimpleNamespace(key="m_slippage", detail="M最近10笔实盘平均双边滑点+2.00%", tier=1)] if stats["triggered"] else []
def ace_slippage_stats(live, **kwargs):
    kwargs["live_codes"] = sorted(live["ts_code"])
    json.dump(kwargs, open(ROOT / "ace_slippage_call.json", "w"))
    bad = (ROOT / "ace_bad").exists()
    return {"completed": 10 if bad else 0, "trades": kwargs["trades"], "limit_pct": kwargs["limit_pct"],
            "average_pct": 1.6 if bad else None, "window": [], "triggered": bad}
def ace_slippage_triggers(stats):
    return [SimpleNamespace(key="ace_slippage", detail="A/C/E最近10笔实盘平均双边滑点+1.60%", tier=1)] if stats["triggered"] else []
def pool_coverage_triggers(day, rows, limit_up, min_ratio, min_limit_up):
    if limit_up and limit_up >= min_limit_up and rows / limit_up < min_ratio:
        return [SimpleNamespace(key="pool_coverage", detail="%s研究池只有%d只涨停股，全市场%d只" % (day, rows, limit_up), tier=1)]
    return []
def shadow_triggers(values, **kwargs):
    kwargs["values"] = list(values)
    json.dump(kwargs, open(ROOT / "shadow_call.json", "w"), default=str)
    return []
def regime_stall_triggers(stall):
    return []
def monthly_realized_pnl(complete, months):
    return [("202609", 5.0, 1), ("202610", -1.0, 2)]
def live_loss_streak_triggers(monthly, need_months):
    json.dump({"monthly": monthly, "need_months": need_months}, open(ROOT / "loss_call.json", "w"))
    return []
''',
    'src/daily_report.py': '''
import json, os
from pathlib import Path
def value_lines(values):
    json.dump(values, open(Path(os.environ["A_SYSTEM_ROOT"]) / "values_call.json", "w"), ensure_ascii=False, default=str)
    return ["VALUES"]
def build_daily_report(day, triggers, acknowledged, recheck, first_seen, lines, final_standard=""):
    pending = [t for t in triggers if t.key not in acknowledged]
    level = "ok" if not pending else ("final" if any(getattr(t, "tier", 1) >= 3 for t in pending) else "review")
    seen = dict(first_seen or {})
    body = list(lines) + ["观察中:" + ",".join("%s@%s" % (k, recheck[k]) for k in sorted(recheck))]
    return {"signal_date": day, "level": level, "title": "日报 %s %s %d项" % (day, level, len(pending)),
            "lines": body, "body": "\\n".join(body), "first_seen": {t.key: seen.get(t.key, day) for t in pending}}
''',
    'scripts/__init__.py': '',
    'scripts/update_equity_curve_stop.py': '''
import dataclasses, json, os
from pathlib import Path
ROOT = Path(os.environ["A_SYSTEM_ROOT"])
@dataclasses.dataclass
class S:
    work_dir: Path; history_start: str = "20190101"; ma_window: int = 60; lag_trading_days: int = 6
class Nav:
    index = ["20261109", "20261110", "20261111"]
    def to_numpy(self): return [1.0, 1.1, 1.2]
def load_settings(config, root): return S(work_dir=root / "w")
def build_dataset(settings, day): return settings.work_dir
def build_shadow_replay(**kwargs): return {"nav": Nav(), "tag": "SHADOW"}
def compute_factor_health_checked(config, dataset, calendar):
    return json.load(open(ROOT / "health.json")), ""
def pool_coverage_inputs(dataset, day):
    json.dump({"dataset": str(dataset), "day": day}, open(ROOT / "coverage_call.json", "w"))
    return (2, 82.0) if (ROOT / "pool_bad").exists() else (50, 60.0)
def compute_regime_stall(config, nav, sentiment_path, day):
    json.dump({"sentiment": str(sentiment_path), "day": day}, open(ROOT / "stall_call.json", "w"))
    return {"streak": 1, "need_months": 3, "triggered": False}
def _completed_months(day, count=12):
    return ["202609", "202610"]
def notify(title, body, level="active"):
    if (ROOT / "fail_push").exists(): return False
    with open(ROOT / "pushes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"title": title, "body": body, "level": level}, ensure_ascii=False) + "\\n")
    return True
''',
}

STUBS_ALIGN = {
    'src/live_alignment.py': '''
import json, os
from pathlib import Path
from types import SimpleNamespace
def run_alignment(shadow, live, **kwargs):
    kwargs.update(raw_dir=str(kwargs["raw_dir"]), shadow=shadow["tag"], live_codes=sorted(live["ts_code"]))
    json.dump(kwargs, open(Path(os.environ["A_SYSTEM_ROOT"]) / "alignment_call.json", "w"))
    return [SimpleNamespace(key="alignment:m_extra:20261113:300001.SZ", detail="20261113M买入了300001.SZ，不在当天的M候选单里",
                            tier=1)]
''',
    'src/live_performance.py': 'def completed_live_trades(filled, report):\n    return filled, {}\n',
    'src/strategy_warnings.py': '''
import json, os
from pathlib import Path
from types import SimpleNamespace
def run_strategy_warnings(shadow, health, config, **kwargs):
    kwargs.update(raw_dir=str(kwargs["raw_dir"]), cache_path=str(kwargs["cache_path"]), shadow=shadow["tag"],
                  health_legs=sorted(health))
    json.dump(kwargs, open(Path(os.environ["A_SYSTEM_ROOT"]) / "warnings_call.json", "w"))
    return [SimpleNamespace(key="warn3:leg:A", detail="A腿单独按规则最近12个月-55.0%，低于历史最差-50.7%", tier=3, scope="A腿",
                            action="建议先暂停交易")], {"legs": {}, "combo_drawdown": {"value": -0.2}, "signals": {"gap": 0}}
def load_warning_settings(config):
    return SimpleNamespace(escalate_open_days=60)
def escalate(triggers, state, **kwargs):
    open(Path(os.environ["A_SYSTEM_ROOT"]) / "escalate_called", "w").close()
    return list(triggers)
def warning_report(triggers, acknowledged, metrics, **kwargs):
    return {"signal_date": kwargs["signal_date"], "warnings": [t.key for t in triggers if getattr(t, "tier", 1) >= 2]}
''',
    'src/strategy_equity_ledger.py': 'def _report_config(config):\n    return {}\n',
    'src/strategy_identity.py': 'def normalize_strategy_frame(frame):\n    return frame\n',
}


def setup(root, align=False, **review):
    for rel, text in STUBS.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w', encoding='utf-8').write(text)
    if align:
        for rel, text in STUBS_ALIGN.items():
            open(os.path.join(root, rel), 'w', encoding='utf-8').write(text)
    os.makedirs(os.path.join(root, 'config'))
    section = {'alignment_start_date': '', 'remind_every_open_days': 5, 'acknowledged': [
        {'key': 'factor_health:A', 'reviewed_on': '2026-09-30', 'conclusion': '维持', 'recheck_by': '2027-01-02'},
        {'key': 'shadow_3m', 'reviewed_on': '2026-11-11', 'conclusion': '维持', 'recheck_by': '2026-12-11'}]}
    section.update(review)
    json.dump({'review_triggers': section}, open(os.path.join(root, 'config', 'config.json'), 'w'))
    os.makedirs(os.path.join(root, 'data', 'raw'))
    rows = ['exchange,cal_date,is_open'] + ['SSE,%s,1' % d for d in ('20261109', '20261110', '20261111', '20261112', '20261113', '20261116')]
    open(os.path.join(root, 'data', 'raw', 'trade_calendar.csv'), 'w').write('\n'.join(rows) + '\n')
    json.dump(HEALTH, open(os.path.join(root, 'health.json'), 'w'))


def export_buys(local, rows):
    export = os.path.join(local, 'A_System', 'qmt_native', 'qmt_export')
    os.makedirs(export, exist_ok=True)
    open(os.path.join(export, 'trade_completion_summary.csv'), 'w').write(
        'trade_key,entry_date,ts_code,strategy_leg,entry_filled_qty\n' + ''.join(r + '\n' for r in rows))


def enable_warnings(root):
    cfg_path = os.path.join(root, 'config', 'config.json')
    cfg = json.load(open(cfg_path))
    cfg['strategy_warnings'] = {'enabled': True}
    json.dump(cfg, open(cfg_path, 'w'))


def run(root, local, day):
    env = dict(os.environ, A_SYSTEM_ROOT=root, LOCALAPPDATA=local, PYTHONIOENCODING='utf-8')
    p = subprocess.run([sys.executable, CHECKS, '--signal-date', day, '--skip-collect'], env=env,
                       capture_output=True, text=True, encoding='utf-8')
    return p.returncode, p.stdout + p.stderr


def pushes(root):
    path = os.path.join(root, 'pushes.jsonl')
    return [json.loads(l) for l in open(path, encoding='utf-8')] if os.path.exists(path) else []


def load(path):
    return json.load(open(path, encoding='utf-8'))


def main():
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        root, local = os.path.join(tmp, 'A_System'), os.path.join(tmp, 'local')
        setup(root)
        work = os.path.join(local, 'A_System', 'qmt_native', 'external_checks')
        code, out = run(root, local, '20261111')
        got = pushes(root)
        latest = load(os.path.join(work, 'daily_report_latest.json'))
        state = load(os.path.join(work, 'review_trigger_state.json'))
        mirror = os.path.join(root, 'reports', 'qmt_native', 'external_checks')
        mirrored = (load(os.path.join(mirror, 'daily_report_latest.json')) == latest
                    and os.path.exists(os.path.join(mirror, 'factor_health_latest.json')))
        os.remove(os.path.join(work, 'done_20261111.json'))
        code2, _ = run(root, local, '20261111')
        c1 = (code == 0 and code2 == 0 and mirrored and len(pushes(root)) == 1 and got[0]['title'] == '日报 20261111 ok 0项'
              and got[0]['level'] == 'active' and latest['title'] == got[0]['title'] and latest['level'] == 'ok'
              and state == {'first_seen': {}, 'active_keys': []}
              and os.path.exists(os.path.join(work, 'factor_health_latest.json'))
              and 'STRATEGY_DAILY 20261111 level=ok' in out)
        print('1. normal day: exit %s, pushes %d %s, rerun pushes again %s -> %s' % (
            code, len(got), got[0]['title'] if got else '', len(pushes(root)) > 1, 'ok' if c1 else 'FAIL'))
        ok &= c1
        open(os.path.join(root, 'fail_push'), 'w').close()
        code, out = run(root, local, '20261112')
        c2 = (code == 2 and not os.path.exists(os.path.join(work, 'done_20261112.json'))
              and not os.path.exists(os.path.join(work, 'daily_20261112.json')) and 'STRATEGY_DAILY_PUSH_FAILED' in out
              and load(os.path.join(work, 'daily_report_latest.json'))['signal_date'] == '20261111')
        os.remove(os.path.join(root, 'fail_push'))
        code, out = run(root, local, '20261112')
        c2 = c2 and code == 0 and [p['title'] for p in pushes(root)][1:] == ['日报 20261112 ok 0项']
        print('2. failing push: exit 2 and not done, retry pushes once -> %s' % ('ok' if c2 else 'FAIL'))
        ok &= c2
        code, out = run(root, local, '20261113')
        titles = [p['title'] for p in pushes(root)]
        c3 = code == 0 and titles[2:] == ['日报 20261113 ok 0项'] and not any('周报' in t for t in titles)
        print('3. Friday: one daily push, no weekly push -> %s' % ('ok' if c3 else 'FAIL'))
        ok &= c3
        values = load(os.path.join(root, 'values_call.json'))
        c4 = (values['health']['A']['value'] == 0.00052 and values['alignment'] is None and values['alignment_error'] == ''
              and values['warnings'] is None and values['shadow'] is None
              and pushes(root)[-1]['body'].endswith('观察中:factor_health:A@2027-01-02,shadow_3m@2026-12-11'))
        print('4. values to the report: factor health, alignment disabled, watched items with recheck dates -> %s' % (
            'ok' if c4 else 'FAIL'))
        ok &= c4
    with tempfile.TemporaryDirectory() as tmp:
        root, local = os.path.join(tmp, 'A_System'), os.path.join(tmp, 'local')
        setup(root, align=True, alignment_start_date='20260921', ace_m_start_date='20261113', ace_slippage_trades=10,
              ace_slippage_limit_pct=1.2, pool_coverage_min_ratio=0.3, pool_coverage_min_limit_up=10)
        export_buys(local, ['k1,2026-11-13,300001.SZ,M,100', 'k2,20261113,000001.SZ,A,0'])
        open(os.path.join(root, 'pool_bad'), 'w').close()
        work = os.path.join(local, 'A_System', 'qmt_native', 'external_checks')
        code, out = run(root, local, '20261111')
        call = load(os.path.join(root, 'alignment_call.json'))
        slip = load(os.path.join(root, 'slippage_call.json'))
        ace = load(os.path.join(root, 'ace_slippage_call.json'))
        cov = load(os.path.join(root, 'coverage_call.json'))
        got = pushes(root)
        first_day = load(os.path.join(work, 'review_trigger_state.json'))['first_seen']
        code2, _ = run(root, local, '20261112')
        second_day = load(os.path.join(work, 'review_trigger_state.json'))['first_seen']
        c5 = (code == 0 and call['start'] == '20260921' and call['end'] == '20261111' and call['ace_m_start'] == '20261113'
              and call['raw_dir'] == os.path.join(root, 'data', 'raw') and call['shadow'] == 'SHADOW'
              and call['live_codes'] == ['300001.SZ'] and (call['ma_window'], call['lag']) == (60, 6)
              and (slip['start'], slip['trades'], slip['limit_pct'], slip['live_codes']) == ('20261113', 10, 1.5, ['300001.SZ'])
              and (ace['start'], ace['trades'], ace['limit_pct']) == ('20260921', 10, 1.2)
              and cov == {'dataset': os.path.join(work, 'work'), 'day': '20261111'}
              and len(got) == 1 and got[0]['title'] == '日报 20261111 review 3项' and got[0]['level'] == 'timeSensitive'
              and sorted(first_day) == ['alignment:m_extra:20261113:300001.SZ', 'm_slippage', 'pool_coverage']
              and set(first_day.values()) == {'20261111'}
              and code2 == 0 and second_day == first_day and len(pushes(root)) == 2)
        print('5. alignment (ACE+M): run_alignment/slippage/coverage wiring, one daily push with 3 problems, '
              'first-seen kept next day -> %s' % ('ok' if c5 else 'FAIL'))
        ok &= c5
    with tempfile.TemporaryDirectory() as tmp:
        root, local = os.path.join(tmp, 'A_System'), os.path.join(tmp, 'local')
        setup(root, align=True, alignment_start_date='20261001', ace_m_start_date='20261113')
        enable_warnings(root)
        export_buys(local, ['k1,2026-11-13,300001.SZ,M,100'])
        work = os.path.join(local, 'A_System', 'qmt_native', 'external_checks')
        code, out = run(root, local, '20261111')
        call = load(os.path.join(root, 'warnings_call.json'))
        report = load(os.path.join(work, 'strategy_warnings_latest.json'))
        daily = load(os.path.join(work, 'daily_report_latest.json'))
        values = load(os.path.join(root, 'values_call.json'))
        c6 = (code == 0 and call['shadow'] == 'SHADOW' and call['health_legs'] == ['A', 'C'] and call['today'] == '20261111'
              and call['raw_dir'] == os.path.join(root, 'data', 'raw')
              and call['cache_path'] == os.path.join(work, 'strategy_warning_cache.json')
              and (call['ma_window'], call['lag']) == (60, 6) and os.path.exists(os.path.join(root, 'escalate_called'))
              and report['warnings'] == ['warn3:leg:A'] and daily['level'] == 'final' and values['signals'] == {'gap': 0}
              and len(pushes(root)) == 1 and pushes(root)[0]['title'].startswith('日报 20261111 final'))
        print('6. three-tier warnings: wiring, escalation, report level final, both terminal files -> %s' % (
            'ok' if c6 else 'FAIL'))
        ok &= c6
        shadow = load(os.path.join(root, 'shadow_call.json'))
        stall = load(os.path.join(root, 'stall_call.json'))
        loss = load(os.path.join(root, 'loss_call.json'))
        episode = load(os.path.join(root, 'episode_call.json'))
        c7 = (shadow['values'] == [1.0, 1.1, 1.2] and (shadow['window_3m'], shadow['window_6m']) == (1, 2)
              and (shadow['fail_3m'], shadow['fail_6m'], shadow['worst_missed_gain']) == (-0.241, -0.194, 0.269)
              and episode == {'labels': ['20261109', '20261110', '20261111'], 'kwargs': {'ma_window': 60, 'lag': 6}}
              and stall == {'sentiment': os.path.join(work, 'work', 'market_sentiment.csv'), 'day': '20261111'}
              and loss['need_months'] == 3 and values['live_loss'] == {'months': 1, 'need': 3}
              and abs(values['shadow']['r3'] - (1.2 / 1.1 - 1)) < 1e-9 and abs(values['shadow']['r6'] - 0.2) < 1e-9
              and values['stall'] == {'streak': 1, 'need_months': 3, 'triggered': False})
        print('7. shadow lines, normal-market stall and live loss streak computed here too -> %s' % ('ok' if c7 else 'FAIL'))
        ok &= c7
    print('RESULT', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
