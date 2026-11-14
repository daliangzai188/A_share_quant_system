"""
全量数据（2020-2026）可执行策略穷举搜索

硬约束：T+1 不涨停、T+2 不跌停
目标：复利 > 50x，固定 0.1% 滑点，80% 仓位，单持仓，T+2 收盘卖出
"""
from __future__ import annotations
import sys, pandas as pd
from pathlib import Path
from itertools import combinations

PROJECT_ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

INITIAL    = 500_000
POS        = 0.80
MIN_TRADES = 30          # 全量数据要求笔数更多
TARGET_EQ  = 50.0
MAX_SIGNAL_RATIO = 0.85  # 不超过总量85%

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

EVAL_YEARS = ['2020','2021','2022','2023','2024','2025','2026']


def simulate(df: pd.DataFrame, sc: str, sa: bool) -> dict | None:
    if len(df) < MIN_TRADES:
        return None
    if sc not in df.columns:
        return None
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
        if float(nr) > 0:
            w += 1
        year_eq[str(r['trade_date'])[:4]] = eq
    if n < MIN_TRADES:
        return None
    prev = INITIAL
    yr_ret = {}
    for yr in sorted(year_eq):
        yr_ret[yr] = year_eq[yr] / prev - 1
        prev = year_eq[yr]
    # 最大回撤
    peak = INITIAL
    max_dd = 0.0
    eq2 = INITIAL
    for _, r in daily.iterrows():
        bd = str(r.get('next_trade_date', ''))
        nr = r.get('net_return_r')
        if pd.isna(nr):
            continue
        eq2 *= (1 + float(nr) * POS)
        peak = max(peak, eq2)
        max_dd = min(max_dd, eq2 / peak - 1)
    return {
        'eq': eq / INITIAL, 'n': n, 'wr': w / n,
        'years': yr_ret, 'max_dd': max_dd,
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


def fmt(conds) -> str:
    if isinstance(conds, tuple) and len(conds) == 2 and isinstance(conds[0], str):
        return f'{conds[0]}={conds[1]}'
    return ';'.join(f'{c}={v}' for c, v in conds)


def main():
    print('加载全量干净信号...')
    clean = pd.read_csv('/tmp/clean_full.csv', low_memory=False,
                        dtype={'ts_code': str, 'trade_date': str,
                               'next_trade_date': str, 'exit_trade_date': str})
    # 还原 factor 列字符串类型
    for col in FACTOR_COLS:
        if col in clean.columns:
            clean[col] = clean[col].astype(str)
    total = len(clean)
    print(f'共 {total} 条，年份分布: {clean["trade_date"].str[:4].value_counts().sort_index().to_dict()}\n')

    # ── 单因子 ──────────────────────────────────────────────────────────
    print('=== 单因子搜索 ===')
    single_res: list[tuple] = []
    for col in FACTOR_COLS:
        if col not in clean.columns:
            continue
        for val in clean[col].dropna().unique():
            if str(val) in ('unknown', 'missing', 'nan', 'None'):
                continue
            sub = clean[clean[col] == str(val)]
            if len(sub) < MIN_TRADES or len(sub) > total * MAX_SIGNAL_RATIO:
                continue
            eq, r, sc = best_sim(sub)
            if r:
                single_res.append(((col, str(val)), eq, r, sc))
    single_res.sort(key=lambda x: x[1], reverse=True)
    print(f'单因子 {len(single_res)} 个, 最高: {single_res[0][1]:.1f}x')
    for cv, eq, r, sc in single_res[:8]:
        y_str = '  '.join(f'{yr}:{r["years"].get(yr,0)*100:+.0f}%' for yr in EVAL_YEARS if yr in r['years'])
        print(f'  {cv[0]}={cv[1]}: {eq:.1f}x {r["n"]}笔 {r["wr"]:.0%}  {y_str}  [{sc[2]}]')

    top_conds = [cv for cv, _, _, _ in single_res[:100]]

    # ── 双因子 ──────────────────────────────────────────────────────────
    print('\n=== 双因子搜索 ===')
    pair_res: list[tuple] = []
    pairs = [(a, b) for a, b in combinations(top_conds, 2) if a[0] != b[0]]
    for i, (a, b) in enumerate(pairs):
        sub = clean[(clean[a[0]] == a[1]) & (clean[b[0]] == b[1])]
        if len(sub) < MIN_TRADES:
            continue
        eq, r, sc = best_sim(sub)
        if r:
            pair_res.append(((a, b), eq, r, sc))
        if (i + 1) % 1000 == 0:
            best_now = max(p[1] for p in pair_res) if pair_res else 0
            hit = sum(1 for p in pair_res if p[1] >= TARGET_EQ)
            print(f'  {i+1}/{len(pairs)}, 最高:{best_now:.1f}x, >50x:{hit}个')
    pair_res.sort(key=lambda x: x[1], reverse=True)
    hit_pair = sum(1 for p in pair_res if p[1] >= TARGET_EQ)
    print(f'双因子 {len(pair_res)} 个, 最高:{pair_res[0][1]:.1f}x, >50x:{hit_pair}个')
    for (a, b), eq, r, sc in pair_res[:8]:
        y_str = '  '.join(f'{yr}:{r["years"].get(yr,0)*100:+.0f}%' for yr in EVAL_YEARS if yr in r['years'])
        print(f'  {a[0]}={a[1]};{b[0]}={b[1]}: {eq:.1f}x {r["n"]}笔 {r["wr"]:.0%}  {y_str}  [{sc[2]}]')

    top_pairs = [(a, b) for (a, b), _, _, _ in pair_res[:100]]

    # ── 三因子 ──────────────────────────────────────────────────────────
    print('\n=== 三因子搜索 ===')
    triple_seen: set[str] = set()
    triple_cands: list[tuple] = []
    for a, b in top_pairs:
        used = {a[0], b[0]}
        for cv in top_conds[:80]:
            if cv[0] in used:
                continue
            tri = tuple(sorted([a, b, cv], key=lambda x: x[0]))
            key = str(tri)
            if key in triple_seen:
                continue
            triple_seen.add(key)
            triple_cands.append(tri)

    triple_res: list[tuple] = []
    limit = min(len(triple_cands), 30000)
    for i, conds in enumerate(triple_cands[:limit]):
        sub = apply_conds(clean, conds)
        if len(sub) < MIN_TRADES:
            continue
        eq, r, sc = best_sim(sub)
        if r:
            triple_res.append((conds, eq, r, sc))
        if (i + 1) % 5000 == 0:
            best_now = max(p[1] for p in triple_res) if triple_res else 0
            hit = sum(1 for p in triple_res if p[1] >= TARGET_EQ)
            print(f'  {i+1}/{limit}, 最高:{best_now:.1f}x, >50x:{hit}个')
    triple_res.sort(key=lambda x: x[1], reverse=True)
    hit_tri = sum(1 for p in triple_res if p[1] >= TARGET_EQ)
    print(f'三因子 {len(triple_res)} 个, 最高:{triple_res[0][1] if triple_res else 0:.1f}x, >50x:{hit_tri}个')

    # ── 汇总 ────────────────────────────────────────────────────────────
    all_res = ([(c, e, r, s) for c, e, r, s in single_res] +
               [((a, b), e, r, s) for (a, b), e, r, s in pair_res] +
               [(c, e, r, s) for c, e, r, s in triple_res])
    all_res.sort(key=lambda x: x[1], reverse=True)
    hit_all = [p for p in all_res if p[1] >= TARGET_EQ]

    print(f'\n{"="*100}')
    print(f'全量数据 T+1不涨停+T+2不跌停 硬约束穷举完成')
    print(f'达到 {TARGET_EQ}x 的方案: {len(hit_all)} 个')
    print(f'{"="*100}\n')

    show = hit_all[:30] if hit_all else all_res[:20]
    for conds, eq, r, sc in show:
        cstr = fmt(conds)
        y_str = '  '.join(f'{yr}:{r["years"].get(yr,0)*100:+.0f}%'
                          for yr in EVAL_YEARS if yr in r['years'])
        mark = '★' if eq >= TARGET_EQ else ' '
        print(f'{mark} {cstr}')
        print(f'    [{sc[2]}] {eq:.1f}x  {r["n"]}笔  {r["wr"]:.0%}  DD:{r["max_dd"]*100:.1f}%')
        print(f'    {y_str}')

    # 保存
    rows = []
    for conds, eq, r, sc in all_res[:500]:
        rows.append({
            'conditions': fmt(conds),
            'sort_rule': sc[2],
            'equity_multiple': round(eq, 3),
            'executed_trades': r['n'],
            'win_rate': round(r['wr'], 3),
            'max_drawdown': round(r['max_dd'], 3),
            **{f'return_{yr}': round(r['years'].get(yr, 0), 3) for yr in EVAL_YEARS},
            'hit_target': eq >= TARGET_EQ,
        })
    out = pd.DataFrame(rows)
    out_path = PROJECT_ROOT / 'reports/executable_full_strategy_search.csv'
    out.to_csv(out_path, index=False, encoding='utf-8-sig')
    print(f'\n结果已保存: {out_path}')


if __name__ == '__main__':
    main()
