# coding: utf-8
"""vm_scripts/qmt_external_checks.py: the daily result file and the weekly factor-health push.

Runs the real program against a stub A_System root (stub production modules record pushes), so it
needs no data and no network:
  1. a mid-week day: result file written, no weekly push, run marked done;
  2. the week's last open day with a failing push: exit 2, not marked done (retried in 30 min);
  3. the same day again: exactly one weekly push, acknowledged A leg shown with its values and
     recheck date, healthy C leg, alignment disabled; a later rerun does not push again;
  4. the message text for an unacknowledged breach, alignment issues, M's live slippage and a failed check;
  5. alignment enabled (ACE+M): QMT's exported real buys, the ACE+M start date and the raw data
     folder (for recomputing M's candidates) reach the shared production entry run_alignment, the
     M slippage check gets the same buys with its configured window and line, and what both find
     is reported;
  6. three-tier strategy warnings enabled: the shadow, factor health, raw data folder and cache file
     reach run_strategy_warnings, a final warning is reported, escalation runs on the push state,
     and the terminal file strategy_warnings_latest.json is written; the weekly line shows the tier;
  7. (2026-11-13) A/C/E slippage gets the same buys with the A/C/E window, line and the jia start date,
     the research pool coverage is computed from the day's dataset and reported, both reach the result
     file; the weekly message shows A/C/E slippage, pool coverage and the signal drought line.

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
def m_slippage_stats(live, **kwargs):
    import json, os
    from pathlib import Path
    kwargs["live_codes"] = sorted(live["ts_code"])
    json.dump(kwargs, open(Path(os.environ["A_SYSTEM_ROOT"]) / "slippage_call.json", "w"))
    return {"completed": 10, "trades": kwargs["trades"], "limit_pct": kwargs["limit_pct"], "average_pct": 2.0,
            "window": [], "triggered": True}
def m_slippage_triggers(stats):
    return [SimpleNamespace(key="m_slippage", detail="M最近10笔实盘平均双边滑点+2.00%")] if stats["triggered"] else []
def ace_slippage_stats(live, **kwargs):
    import json, os
    from pathlib import Path
    kwargs["live_codes"] = sorted(live["ts_code"])
    json.dump(kwargs, open(Path(os.environ["A_SYSTEM_ROOT"]) / "ace_slippage_call.json", "w"))
    bad = (Path(os.environ["A_SYSTEM_ROOT"]) / "ace_bad").exists()
    return {"completed": 10 if bad else 0, "trades": kwargs["trades"], "limit_pct": kwargs["limit_pct"],
            "average_pct": 1.6 if bad else None, "window": [], "triggered": bad}
def ace_slippage_triggers(stats):
    return [SimpleNamespace(key="ace_slippage", detail="A/C/E最近10笔实盘平均双边滑点+1.60%")] if stats["triggered"] else []
def pool_coverage_triggers(day, rows, limit_up, min_ratio, min_limit_up):
    if limit_up and limit_up >= min_limit_up and rows / limit_up < min_ratio:
        return [SimpleNamespace(key="pool_coverage", detail="%s研究池只有%d只涨停股，全市场%d只" % (day, rows, limit_up))]
    return []
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
def build_shadow_replay(**kwargs): return "SHADOW"
def compute_factor_health_checked(config, dataset, calendar):
    return json.load(open(ROOT / "health.json")), ""
def pool_coverage_inputs(dataset, day):
    json.dump({"dataset": str(dataset), "day": day}, open(ROOT / "coverage_call.json", "w"))
    return (2, 82.0) if (ROOT / "pool_bad").exists() else (50, 60.0)
def notify(title, body, level="active"):
    if (ROOT / "fail_push").exists(): return False
    with open(ROOT / "pushes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"title": title, "body": body}, ensure_ascii=False) + "\\n")
    return True
''',
}

STUBS_ALIGN = {
    'src/live_alignment.py': '''
import json, os
from pathlib import Path
from types import SimpleNamespace
def run_alignment(shadow, live, **kwargs):
    kwargs.update(raw_dir=str(kwargs["raw_dir"]), shadow=shadow, live_codes=sorted(live["ts_code"]))
    json.dump(kwargs, open(Path(os.environ["A_SYSTEM_ROOT"]) / "alignment_call.json", "w"))
    return [SimpleNamespace(key="alignment:m_extra:20261113:300001.SZ", detail="20261113M买入了300001.SZ，不在当天的M候选单里")]
''',
    'src/live_performance.py': 'def completed_live_trades(filled, report):\n    return filled, {}\n',
    'src/strategy_warnings.py': '''
import json, os
from pathlib import Path
from types import SimpleNamespace
def run_strategy_warnings(shadow, health, config, **kwargs):
    kwargs.update(raw_dir=str(kwargs["raw_dir"]), cache_path=str(kwargs["cache_path"]), shadow=shadow,
                  health_legs=sorted(health))
    json.dump(kwargs, open(Path(os.environ["A_SYSTEM_ROOT"]) / "warnings_call.json", "w"))
    return [SimpleNamespace(key="warn3:leg:A", detail="A腿单独按规则最近12个月-55.0%，低于历史最差-50.7%", tier=3, scope="A腿",
                            action="建议先暂停交易")], {"legs": {}, "combo_drawdown": {"value": -0.2}}
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
    _t, b3 = xc.weekly_health_message('20261113', {}, '', [], [], '', {'completed': 12, 'trades': 10, 'limit_pct': 1.5,
                                                                       'average_pct': 0.42, 'triggered': False})
    _t, b4 = xc.weekly_health_message('20261113', {}, '', [], [], '', {'completed': 3, 'trades': 10, 'limit_pct': 1.5,
                                                                       'average_pct': 0.3})
    _t, b5 = xc.weekly_health_message('20261113', {}, '', [], [], '', None, 'KeyError: x')
    from types import SimpleNamespace as NS
    _t, b6 = xc.weekly_health_message('20261113', {}, '', [], [], '', None, '',
                                      {'legs': {'A': {'value': -0.55}, 'M': {'value': None}}, 'combo_drawdown': {'value': -0.237}},
                                      '', [NS(tier=3, scope='A腿'), NS(tier=2, scope='M')])
    _t, b7 = xc.weekly_health_message('20261113', {}, '', [], [], '', None, '', None, 'ValueError: y')
    _t, b8 = xc.weekly_health_message('20261113', {}, '', [{'key': 'warn2:leg:A', 'recheck_by': '2027-01-02'}], [], '', None, '',
                                      {'legs': {}, 'combo_drawdown': {'value': -0.245}}, '',
                                      [NS(tier=2, scope='A腿', key='warn2:leg:A'), NS(tier=2, scope='M', key='warn2:leg:M')])
    _t, b9 = xc.weekly_health_message('20261113', {}, '', [], [], '', None, '',
                                      {'legs': {}, 'combo_drawdown': {'value': -0.2},
                                       'signals': {'gap': 3, 'line': 22, 'last_any': '20261110',
                                                   'last': {'A': '20261110', 'C': '', 'E': '20261102', 'M': '20261028'}}},
                                      '', [], {'completed': 4, 'trades': 10, 'limit_pct': 1.2, 'average_pct': 0.35}, '',
                                      {'pool_rows': 49, 'limit_up_count': 61.0, 'min_ratio': 0.3}, '')
    _t, b10 = xc.weekly_health_message('20261113', {}, '', [], [], '', None, '', None, '', (),
                                       {'completed': 0, 'trades': 10, 'limit_pct': 1.2}, '', None, 'OSError: z')
    c4 = c4 and ('A/C/E实盘滑点：已平仓4笔，平均双边+0.35%（满10笔后超过1.2%报警）' in b9
                 and '研究池覆盖率：20261113研究池49只/全市场涨停61只（80%，低于30%报警，正常时最低32%）' in b9
                 and '选股信号：最近一次20261110（A 20261110、C 无、E 20261102、M 20261028），之后3个行动日没有新信号（超过22天报警）' in b9
                 and 'A/C/E实盘滑点：还没有已平仓的A/C/E' in b10 and '研究池覆盖率：没算成（OSError: z）' in b10
                 and '研究池覆盖率' not in b3 and 'A/C/E实盘滑点' not in b3 and '选股信号' not in b6)
    c4 = c4 and ('M实盘滑点：最近10笔平均双边+0.42%（报警线1.5%）' in b3 and '已平仓3笔，平均双边+0.30%（满10笔后超过1.5%报警）' in b4
                 and 'M实盘滑点：没算成（KeyError: x）' in b5 and 'M实盘滑点' not in b2
                 and '三级警告：终极（A腿）；ACE+M规则回放回撤-23.7%，最近12个月A-55.0%、C不足10笔、E不足10笔、M不足10笔' in b6
                 and '三级警告：没算成（ValueError: y）' in b7 and '三级警告' not in b3
                 and '三级警告：中级（A腿，已复核登记，2027-01-02复查；M）；ACE+M规则回放回撤-24.5%' in b8)
    print('4. expired acknowledgement -> 需临时复核, 2 alignment issues, M slippage lines, failed checks -> %s' % (
        'ok' if c4 else 'FAIL'))
    ok &= c4
    with tempfile.TemporaryDirectory() as tmp:
        root, local = os.path.join(tmp, 'A_System'), os.path.join(tmp, 'local')
        setup(root)
        for rel, text in STUBS_ALIGN.items():
            open(os.path.join(root, rel), 'w', encoding='utf-8').write(text)
        cfg_path = os.path.join(root, 'config', 'config.json')
        cfg = json.load(open(cfg_path))
        cfg['review_triggers'].update(alignment_start_date='20261001', ace_m_start_date='20261113')
        json.dump(cfg, open(cfg_path, 'w'))
        export = os.path.join(local, 'A_System', 'qmt_native', 'qmt_export')
        os.makedirs(export)
        open(os.path.join(export, 'trade_completion_summary.csv'), 'w').write(
            'trade_key,entry_date,ts_code,strategy_leg,entry_filled_qty\n'
            'k1,2026-11-13,300001.SZ,M,100\nk2,20261113,000001.SZ,A,0\n')
        code, out = run(root, local, '20261111')
        call = json.load(open(os.path.join(root, 'alignment_call.json')))
        slip = json.load(open(os.path.join(root, 'slippage_call.json')))
        c5 = (code == 0 and call['start'] == '20261001' and call['end'] == '20261111' and call['ace_m_start'] == '20261113'
              and call['raw_dir'] == os.path.join(root, 'data', 'raw') and call['shadow'] == 'SHADOW'
              and call['live_codes'] == ['300001.SZ'] and (call['ma_window'], call['lag']) == (60, 6)
              and '不在当天的M候选单里' in out and '"alignment_issues": 1' in out
              and (slip['start'], slip['trades'], slip['limit_pct'], slip['live_codes']) == ('20261113', 10, 1.5, ['300001.SZ'])
              and 'M最近10笔实盘平均双边滑点+2.00%' in out and 'active=3' in out)
        print('5. alignment (ACE+M): start %s, ace_m_start %s, raw dir passed %s, live buys %s; slippage window %s line %s%% -> %s' % (
            call['start'], call['ace_m_start'], call['raw_dir'] == os.path.join(root, 'data', 'raw'), call['live_codes'],
            slip['trades'], slip['limit_pct'], 'ok' if c5 else 'FAIL'))
        ok &= c5
    with tempfile.TemporaryDirectory() as tmp:
        root, local = os.path.join(tmp, 'A_System'), os.path.join(tmp, 'local')
        setup(root)
        for rel, text in STUBS_ALIGN.items():
            open(os.path.join(root, rel), 'w', encoding='utf-8').write(text)
        cfg_path = os.path.join(root, 'config', 'config.json')
        cfg = json.load(open(cfg_path))
        cfg['review_triggers'].update(alignment_start_date='20261001', ace_m_start_date='20261113')
        cfg['strategy_warnings'] = {'enabled': True}
        json.dump(cfg, open(cfg_path, 'w'))
        export = os.path.join(local, 'A_System', 'qmt_native', 'qmt_export')
        os.makedirs(export)
        open(os.path.join(export, 'trade_completion_summary.csv'), 'w').write(
            'trade_key,entry_date,ts_code,strategy_leg,entry_filled_qty\nk1,2026-11-13,300001.SZ,M,100\n')
        code, out = run(root, local, '20261111')
        call = json.load(open(os.path.join(root, 'warnings_call.json')))
        work = os.path.join(local, 'A_System', 'qmt_native', 'external_checks')
        report = json.load(open(os.path.join(work, 'strategy_warnings_latest.json'), encoding='utf-8'))
        c6 = (code == 0 and call['shadow'] == 'SHADOW' and call['health_legs'] == ['A', 'C'] and call['today'] == '20261111'
              and call['raw_dir'] == os.path.join(root, 'data', 'raw')
              and call['cache_path'] == os.path.join(work, 'strategy_warning_cache.json')
              and (call['ma_window'], call['lag']) == (60, 6) and os.path.exists(os.path.join(root, 'escalate_called'))
              and report['warnings'] == ['warn3:leg:A'] and '低于历史最差-50.7%' in out)
        print('6. three-tier warnings: shadow/health/raw/cache passed %s, escalation ran %s, terminal file %s -> %s' % (
            call['shadow'] == 'SHADOW', os.path.exists(os.path.join(root, 'escalate_called')), report['warnings'],
            'ok' if c6 else 'FAIL'))
        ok &= c6
    with tempfile.TemporaryDirectory() as tmp:
        root, local = os.path.join(tmp, 'A_System'), os.path.join(tmp, 'local')
        setup(root)
        for rel, text in STUBS_ALIGN.items():
            open(os.path.join(root, rel), 'w', encoding='utf-8').write(text)
        cfg_path = os.path.join(root, 'config', 'config.json')
        cfg = json.load(open(cfg_path))
        cfg['review_triggers'].update(alignment_start_date='20260921', ace_m_start_date='20261113',
                                      ace_slippage_trades=10, ace_slippage_limit_pct=1.2,
                                      pool_coverage_min_ratio=0.3, pool_coverage_min_limit_up=10)
        json.dump(cfg, open(cfg_path, 'w'))
        export = os.path.join(local, 'A_System', 'qmt_native', 'qmt_export')
        os.makedirs(export)
        open(os.path.join(export, 'trade_completion_summary.csv'), 'w').write(
            'trade_key,entry_date,ts_code,strategy_leg,entry_filled_qty\nk1,2026-11-13,600001.SH,A,100\n')
        open(os.path.join(root, 'ace_bad'), 'w').close()
        open(os.path.join(root, 'pool_bad'), 'w').close()
        code, out = run(root, local, '20261111')
        slip = json.load(open(os.path.join(root, 'ace_slippage_call.json')))
        cov = json.load(open(os.path.join(root, 'coverage_call.json')))
        work = os.path.join(local, 'A_System', 'qmt_native', 'external_checks')
        latest = json.load(open(os.path.join(work, 'factor_health_latest.json'), encoding='utf-8'))
        c7 = (code == 0 and (slip['start'], slip['trades'], slip['limit_pct'], slip['live_codes']) == ('20260921', 10, 1.2, ['600001.SH'])
              and cov == {'dataset': os.path.join(work, 'work'), 'day': '20261111'}
              and 'A/C/E最近10笔实盘平均双边滑点+1.60%' in out and '20261111研究池只有2只涨停股，全市场82只' in out
              and latest['ace_slippage']['triggered'] and latest['pool_coverage'] == {'pool_rows': 2, 'limit_up_count': 82.0, 'min_ratio': 0.3}
              and latest['ace_slippage_error'] == '' and latest['pool_coverage_error'] == '')
        print('7. A/C/E slippage start %s window %s line %s%%, pool coverage from %s -> %s' % (
            slip['start'], slip['trades'], slip['limit_pct'], os.path.basename(cov['dataset']), 'ok' if c7 else 'FAIL'))
        ok &= c7
    print('RESULT', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
