"""使用现有 Tushare 权限补采针对性原件，核验历史收盘口径与首日量比。"""
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
import datetime as dt
import hashlib
import json
import sys

import pandas as pd
from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent
PARENT = BASE.parent
EARLY = Path('/Users/eass/A_System_复核/2026-10-09_甲临时复核_独立核验/data_recovery/early_research_supplement')
MINUTE = EARLY.parent / 'early_minute_supplement'
sys.path.insert(0, '/Users/eass/A_System_Rebuild/2026-10-08/pipeline_repair/backend_inspection')
from run_official_history_collection import OfficialClient, ROOT

RULE_BEFORE = 'https://www.sse.com.cn/lawandrules/sselawsrules/repeal/rules/c/c_20210531_5478084.shtml'
RULE_CHANGE = 'https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/c_20180806_10784917.shtml'
RULE_EFFECTIVE = 'https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20180806_4607055.shtml'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(client, token, api, date, code, fields, **params):
    receipt = BASE / 'evidence' / f'{api}_{code}_{date or "metadata"}.json'
    if receipt.exists():
        record = json.loads(receipt.read_text())
        if record.get('file') and sha(BASE / record['file']) != record['sha256']:
            raise ValueError('已采针对性原件哈希不符')
        return record
    record = {'api': api, 'trade_date_requested': date, 'ts_code': code, 'params': params,
              'queried_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    try:
        frame = client.query(api, date, fields=fields, ts_code=code, **params)
        if not set(fields.split(',')).issubset(frame.columns) or len(frame) >= 7000:
            raise ValueError('返回字段不全或可能触及截断上限')
        if len(frame):
            if not frame.ts_code.eq(code).all():
                raise ValueError('证券主键错误')
            if api == 'bak_daily' and not frame.trade_date.astype(str).eq(date).all():
                raise ValueError('日线交易日期错误')
            if api == 'stk_mins' and not frame.trade_time.str[:10].str.replace('-', '').eq(date).all():
                raise ValueError('分钟交易日期错误')
        path = BASE / 'raw' / f'{api}_{code}_{date or "metadata"}.csv'
        frame.to_csv(path, index=False, encoding='utf-8-sig')
        path.chmod(0o600)
        record.update(status='RAW_CAPTURED' if len(frame) else 'SOURCE_EMPTY', rows=len(frame),
                      file=str(path.relative_to(BASE)), sha256=sha(path))
    except Exception as error:
        record.update(status='SOURCE_QUERY_FAILED', rows=0, error_type=type(error).__name__,
                      error=str(error).replace(token, '[REDACTED]')[:250])
    receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    return record


def closing_diagnostics(records):
    diagnostics = pd.read_csv(MINUTE / 'traded_bar_price_diagnostic.csv', dtype={'trade_date': str})
    cases = diagnostics.loc[diagnostics.close_traded_difference.abs().gt(.005000001)].copy()
    results = []
    minute_records = json.loads((MINUTE / 'status.json').read_text())['records'].values()
    for case in cases.itertuples():
        daily_path = EARLY / 'data/raw/daily' / (case.trade_date + '.csv')
        daily = pd.read_csv(daily_path, dtype={'ts_code': str}).set_index('ts_code').loc[case.ts_code]
        backup = next((x for x in records if x['api'] == 'bak_daily' and x['ts_code'] == case.ts_code
                       and x['trade_date_requested'] == case.trade_date and x.get('file')), None)
        row = {'ts_code': case.ts_code, 'trade_date': case.trade_date,
               'minute_last_price': round(float(case.daily_close + case.close_traded_difference), 4),
               'official_daily_close': float(daily.close), 'last_clock_minute_vwap': case.last_minute_vwap,
               'rounded_clock_minute_vwap': float(Decimal(str(case.last_minute_vwap)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)),
               'primary_daily_sha256': sha(daily_path), 'tick_window_verified': False,
               'prices_rewritten': False, 'formal_price_certified': False}
        original_source = next(x for x in minute_records if x['code'] == case.ts_code
                               and any(y['trade_date'] == case.trade_date for y in x['coverage']))
        minute_path = MINUTE / original_source['file']
        if sha(minute_path) != original_source['sha256']:
            raise ValueError('收盘口径核验的原分钟数据被修改')
        original_minutes = pd.read_csv(minute_path)
        prefix = dt.datetime.strptime(case.trade_date, '%Y%m%d').strftime('%Y-%m-%d')
        original_day = original_minutes.loc[original_minutes.trade_time.str.startswith(prefix)].sort_values('trade_time')
        last = original_day.loc[original_day.vol.gt(0)].iloc[-1]
        if abs(float(last.amount / last.vol)-case.last_minute_vwap) > 1e-8:
            raise ValueError('历史诊断的成交均价与分钟原件不符')
        row['original_minute_source_sha256'] = original_source['sha256']
        fresh = next((x for x in records if x['api'] == 'stk_mins' and x['ts_code'] == case.ts_code
                      and x['trade_date_requested'] == case.trade_date and x.get('file')), None)
        if fresh:
            new_day = pd.read_csv(BASE / fresh['file']).sort_values('trade_time')
            columns = ['ts_code', 'trade_time', 'open', 'high', 'low', 'close', 'vol', 'amount']
            pd.testing.assert_frame_equal(original_day[columns].reset_index(drop=True),
                                          new_day[columns].reset_index(drop=True), check_exact=True)
            row['fresh_minute_reply_matches_original_day'] = True
            row['fresh_minute_source_sha256'] = fresh['sha256']
        if backup:
            frame = pd.read_csv(BASE / backup['file'])
            if len(frame) == 1:
                row['backup_daily_close'] = float(frame.iloc[0].close)
                row['backup_matches_primary_close'] = abs(row['backup_daily_close']-row['official_daily_close']) < 1e-8
                row['backup_source_sha256'] = backup['sha256']
        old_rule_applies = case.ts_code.endswith('.SH') and case.trade_date < '20180820'
        rounded_matches = abs(row['rounded_clock_minute_vwap']-row['official_daily_close']) < 1e-8
        row['old_shanghai_close_rule_applies'] = old_rule_applies
        row['compatible_with_historical_close_rule'] = bool(old_rule_applies and rounded_matches)
        row['status'] = ('RULE_COMPATIBLE_CLOCK_MINUTE_NOT_EXACT_FINAL_TRADE_WINDOW'
                         if row['compatible_with_historical_close_rule'] else 'UNEXPLAINED_SOURCE_DIFFERENCE')
        results.append(row)
    return {'cases': results, 'source_diagnostic_sha256': sha(MINUTE / 'traded_bar_price_diagnostic.csv'),
            'official_rule_sources': [RULE_BEFORE, RULE_CHANGE, RULE_EFFECTIVE],
            'limitation': '最后时钟分钟不一定等于最后一笔成交向前推一分钟；没有逐笔时戳，不能认定已严格复原官方收盘价。',
            'remaining_low_difference_stock_days': int(diagnostics.low_traded_difference.abs().gt(.005000001).sum()),
            'formal_price_differences_repaired': 0}


def ipo_diagnostic(records):
    record = next((x for x in records if x['api'] == 'stock_basic' and x.get('file')), None)
    result = {'ts_code': '603629.SH', 'trade_date': '20181224', 'ratio_written': False,
              'eligibility_filter_changed': False, 'point_in_time_factor_input_written': False}
    if record:
        stock = pd.read_csv(BASE / record['file'], dtype=str)
        if len(stock) != 1:
            raise ValueError('上市资料主键不唯一')
        listed = stock.iloc[0].list_date
        result.update(official_list_date=listed, metadata_sha256=record['sha256'])
        acceptance = json.loads((PARENT / 'evidence/early_basic_supplement_acceptance.json').read_text())
        prior = acceptance['remaining_stock_history']['603629.SH']['prior_observed_days']
        result['prior_observed_days'] = prior
        if listed == result['trade_date'] and prior == 0:
            result['status'] = 'NOT_APPLICABLE_FIRST_LISTING_DAY_NO_PRIOR_VOLUME_BASELINE'
            result['reason'] = '上市首日没有该证券此前五个有成交交易日的平均成交量；不填零，不视为可恢复的量比原件。'
        else:
            result['status'] = 'LISTING_DATE_OR_HISTORY_REQUIRES_INVESTIGATION'
    else:
        result['status'] = 'OFFICIAL_METADATA_NOT_CAPTURED'
    return result


def main():
    for folder in ('raw', 'evidence'):
        (BASE / folder).mkdir(exist_ok=True)
    token = dotenv_values(ROOT / '.env').get('TUSHARE_TOKEN')
    if not token:
        raise RuntimeError('现有行情源凭据不可用')
    client = OfficialClient(token, [], timeout=25)
    records = []
    try:
        fields = 'ts_code,trade_date,name,open,high,low,close,vol,amount'
        for code, date in (('603598.SH', '20180409'), ('600810.SH', '20180515')):
            records.append(capture(client, token, 'bak_daily', date, code, fields))
        records.append(capture(client, token, 'stock_basic', '', '603629.SH',
                               'ts_code,name,exchange,list_status,list_date,delist_date', list_status='L'))
        records.append(capture(client, token, 'stk_mins', '20180515', '600810.SH',
                               'ts_code,trade_time,open,high,low,close,vol,amount', freq='1min',
                               start_date='2018-05-15 09:00:00', end_date='2018-05-15 16:00:00'))
    finally:
        client.pool.shutdown(wait=True, cancel_futures=True)
    report = {'queries': records, 'closing_price_diagnostic': closing_diagnostics(records),
              'first_day_ratio_diagnostic': ipo_diagnostic(records),
              'captured_source_rows': sum(x['rows'] for x in records),
              'source_rows_restoring_previous_missing_fields': 0,
              'production_writes': False, 'paid_data_purchased': False,
              'completed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    (BASE / 'evidence/existing_permission_checks.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
