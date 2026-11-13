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
     files are not visible outside QMT); M's live slippage (average of the last closed M trades)
     is checked against its alarm line; the three-tier strategy warnings (src/strategy_warnings.py:
     middle = strategy review, final = stop trading and optimise is advised; push and terminal only,
     nothing is ever stopped) replay ACE+M under the live rules and sizing;
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


def weekly_health_message(day, health, health_error, ack_entries, alignment, alignment_error,
                          slippage=None, slippage_error='', warnings=None, warnings_error='', tiered=()):
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
    if slippage_error:
        lines.append('M实盘滑点：没算成（%s），已单独推送' % slippage_error)
    elif slippage is not None:
        done, need = int(slippage.get('completed', 0)), int(slippage.get('trades', 10))
        limit, average = float(slippage.get('limit_pct', 1.5)), slippage.get('average_pct')
        if not done:
            lines.append('M实盘滑点：还没有已平仓的M')
        elif done < need:
            lines.append('M实盘滑点：已平仓%d笔，平均双边%+.2f%%（满%d笔后超过%.1f%%报警）' % (done, average, need, limit))
        else:
            lines.append('M实盘滑点：最近%d笔平均双边%+.2f%%（报警线%.1f%%）%s' % (
                need, average, limit, '，已单独推送' if slippage.get('triggered') else ''))
    if warnings_error:
        lines.append('三级警告：没算成（%s），已单独推送' % warnings_error)
    elif warnings is not None:
        finals = sorted({t.scope for t in tiered if getattr(t, 'tier', 1) >= 3})
        middles = sorted({t.scope for t in tiered if getattr(t, 'tier', 1) == 2})
        state = '终极（%s）' % '、'.join(finals) if finals else ('中级（%s）' % '、'.join(middles) if middles else '无')
        legs = warnings.get('legs', {})
        def pct(item):
            value = (item or {}).get('value')
            return '不足10笔' if value is None else '%+.1f%%' % (value * 100)
        combo = (warnings.get('combo_drawdown') or {}).get('value')
        lines.append('三级警告：%s；ACE+M规则回放回撤%s，最近12个月A%s、C%s、E%s、M%s' % (
            state, '%.1f%%' % (combo * 100) if combo is not None else '?', pct(legs.get('A')), pct(legs.get('C')),
            pct(legs.get('E')), pct(legs.get('M'))))
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
    slip, slip_error = None, ''
    warn_metrics, warn_error = None, ''
    start = str(section.get('alignment_start_date', '') or '')
    if start:
        filled = None
        shadow = None
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
        try:
            from src.review_triggers import m_slippage_stats, m_slippage_triggers
            if filled is None:
                filled, _complete = live_trades(config)
            slip = m_slippage_stats(filled, start=str(section.get('ace_m_start_date', '') or ''),
                                    trades=int(section.get('m_slippage_trades', 10)),
                                    limit_pct=float(section.get('m_slippage_limit_pct', 1.5)))
            triggers.extend(m_slippage_triggers(slip))
        except Exception as exc:
            traceback.print_exc()
            slip_error = '%s: %s' % (type(exc).__name__, exc)
            triggers.append(check_failure_trigger('M实盘滑点', slip_error))
        if (config.get('strategy_warnings') or {}).get('enabled'):
            try:
                from src.strategy_warnings import run_strategy_warnings
                if shadow is None:
                    raise RuntimeError('影子账没建成，三级警告无法回放')
                found, warn_metrics = run_strategy_warnings(
                    shadow, health, config, raw_dir=ROOT / 'data' / 'raw',
                    cache_path=WORK / 'strategy_warning_cache.json', today=day, ma_window=settings.ma_window,
                    lag=settings.lag_trading_days)
                triggers.extend(found)
            except Exception as exc:
                traceback.print_exc()
                warn_error = '%s: %s' % (type(exc).__name__, exc)
                triggers.append(check_failure_trigger('三级警告（策略复核/停止交易建议）', warn_error))
    print('FACTOR_HEALTH %s %s' % (day, json.dumps({'health': health, 'error': error, 'alignment_issues': None if align is None
                                                    else len(align), 'alignment_error': align_error,
                                                    'm_slippage': slip, 'm_slippage_error': slip_error},
                                                   ensure_ascii=False, default=str)), flush=True)

    state_path = WORK / 'review_trigger_state.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    acknowledged = active_acknowledgements(section.get('acknowledged'), day)
    if warn_metrics is not None:
        from src.strategy_warnings import escalate, load_warning_settings
        triggers = escalate(triggers, state, acknowledged=acknowledged, today=day, open_dates=dates,
                            open_days=load_warning_settings(config).escalate_open_days)
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
        if warn_metrics is not None or warn_error:
            from src.strategy_warnings import warning_report
            report = warning_report(triggers, acknowledged, warn_metrics or {}, signal_date=day,
                                    generated_at=dt.datetime.now(BJ).isoformat())
            report['error'] = warn_error
            tmp = WORK / 'strategy_warnings_latest.tmp'
            tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
            os.replace(tmp, WORK / 'strategy_warnings_latest.json')
        (WORK / 'factor_health_latest.json').write_text(json.dumps({
            'signal_date': day, 'factor_health': health, 'factor_health_error': error,
            'alignment_issues': None if align is None else [t.detail for t in align], 'alignment_error': align_error,
            'm_slippage': slip, 'm_slippage_error': slip_error,
            'strategy_warnings_error': warn_error,
            'finished_at': dt.datetime.now(BJ).isoformat()}, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
        weekly_flag = WORK / ('weekly_%s.json' % day)
        if is_week_last_open_day(dates, day) and not weekly_flag.exists():
            title, body = weekly_health_message(day, health, error, section.get('acknowledged'), align, align_error,
                                                slip, slip_error, warn_metrics, warn_error,
                                                [t for t in triggers if getattr(t, 'tier', 1) >= 2])
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
