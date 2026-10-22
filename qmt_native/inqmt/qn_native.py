# coding: utf-8
# QMT-native replacement for close-pipeline step 1 (Tushare collection).
# Produces data/raw/{daily,daily_basic,limit_list}/<date>.csv in the exact Tushare
# layout the cleaner reads, from QMT data only. Python 3.6 / pandas 0.22 compatible.
#
# Rules validated against Tushare on 2026-09-21..10-19 (815/815 limit-up members,
# 15/15 identical strategy selections):
#   daily        QMT 1d bars, dividend_type none; amount CNY -> thousand CNY; vol in lots;
#                zero-volume (suspended) rows dropped like Tushare.
#   daily_basic  turnover = vol*100/FloatVolume*100; volume_ratio = vol / mean(prev 5 vols);
#                shares in 10k, market values in 10k CNY; free_share/turnover_rate_f empty.
#   limit_list   close at the exchange limit price (src/market_rules.py), non-ST, seal still
#                standing after the close; fields rebuilt from 3-second snapshots with rule
#                bid_raw_merge_minute; limit_times continues across suspension days.
import csv as _csv_mod
import io as _io_mod
import math as _math

import numpy as _np


def _npr(value, digits):
    """pandas/numpy rounding (what the validated native builders used), not Python's round()."""
    return float(_np.round(float(value), digits))

QN_NATIVE_SECTORS = [u'沪深A股', u'京市A股']
QN_DAILY_FIELDS = ['open', 'high', 'low', 'close', 'preClose', 'volume', 'amount']
QN_LIMIT_RULE = 'bid_raw_merge_minute'
QN_LIMIT_COLUMNS = ['trade_date', 'ts_code', 'name', 'close', 'pct_chg', 'fd_amount', 'first_time', 'last_time',
                    'open_times', 'limit', 'limit_amount', 'limit_times', 'limit_data_source',
                    'limit_data_quality', 'strategy_compatible']


class QnNativeError(RuntimeError):
    pass


# ---------------------------------------------------------------- QMT calls (callback thread)
def qmt_universe(C, g):
    codes = set()
    for sector in QN_NATIVE_SECTORS:
        codes.update(C.get_stock_list_in_sector(sector) or [])
    if len(codes) < 4000:
        raise QnNativeError('stock universe incomplete: %d codes' % len(codes))
    return sorted(codes)


def _frame_rows(frame):
    out = []
    if frame is None or len(frame) == 0:
        return out
    cols = list(frame.columns)
    for index, row in zip(frame.index, frame.values.tolist()):
        rec = dict(zip(cols, row))
        rec['date'] = str(index)[:8]
        out.append(rec)
    return out


def qmt_daily_bars(C, g, codes, start, end, download_missing=False):
    """{code: [bar dicts]} for [start, end] from QMT's local daily cache (one fast call)."""
    data = C.get_market_data_ex(QN_DAILY_FIELDS, list(codes), period='1d', start_time=start, end_time=end,
                                count=-1, dividend_type='none', fill_data=False, subscribe=False) or {}
    return dict((code, _frame_rows(data.get(code))) for code in codes)


def qmt_download_daily(C, g, code, start, end):
    """One code per call: QMT's download can block for a second or more, so the caller issues
    one job per code and the callback thread keeps serving the engine between them."""
    try:
        g['download_history_data'](code, '1d', start, end)
        return True
    except Exception:
        return False


def qmt_full_ticks(C, g, codes):
    raw = C.get_full_tick(list(codes)) or {}
    out = {}
    for code in codes:
        tick = raw.get(code)
        if tick:
            out[code] = dict(tick)
    return out


def qmt_details(C, g, codes, fields=('InstrumentName', 'OpenDate', 'FloatVolume', 'TotalVolume',
                                       'UpStopPrice', 'DownStopPrice', 'PreClose')):
    out = {}
    for code in codes:
        try:
            detail = C.get_instrument_detail(code) or {}
        except Exception:
            detail = {}
        out[code] = dict((f, detail.get(f)) for f in fields)
    return out


def qmt_ticks(C, g, code, day):
    start, end = day + '091500', day + '150100'
    try:
        g['download_history_data'](code, 'tick', start, end)
    except Exception:
        pass
    data = C.get_market_data_ex([], [code], period='tick', start_time=start, end_time=end, count=-1,
                                dividend_type='none', fill_data=False, subscribe=False) or {}
    frame = data.get(code)
    if frame is None or len(frame) == 0:
        return []
    cols = set(frame.columns)
    stamps = [str(x) for x in frame.index]
    rows = []
    get = lambda name: frame[name].values if name in cols else None
    last, last_close = get('lastPrice'), get('lastClose')
    levels = dict((name, get(name)) for name in ('bidPrice', 'bidVol', 'askPrice', 'askVol'))
    for i, stamp in enumerate(stamps):
        row = {'ts': stamp,
               'lastPrice': None if last is None else last[i],
               'lastClose': None if last_close is None else last_close[i]}
        for name, arr in levels.items():
            cell = None if arr is None else arr[i]
            try:
                row[name + '1'] = cell[0]
            except Exception:
                row[name + '1'] = None
        rows.append(row)
    return rows


def qmt_trading_dates(C, g, start, end):
    getter = getattr(C, 'get_trading_dates', None)
    if getter is None:
        raise QnNativeError('get_trading_dates unavailable')
    days = getter('SH', start, end, -1, '1d') or []
    return [str(d)[:8] for d in days]


# ---------------------------------------------------------------- pure producers
def _fmt_round(value, digits):
    if value is None or (isinstance(value, float) and value != value):
        return float('nan')
    return round(float(value), digits)


def build_daily_rows(bars_by_code, day):
    rows = []
    for code in sorted(bars_by_code):
        for b in bars_by_code[code]:
            if b['date'] != day:
                continue
            vol = float(b['volume'] or 0)
            if not vol > 0:
                continue
            o, h, l, c, pc = (float(b[k]) for k in ('open', 'high', 'low', 'close', 'preClose'))
            rows.append({'ts_code': code, 'trade_date': day, 'open': o, 'high': h, 'low': l, 'close': c,
                         'pre_close': pc, 'change': _npr(c - pc, 4), 'pct_chg': _npr((c / pc - 1) * 100, 4),
                         'vol': vol, 'amount': _npr(float(b['amount']) / 1000.0, 4)})
    return rows


def build_daily_basic_rows(bars_by_code, details, day):
    rows = []
    for code in sorted(bars_by_code):
        bars = [b for b in bars_by_code[code] if float(b['volume'] or 0) > 0]
        idx = [i for i, b in enumerate(bars) if b['date'] == day]
        if not idx:
            continue
        i = idx[0]
        b = bars[i]
        vol = float(b['volume'])
        prev = [float(x['volume']) for x in bars[max(0, i - 5):i]]
        vr = _npr(vol / (sum(prev) / 5.0), 2) if len(prev) == 5 and sum(prev) > 0 else float('nan')
        det = details.get(code) or {}
        fv = float(det.get('FloatVolume') or 0)
        tv = float(det.get('TotalVolume') or 0)
        fs = fv / 1e4 if fv else float('nan')
        ts = tv / 1e4 if tv else float('nan')
        close = float(b['close'])
        nan = float('nan')
        rows.append({'ts_code': code, 'trade_date': day,
                     'turnover_rate': _npr(vol * 100 / fv * 100, 4) if fv else nan, 'turnover_rate_f': nan,
                     'volume_ratio': vr, 'pe': nan, 'pe_ttm': nan, 'pb': nan, 'ps': nan, 'ps_ttm': nan,
                     'dv_ratio': nan, 'dv_ttm': nan, 'total_share': _npr(ts, 4) if ts == ts else nan,
                     'float_share': _npr(fs, 4) if fs == fs else nan, 'free_share': nan,
                     'total_mv': _npr(close * ts, 4) if ts == ts else nan,
                     'circ_mv': _npr(close * fs, 4) if fs == fs else nan})
    return rows


def limit_candidates(bars_by_code, details, day, trade_days, rules):
    """Daily-bar membership: closed at the exchange limit price, non-ST. Returns {code: (bar, limit, name)}."""
    idx = dict((d, i) for i, d in enumerate(trade_days))
    out = {}
    for code, bars in bars_by_code.items():
        bar = [b for b in bars if b['date'] == day and float(b['volume'] or 0) > 0]
        if not bar:
            continue
        bar = bar[0]
        det = details.get(code) or {}
        name = str(det.get('InstrumentName') or '')
        if rules['is_st_name'](name):
            continue
        od = str(det.get('OpenDate') or '')[:8]
        n = idx[day] - idx[od] + 1 if od in idx and day in idx else None
        pct = rules['price_limit_pct'](code, name=name, trade_date=day, listing_day_number=n)
        lim = rules['limit_up_price'](float(bar['preClose']), pct) if pct is not None else None
        if lim is not None and int(round(float(bar['close']) * 100)) == int(round(lim * 100)):
            out[code] = (bar, lim, name)
    return out


def streak_limit_times(last, code_bars, day, prev_market_day):
    """Tushare limit_times continues across the stock's own suspension days."""
    if not last:
        return 1
    last_day, times = last
    if last_day == prev_market_day:
        return times + 1
    if last_day < prev_market_day:
        traded_between = [b for b in code_bars if last_day < b['date'] < day and float(b['volume'] or 0) > 0]
        if not traded_between:
            return times + 1
    return 1


def build_limit_rows(candidates, ticks_by_code, bars_by_code, streak_state, day, prev_market_day, reconstruct):
    """streak_state: {code: (last_limit_up_date, limit_times)} from earlier limit lists."""
    rows = []
    stats = {'candidates': len(candidates), 'no_ticks': [], 'unsealed': [], 'written': 0}
    for code in sorted(candidates):
        bar, lim, name = candidates[code]
        ticks = ticks_by_code.get(code) or []
        if not ticks:
            stats['no_ticks'].append(code)
            continue
        g = reconstruct(ticks, lim, QN_LIMIT_RULE)
        if not g['closed_sealed']:
            stats['unsealed'].append(code)
            continue
        times = streak_limit_times(streak_state.get(code), bars_by_code.get(code) or [], day, prev_market_day)
        close, pre = float(bar['close']), float(bar['preClose'])
        rows.append({'trade_date': day, 'ts_code': code, 'name': name, 'close': close,
                     'pct_chg': round((close / pre - 1) * 100, 4), 'fd_amount': g['fd_amount'],
                     'first_time': g['first_time'], 'last_time': g['last_time'], 'open_times': g['open_times'],
                     'limit': 'U', 'limit_amount': '', 'limit_times': times, 'limit_data_source': 'qmt_native',
                     'limit_data_quality': 'full', 'strategy_compatible': True})
    stats['written'] = len(rows)
    return rows, stats


def streak_state_from_lists(lists):
    """lists: iterable of (date, rows) in date order; rows carry ts_code and limit_times."""
    state = {}
    for day, rows in lists:
        for r in rows:
            if str(r.get('limit', 'U')) != 'U':
                continue
            try:
                times = int(float(r.get('limit_times') or 1))
            except ValueError:
                times = 1
            state[str(r['ts_code'])] = (day, times)
    return state


def apply_today_ticks_to_bars(bars_by_code, full_ticks, day):
    """A stock missing today's bar after a forced download but with a traded full tick gets
    today's bar from that tick (the cache, not the market, was stale)."""
    patched = []
    for code, tick in full_ticks.items():
        bars = bars_by_code.setdefault(code, [])
        if bars and bars[-1]['date'] >= day:
            continue
        stamp = str(tick.get('timetag') or tick.get('time') or '')
        stamp_day = stamp.replace('-', '')[:8]
        vol = float(tick.get('volume') or 0)
        if stamp_day != day or not vol > 0:
            continue
        bars.append({'date': day, 'open': float(tick.get('open')), 'high': float(tick.get('high')),
                     'low': float(tick.get('low')), 'close': float(tick.get('lastPrice')),
                     'preClose': float(tick.get('lastClose')), 'volume': vol,
                     'amount': float(tick.get('amount') or 0)})
        patched.append(code)
    return patched
