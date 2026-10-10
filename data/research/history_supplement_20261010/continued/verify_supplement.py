"""核验增量原件，生成IPO量比不适用明细与剩余缺口清单。"""
from pathlib import Path
from collections import Counter
import ast
import datetime as dt
import hashlib
import json
import subprocess

import pandas as pd

BASE = Path(__file__).resolve().parent
PRODUCTION = Path('/Users/eass/Desktop/A_System')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(name):
    return json.loads((BASE / 'evidence' / name).read_text())


def verify_file(record):
    if record.get('file') and record.get('sha256'):
        path = Path(record['file'])
        if not path.is_absolute():
            path = BASE / path
        if sha(path) != record['sha256']:
            raise ValueError('数据依赖哈希不符：' + str(path))


def main():
    ratios = read('remaining_ratio_supplement.json')
    dependencies = ratios['source_dependencies']
    for record in dependencies + ratios['candidate_files']:
        verify_file(record)
    verify_file(ratios['calendar_source'])
    gaps_path = BASE / 'evidence/remaining_ratio_gaps.json'
    controls_path = BASE / 'evidence/remaining_ratio_controls.csv'
    if sha(gaps_path) != ratios['gap_details_sha256'] or sha(controls_path) != ratios['control_sha256']:
        raise ValueError('量比审计明细已被修改')
    gaps = json.loads(gaps_path.read_text())
    stock_path = PRODUCTION / 'data/raw/stock_basic/stock_basic_all.csv'
    stocks = pd.read_csv(stock_path, dtype=str).set_index('ts_code')
    if not stocks.index.is_unique:
        raise ValueError('股票上市信息主键不唯一')
    ipo = []
    for gap in gaps:
        if gap['status'] != 'INSUFFICIENT_PRIOR_5_TRADED_DAYS':
            continue
        code, date = gap['ts_code'], gap['trade_date']
        listed = stocks.loc[code, 'list_date']
        if not '20180101' <= listed <= date:
            raise ValueError('不能据2018年起的前史把旧股复牌误报成IPO基准不足')
        prior = gap['prior_volume_dates']
        if not len(prior) == gap['prior_observed_days'] < 5 or len(set(prior)) != len(prior):
            raise ValueError('IPO前史数量或日期重复')
        if any(not listed <= day < date for day in prior):
            raise ValueError('IPO基准含上市前数据或未来数据')
        ipo.append({'ts_code': code, 'trade_date': date, 'official_list_date': listed,
                    'prior_traded_days': len(prior), 'prior_traded_dates': '|'.join(prior),
                    'current_volume_hands': gap['current_volume_hands'],
                    'classification': 'NOT_APPLICABLE_INSUFFICIENT_PRIOR_5_TRADED_DAYS',
                    'ratio_value': None, 'source_daily_basic_sha256': gap['source_daily_basic_sha256']})
    ipo_path = BASE / 'evidence/ipo_ratio_not_applicable.csv'
    pd.DataFrame(ipo).to_csv(ipo_path, index=False, encoding='utf-8-sig')
    controls = pd.read_csv(controls_path)
    if len(controls) != ratios['official_known_control_samples'] or not controls.difference.le(.0050001).all():
        raise ValueError('官方量比控制验收未全部通过')
    checks = read('existing_permission_checks.json')
    for record in checks['queries']:
        verify_file(record)
    public = read('legacy_tick_sources.json')
    for record in public['queries']:
        if record.get('raw_file') and sha(BASE / record['raw_file']) != record['raw_sha256']:
            raise ValueError('公开入口回包哈希不符')
    for name in ('tdx_archive_sources.json', 'tdx_archive_market_sources.json', 'tdx_archive_current_format_sources.json'):
        document = read(name)
        for record in document.get('index_requests', []) + document.get('queries', []):
            verify_file(record)
    scripts = []
    for path in BASE.glob('*.py'):
        ast.parse(path.read_text())
        scripts.append({'file': path.name, 'sha256': sha(path), 'syntax_valid': True})
    git = subprocess.run(['git', 'status', '--porcelain'], cwd=PRODUCTION, text=True, capture_output=True, check=True)
    broker = read('broker_entry_refresh.json')
    report = {'status': 'PARTIAL_SUPPLEMENT_WITH_EXPLICIT_NOT_APPLICABLE_FIELDS',
              'checked_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
              'new_official_source_rows_captured': checks['captured_source_rows'] + ratios['calendar_source']['rows'],
              'new_official_price_controls': 2, 'new_official_listing_controls': 1,
              'existing_target_stock_days_checked': ratios['target_stock_days'],
              'target_ratios_previously_null': len(gaps), 'new_ratio_values_written': ratios['new_derived_values_written'],
              'ipo_ratio_not_applicable_stock_days': len(ipo),
              'ipo_ratio_not_applicable_unique_stocks': len({x['ts_code'] for x in ipo}),
              'ipo_prior_days_distribution': dict(Counter(str(x['prior_traded_days']) for x in ipo)),
              'ipo_reason_file': str(ipo_path.relative_to(BASE)), 'ipo_reason_file_sha256': sha(ipo_path),
              'stock_listing_source_sha256': sha(stock_path),
              'stock_metadata_used_for_strategy_factors': False,
              'official_known_ratio_controls': len(controls), 'official_known_ratio_matches': int(controls.difference.le(.0050001).sum()),
              'source_dependency_files_verified': len(dependencies),
              'compatible_historical_close_rule_cases': sum(x['compatible_with_historical_close_rule'] for x in checks['closing_price_diagnostic']['cases']),
              'exact_final_trade_window_certified': False, 'remaining_low_difference_stock_days': checks['closing_price_diagnostic']['remaining_low_difference_stock_days'],
              'historical_seal_measurement_certified': False,
              'historical_broker_deals_recovered': 0, 'broker_history_query_executed': False,
              'broker_state_at_latest_read': broker['state'], 'broker_online_at_latest_read': broker['broker_online'],
              'broker_status_observed_at_utc': broker['observed_at_utc'],
              'historical_equity_ledger_restored': False,
              'user_paid_data_preference': 'DO_NOT_PURCHASE_CONTINUE_WITH_EXISTING_PERMISSIONS',
              'paid_data_purchased': False, 'orders_or_cancellations_sent': False,
              'production_writes': False, 'production_worktree_clean': not bool(git.stdout.strip()),
              'formal_baseline_certified': False, 'full_review_status': 'DATA_INCOMPLETE_NOT_READY',
              'script_files': scripts}
    (BASE / 'evidence/继续补充验收.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in report.items() if k != 'script_files'}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
