# coding: utf-8
"""QMT-native close pipeline step 1: same CLI and outputs as the Tushare collector.

Writes data/raw/{daily,daily_basic,limit_list}/<date>.csv for every open day in
[--start-date, --end-date] that is missing (the end date is always rebuilt), from QMT
data obtained through the host's callback-thread service. Fails (non-zero exit) when
the end date cannot be produced completely, so the daemon keeps its fail-closed rule:
no plans from incomplete data.
"""
import argparse
import csv
import os
import sys
import time
from pathlib import Path

import pandas as pd

from qn_host import native_service
from qn.limit_reconstruct import reconstruct
from qn.native import (QN_LIMIT_COLUMNS, apply_today_ticks_to_bars, build_daily_basic_rows, build_daily_rows,
                       build_limit_rows, limit_candidates, qmt_daily_bars, qmt_details, qmt_download_daily,
                       qmt_full_ticks, qmt_ticks, qmt_universe, streak_state_from_lists)
from src.market_rules import is_st_name, limit_up_price, price_limit_pct

PROJECT_ROOT = Path(__file__).absolute().parents[1]
RAW = PROJECT_ROOT / 'data' / 'raw'
DAILY_COLUMNS = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_chg', 'vol',
                 'amount']
BASIC_COLUMNS = ['ts_code', 'trade_date', 'turnover_rate', 'turnover_rate_f', 'volume_ratio', 'pe', 'pe_ttm', 'pb',
                 'ps', 'ps_ttm', 'dv_ratio', 'dv_ttm', 'total_share', 'float_share', 'free_share', 'total_mv',
                 'circ_mv']
BAR_CHUNK = 200
DETAIL_CHUNK = 100
MAX_DAILY_DOWNLOADS = 400  # stale local-cache names re-downloaded one by one (QMT local cache can lag)
HISTORY_DAYS = 60
TICK_RETRY_ROUNDS = 6
TICK_RETRY_WAIT = 30
ENSURE_DAYS = 15  # recent open days whose raw tables step 13 and strategy E read back


def parse_args():
    parser = argparse.ArgumentParser(description='QMT 原生数据采集（替代 Tushare 第①步）')
    parser.add_argument('--start-date')
    parser.add_argument('--end-date')
    parser.add_argument('--config', default='config/config.json')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--no-daily-basic', action='store_true')
    parser.add_argument('--no-adj-factor', action='store_true')
    parser.add_argument('--skip-daily', action='store_true')
    parser.add_argument('--skip-limit', action='store_true')
    parser.add_argument('--require-end-date-limit', action='store_true')
    return parser.parse_args()


def open_days():
    with (RAW / 'trade_calendar.csv').open(encoding='utf-8-sig') as stream:
        rows = list(csv.DictReader(stream))
    return sorted({r['cal_date'] for r in rows if str(r.get('is_open')) in ('1', '1.0', 'True')
                   and r.get('exchange', 'SSE') in ('SSE', '')})


def has_rows(path):
    if not path.exists():
        return False
    with path.open(encoding='utf-8-sig') as stream:
        stream.readline()
        return bool(stream.readline().strip())


def write_frame(rows, columns, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def load_limit_lists(days):
    lists = []
    for day in days:
        path = RAW / 'limit_list' / ('%s.csv' % day)
        if path.exists():
            with path.open(encoding='utf-8-sig') as stream:
                lists.append((day, list(csv.DictReader(stream))))
    return lists


def main():
    args = parse_args()
    calendar = open_days()
    end = args.end_date or calendar[-1]
    start = args.start_date or end
    targets = [d for d in calendar if start <= d <= end]
    if not targets:
        raise RuntimeError('no open day in %s..%s' % (start, end))
    # After a QMT restart only the persisted limit lists survive; every recent day that lacks
    # a raw table is rebuilt from QMT (ticks are kept by QMT for about a month).
    end_idx = calendar.index(end) if end in calendar else len(calendar) - 1
    recent = calendar[max(0, end_idx - ENSURE_DAYS + 1):end_idx + 1]
    todo = [d for d in sorted(set(targets) | set(recent))
            if d == end or args.overwrite or not all(has_rows(RAW / sub / ('%s.csv' % d))
                                                       for sub in ('daily', 'daily_basic', 'limit_list'))]
    print('QMT原生采集：目标交易日 %s，需生成 %s' % (','.join(targets), ','.join(todo) or '无'))
    if not todo:
        return
    first_idx = calendar.index(todo[0])
    hist_start = calendar[max(0, first_idx - HISTORY_DAYS)]
    rules = {'is_st_name': is_st_name, 'price_limit_pct': price_limit_pct, 'limit_up_price': limit_up_price}

    t0 = time.time()
    universe = native_service.call(qmt_universe)
    details = {}
    for part in chunks(universe, DETAIL_CHUNK):
        details.update(native_service.call(qmt_details, part))
    bars = {}
    for part in chunks(universe, BAR_CHUNK):
        bars.update(native_service.call(qmt_daily_bars, part, hist_start, end))
    stale = [c for c in universe if not bars.get(c) or bars[c][-1]['date'] < end]
    today = time.strftime('%Y%m%d', time.gmtime(time.time() + 8 * 3600))
    if end >= today:
        # Today's daily bar is not in QMT's local cache until later in the evening; after the close
        # the full-tick snapshot carries the final OHLCV (identical to the daily bar), applied below.
        stale_past = [c for c in stale if bars.get(c) and len(bars[c]) and bars[c][-1]['date'] < calendar[calendar.index(end) - 1]]
    else:
        stale_past = stale
    print('QMT原生采集：日线首读 %.1fs，本地缓存未到 %s 的股票 %d 只（其中历史日期缺失 %d 只，逐只补下载，上限 %d）' % (
        time.time() - t0, end, len(stale), len(stale_past), MAX_DAILY_DOWNLOADS))
    stale = stale_past
    for code in stale[:MAX_DAILY_DOWNLOADS]:
        native_service.call(qmt_download_daily, code, hist_start, end)
    for part in chunks(stale, BAR_CHUNK):
        again = native_service.call(qmt_daily_bars, part, hist_start, end)
        for code, rows in again.items():
            if rows and (not bars.get(code) or rows[-1]['date'] >= bars[code][-1]['date']):
                bars[code] = rows
    missing_end = [c for c in universe if not bars.get(c) or bars[c][-1]['date'] < end]
    patched = []
    if missing_end:
        ticks_now = {}
        for part in chunks(missing_end, BAR_CHUNK):
            ticks_now.update(native_service.call(qmt_full_ticks, part))
        patched = apply_today_ticks_to_bars(bars, ticks_now, end)
    print('QMT原生采集：股票 %d 只，日线 %.1fs，%s 无当日日线 %d 只（停牌或缓存滞后），由当日快照补齐 %d 只' % (
        len(universe), time.time() - t0, end, len(missing_end), len(patched)))

    # Strategy E reads the raw daily table of the 5 open days before the signal date; after a
    # cold start those files may be missing, so they are rebuilt from the same QMT bars.
    for day in calendar[max(0, calendar.index(targets[0]) - 6):calendar.index(targets[0])]:
        if not has_rows(RAW / 'daily' / ('%s.csv' % day)):
            write_frame(build_daily_rows(bars, day), DAILY_COLUMNS, RAW / 'daily' / ('%s.csv' % day))
            write_frame(build_daily_basic_rows(bars, details, day), BASIC_COLUMNS,
                        RAW / 'daily_basic' / ('%s.csv' % day))
            print('QMT原生采集：补齐 %s 日线（供E策略量比回看）' % day)

    previous_days = calendar[max(0, first_idx - HISTORY_DAYS):first_idx]
    streak = streak_state_from_lists(load_limit_lists(previous_days))
    for day in todo:
        daily_rows = build_daily_rows(bars, day)
        if len(daily_rows) < 4000:
            raise RuntimeError('%s 日线只有 %d 行，数据不完整，拒绝生成' % (day, len(daily_rows)))
        if day < today:
            # A past day comes only from QMT's local cache (today's snapshot cannot fill it). 10-28:
            # the cache held 3447 of ~5400 bars of 10-27; with up to 400 downloads per attempt a
            # retry would have passed the 4000-row floor and stored a table missing ~1100 stocks
            # for good (a stored past day is never rebuilt). It must match the previous days.
            prior = calendar[max(0, calendar.index(day) - 5):calendar.index(day)]
            expected = max([len(build_daily_rows(bars, d)) for d in prior] or [0])
            if len(daily_rows) < 0.98 * expected:
                raise RuntimeError('%s 日线只有 %d 行（前 5 个交易日最多 %d 行），QMT 本地缓存未补全，拒绝生成' % (
                    day, len(daily_rows), expected))
        write_frame(daily_rows, DAILY_COLUMNS, RAW / 'daily' / ('%s.csv' % day))
        write_frame(build_daily_basic_rows(bars, details, day), BASIC_COLUMNS, RAW / 'daily_basic' / ('%s.csv' % day))
        if day != end and has_rows(RAW / 'limit_list' / ('%s.csv' % day)):
            # a persisted list for a past day stays as it was produced on that day
            with (RAW / 'limit_list' / ('%s.csv' % day)).open(encoding='utf-8-sig') as stream:
                for row in csv.DictReader(stream):
                    streak[row['ts_code']] = (day, int(float(row.get('limit_times') or 1)))
            continue
        candidates = limit_candidates(bars, details, day, calendar, rules)
        ticks = {}
        pending = sorted(candidates)
        for round_no in range(TICK_RETRY_ROUNDS):
            for code in pending:
                rows = native_service.call(qmt_ticks, code, day)
                if rows:
                    ticks[code] = rows
            pending = [c for c in pending if c not in ticks]
            if not pending or day != end or os.environ.get('QN_TEST_MISSING_TICKS_ARE_UNSEALED') == '1':
                break
            print('QMT原生采集：%s 还有 %d 只涨停候选没有逐笔数据，%d秒后重试（%d/%d）' % (
                day, len(pending), TICK_RETRY_WAIT, round_no + 1, TICK_RETRY_ROUNDS))
            time.sleep(TICK_RETRY_WAIT)
        # Offline tests only: the phase-0 export has no ticks for close==limit names Tushare did not
        # count, so they are treated as not sealed. Never set inside QMT.
        allow_missing = os.environ.get('QN_TEST_MISSING_TICKS_ARE_UNSEALED') == '1'
        if pending and day == end and not allow_missing:
            raise RuntimeError('%s 有 %d 只涨停候选缺少逐笔数据，无法判断收盘封板，拒绝生成涨停池: %s'
                               % (day, len(pending), ','.join(pending[:10])))
        prev_market_day = calendar[calendar.index(day) - 1]
        limit_rows, stats = build_limit_rows(candidates, ticks, bars, streak, day, prev_market_day, reconstruct)
        write_frame(limit_rows, QN_LIMIT_COLUMNS, RAW / 'limit_list' / ('%s.csv' % day))
        for row in limit_rows:
            streak[row['ts_code']] = (day, int(row['limit_times']))
        print('QMT原生采集：%s 日线 %d 行，涨停候选 %d，收盘封住 %d，未封住 %d，无逐笔 %d' % (
            day, len(daily_rows), stats['candidates'], stats['written'], len(stats['unsealed']),
            len(stats['no_ticks'])))
    if args.require_end_date_limit and not (RAW / 'limit_list' / ('%s.csv' % end)).exists():
        raise RuntimeError('%s 涨停池未生成' % end)
    print('QMT原生采集完成，用时 %.1fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
