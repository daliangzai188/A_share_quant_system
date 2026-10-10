"""重算有完整前史的量比缺口，保留来源标签与上市首日未知值。"""
from collections import defaultdict, deque
from pathlib import Path
import datetime as dt
import hashlib
import json

import pandas as pd

BASE = Path(__file__).resolve().parent
EARLY = Path('/Users/eass/A_System_复核/2026-10-09_甲临时复核_独立核验/data_recovery/early_research_supplement')
DATE = '20181224'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    old_manifest = json.loads((EARLY / 'derived_input_manifest.json').read_text())
    dependencies = []
    for record in old_manifest['historical_volume_dependencies']:
        path = EARLY / record['file']
        if sha(path) != record['sha256']:
            raise RuntimeError('历史成交量原件变化：' + record['file'])
        dependencies.append(record)
    history = defaultdict(lambda: deque(maxlen=5))
    observed_days = defaultdict(int)
    derived = {}
    current_codes = set()
    for record in sorted(dependencies, key=lambda x: x['file']):
        path = EARLY / record['file']
        day = path.stem
        frame = pd.read_csv(path, dtype={'ts_code':str, 'trade_date':str})
        if frame.duplicated(['ts_code','trade_date']).any() or not frame.trade_date.eq(day).all():
            raise RuntimeError('历史日线日期或主键错误')
        if day > DATE:
            raise RuntimeError('禁止未来成交量进入量比')
        for row in frame.itertuples():
            values = history[row.ts_code]
            if day == DATE:
                current_codes.add(row.ts_code)
                if len(values)==5 and sum(values)>0:
                    derived[row.ts_code] = float(row.vol)/(sum(values)/5)
            values.append(float(row.vol))
            if day < DATE:
                observed_days[row.ts_code] += 1
    raw = EARLY / old_manifest['raw_source_file']
    if sha(raw) != old_manifest['raw_source_sha256']:
        raise RuntimeError('原始基本面哈希不符')
    original = pd.read_csv(raw, dtype={'ts_code':str, 'trade_date':str})
    fresh_path = BASE / 'market/daily_basic_20181224_ALL_daily.csv'
    fresh = pd.read_csv(fresh_path, dtype={'ts_code':str, 'trade_date':str}).set_index('ts_code')
    for column in ['turnover_rate','turnover_rate_f','volume_ratio','free_share','float_share','total_share','circ_mv','total_mv']:
        observed = original.set_index('ts_code')[column].sort_index()
        latest = fresh[column].reindex(observed.index)
        if not ((observed.isna() & latest.isna()) | (observed-latest).abs().le(1e-8)).all():
            raise RuntimeError('新查询与原始字段不一致：'+column)
    candidate = original.copy()
    candidate['volume_ratio_source'] = 'tushare_daily_basic_original'
    calculated = candidate.ts_code.map(derived)
    known = candidate.volume_ratio.notna() & calculated.notna()
    difference = (candidate.volume_ratio-calculated).abs()
    if int(known.sum()) < 3000 or not difference.loc[known].le(.005000001).all():
        raise RuntimeError('量比口径独立复算不能复现全部已知值')
    missing = candidate.volume_ratio.isna() & candidate.ts_code.str.endswith(('.SH','.SZ'))
    fill = missing & calculated.notna()
    candidate.loc[fill,'volume_ratio'] = calculated.loc[fill]
    candidate.loc[fill,'volume_ratio_source'] = 'derived_today_over_prior_5_observed_stock_days'
    candidate.loc[missing & ~fill,'volume_ratio_source'] = 'undefined_without_5_prior_stock_days'
    if not candidate.loc[~fill, original.columns].equals(original.loc[~fill]):
        raise RuntimeError('已有官方字段发生变化')
    for column in original.columns:
        if column != 'volume_ratio' and not candidate[column].equals(original[column]):
            raise RuntimeError('禁止修改无关官方字段')
    target = BASE / 'market/verified_derived/daily_basic/20181224.csv'
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate.to_csv(target, index=False, encoding='utf-8-sig')
    target.chmod(0o600)
    backup = pd.read_csv(BASE/'market/bak_daily_20181224_ALL_daily.csv', dtype={'ts_code':str}).set_index('ts_code')
    unresolved = candidate.loc[missing & ~fill,'ts_code'].tolist()
    report = {
        'status':'DERIVED_FIELD_VALIDATED_NOT_FORMAL_RELEASE',
        'validated_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        'trade_date':DATE, 'rows':len(candidate),
        'dependencies_verified':len(dependencies),
        'known_control_samples':int(known.sum()),
        'known_control_matches_within_source_rounding':int(difference.loc[known].le(.005000001).sum()),
        'missing_SH_SZ_before':int(missing.sum()), 'derived_values_written':int(fill.sum()),
        'remaining_undefined_SH_SZ':unresolved,
        'remaining_stock_history':{code:{'prior_observed_days':observed_days[code],
                                  'backup_ratio':float(backup.loc[code,'vol_ratio']) if code in backup.index else None}
                                   for code in unresolved},
        'backup_zero_not_used_as_replacement':True,
        'source_dependencies':dependencies,
        'raw_source_file':str(raw), 'raw_source_sha256':sha(raw),
        'fresh_official_crosscheck_file':str(fresh_path.relative_to(BASE)), 'fresh_official_crosscheck_sha256':sha(fresh_path),
        'candidate_file':str(target.relative_to(BASE)), 'candidate_sha256':sha(target),
        'all_original_values_preserved':True, 'no_future_data_used':True,
        'values_are_derived_and_labelled':True, 'full_2018_limit_pool_certified':False,
        'production_writes':False, 'orders_sent':False}
    (BASE/'evidence/early_basic_supplement_acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='source_dependencies'},ensure_ascii=False))


if __name__=='__main__':
    main()
