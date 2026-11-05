# coding: gbk
# A_System probe 6: the QMT market-data calls the trading engine and the native collector make,
# with their exact arguments, checked for the fields our code reads (QMT 2.1.33, 11-05).
# Read-only: market data only. Opens no file, no network, places no order. Run with the editor's 运行.
import datetime
import time

CODES = ['600000.SH', '000001.SZ', '300750.SZ', '688981.SH']
TICK_FIELDS = ('lastPrice', 'lastClose', 'open', 'high', 'low', 'volume', 'amount', 'bidPrice', 'askPrice',
               'bidVol', 'askVol')
DETAIL_FIELDS = ('InstrumentID', 'ExchangeID', 'InstrumentName', 'OpenDate', 'FloatVolume', 'TotalVolume',
                 'UpStopPrice', 'DownStopPrice', 'PreClose')
RESULTS = []


def _log(msg):
    print('[QN_INTRA] ' + msg)


def check(label, ok, detail):
    RESULTS.append(bool(ok))
    _log('%s %s: %s' % ('OK  ' if ok else 'FAIL', label, str(detail)[:300]))


def plain(row):
    if isinstance(row, dict):
        return dict(row)
    return dict((n, getattr(row, n)) for n in dir(row) if not n.startswith('_'))


def init(ContextInfo):
    C = ContextInfo
    g = globals()
    today = datetime.datetime.now().strftime('%Y%m%d')
    # engine.quotes
    try:
        raw = C.get_full_tick(CODES)
        rows = dict((c, plain(raw[c])) for c in CODES if raw and raw.get(c))
        missing = dict((c, [f for f in TICK_FIELDS if f not in r]) for c, r in rows.items())
        stamp = dict((c, r.get('timetag') or r.get('time')) for c, r in rows.items())
        check('get_full_tick', isinstance(raw, dict) and len(rows) == len(CODES) and not any(missing.values()),
              'codes %d, missing fields %s, stamps %s, 600000 last %s bid1 %s' % (
                  len(rows), missing, stamp, rows.get('600000.SH', {}).get('lastPrice'),
                  (rows.get('600000.SH', {}).get('bidPrice') or [None])[0]))
    except BaseException as e:
        check('get_full_tick', False, '%s: %s' % (type(e).__name__, e))
    # engine price bands / native details
    for code in CODES:
        try:
            d = C.get_instrument_detail(code)
            miss = [f for f in DETAIL_FIELDS if f not in (d or {})]
            up, down, pre = (float((d or {}).get(k, 0) or 0) for k in ('UpStopPrice', 'DownStopPrice', 'PreClose'))
            ident = str(d.get('InstrumentID')) == code.split('.')[0] and str(d.get('ExchangeID')).upper() == code.split('.')[1]
            check('get_instrument_detail %s' % code, isinstance(d, dict) and not miss and ident and 0 < down < pre < up,
                  'missing %s, id ok %s, bands %s/%s pre %s' % (miss, ident, down, up, pre))
        except BaseException as e:
            check('get_instrument_detail %s' % code, False, '%s: %s' % (type(e).__name__, e))
    # engine.minutes
    seqs = {}
    try:
        for code in CODES[:2]:
            seqs[code] = C.subscribe_quote(code, period='1m', dividend_type='none')
        check('subscribe_quote 1m', all(s is not None and int(s) >= 0 for s in seqs.values()), seqs)
    except BaseException as e:
        check('subscribe_quote 1m', False, '%s: %s' % (type(e).__name__, e))
    try:
        for code in CODES[:2]:
            g['download_history_data'](code, '1m', today + '093000', today + '150000')
        raw = C.get_market_data_ex(['open', 'high', 'low', 'close', 'volume', 'amount'], CODES[:2], period='1m',
                                   start_time=today + '093000', end_time=today + '150000', count=-1,
                                   dividend_type='none', fill_data=True, subscribe=False)
        frames = dict((c, raw.get(c)) for c in CODES[:2])
        sizes = dict((c, None if f is None else len(f)) for c, f in frames.items())
        f0 = frames[CODES[0]]
        first = None if f0 is None or not len(f0) else (str(f0.index[0]), dict(f0.iloc[0]))
        check('get_market_data_ex 1m', isinstance(raw, dict) and all(s and s >= 230 for s in sizes.values())
              and hasattr(f0, 'iterrows'), 'rows %s, columns %s, first %s' % (sizes, list(f0.columns) if f0 is not None else None, first))
    except BaseException as e:
        check('get_market_data_ex 1m', False, '%s: %s' % (type(e).__name__, e))
    for code, seq in seqs.items():
        try:
            C.unsubscribe_quote(seq)
        except BaseException as e:
            _log('unsubscribe %s: %s' % (code, e))
    # native collector: universe, daily bars, ticks
    try:
        codes = set()
        for sector in (u'沪深A股', u'京市A股'):
            codes.update(C.get_stock_list_in_sector(sector) or [])
        check('get_stock_list_in_sector', len(codes) >= 4000, '%d codes, BJ %d' % (len(codes), sum(1 for c in codes if c.endswith('.BJ'))))
    except BaseException as e:
        check('get_stock_list_in_sector', False, '%s: %s' % (type(e).__name__, e))
    try:
        raw = C.get_market_data_ex(['open', 'high', 'low', 'close', 'preClose', 'volume', 'amount'], CODES, period='1d',
                                   start_time='20261020', end_time=today, count=-1, dividend_type='none',
                                   fill_data=False, subscribe=False)
        sizes = dict((c, len(raw.get(c)) if raw.get(c) is not None else None) for c in CODES)
        f0 = raw.get(CODES[0])
        check('get_market_data_ex 1d', all(s and s >= 10 for s in sizes.values()),
              'rows %s, last index %s, columns %s' % (sizes, str(f0.index[-1]) if f0 is not None and len(f0) else None,
                                                       list(f0.columns) if f0 is not None else None))
    except BaseException as e:
        check('get_market_data_ex 1d', False, '%s: %s' % (type(e).__name__, e))
    try:
        code = CODES[0]
        g['download_history_data'](code, 'tick', today + '091500', today + '150100')
        raw = C.get_market_data_ex([], [code], period='tick', start_time=today + '091500', end_time=today + '150100',
                                   count=-1, dividend_type='none', fill_data=False, subscribe=False)
        f = raw.get(code)
        cols = list(f.columns) if f is not None else []
        need = [c for c in ('lastPrice', 'lastClose', 'bidPrice', 'bidVol', 'askPrice', 'askVol') if c not in cols]
        level1 = None
        if f is not None and len(f):
            cell = f['bidPrice'].values[-1] if 'bidPrice' in cols else None
            level1 = cell[0] if cell is not None and hasattr(cell, '__getitem__') else cell
        check('get_market_data_ex tick', f is not None and len(f) > 1000 and not need and level1 is not None,
              'rows %s, missing %s, first %s, last %s, bid1 %s' % (None if f is None else len(f), need,
                                                                  str(f.index[0]) if f is not None and len(f) else None,
                                                                  str(f.index[-1]) if f is not None and len(f) else None, level1))
    except BaseException as e:
        check('get_market_data_ex tick', False, '%s: %s' % (type(e).__name__, e))
    try:
        days = C.get_trading_dates('SH', '20261020', today, -1, '1d') or []
        check('get_trading_dates', len(days) >= 10 and str(days[-1])[:8] == today, 'n %d last %s' % (len(days), days[-1:] ))
    except BaseException as e:
        check('get_trading_dates', False, '%s: %s' % (type(e).__name__, e))
    _log('SUMMARY %d of %d checks OK' % (sum(RESULTS), len(RESULTS)))


def handlebar(ContextInfo):
    pass
