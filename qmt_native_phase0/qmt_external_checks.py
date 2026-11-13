# -*- coding: utf-8 -*-
"""Review checks of step 13 that cannot run inside QMT, run outside QMT with production code.

The QMT-native system computes the stop decision, the weekly report and most review checks
inside QMT. Two checks need data QMT does not have: factor health (15-month research pool and
market-wide daily history) and the trade-by-trade alignment of live fills with the formal
rule (full shadow replay since 2019). This program runs them once per trading day on the
Windows VM, outside QMT, where files and the network may still be used:

  1. production step 1 (Tushare collection) brings C:\\A_System\\data\\raw up to the signal day;
  2. production step 13's dataset build and shadow replay;
  3. production factor health and alignment checks; live fills come from QMT's export
     (rebuilt by qmt_notify_forwarder.py), else from the old system's file. From ACE+M on,
     M's candidate list is recomputed from the Tushare files of step 1 (QMT's own M signal
     files are not visible outside QMT);
  4. production push planning (acknowledged keys, reminder every N open days) and Bark push;
  5. the results of 3 go to external_checks\\factor_health_latest.json and the run log every day,
     and on the last open day of each week a "因子健康周报" push shows the values of both legs and
     the alignment result, acknowledged conditions included (they are not alarmed again, but must
     stay visible: before 11-11 an acknowledged leg's values were seen by nobody).

It never talks to QMT and never changes the stop decision or any trading state.
Started by qmt_notify_forwarder.py after 18:40 on trading days.

Usage: py -3.11 qmt_external_checks.py --signal-date YYYYMMDD [--skip-collect] [--no-push]
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(os.environ.get('A_SYSTEM_ROOT', r'C:\A_System'))
BASE = Path(os.environ.get('LOCALAPPDATA', str(ROOT / 'runtime'))) / 'A_System' / 'qmt_native'
WORK = BASE / 'external_checks'
EXPORTED_SUMMARY = BASE / 'qmt_export' / 'trade_completion_summary.csv'
OLD_SUMMARY = ROOT / 'reports' / 'execution_tracking' / 'trade_completion_summary.csv'
BJ = dt.timezone(dt.timedelta(hours=8))

sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def collect(signal_date: str, dates: list[str]) -> None:
    """Production step 1, the same command the old daemon ran."""
    start = dates[max(0, dates.index(signal_date) - 2)]
    cmd = [sys.executable, '-u', '-B', str(ROOT / 'scripts' / 'collect_all_data.py'),
           '--start-date', start, '--end-date', signal_date, '--require-end-date-limit']
    print('COLLECT', ' '.join(cmd[3:]), flush=True)
    result = subprocess.run(cmd, cwd=str(ROOT), timeout=1800)
    if result.returncode != 0:
        raise RuntimeError('数据采集失败（退出码%s），稍后自动重试' % result.returncode)


def live_trades(config):
    """Production _live_trades, reading QMT's exported summary when the new system runs."""
    import pandas as pd
    from src.live_performance import completed_live_trades
    from src.strategy_equity_ledger import _report_config
    from src.strategy_identity import normalize_strategy_frame

    path = EXPORTED_SUMMARY if EXPORTED_SUMMARY.is_file() else OLD_SUMMARY
    print('LIVE_TRADES_SOURCE', path, flush=True)
    raw = pd.read_csv(path, dtype={'trade_key': str, 'ts_code': str}, low_memory=False)
    if raw.empty:
        return raw, raw
    filled = raw[pd.to_numeric(raw['entry_filled_qty'], errors='coerce').fillna(0).gt(0)].copy()
    filled = normalize_strategy_frame(filled)
    filled['entry_date'] = filled['entry_date'].astype(str).str.replace('-', '', regex=False).str[:8]
    report = _report_config(config)
    report['active_legs'] = sorted({str(leg) for leg in filled['strategy_leg'].fillna('') if str(leg)})
    complete, _quality = completed_live_trades(filled, report)
    return filled, complete


def weekly_health_message(day, health, health_error, ack_entries, alignment, alignment_error):
    """Title and body of the weekly push of the outside-QMT checks (pure, for tests)."""
    recheck = {str(e.get('key', '')): str(e.get('recheck_by', ''))
               for e in (ack_entries or []) if str(e.get('recheck_by', '') or '').replace('-', '') > str(day)}
    lines = []
    if health_error:
        lines.append('因子健康：没算成（%s），已按“检查失效”单独推送' % health_error)
    for leg, item in sorted((health or {}).items()):
        if not item:
            continue
        text = '%s腿：滚动12个月优势%+.2f%%，冻结线%+.2f%%（%d个样本，截至%s）' % (
            leg, float(item['value']) * 100, float(item['line']) * 100, int(item['samples']), item.get('as_of', ''))
        below = int(item.get('months_below', 0) or 0)
        if item.get('triggered'):
            key = 'factor_health:%s' % leg
            text += '，连续%d个月在线下，%s' % (below, ('已复核登记，%s复查' % recheck[key]) if key in recheck else '需临时复核')
        elif below:
            text += '，已连续%d个月在线下（%d个月报警）' % (below, int(item.get('need_months', 2) or 2))
        else:
            text += '，健康'
        lines.append(text)
    if alignment_error:
        lines.append('实盘与正式规则逐笔对账：没算成（%s），已单独推送' % alignment_error)
    elif alignment is None:
        lines.append('实盘与正式规则逐笔对账：未启用')
    elif alignment:
        lines.append('实盘与正式规则逐笔对账：%d处不一致，已单独推送' % len(alignment))
    else:
        lines.append('实盘与正式规则逐笔对账：一致')
    lines.append('优势=满足该腿条件的涨停股（T+1开盘买、T+2收盘卖）平均收益减去其余涨停股；只做观察，不影响停手判定。')
    return '📊 方案甲因子健康周报 %s' % day, '\n'.join(lines)


def run(args) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False)
    except ImportError:
        pass
    from src.utils.config import load_json_config
    from src.equity_curve_stop import is_week_last_open_day, open_dates_from_calendar
    from src.review_triggers import active_acknowledgements, check_failure_trigger, factor_health_triggers, plan_push
    import scripts.update_equity_curve_stop as prod

    day = str(args.signal_date)
    config = load_json_config(ROOT / 'config' / 'config.json')
    section = dict(config.get('review_triggers', {}) or {})
    calendar = ROOT / 'data' / 'raw' / 'trade_calendar.csv'
    dates = open_dates_from_calendar(calendar)
    if day not in dates:
        raise RuntimeError('%s 不是交易日' % day)
    if not args.skip_collect:
        collect(day, dates)
    settings = dataclasses.replace(prod.load_settings(config, ROOT), work_dir=WORK / 'work')
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    dataset = prod.build_dataset(settings, day)

    triggers = []
    health, error = prod.compute_factor_health_checked(config, dataset, calendar)
    if error:
        triggers.append(check_failure_trigger('因子健康', error))
    else:
        triggers.extend(factor_health_triggers(health))
    align, align_error = None, ''
    start = str(section.get('alignment_start_date', '') or '')
    if start:
        try:
            shadow = prod.build_shadow_replay(
                project_root=ROOT, feature_path=dataset / 'strict_feature_pool.csv',
                sentiment_path=dataset / 'market_sentiment.csv', calendar_path=calendar,
                start=settings.history_start, end=day, work_dir=settings.work_dir)
            filled, _complete = live_trades(config)
            from src.live_alignment import run_alignment
            align = run_alignment(
                shadow, filled, ma_window=settings.ma_window, lag=settings.lag_trading_days, start=start, end=day,
                ace_m_start=str(section.get('ace_m_start_date', '') or ''), raw_dir=ROOT / 'data' / 'raw')
            triggers.extend(align)
        except Exception as exc:
            traceback.print_exc()
            align_error = '%s: %s' % (type(exc).__name__, exc)
            triggers.append(check_failure_trigger('实盘与正式规则逐笔对账', align_error))
    print('FACTOR_HEALTH %s %s' % (day, json.dumps({'health': health, 'error': error, 'alignment_issues': None if align is None
                                                    else len(align), 'alignment_error': align_error},
                                                   ensure_ascii=False, default=str)), flush=True)

    state_path = WORK / 'review_trigger_state.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    acknowledged = active_acknowledgements(section.get('acknowledged'), day)
    plan = plan_push(triggers, state, today=day, open_dates=dates,
                     remind_every_open_days=int(section.get('remind_every_open_days', 5)),
                     acknowledged=acknowledged)
    print('REVIEW_TRIGGERS_EXTERNAL %s active=%d acknowledged=%d push=%s：%s' % (
        day, len(triggers), sum(1 for t in triggers if t.key in acknowledged), plan.kind or 'none',
        '；'.join(t.detail for t in triggers) or '无'), flush=True)
    if plan.kind and not args.no_push:
        if not prod.notify(plan.title, plan.body, level='timeSensitive'):
            print('REVIEW_TRIGGERS_EXTERNAL_PUSH_FAILED %s（下次重试）' % day, flush=True)
            return 2
    if not args.no_push:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(plan.state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, state_path)
        (WORK / 'factor_health_latest.json').write_text(json.dumps({
            'signal_date': day, 'factor_health': health, 'factor_health_error': error,
            'alignment_issues': None if align is None else [t.detail for t in align], 'alignment_error': align_error,
            'finished_at': dt.datetime.now(BJ).isoformat()}, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
        weekly_flag = WORK / ('weekly_%s.json' % day)
        if is_week_last_open_day(dates, day) and not weekly_flag.exists():
            title, body = weekly_health_message(day, health, error, section.get('acknowledged'), align, align_error)
            print('FACTOR_HEALTH_WEEKLY %s' % body.replace('\n', '；'), flush=True)
            if not prod.notify(title, body, level='active'):
                print('FACTOR_HEALTH_WEEKLY_PUSH_FAILED %s（下次重试）' % day, flush=True)
                return 2
            weekly_flag.write_text('{}', encoding='utf-8')
        (WORK / ('done_%s.json' % day)).write_text(json.dumps({
            'signal_date': day, 'triggers': [t.key for t in triggers], 'push': plan.kind or '',
            'finished_at': dt.datetime.now(BJ).isoformat()}, ensure_ascii=False), encoding='utf-8')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--signal-date', required=True)
    parser.add_argument('--skip-collect', action='store_true')
    parser.add_argument('--no-push', action='store_true', help='test run: no push, no state change')
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        return run(args)
    except Exception as exc:
        traceback.print_exc()
        late = dt.datetime.now(BJ).strftime('%H%M') >= '2230'
        flag = WORK / ('failed_%s.json' % args.signal_date)
        if late and not args.no_push and not flag.exists():
            import scripts.update_equity_curve_stop as prod
            prod.notify('⚠️ 临时复核触发检查没有完成',
                        '%s: %s。QMT外的因子健康/逐笔对账今天没有被检查，次日停手判定不受影响。'
                        '请在Claude发送：甲·临时复核（先修好这项检查）。' % (type(exc).__name__, exc),
                        level='timeSensitive')
            flag.write_text('{}', encoding='utf-8')
        return 1


if __name__ == '__main__':
    sys.exit(main())
