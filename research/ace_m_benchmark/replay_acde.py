# coding: utf-8
"""Offline replay of the live A/C/E shadow with the system's own code (copy outside the sync folder).
  python replay_acde.py ACDE_COPY WORK END [--reuse]

ACE+M 基准回放要的 ACE 输入（ace/shadow_nav.csv、ace/plans.pkl）就是它算的：用一份冻结代码（ACDE_COPY，含 data/raw）
里的生产函数 build_shadow_replay（影子账）重放 A/C/E。--reuse 时直接用 WORK/dataset 里现成的研究池，否则先用
FiveYearResearchDatasetBuilder 从 2019-01-01 建到 END。

2026-11-23 移入 git：原文件在 ~/A_System_复核/2026-11-11_整体策略重评/scripts/，11-22 随回收站清空，从 11-11 的会话记录
原样恢复；只在最后加了 plans.pkl（基准回放读的格式：键为 (腿, '腿_CURRENT')，每行加 leg_rank=0，即当天第1名）。
11-11 的 ace/plans.pkl 是另一个研究脚本顺带存的（57个方案，回放只用其中3个现行的），这里只写这3个。
"""
import os, sys, time, pickle
from pathlib import Path
root, work, end = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3]
reuse = '--reuse' in sys.argv
os.chdir(root); sys.path.insert(0, str(root))
import pandas as pd
from src.five_year_research import FiveYearResearchDatasetBuilder
from src.equity_curve_stop import build_shadow_replay, gated_replay
t = time.time()
ds = work / 'dataset'
if not (reuse and (ds / 'strict_feature_pool.csv').exists()):
    b = FiveYearResearchDatasetBuilder(research_root=ds)
    b.build_base_tables(start_date='20190101', end_date=end, overwrite=True)
    b.build_strict_features(412500)
print('dataset %.0fs' % (time.time() - t), flush=True)
shadow = build_shadow_replay(project_root=root, feature_path=ds / 'strict_feature_pool.csv',
                             sentiment_path=ds / 'market_sentiment.csv', calendar_path=root / 'data/raw/trade_calendar.csv',
                             start='20190101', end=end, work_dir=work)
out = work / 'replay'; out.mkdir(parents=True, exist_ok=True)
shadow['nav'].to_csv(out / 'shadow_nav.csv')
shadow['detail'].to_csv(out / 'shadow_detail.csv', index=False)
for leg, plan in shadow['legs'].items():
    if len(plan):
        plan.to_csv(out / ('plan_%s.csv' % leg), index=False)
g = gated_replay(shadow, ma_window=60, lag=6)
g.to_csv(out / 'gated_detail.csv', index=False)
# 2026-11-23 加：基准回放（m14_verify.py）读的计划格式
plans = {}
for leg in 'ACE':
    plan = shadow['legs'].get(leg, pd.DataFrame()).reset_index(drop=True).copy()
    plan['leg_rank'] = 0
    plans[(leg, '%s_CURRENT' % leg)] = plan
with open(out / 'plans.pkl', 'wb') as fh:
    pickle.dump(plans, fh)
print('replay done %.0fs; shadow last %s %.4f; detail rows %d gated rows %d' % (
    time.time() - t, shadow['nav'].index[-1], shadow['nav'].iloc[-1], len(shadow['detail']), len(g)), flush=True)
