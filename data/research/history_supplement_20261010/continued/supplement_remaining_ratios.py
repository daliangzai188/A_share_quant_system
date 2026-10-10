"""补充完整早期涨停样本的量比缺口；只使用当日及此前五个成交日。"""
from collections import deque
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
import datetime as dt
import hashlib
import json
import sys

import numpy as np
import pandas as pd
from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent
EARLY = Path('/Users/eass/A_System_复核/2026-10-09_甲临时复核_独立核验/data_recovery/early_research_supplement')
MINUTE = EARLY.parent / 'early_minute_supplement'
PRODUCTION = Path('/Users/eass/Desktop/A_System')
sys.path.insert(0, '/Users/eass/A_System_Rebuild/2026-10-08/pipeline_repair/backend_inspection')
from run_official_history_collection import OfficialClient, ROOT


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def calendar(client, token, end):
    path = BASE / 'raw/trade_calendar_20180101_' / (end + '.csv')
    receipt = BASE / 'evidence/ratio_calendar_source.json'
    if receipt.exists():
        report = json.loads(receipt.read_text())
        if sha(BASE / report['file']) != report['sha256']:
            raise ValueError('历史交易日历原件哈希不符')
        return pd.read_csv(BASE / report['file'], dtype=str), report
    frame = client.query('trade_cal', '', fields='exchange,cal_date,is_open,pretrade_date',
                         exchange='SSE', start_date='20180101', end_date=end)
    if frame.empty or frame.cal_date.astype(str).duplicated().any() or len(frame) >= 7000:
        raise ValueError('历史交易日历为空、不唯一或触及截断上限')
    dates = pd.to_datetime(frame.cal_date.astype(str), format='%Y%m%d')
    expected = pd.date_range('2018-01-01', dt.datetime.strptime(end, '%Y%m%d'))
    if set(dates) != set(expected) or not frame.exchange.eq('SSE').all():
        raise ValueError('交易日历未完整覆盖补充范围')
    path.parent.mkdir(exist_ok=True, parents=True)
    frame.to_csv(path, index=False, encoding='utf-8-sig')
    path.chmod(0o600)
    report = {'api': 'trade_cal', 'start': '20180101', 'end': end, 'rows': len(frame),
              'file': str(path.relative_to(BASE)), 'sha256': sha(path),
              'queried_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
              'production_writes': False}
    receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return frame.astype(str), report


def source_path(kind, date):
    if kind == 'daily_basic' and date == '20181224':
        return BASE.parent / 'market/verified_derived/daily_basic/20181224.csv'
    return (EARLY if date < '20190101' else PRODUCTION) / 'data/raw' / kind / (date + '.csv')


def analyze(open_dates, targets):
    histories = {}
    dependencies = []
    gaps, controls = [], []
    target_groups = {date: group.ts_code.tolist() for date, group in targets.groupby('trade_date')}
    missing_dates = []
    for date in open_dates:
        daily_path = source_path('daily', date)
        if not daily_path.exists():
            missing_dates.append(date)
            continue
        data = pd.read_csv(daily_path, usecols=['ts_code', 'trade_date', 'vol'], dtype={'ts_code': str, 'trade_date': str})
        if not data.trade_date.eq(date).all() or data.ts_code.duplicated().any():
            raise ValueError('日线日期或主键错误：' + date)
        dependencies.append({'kind': 'daily', 'date': date, 'file': str(daily_path), 'sha256': sha(daily_path)})
        volumes = data.set_index('ts_code').vol.to_dict()
        if date in target_groups:
            basic_path = source_path('daily_basic', date)
            basic = pd.read_csv(basic_path, dtype=str)
            if basic.ts_code.duplicated().any() or not basic.trade_date.eq(date).all():
                raise ValueError('每日指标日期或主键错误：' + date)
            dependencies.append({'kind': 'daily_basic', 'date': date, 'file': str(basic_path), 'sha256': sha(basic_path)})
            indexed = basic.set_index('ts_code')
            for code in target_groups[date]:
                if code not in indexed.index or code not in volumes:
                    raise ValueError('目标证券缺少每日指标或日线：' + date + '/' + code)
                row = indexed.loc[code]
                prior = list(histories.get(code, []))
                prior_volumes = [x['vol'] for x in prior]
                current = float(volumes[code])
                computed = (current / np.mean(prior_volumes)) if len(prior) == 5 and current > 0 else None
                raw = row.volume_ratio
                known = pd.notna(raw) and str(raw).strip() != ''
                provenance = str(row.get('volume_ratio_source', 'official_original'))
                original_source = provenance in ('official_original', 'tushare_daily_basic_original')
                if known and computed is not None and original_source:
                    difference = abs(computed - float(raw))
                    controls.append({'trade_date': date, 'ts_code': code, 'official_ratio': float(raw),
                                     'computed_ratio': computed, 'difference': difference,
                                     'match': bool(difference <= .0050001), 'control_source': provenance})
                if not known:
                    gap = {'trade_date': date, 'ts_code': code, 'current_volume_hands': current,
                           'prior_observed_days': len(prior), 'prior_volume_dates': [x['date'] for x in prior],
                           'prior_volume_hands': prior_volumes, 'source_daily_basic_sha256': sha(basic_path),
                           'unrounded_derived_ratio': computed}
                    gap['status'] = ('PENDING_CONTROL_VALIDATION' if computed is not None
                                     else 'INSUFFICIENT_PRIOR_5_TRADED_DAYS')
                    gaps.append(gap)
        # 今日成交量只能在当日计算结束后加入，保证历史五日均量不包含今天。
        for code, volume in volumes.items():
            if pd.notna(volume) and float(volume) > 0:
                histories.setdefault(code, deque(maxlen=5)).append({'date': date, 'vol': float(volume)})
    if missing_dates:
        raise ValueError('存在缺失交易日日线，禁止据不完整前史补算：' + ','.join(missing_dates))
    return gaps, controls, dependencies


def build(gaps, controls, dependencies):
    grouped = {}
    for record in controls:
        grouped.setdefault(record['trade_date'], []).append(record)
    # 对应日期已知官方值必须全部吻合；失败时只生成证据，不填该日字段。
    for gap in gaps:
        if gap['status'] != 'PENDING_CONTROL_VALIDATION':
            continue
        day_controls = grouped.get(gap['trade_date'], [])
        if not day_controls or not all(x['match'] for x in day_controls):
            gap['status'] = 'FORMULA_CONTROL_FAILED_OR_UNAVAILABLE'
            continue
        if len(controls) < 200:
            gap['status'] = 'INSUFFICIENT_GLOBAL_CONTROL_SAMPLES'
            continue
        value = Decimal(str(gap['unrounded_derived_ratio'])).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        gap.update(status='VERIFIED_DERIVED_PREVIOUS_5_TRADED_DAYS', derived_ratio=str(value),
                   same_date_official_controls=len(day_controls), no_future_data=True)
    candidates = []
    dates = sorted({x['trade_date'] for x in gaps if x['status'] == 'VERIFIED_DERIVED_PREVIOUS_5_TRADED_DAYS'})
    for date in dates:
        source = source_path('daily_basic', date)
        original = pd.read_csv(source, dtype=str)
        frame = original.copy(deep=True)
        if 'volume_ratio_source' not in frame:
            frame['volume_ratio_source'] = np.where(frame.volume_ratio.notna(), 'official_original', 'official_missing')
        additions = [x for x in gaps if x['trade_date'] == date and x['status'] == 'VERIFIED_DERIVED_PREVIOUS_5_TRADED_DAYS']
        codes = set()
        for gap in additions:
            mask = frame.ts_code.eq(gap['ts_code'])
            if int(mask.sum()) != 1 or frame.loc[mask, 'volume_ratio'].notna().any():
                raise ValueError('禁止覆盖已有值或写入不唯一主键')
            frame.loc[mask, 'volume_ratio'] = gap['derived_ratio']
            frame.loc[mask, 'volume_ratio_source'] = 'derived_previous_5_traded_stock_days_verified'
            codes.add(gap['ts_code'])
        path = BASE / 'verified_derived/daily_basic' / (date + '.csv')
        path.parent.mkdir(exist_ok=True, parents=True)
        frame.to_csv(path, index=False, encoding='utf-8-sig')
        path.chmod(0o600)
        readback = pd.read_csv(path, dtype=str)
        # 全部其他原字段和已存在量比按原始文本逐格核验，衍生值按主键核验。
        for col in original.columns:
            unchanged = ~original.ts_code.isin(codes) if col in ('volume_ratio', 'volume_ratio_source') else pd.Series(True, index=original.index)
            if not original.loc[unchanged, col].fillna('').equals(readback.loc[unchanged, col].fillna('')):
                raise ValueError('原字段在保存时改变：' + date + '/' + col)
        for gap in additions:
            observed = readback.set_index('ts_code').loc[gap['ts_code'], 'volume_ratio']
            if observed != gap['derived_ratio']:
                raise ValueError('衍生量比回读不符')
        candidates.append({'date': date, 'file': str(path.relative_to(BASE)), 'sha256': sha(path),
                           'original_file': str(source), 'original_sha256': sha(source), 'derived_values_written': len(additions),
                           'rows': len(frame), 'all_existing_values_preserved': True})
    return candidates


def main():
    for folder in ('raw', 'evidence'):
        (BASE / folder).mkdir(exist_ok=True)
    target_path = MINUTE / 'target_stock_days.csv'
    targets = pd.read_csv(target_path, dtype=str)
    if targets.duplicated(['ts_code', 'trade_date']).any():
        raise ValueError('目标涨停样本主键重复')
    end = targets.trade_date.max()
    token = dotenv_values(ROOT / '.env').get('TUSHARE_TOKEN')
    client = OfficialClient(token, [], timeout=25)
    try:
        dates, calendar_source = calendar(client, token, end)
    finally:
        client.pool.shutdown(wait=True, cancel_futures=True)
    open_dates = sorted(dates.loc[dates.is_open.eq('1'), 'cal_date'].tolist())
    gaps, controls, dependencies = analyze(open_dates, targets)
    candidates = build(gaps, controls, dependencies)
    gap_path, controls_path = BASE / 'evidence/remaining_ratio_gaps.json', BASE / 'evidence/remaining_ratio_controls.csv'
    gap_path.write_text(json.dumps(gaps, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    pd.DataFrame(controls).to_csv(controls_path, index=False, encoding='utf-8-sig')
    count = lambda status: sum(x['status'] == status for x in gaps)
    report = {'status': 'DERIVED_SUPPLEMENT_VALIDATED_NOT_FORMAL_RELEASE',
              'range': [targets.trade_date.min(), end], 'target_stock_days': len(targets),
              'missing_target_ratios_before': len(gaps),
              'new_derived_values_written': count('VERIFIED_DERIVED_PREVIOUS_5_TRADED_DAYS'),
              'remaining_insufficient_5_day_baseline': count('INSUFFICIENT_PRIOR_5_TRADED_DAYS'),
              'remaining_control_failures': count('FORMULA_CONTROL_FAILED_OR_UNAVAILABLE') + count('INSUFFICIENT_GLOBAL_CONTROL_SAMPLES'),
              'official_known_control_samples': len(controls), 'official_known_control_matches': sum(x['match'] for x in controls),
              'control_sha256': sha(controls_path), 'gap_details_sha256': sha(gap_path),
              'target_manifest_sha256': sha(target_path), 'calendar_source': calendar_source,
              'source_dependencies': dependencies, 'candidate_files': candidates,
              'no_existing_official_values_overwritten': True, 'no_future_data_used': True,
              'values_labelled_derived': True, 'production_writes': False, 'paid_data_purchased': False,
              'orders_or_cancellations_sent': False, 'formal_baseline_certified': False,
              'completed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    (BASE / 'evidence/remaining_ratio_supplement.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('source_dependencies', 'candidate_files')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
