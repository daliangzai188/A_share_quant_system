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
     (rebuilt by qmt_notify_forwarder.py), else from the old system's file;
  4. production push planning (acknowledged keys, reminder every N open days) and Bark push.

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


def run(args) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False)
    except ImportError:
        pass
    from src.utils.config import load_json_config
    from src.equity_curve_stop import allowed_flags, gated_replay, open_dates_from_calendar
    from src.review_triggers import (active_acknowledgements, alignment_triggers, check_failure_trigger,
                                     factor_health_triggers, plan_push)
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
    start = str(section.get('alignment_start_date', '') or '')
    if start:
        try:
            shadow = prod.build_shadow_replay(
                project_root=ROOT, feature_path=dataset / 'strict_feature_pool.csv',
                sentiment_path=dataset / 'market_sentiment.csv', calendar_path=calendar,
                start=settings.history_start, end=day, work_dir=settings.work_dir)
            filled, _complete = live_trades(config)
            nav = shadow['nav']
            labels = [str(d) for d in nav.index]
            flags = allowed_flags(nav.to_numpy(), ma_window=settings.ma_window, lag=settings.lag_trading_days)
            triggers.extend(alignment_triggers(
                gated_replay(shadow, ma_window=settings.ma_window, lag=settings.lag_trading_days), filled,
                stop_dates={d for d, ok in zip(labels, flags) if not ok}, start=start, end=day))
        except Exception as exc:
            traceback.print_exc()
            triggers.append(check_failure_trigger('实盘与正式规则逐笔对账', '%s: %s' % (type(exc).__name__, exc)))

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
