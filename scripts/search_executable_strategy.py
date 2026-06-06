"""
可执行策略穷举搜索

硬约束：
- T+1 当天不涨停（无论有没有开板）→ 集合竞价可以正常买入
- T+2 当天不跌停 → 可以正常卖出
- 近2年数据（2024-05-20 至今）
- 固定 0.1% 滑点，80% 仓位，单持仓，T+2 收盘卖出

目标：找到复利 > 50x 的条件 × 排序规则组合
"""
from __future__ import annotations
import sys, pandas as pd, numpy as np
from pathlib import Path
from itertools import combinations

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.optimize_recent_2y_fresh import Recent2YFreshOptimizer

INITIAL   = 500_000
POS       = 0.80
MIN_TRADES = 15
TARGET_EQ  = 50.0

FACTOR_COLS = [
    'market_segment','limit_pct_bucket','market_sentiment_level',
    'segment_market_sentiment_level','market_emotion_state_bucket',
    'segment_emotion_state_bucket','market_chain_count_bucket',
    'segment_chain_count_bucket','market_limit_down_count_bucket',
    'segment_limit_down_count_bucket','segment_limit_down_ratio_bucket',
    'segment_limit_max_height_bucket','market_leader_rank_bucket',
    'segment_market_leader_rank_bucket','limit_height_rank_bucket',
    'segment_limit_height_rank_bucket','first_time_bucket',
    'first_time_detail_bucket','limit_times_bucket','limit_times_detail_bucket',
    'open_times_bucket','amount_bucket','turnover_rate_bucket',
    'volume_ratio_bucket','fd_ratio_bucket','pct_chg_bucket',
    'limit_up_count_bucket','segment_limit_up_count_bucket',
    'segment_limit_up_ratio_bucket','retreat_state_bucket',
    'segment_retreat_state_bucket','board_type',
]

SORT_RULES = [
    ('turnover_rate', False, 'tr_desc'),
    ('circ_mv',       True,  'mv_asc'),
    ('volume_ratio',  False, 'vr_desc'),
    ('amount',        False, 'amt_desc'),
    ('amount',        True,  'amt_asc'),
    ('fill_probability', False, 'fp_desc'),
]


def load_clean_signals() -> pd.DataFrame:
    opt = Recent2YFreshOptimizer()
    full = opt.load_recent_candidates()

    raw = pd.read_csv(
        PROJECT_ROOT / 'data/processed/next_day_premium_trades.csv',
        low_memory=False, dtype={'ts_code': str, 'trade_date': str},
    )
    raw['next_trade_date'] = raw['next_trade_date'].astype(str)
    raw['exit_trade_date'] = raw['exit_trade_date'].astype(str)
    raw = raw[raw['trade_date'] >= '20240520'][[
        'trade_date', 'ts_code', 'next_trade_date', 'exit_trade_date',
        'next_open', 'exit_close', 'net_return', 'fee_rate',
    ]].drop_duplicates(['trade_date', 'ts_code'])

    dm = pd.read_csv(
        PROJECT_ROOT / 'data/processed/daily_merged.csv',
        usecols=['ts_code', 'trade_date', 'pct_chg'],
        low_memory=False, dtype={'ts_code': str, 'trade_date': str},
    )
    t1 = dm.rename(columns={'trade_date': 'next_trade_date', 'pct_chg': 't1_p'})
    t2 = dm.rename(columns={'trade_date': 'exit_trade_date', 'pct_chg': 't2_p'})

    raw = raw.merge(t1, on=['ts_code', 'next_trade_date'], how='left')
    raw = raw.merge(t2, on=['ts_code', 'exit_trade_date'], how='left')

    full = full.merge(
        raw[['trade_date', 'ts_code', 'next_trade_date', 'exit_trade_date',
             'net_return', 't1_p', 't2_p']],
        on=['trade_date', 'ts_code'], how='left', suffixes=('', '_r'),
    )

    full['lmt'] = full['market_segment'].map(
        {'chi_next': 20, 'star': 20, 'bj': 30}
    ).fillna(10)
    full['t1_limitup']   = full['t1_p']  >= full['lmt'] * 0.99
    full['t2_limitdown'] = full['t2_p']  <= -full['lmt'] * 0.99

    clean = full[
        ~full['t1_limitup'] & ~full['t2_limitdown'] & full['net_return_r'].notna()
    ].copy()

    print(f'原始信号: {len(full)}  排除T+1涨停:{full["t1_limitup"].sum()}'
          f'  排除T+2跌停:{full["t2_limitdown"].sum()}  剩余:{len(clean)}')
    return clean


def simulate(df: pd.DataFrame, sc: str, sa: bool) -> dict | None:
    if len(df) < MIN_TRADES:
        return None
    if sc not in df.columns:
        sc = 'turnover_rate'
    daily = (
        df.sort_values(['trade_date', sc], ascending=[True, sa])
          .groupby('trade_date').head(1)
          .reset_index(drop=True)
    )
    eq, occ, n, w = INITIAL, '', 0, 0
    year_eq: dict[str, float] = {}
    for _, r in daily.iterrows():
        bd = str(r.get('next_trade_date', ''))
        if occ and bd <= occ:
            continue
        nr = r.get('net_return_r')
        if pd.isna(nr):
            continue
        eq *= (1 + float(nr) * POS)
        occ = str(r.get('exit_trade_date', ''))
        n  += 1
        if float(nr) > 0: w += 1
        year_eq[str(r['trade_date'])[:4]] = eq
    if n < MIN_TRADES:
        return None
    prev = INITIAL
    yr_ret = {}
    for yr in sorted(year_eq):
        yr_ret[yr] = year_eq[yr] / prev - 1
        prev = year_eq[yr]
    peak = INITIAL
    max_dd = 0.0
    for _, r in daily.iterrows():
        pass  # simplified
    return {
        'eq': eq / INITIAL, 'n': n, 'wr': w / n,
        'years': yr_ret, 'final_eq': eq,
    }


def apply_conds(df: pd.DataFrame, conds: tuple) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for col, val in conds:
        if col in df.columns:
            mask &= df[col].astype(str) == str(val)
    return df[mask]


def best_sim(df: pd.DataFrame) -> tuple[float, dict | None, tuple]:
    best_eq, best_r, best_sc = 0.0, None, ('turnover_rate', False, 'tr_desc')
    for sc, sa, sl in SORT_RULES:
        if sc not in df.columns:
            continue
        r = simulate(df, sc, sa)
        if r and r['eq'] > best_eq:
            best_eq, best_r, best_sc = r['eq'], r, (sc, sa, sl)
    return best_eq, best_r, best_sc


def main():
    clean = load_clean_signals()
    total = len(clean)
    print(f'按年份: {clean["trade_date"].str[:4].value_counts().sort_index().to_dict()}\n')

    # ── 单因子 ──────────────────────────────────────────────────────────
    print('=== 搜索单因子 ===')
    single_res: list[tuple[tuple, float, dict, tuple]] = []
    for col in FACTOR_COLS:
        if col not in clean.columns:
            continue
        for val in clean[col].dropna().unique():
            if str(val) in ('unknown', 'missing', 'nan', 'None'):
                continue
            sub = clean[clean[col].astype(str) == str(val)]
            if len(sub) < MIN_TRADES or len(sub) > total * 0.85:
                continue
            eq, r, sc = best_sim(sub)
            if r:
                single_res.append(((col, str(val)), eq, r, sc))
    single_res.sort(key=lambda x: x[1], reverse=True)
    print(f'单因子: {len(single_res)} 个，TOP10:')
    for (col, val), eq, r, sc in single_res[:10]:
        y24 = r['years'].get('2024', 0) * 100
        y25 = r['years'].get('2025', 0) * 100
        y26 = r['years'].get('2026', 0) * 100
        print(f'  {col}={val:<35} {eq:6.1f}x {r["n"]:4}笔 {r["wr"]:5.0%} '
              f'2024:{y24:+.0f}% 2025:{y25:+.0f}% 2026:{y26:+.0f}%  [{sc[2]}]')

    top_conds = [(cv,) for cv, _, _, _ in single_res[:80]]

    # ── 双因子 ──────────────────────────────────────────────────────────
    print('\n=== 搜索双因子 ===')
    pair_res: list[tuple[tuple, float, dict, tuple]] = []
    pair_candidates = [
        (a[0], b[0])
        for a, b in combinations(top_conds, 2)
        if a[0][0] != b[0][0]
    ]
    for i, (a, b) in enumerate(pair_candidates):
        sub = clean[(clean[a[0]].astype(str) == a[1]) & (clean[b[0]].astype(str) == b[1])]
        if len(sub) < MIN_TRADES:
            continue
        eq, r, sc = best_sim(sub)
        if r:
            pair_res.append(((a, b), eq, r, sc))
        if (i + 1) % 500 == 0:
            best_so_far = max(p[1] for p in pair_res) if pair_res else 0
            hit = sum(1 for p in pair_res if p[1] >= TARGET_EQ)
            print(f'  双因子进度 {i+1}/{len(pair_candidates)}, 最高:{best_so_far:.1f}x, >50x:{hit}个')
    pair_res.sort(key=lambda x: x[1], reverse=True)
    print(f'双因子: {len(pair_res)} 个，TOP10:')
    for (a, b), eq, r, sc in pair_res[:10]:
        y24 = r['years'].get('2024', 0) * 100
        y25 = r['years'].get('2025', 0) * 100
        y26 = r['years'].get('2026', 0) * 100
        print(f'  {a[0]}={a[1]};{b[0]}={b[1]}')
        print(f'    {eq:6.1f}x {r["n"]:4}笔 {r["wr"]:5.0%} '
              f'2024:{y24:+.0f}% 2025:{y25:+.0f}% 2026:{y26:+.0f}%  [{sc[2]}]')

    top_pairs = [(conds,) for conds, _, _, _ in pair_res[:80]]

    # ── 三因子 ──────────────────────────────────────────────────────────
    print('\n=== 搜索三因子 ===')
    triple_seen: set[str] = set()
    triple_candidates: list[tuple] = []
    for (base,) in top_pairs:
        used = {c[0] for c in base}
        for (cv,) in top_conds[:60]:
            if cv[0] in used:
                continue
            tri = tuple(sorted(list(base) + [cv], key=lambda x: x[0]))
            key = str(tri)
            if key in triple_seen:
                continue
            triple_seen.add(key)
            triple_candidates.append(tri)

    triple_res: list[tuple[tuple, float, dict, tuple]] = []
    limit = min(len(triple_candidates), 25000)
    for i, conds in enumerate(triple_candidates[:limit]):
        sub = apply_conds(clean, conds)
        if len(sub) < MIN_TRADES:
            continue
        eq, r, sc = best_sim(sub)
        if r:
            triple_res.append((conds, eq, r, sc))
        if (i + 1) % 3000 == 0:
            best_so_far = max(p[1] for p in triple_res) if triple_res else 0
            hit = sum(1 for p in triple_res if p[1] >= TARGET_EQ)
            print(f'  三因子进度 {i+1}/{limit}, 最高:{best_so_far:.1f}x, >50x:{hit}个')

    triple_res.sort(key=lambda x: x[1], reverse=True)
    hit = [p for p in triple_res if p[1] >= TARGET_EQ]
    print(f'\n三因子完成: {len(triple_res)} 个，>50x: {len(hit)} 个')

    # ── 汇总输出 ─────────────────────────────────────────────────────────
    print('\n' + '=' * 90)
    print(f'最终结果（T+1不涨停 + T+2不跌停 硬约束，>50x 方案）')
    print('=' * 90)

    all_res = single_res + [(c,e,r,s) for c,e,r,s in pair_res] + triple_res
    all_res.sort(key=lambda x: x[1], reverse=True)

    hit_all = [p for p in all_res if p[1] >= TARGET_EQ]
    print(f'达到 {TARGET_EQ}x 的方案: {len(hit_all)} 个\n')

    top_n = hit_all[:30] if hit_all else all_res[:20]
    for conds, eq, r, sc in top_n:
        if isinstance(conds, tuple) and isinstance(conds[0], str):
            cstr = f'{conds[0]}={conds[1]}'
        else:
            cstr = ';'.join(f'{c}={v}' for c, v in conds)
        y24 = r['years'].get('2024', 0) * 100
        y25 = r['years'].get('2025', 0) * 100
        y26 = r['years'].get('2026', 0) * 100
        mark = '★' if eq >= TARGET_EQ else ''
        print(f'{mark} {cstr}')
        print(f'    [{sc[2]}] {eq:.1f}x  {r["n"]}笔  {r["wr"]:.0%}胜率  '
              f'2024:{y24:+.0f}%  2025:{y25:+.0f}%  2026:{y26:+.0f}%')

    # 保存
    out_rows = []
    for conds, eq, r, sc in all_res[:200]:
        if isinstance(conds, tuple) and isinstance(conds[0], str):
            cstr = f'{conds[0]}={conds[1]}'
        else:
            cstr = ';'.join(f'{c}={v}' for c, v in conds)
        out_rows.append({
            'conditions': cstr, 'sort_rule': sc[2],
            'equity_multiple': round(eq, 4), 'executed_trades': r['n'],
            'win_rate': round(r['wr'], 4),
            'return_2024': round(r['years'].get('2024', 0), 4),
            'return_2025': round(r['years'].get('2025', 0), 4),
            'return_2026': round(r['years'].get('2026', 0), 4),
            'hit_target': eq >= TARGET_EQ,
        })
    out = pd.DataFrame(out_rows)
    out_path = PROJECT_ROOT / 'reports/executable_strategy_search.csv'
    out.to_csv(out_path, index=False, encoding='utf-8-sig')
    print(f'\n结果已保存: {out_path}')


if __name__ == '__main__':
    main()
