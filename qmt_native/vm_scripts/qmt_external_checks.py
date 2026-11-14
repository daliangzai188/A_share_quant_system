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
     is checked against its alarm line, and from 2026-11-13 so is A/C/E's (ACE fears slippage more than M);
     the research pool's share of the day's market-wide limit-ups is checked (a data error once left it at
     0~3% and no leg picked a stock for 94 days, while alignment and factor health stayed silent); the
     three-tier strategy warnings (src/strategy_warnings.py: middle = strategy review, final = stop trading
     and optimise is advised; push and terminal only, nothing is ever stopped) replay ACE+M under the live
     rules and sizing, and also report how many open days have passed since any leg last picked a stock
     (signal drought, a first-tier alarm); factor health covers A, C and, from 2026-11-13, E. From
     2026-11-14 the shadow account's 3/6-month lines, the normal-market stall and the live loss streak
     (checked and pushed inside QMT as before) are also computed here, so the daily report is complete;
  4. (2026-11-14, user's request) exactly one push per trading day: the strategy daily report
     (src/daily_report.py). Its title carries the level (✅ all normal, 🚨 first-tier review, ⚠️ middle,
     ⛔ final), the problems come first with what to do, acknowledged conditions are listed as "观察中"
     with their recheck dates, then the values. It replaces the separate review pushes, the reminder every
     N open days and the weekly factor-health push. A failed push is retried (exit 2, not marked done);
     a sent report is never sent twice for the same day. Escalation of unhandled middle warnings still
     uses the first-seen dates kept in review_trigger_state.json;
  5. the report goes to external_checks\\daily_report_latest.json, which the terminal (start_windows.py's
     log window and status) prints; the old result files are still written.

Morning notices, intraday alerts and everything QMT pushes by itself are not touched.
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
MIRROR = ROOT / 'reports' / 'qmt_native' / 'external_checks'  # 2026-11-14：结果再放一份进 C:\A_System，随双向同步到 Mac
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


def _write_json(path: Path, payload) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    os.replace(tmp, path)


def _mirror(names) -> None:
    """把结果文件复制一份到 C:\\A_System 里（同步到 Mac 用）；复制不成只记日志，不影响检查。"""
    try:
        MIRROR.mkdir(parents=True, exist_ok=True)
        for name in names:
            source = WORK / name
            if source.is_file():
                tmp = MIRROR / (name + '.tmp')
                tmp.write_bytes(source.read_bytes())
                os.replace(tmp, MIRROR / name)
    except OSError as exc:
        print('MIRROR_FAILED %s' % exc, flush=True)


def run(args) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False)
    except ImportError:
        pass
    from src.utils.config import load_json_config
    from src.equity_curve_stop import open_dates_from_calendar
    from src.review_triggers import FINAL_STANDARD, active_acknowledgements, check_failure_trigger, factor_health_triggers
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
    ace_slip, ace_slip_error = None, ''
    coverage, coverage_error = None, ''
    warn_metrics, warn_error = None, ''
    shadow_values, shadow_error = None, ''
    stall, stall_error = None, ''
    live_loss, live_loss_error = None, ''
    start = str(section.get('alignment_start_date', '') or '')
    if start:
        filled = complete = None
        shadow = None
        try:
            shadow = prod.build_shadow_replay(
                project_root=ROOT, feature_path=dataset / 'strict_feature_pool.csv',
                sentiment_path=dataset / 'market_sentiment.csv', calendar_path=calendar,
                start=settings.history_start, end=day, work_dir=settings.work_dir)
            filled, complete = live_trades(config)
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
                filled, complete = live_trades(config)
            slip = m_slippage_stats(filled, start=str(section.get('ace_m_start_date', '') or ''),
                                    trades=int(section.get('m_slippage_trades', 10)),
                                    limit_pct=float(section.get('m_slippage_limit_pct', 1.5)))
            triggers.extend(m_slippage_triggers(slip))
        except Exception as exc:
            traceback.print_exc()
            slip_error = '%s: %s' % (type(exc).__name__, exc)
            triggers.append(check_failure_trigger('M实盘滑点', slip_error))
        try:
            from src.review_triggers import ace_slippage_stats, ace_slippage_triggers
            if filled is None:
                filled, complete = live_trades(config)
            ace_slip = ace_slippage_stats(filled, start=start, trades=int(section.get('ace_slippage_trades', 10)),
                                          limit_pct=float(section.get('ace_slippage_limit_pct', 1.2)))
            triggers.extend(ace_slippage_triggers(ace_slip))
        except Exception as exc:
            traceback.print_exc()
            ace_slip_error = '%s: %s' % (type(exc).__name__, exc)
            triggers.append(check_failure_trigger('A/C/E实盘滑点', ace_slip_error))
        try:
            from src.review_triggers import pool_coverage_triggers
            rows, limit_up = prod.pool_coverage_inputs(dataset, day)
            coverage = {'pool_rows': rows, 'limit_up_count': limit_up,
                        'min_ratio': float(section.get('pool_coverage_min_ratio', 0.3))}
            triggers.extend(pool_coverage_triggers(day, rows, limit_up, min_ratio=coverage['min_ratio'],
                                                   min_limit_up=float(section.get('pool_coverage_min_limit_up', 10))))
        except Exception as exc:
            traceback.print_exc()
            coverage_error = '%s: %s' % (type(exc).__name__, exc)
            triggers.append(check_failure_trigger('研究池覆盖率', coverage_error))
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
        # 2026-11-14起：QMT内照旧检查、推送的影子账失败线、正常行情连续不赚钱、实盘连续亏损也在这里算一遍，写进日报
        if shadow is not None:
            try:
                from src.equity_curve_stop import current_stop_episode, load_weekly_report_settings
                from src.review_triggers import shadow_triggers
                weekly = load_weekly_report_settings(config)
                nav = shadow['nav']
                navs = nav.to_numpy()
                triggers.extend(shadow_triggers(
                    navs, window_3m=weekly.window_3m, window_6m=weekly.window_6m, fail_3m=weekly.shadow_3m_fail,
                    fail_6m=weekly.shadow_6m_fail,
                    stop_episode=current_stop_episode([str(d) for d in nav.index], navs, ma_window=settings.ma_window,
                                                      lag=settings.lag_trading_days),
                    worst_missed_gain=weekly.worst_stop_missed_gain))

                def change(n):
                    return float(navs[-1] / navs[-1 - n] - 1.0) if len(navs) > n else None
                shadow_values = {'r3': change(weekly.window_3m), 'r6': change(weekly.window_6m),
                                 'fail_3m': weekly.shadow_3m_fail, 'fail_6m': weekly.shadow_6m_fail}
            except Exception as exc:
                traceback.print_exc()
                shadow_error = '%s: %s' % (type(exc).__name__, exc)
                triggers.append(check_failure_trigger('影子账失败线', shadow_error))
            try:
                from src.review_triggers import regime_stall_triggers
                stall = prod.compute_regime_stall(config, shadow['nav'], dataset / 'market_sentiment.csv', day)
                triggers.extend(regime_stall_triggers(stall))
            except Exception as exc:
                traceback.print_exc()
                stall_error = '%s: %s' % (type(exc).__name__, exc)
                triggers.append(check_failure_trigger('正常行情连续不赚钱', stall_error))
        try:
            from src.review_triggers import live_loss_streak_triggers, monthly_realized_pnl
            if complete is None:
                filled, complete = live_trades(config)
            need = int(section.get('live_loss_months', 3))
            monthly = monthly_realized_pnl(complete, prod._completed_months(day))
            triggers.extend(live_loss_streak_triggers(monthly, need_months=need))
            streak = 0
            for _month, pnl, count in reversed(list(monthly)):
                if count > 0 and pnl < 0:
                    streak += 1
                else:
                    break
            live_loss = {'months': streak, 'need': need}
        except Exception as exc:
            traceback.print_exc()
            live_loss_error = '%s: %s' % (type(exc).__name__, exc)
            triggers.append(check_failure_trigger('实盘连续亏损', live_loss_error))
    print('FACTOR_HEALTH %s %s' % (day, json.dumps({'health': health, 'error': error, 'alignment_issues': None if align is None
                                                    else len(align), 'alignment_error': align_error,
                                                    'm_slippage': slip, 'm_slippage_error': slip_error,
                                                    'ace_slippage': ace_slip, 'ace_slippage_error': ace_slip_error,
                                                    'pool_coverage': coverage, 'pool_coverage_error': coverage_error},
                                                   ensure_ascii=False, default=str)), flush=True)

    state_path = WORK / 'review_trigger_state.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    entries = section.get('acknowledged') or []
    acknowledged = active_acknowledgements(entries, day)
    recheck = {str(e.get('key', '')): str(e.get('recheck_by', '')) for e in entries if str(e.get('key', '')) in acknowledged}
    if warn_metrics is not None:
        from src.strategy_warnings import escalate, load_warning_settings
        triggers = escalate(triggers, state, acknowledged=acknowledged, today=day, open_dates=dates,
                            open_days=load_warning_settings(config).escalate_open_days)
    from src.daily_report import build_daily_report, value_lines
    warn_section = dict(config.get('strategy_warnings', {}) or {})
    values = {
        'alignment': None if align is None else [t.detail for t in align], 'alignment_error': align_error,
        'm_slippage': slip, 'm_slippage_error': slip_error, 'ace_slippage': ace_slip, 'ace_slippage_error': ace_slip_error,
        'live_loss': live_loss, 'live_loss_error': live_loss_error,
        'coverage': coverage, 'coverage_error': coverage_error,
        'signals': (warn_metrics or {}).get('signals'),
        'health': health, 'health_error': error,
        'warnings': warn_metrics, 'warnings_error': warn_error,
        'thresholds': {'combo_middle': (warn_section.get('middle') or {}).get('combo_drawdown', -0.315),
                       'combo_final': (warn_section.get('final') or {}).get('combo_drawdown', -0.5)},
        'shadow': shadow_values, 'shadow_error': shadow_error, 'stall': stall, 'stall_error': stall_error,
    }
    report = build_daily_report(day, triggers, acknowledged, recheck, state.get('first_seen', {}), value_lines(values),
                                final_standard=FINAL_STANDARD)
    print('STRATEGY_DAILY %s level=%s active=%d acknowledged=%d：%s' % (
        day, report['level'], len(triggers), sum(1 for t in triggers if t.key in acknowledged),
        '；'.join(t.detail for t in triggers) or '无'), flush=True)
    print('STRATEGY_DAILY_REPORT %s\n%s' % (report['title'], report['body']), flush=True)
    if args.no_push:
        return 0
    flag = WORK / ('daily_%s.json' % day)
    if not flag.exists():
        if not prod.notify(report['title'], report['body'], level='active' if report['level'] == 'ok' else 'timeSensitive'):
            print('STRATEGY_DAILY_PUSH_FAILED %s（下次重试）' % day, flush=True)
            return 2
        flag.write_text(json.dumps({'title': report['title'], 'pushed_at': dt.datetime.now(BJ).isoformat()},
                                   ensure_ascii=False), encoding='utf-8')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(state_path, {'first_seen': report['first_seen'], 'active_keys': sorted(report['first_seen'])})
    _write_json(WORK / 'daily_report_latest.json', dict(report, generated_at=dt.datetime.now(BJ).isoformat()))
    if warn_metrics is not None or warn_error:
        from src.strategy_warnings import warning_report
        old = warning_report(triggers, acknowledged, warn_metrics or {}, signal_date=day,
                             generated_at=dt.datetime.now(BJ).isoformat())
        old['error'] = warn_error
        _write_json(WORK / 'strategy_warnings_latest.json', old)
    _write_json(WORK / 'factor_health_latest.json', {
        'signal_date': day, 'factor_health': health, 'factor_health_error': error,
        'alignment_issues': None if align is None else [t.detail for t in align], 'alignment_error': align_error,
        'm_slippage': slip, 'm_slippage_error': slip_error,
        'ace_slippage': ace_slip, 'ace_slippage_error': ace_slip_error,
        'pool_coverage': coverage, 'pool_coverage_error': coverage_error,
        'strategy_warnings_error': warn_error,
        'finished_at': dt.datetime.now(BJ).isoformat()})
    _mirror(['daily_report_latest.json', 'factor_health_latest.json', 'strategy_warnings_latest.json'])
    (WORK / ('done_%s.json' % day)).write_text(json.dumps({
        'signal_date': day, 'triggers': [t.key for t in triggers], 'level': report['level'],
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
            prod.notify('⚠️ 今天的方案甲策略日报没有生成',
                        '%s: %s。QMT外的策略检查今天没有完成，次日停手判定不受影响。'
                        '请在Claude发送：甲·临时复核（先修好这项检查）。' % (type(exc).__name__, exc),
                        level='timeSensitive')
            flag.write_text('{}', encoding='utf-8')
        return 1


if __name__ == '__main__':
    sys.exit(main())
