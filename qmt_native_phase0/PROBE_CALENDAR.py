# coding: gbk
# A_System probe 5: can QMT's built-in Python tell future trading days (2027), and how?
# Read-only: market-data / calendar queries only. Opens no file, no network, places no order.
import datetime


def _log(msg):
    print('[QN_CAL] ' + msg)


def _try(label, fn):
    try:
        r = fn()
        if isinstance(r, (list, tuple)):
            items = [str(x) for x in r]
            _log('%s -> %s items, first %s, last %s' % (label, len(items), items[:2], items[-3:]))
        elif isinstance(r, dict):
            _log('%s -> dict %s keys, sample %s' % (label, len(r), list(r.items())[:2]))
        else:
            _log('%s -> %s %r' % (label, type(r).__name__, str(r)[:200]))
    except BaseException as e:
        _log('%s -> %s: %s' % (label, type(e).__name__, str(e)[:200]))


def init(ContextInfo):
    C = ContextInfo
    g = globals()
    names = sorted(n for n in list(g) + dir(C)
                   if any(k in n.lower() for k in ('calendar', 'trading', 'holiday', 'tradeday', 'trade_day', 'tradedate')))
    _log('candidate names: %s' % names)
    for fname in ('get_trading_dates',):
        f = getattr(C, fname, None)
        if f is not None:
            _log('%s doc: %s' % (fname, (getattr(f, '__doc__', '') or '')[:300].replace('\n', ' ')))
    gtd = getattr(C, 'get_trading_dates', None)
    if gtd is not None:
        for code in ('SH', '000001.SH', '600000.SH', 'SZ'):
            _try('C.get_trading_dates(%s, 20261201, 20271231, -1, 1d)' % code,
                 lambda code=code: gtd(code, '20261201', '20271231', -1, '1d'))
        _try('C.get_trading_dates(SH, , , 30, 1d)', lambda: gtd('SH', '', '', 30, '1d'))
    for fname in ('get_trading_calendar', 'get_holidays', 'get_trade_calendar', 'get_trading_dates'):
        f = g.get(fname)
        if f is None:
            continue
        _log('global %s doc: %s' % (fname, (getattr(f, '__doc__', '') or '')[:300].replace('\n', ' ')))
        _try('global %s(SH, 20261201, 20271231)' % fname, lambda f=f: f('SH', '20261201', '20271231'))
        _try('global %s(SH)' % fname, lambda f=f: f('SH'))
    _log('today %s done' % datetime.date.today())


def handlebar(ContextInfo):
    pass
