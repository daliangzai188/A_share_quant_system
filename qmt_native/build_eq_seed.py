"""Build the step-13 seed for the QMT-native incremental equity-curve stop.

Input: a production-like workspace whose step 13 ran for D0 (research dataset under
work_dir/dataset and reports/equity_curve_stop/shadow_nav_latest.csv).
Output: OUT/qn_state/equity_stop/{asof_base.csv, seed_*.csv, state.json} and the NAV file.

Usage: venv/bin/python build_eq_seed.py WORKSPACE DATASET_DIR NAV_CSV D0 OUT_DIR
"""
import json
import sys
from pathlib import Path

import pandas as pd

ws, dataset, nav_csv, d0, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], Path(sys.argv[5])
sys.path.insert(0, str(ws))
from src.fill_model import FillRateTableBuilder  # noqa: E402
from src.equity_curve_stop import build_shadow_replay  # noqa: E402

eq = out / 'qn_state' / 'equity_stop'
eq.mkdir(parents=True, exist_ok=True)
rd = lambda p: pd.read_csv(p, dtype={'trade_date': str, 'ts_code': str}, low_memory=False)
cal = pd.read_csv(ws / 'data/raw/trade_calendar.csv', dtype={'cal_date': str})
days = sorted(cal.loc[pd.to_numeric(cal['is_open'], errors='coerce').eq(1), 'cal_date'])
days = [d for d in days if d <= d0]
keep_from = days[-30]

limit = rd(dataset / 'limit_up_merged.csv')
limit = limit[limit['trade_date'] <= d0]
market = rd(dataset / 'market_sentiment.csv')
cols = [c for c in market.columns if c != 'trade_date' and c not in limit.columns]
keyed = FillRateTableBuilder.add_segment_market_fields(
    limit.merge(market[['trade_date'] + cols], on='trade_date', how='left', validate='many_to_one'))
level = keyed['segment_market_sentiment_level']
pd.DataFrame({'trade_date': keyed['trade_date'], 'ts_code': keyed['ts_code'], 'market_segment': keyed['market_segment'],
              'limit_times': keyed['limit_times'], 'board_type': keyed['board_type'],
              'first_time_bucket': keyed['first_time_bucket'], 'market_sentiment_level': level, 'limit_up_count': 0,
              'segment_market_sentiment_level': level, 'turnover_rate': keyed['turnover_rate']}
             ).to_csv(eq / 'asof_base.csv', index=False)
dtypes = {}
for col, dt in limit.dtypes.items():
    dtypes[col] = 'int' if dt.kind in 'iu' else 'bool' if dt.kind == 'b' else 'float' if dt.kind == 'f' else 'object'
(eq / 'asof_dtypes.json').write_text(json.dumps(dtypes, sort_keys=True, indent=1))
win_limit = limit[limit['trade_date'] >= keep_from]
win_limit.to_csv(eq / 'seed_limit_up_merged.csv', index=False, encoding='utf-8-sig')
# full market for the last 10 days (look-back of stocks that first appear after D0),
# limit-up codes only before that
codes = set(win_limit['ts_code'])
recent = days[-10]
parts = []
for chunk in pd.read_csv(dataset / 'daily_factor_history.csv', dtype={'trade_date': str, 'ts_code': str},
                         low_memory=False, chunksize=500000):
    chunk = chunk[(chunk['trade_date'] >= keep_from) & (chunk['trade_date'] <= d0)
                  & (chunk['ts_code'].isin(codes) | (chunk['trade_date'] >= recent))]
    parts.append(chunk)
pd.concat(parts).to_csv(eq / 'seed_compact_daily.csv', index=False, encoding='utf-8-sig')
market[(market['trade_date'] >= keep_from) & (market['trade_date'] <= d0)].to_csv(
    eq / 'seed_market_sentiment.csv', index=False, encoding='utf-8-sig')
# market sentiment of the last ~15 months: weekly report and review checks (monthly limit-up means)
market[market['trade_date'] <= d0].tail(320).to_csv(eq / 'sentiment_history.csv', index=False, encoding='utf-8-sig')
emo = rd(dataset / 'market_emotion_features.csv')
emo[(emo['trade_date'] >= keep_from) & (emo['trade_date'] <= d0)].to_csv(
    eq / 'seed_market_emotion.csv', index=False, encoding='utf-8-sig')

full = build_shadow_replay(project_root=ws, feature_path=dataset / 'strict_feature_pool.csv',
                           sentiment_path=dataset / 'market_sentiment.csv', calendar_path=ws / 'data/raw/trade_calendar.csv',
                           start='20190101', end=d0, work_dir=out / 'tmp_replay')
detail = full['detail']
flat = detail[(detail['status'].astype(str) == 'NO_PLAN') & (detail['action_date'].astype(str) <= days[-4])]
row = flat.iloc[-1]
state = dict(seed_until=d0, replay_start=str(row['action_date']), replay_cash=float(row['cash_before']),
             initial_cash=float(full['execution']['initial_cash']))
(eq / 'state.json').write_text(json.dumps(state, sort_keys=True, indent=1))
nav = full['nav']
prod = pd.read_csv(nav_csv, dtype={'action_date': str})
prod = pd.Series(prod['shadow_nav'].values, index=prod['action_date'].values)
common = [d for d in nav.index if d in prod.index and d < d0]
mism = [d for d in common if abs(nav[d] - prod[d]) > 1e-12 * max(1.0, abs(prod[d]))]
print('seed state', state, 'replay NAV vs provided NAV mismatches', len(mism), mism[:3])
(out / 'reports' / 'equity_curve_stop').mkdir(parents=True, exist_ok=True)
pd.DataFrame({'action_date': list(nav.index), 'shadow_nav': nav.values}).to_csv(
    out / 'reports' / 'equity_curve_stop' / 'shadow_nav_latest.csv', index=False, encoding='utf-8-sig')
for p in sorted(eq.glob('*')):
    print('%10d %s' % (p.stat().st_size, p.name))
