# coding: utf-8
"""QMT-native close-pipeline step 13: equity-curve stop (方案甲), same outputs as production.

Production rebuilds the whole 2019+ research dataset every day (~11 min, ~2 GB raw), which
cannot run inside QMT. This version produces the same shadow NAV and decision with the
production functions, from a seed taken from the production run plus a short window:

  * as-of fill scores: FillProbabilityEstimator.score_limit_up_table_asof over the seed's
    compact history rows (+ daily deltas) followed by the window's limit-up rows. Only the
    keys and turnover of past rows enter the as-of history, so the scores are identical;
  * strict feature pool: ThemeHeatBuilder + StrategyConditionOptimizer.load_trades on the
    window (5 trading days of look-back suffice for every decision column);
  * shadow account: replay_action_date_cash_portfolio restarted at a flat action date with
    that date's exact cash; NAV is normalised by the configured initial cash, so every NAV
    value from the restart date on is bit-identical to the full replay;
  * NAV before the restart date comes from the production NAV history (fixed history).

Validated offline against full production rebuilds (identical NAV and decisions).
Pushes go through the QMT log forwarder. Weekly report and daily review checks run with the
production functions; factor health (15-month research pool) and the trade-by-trade alignment
(full replay since 2019) cannot be computed inside QMT and are left out.
"""
import argparse
import copy
import hashlib
import json
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data_cleaner import DataCleaner
from src.equity_curve_stop import (SHADOW_LEGS, decision_for_next_action_date, load_decision, load_settings,
                                   next_open_date, shadow_nav_from_detail, utc_now_iso, write_decision,
                                   write_shadow_nav)
from src.fill_model import FillProbabilityEstimator, FillRateTableBuilder
from src.market_emotion import MarketEmotionBuilder
from src.post_market_freshness import producer_identity
from src.strategy_optimizer import StrategyConditionOptimizer
from src.theme_heat import ThemeHeatBuilder
from src.utils.config import load_json_config

EQ = PROJECT_ROOT / 'qn_state' / 'equity_stop'
LOOKBACK_DAYS = 5
CONTEXT_DAYS = 2  # segment state features use the previous two trading days


def parse_args():
    parser = argparse.ArgumentParser(description='方案甲停手门禁（QMT原生增量版）')
    parser.add_argument('--signal-date', required=True)
    parser.add_argument('--reuse-dataset', action='store_true')
    return parser.parse_args()


def open_days(calendar_path):
    cal = pd.read_csv(calendar_path, dtype={'cal_date': str}, low_memory=False)
    return sorted(cal[pd.to_numeric(cal['is_open'], errors='coerce').eq(1)]['cal_date'].astype(str))


def read(path, **kw):
    return pd.read_csv(path, dtype={'trade_date': str, 'ts_code': str}, low_memory=False, **kw)


def compact_asof_rows(keyed):
    """Rows as they enter the as-of history: keys + turnover (see validation note above)."""
    level = keyed['segment_market_sentiment_level']
    return pd.DataFrame({'trade_date': keyed['trade_date'], 'ts_code': keyed['ts_code'],
                         'market_segment': keyed['market_segment'], 'limit_times': keyed['limit_times'],
                         'board_type': keyed['board_type'], 'first_time_bucket': keyed['first_time_bucket'],
                         'market_sentiment_level': level, 'limit_up_count': 0,
                         'segment_market_sentiment_level': level, 'turnover_rate': keyed['turnover_rate']})


def build_window_tables(days, seed_until):
    """compact daily / limit-up / sentiment / emotion rows for `days`: seed rows up to seed_until,
    production cleaning code on the in-memory raw tables after it (as build_base_tables does)."""
    cleaner = DataCleaner()
    emotion_builder = MarketEmotionBuilder()
    segments = list(emotion_builder.SEGMENTS)
    seed = dict(compact=read(EQ / 'seed_compact_daily.csv'), limit=read(EQ / 'seed_limit_up_merged.csv'),
                sentiment=read(EQ / 'seed_market_sentiment.csv'), emotion=read(EQ / 'seed_market_emotion.csv'))
    compact, limit, sentiment, emotion = [], [], [], []
    for day in days:
        if day <= seed_until:
            compact.append(seed['compact'][seed['compact']['trade_date'] == day])
            limit.append(seed['limit'][seed['limit']['trade_date'] == day])
            sentiment.append(seed['sentiment'][seed['sentiment']['trade_date'] == day])
            emotion.append(seed['emotion'][seed['emotion']['trade_date'] == day])
            continue
        daily = cleaner.clean_daily_by_date(day)
        if daily.empty:
            raise RuntimeError('%s 没有清洗后的日线，无法计算影子账' % day)
        compact.append(daily.reindex(columns=_compact_columns()))
        lim = cleaner.clean_limit_up_by_date(day, daily)
        limit.append(lim)
        sentiment.append(pd.DataFrame([cleaner.build_market_sentiment_row(day, daily, lim)]))
        lim_features = lim
        if lim_features.empty and 'market_segment' not in lim_features.columns:
            lim_features = pd.DataFrame(columns=['trade_date', 'ts_code', 'market_segment', 'limit_times',
                                                 'open_times', 'limit_data_quality', 'strategy_compatible'])
        global_features = emotion_builder.build_global_features(daily, lim_features)
        rows = [dict(trade_date=day, market_segment=segment, **dict(global_features, **emotion_builder.build_segment_features(
            daily, lim_features, segment))) for segment in segments]
        emotion.append(pd.DataFrame(rows))
    out = {}
    for name, parts in (('compact', compact), ('limit', limit), ('sentiment', sentiment), ('emotion', emotion)):
        parts = [p for p in parts if p is not None and len(p)]
        out[name] = pd.concat(parts, ignore_index=True, sort=False) if parts else pd.DataFrame()
    base = emotion_builder.drop_state_columns(out['emotion']) if len(out['emotion']) else out['emotion']
    out['emotion'] = emotion_builder.add_state_features(base)
    return out


def _compact_columns():
    from src.five_year_research import FiveYearResearchDatasetBuilder
    return FiveYearResearchDatasetBuilder.COMPACT_DAILY_COLUMNS


def main():
    args = parse_args()
    signal_date = str(args.signal_date)
    config = load_json_config(PROJECT_ROOT / 'config' / 'config.json')
    settings = load_settings(config, PROJECT_ROOT)
    if not settings.enabled:
        print('EQUITY_CURVE_STOP_DISABLED', flush=True)
        return 0
    started = time.time()
    started_identity = producer_identity(PROJECT_ROOT, signal_date, config)
    calendar_path = PROJECT_ROOT / 'data' / 'raw' / 'trade_calendar.csv'
    days = open_days(calendar_path)
    next_date = next_open_date(calendar_path, signal_date)
    state = json.loads((EQ / 'state.json').read_text(encoding='utf-8'))
    replay_start = state['replay_start']
    if replay_start > signal_date:
        raise RuntimeError('影子账起点 %s 晚于收盘日 %s' % (replay_start, signal_date))
    window_start = days[days.index(replay_start) - LOOKBACK_DAYS]
    first_table_day = days[days.index(window_start) - CONTEXT_DAYS]
    window_days = [d for d in days if first_table_day <= d <= signal_date]
    tables = build_window_tables(window_days, state['seed_until'])
    work = PROJECT_ROOT / 'qn_state' / 'equity_stop_work'
    work.mkdir(parents=True, exist_ok=True)
    tables['sentiment'].to_csv(work / 'market_sentiment.csv', index=False, encoding='utf-8-sig')
    tables['emotion'].to_csv(work / 'market_emotion.csv', index=False, encoding='utf-8-sig')
    tables['compact'].to_csv(work / 'compact_daily.csv', index=False, encoding='utf-8-sig')

    # as-of fill scores
    limit = tables['limit']
    market = tables['sentiment']
    cols = [c for c in market.columns if c != 'trade_date' and c not in limit.columns]
    merged = limit.merge(market[['trade_date'] + cols], on='trade_date', how='left', validate='many_to_one')
    keyed = FillRateTableBuilder.add_segment_market_fields(merged)
    for day in sorted(set(keyed['trade_date'])):
        delta = EQ / ('asof_delta_%s.csv' % day)
        if day > state['seed_until'] and not delta.exists():
            compact_asof_rows(keyed[keyed['trade_date'] == day]).to_csv(delta, index=False)
    history = [read(EQ / 'asof_base.csv')]
    for path in sorted(EQ.glob('asof_delta_*.csv')):
        history.append(read(path))
    history = pd.concat(history, ignore_index=True, sort=False)
    history = history[history['trade_date'] < window_start].copy()
    window_rows = merged[merged['trade_date'] >= window_start]
    # The scorer re-infers column types over the whole table. History rows only contribute
    # keys + turnover, but their other cells must not turn integer columns into floats
    # (e.g. first_time 93000 -> 93000.0), so they are filled per the production column types.
    dtypes = json.loads((EQ / 'asof_dtypes.json').read_text(encoding='utf-8'))
    for col in window_rows.columns:
        if col in history.columns:
            continue
        kind = dtypes.get(col)
        if kind == 'int':
            history[col] = 0
        elif kind == 'bool':
            history[col] = False
        elif kind == 'object':
            history[col] = ''
    pd.concat([history, window_rows], ignore_index=True, sort=False).to_csv(work / 'asof_input.csv', index=False)
    amount = float(config.get('fill_model', {}).get('default_planned_buy_amount', 412500))
    FillProbabilityEstimator(PROJECT_ROOT / 'config' / 'config.json').score_limit_up_table_asof(
        work / 'asof_input.csv', work / 'strict_fill_all.csv', planned_buy_amount=amount,
        market_sentiment_path=work / 'market_sentiment.csv')
    strict = read(work / 'strict_fill_all.csv')
    strict[strict['trade_date'] >= window_start].to_csv(work / 'strict_fill.csv', index=False, encoding='utf-8-sig')
    limit[limit['trade_date'] >= window_start].to_csv(work / 'limit_up_merged.csv', index=False, encoding='utf-8-sig')

    # strict feature pool for the window
    ThemeHeatBuilder(PROJECT_ROOT / 'config' / 'config.json').build(
        input_path=str(work / 'limit_up_merged.csv'), output_path=str(work / 'theme_heat.csv'))
    optimizer = StrategyConditionOptimizer(PROJECT_ROOT / 'config' / 'config.json',
                                           optimization_config_key='five_year_walk_forward_research')
    optimizer.input_trades_path = work / 'strict_fill.csv'
    optimizer.input_daily_merged_path = work / 'compact_daily.csv'
    optimizer.optional_market_emotion_features_path = work / 'market_emotion.csv'
    optimizer.optional_theme_heat_features_path = work / 'theme_heat.csv'
    missing = work / 'optional_features_not_used'
    optimizer.optional_auction_features_path = missing / 'auction.csv'
    optimizer.optional_open_5m_features_path = missing / 'open_5m.csv'
    optimizer.optional_sector_moneyflow_features_path = missing / 'moneyflow.csv'
    optimizer.optional_top_list_features_path = missing / 'top_list.csv'
    pool = optimizer.load_trades(require_complete_exit=False)
    pool['trade_date'] = pool['trade_date'].astype(str)
    pool = pool.sort_values(['trade_date', 'ts_code']).reset_index(drop=True)
    pool.to_csv(work / 'strict_feature_pool.csv', index=False, encoding='utf-8-sig')

    # shadow account restarted at the flat date
    from scripts.optimize_acde_rolling_three_year import build_variant_plan
    from src.acde_monthly_research import _context, _execution_kwargs, _variant_sets, load_monthly_config
    from src.acde_rolling_candidates import StaticOutcomeCache
    from src.acde_rolling_framework import FIXED_PRIORITY, ResearchWindow, replay_action_date_cash_portfolio
    monthly = load_monthly_config(PROJECT_ROOT / 'config/acde_rolling_optimization.json')
    empty_d_events = work / 'empty_d_events.csv'
    pd.DataFrame(columns=['trade_date', 'ts_code']).to_csv(empty_d_events, index=False)
    window = ResearchWindow('EQUITY_CURVE_STOP_SHADOW', replay_start, signal_date, 'shadow', False)
    context = _context(window=window, feature_path=work / 'strict_feature_pool.csv',
                       sentiment_path=work / 'market_sentiment.csv', d_event_path=empty_d_events,
                       calendar_path=calendar_path,
                       minimum_limit_up_count=int(monthly['market_controller']['minimum_limit_up_count']))
    baselines, _candidates = _variant_sets()
    cache = StaticOutcomeCache()
    legs = {}
    for leg in FIXED_PRIORITY:
        legs[leg] = (build_variant_plan(baselines[leg], signal_pool=context['signal_pool'], d_events=context['d_events'],
                                        allowed_action_dates=context['allowed_actions'], cutoff=signal_date,
                                        outcome_cache=cache) if leg in SHADOW_LEGS else pd.DataFrame())
    execution = _execution_kwargs(monthly)
    if float(execution['initial_cash']) != float(state['initial_cash']):
        raise RuntimeError('配置的影子账初始资金与种子不一致，拒绝计算')
    replay_execution = dict(execution, initial_cash=float(state['replay_cash']))
    detail = replay_action_date_cash_portfolio(legs, action_dates=context['action_dates'], priority=FIXED_PRIORITY,
                                               **replay_execution)
    tail = shadow_nav_from_detail(detail, context['action_dates'], initial_cash=float(execution['initial_cash']))

    history_nav = pd.read_csv(PROJECT_ROOT / 'reports' / 'equity_curve_stop' / 'shadow_nav_latest.csv',
                              dtype={'action_date': str})
    history_nav = pd.Series(history_nav.iloc[:, 1].astype(float).values, index=history_nav.iloc[:, 0].astype(str).values)
    nav = pd.concat([history_nav[history_nav.index < replay_start], tail])
    nav.index.name = history_nav.index.name
    if nav.empty or str(nav.index[-1]) != signal_date:
        raise RuntimeError('影子净值最后一个行动日=%s，不是收盘日%s' % (nav.index[-1] if len(nav) else '空', signal_date))
    previous = load_decision(settings)
    decision = decision_for_next_action_date(list(nav.index), nav.values, next_date, ma_window=settings.ma_window,
                                             lag=settings.lag_trading_days)
    decision.update(signal_date=signal_date, computed_at=utc_now_iso(), history_start=settings.history_start,
                    elapsed_seconds=round(time.time() - started, 1))
    built_identity = producer_identity(PROJECT_ROOT, signal_date, config)
    if started_identity['source_sha256'] != built_identity['source_sha256']:
        raise RuntimeError('影子账计算期间代码/配置发生变化，拒绝写判定')
    decision['producer_identity'] = built_identity
    decision['research_pool_sha256'] = hashlib.sha256((work / 'strict_feature_pool.csv').read_bytes()).hexdigest()
    decision['diagnostic_dataset_reused'] = False
    decision['qmt_native_incremental'] = {'replay_start': replay_start, 'window_start': window_start,
                                          'seed_until': state['seed_until']}
    write_shadow_nav(settings, nav)
    write_decision(settings, decision)
    sentiment_history = update_sentiment_history(market)

    # advance the restart point to a newer flat date (keeps the window short)
    flat = detail[(detail['status'].astype(str) == 'NO_PLAN') & (detail['action_date'].astype(str) > replay_start)
                  & (detail['action_date'].astype(str) <= days[days.index(signal_date) - 3])]
    if len(flat):
        row = flat.iloc[-1]
        state.update(replay_start=str(row['action_date']), replay_cash=float(row['cash_before']))
        (EQ / 'state.json').write_text(json.dumps(state, sort_keys=True, indent=1), encoding='utf-8')
    state_text = '允许开仓' if decision['allowed'] else '停手'
    print('EQUITY_CURVE_STOP %s %s：%s' % (next_date, state_text, decision['reason']), flush=True)
    was_allowed = previous.get('allowed') if previous else None
    if was_allowed is not None and bool(was_allowed) != bool(decision['allowed']):
        print('EQUITY_CURVE_STOP_CHANGED %s -> %s' % (was_allowed, decision['allowed']), flush=True)
        # same pushes as production (delivered through the QMT log forwarder)
        if decision['allowed']:
            notify('✅ 方案甲恢复开仓：%s' % next_date,
                   '%s。%s起A/C/E/D按规则正常开仓。' % (decision['reason'], next_date), level='timeSensitive')
        else:
            notify('⏸ 方案甲停手：%s起不开新仓' % next_date,
                   '%s。已有持仓照常到期卖出；影子净值回到均线上方后自动恢复。' % decision['reason'],
                   level='timeSensitive')
    advisory_pushes(config, settings, nav, decision, calendar_path, signal_date, sentiment_history)
    return 0


def update_sentiment_history(window_sentiment):
    """Market sentiment history (seeded from production) with the window's rows, which the
    production builder computed above; used only by the weekly report and review checks."""
    path = EQ / 'sentiment_history.csv'
    if not path.exists():
        return None
    history = read(path)
    window = window_sentiment.copy()
    window['trade_date'] = window['trade_date'].astype(str)
    history = history[~history['trade_date'].isin(set(window['trade_date']))]
    out = pd.concat([history, window[[c for c in history.columns if c in window.columns]]],
                    ignore_index=True, sort=False).sort_values('trade_date').tail(400)
    out.to_csv(path, index=False, encoding='utf-8-sig')
    return path


def advisory_pushes(config, settings, nav, decision, calendar_path, signal_date, sentiment_path):
    """Production's weekly report and daily review-trigger check (they never affect the gate)."""
    import traceback
    try:
        from qn import prod_equity_curve_stop as prod
    except Exception:
        traceback.print_exc()
        return
    print('QMT原生模式：因子健康（15个月研究池）与实盘逐笔对账（2019起全量重放）不在QMT内计算；'
          '周报与其余临时复核检查照常。', flush=True)
    prod.push_weekly_report(config, settings, nav, decision, calendar_path, signal_date,
                            sentiment_path=sentiment_path, factor_health={})
    review_config = copy.deepcopy(config)
    review_config.setdefault('review_triggers', {})['alignment_start_date'] = ''
    try:
        prod.run_review_trigger_check(review_config, settings, {'nav': nav}, calendar_path, signal_date,
                                      sentiment_path, {}, '')
    except Exception as exc:
        traceback.print_exc()
        notify('⚠️ 临时复核触发检查没有完成',
               '%s: %s。今天的失败条件没有被检查，次日停手判定不受影响。'
               '请在Claude发送：甲·临时复核（先修好这项检查）。' % (type(exc).__name__, exc), level='timeSensitive')


def notify(title, body, level='active'):
    """Production's helper: src.notify with event equity_curve_stop; never raises."""
    try:
        from src.notify import notify as _notify
        return bool(_notify('equity_curve_stop', title, body, level=level))
    except Exception:
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print('⛔ 方案甲停手门禁计算失败；次日按 fail-closed 不开新仓', flush=True)
        notify('⛔ 方案甲停手门禁计算失败',
               '%s: %s。次日将按fail-closed不开新仓，请检查收盘流水线⑬。' % (type(exc).__name__, exc),
               level='timeSensitive')
        raise SystemExit(1)
