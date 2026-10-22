# coding: utf-8
"""Offline drill: run the packaged strategy file under an accelerated virtual clock against
the mock QMT (date-aware quotes + synthetic minute bars) and the paper broker.

Usage: python run_vday.py STRATEGY_FILE START_BJ(YYYYmmddHHMM) END_BJ SPEED OUT_DIR
"""
import calendar
import datetime as dt
import logging
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mock_qmt import MockMarket  # noqa: E402
import qn_vclock  # noqa: E402

BJ = dt.timezone(dt.timedelta(hours=8))


def bj_epoch(text):
    return dt.datetime.strptime(text, '%Y%m%d%H%M').replace(tzinfo=BJ).timestamp()


class VMarket(MockMarket):
    def __init__(self, clock):
        MockMarket.__init__(self)
        self.clock = clock
        self.subs = {}

    def vnow(self):
        return dt.datetime.fromtimestamp(self.clock.now(), BJ)

    def bar(self, code, day):
        g = self.bars.get(code)
        if g is None:
            return None
        row = g[g['date'] == day]
        return None if row.empty else row.iloc[0]

    def quote(self, code):
        now = self.vnow()
        day = now.strftime('%Y%m%d')
        b = self.bar(code, day)
        if b is None:
            return None
        t = now.strftime('%H%M%S')
        if t < '092500':
            last, vol = float(b['preClose']), 0.0
        elif t < '093000':
            last, vol = float(b['open']), float(b['volume']) * 0.05
        else:
            frac = min(1.0, max(0.0, (now.hour * 60 + now.minute - 570 - (90 if now.hour >= 13 else 0)) / 240.0))
            last = float(b['open']) + (float(b['close']) - float(b['open'])) * frac
            vol = float(b['volume']) * max(frac, 0.05)
        pre = float(b['preClose'])
        pct = 0.3 if code.endswith('.BJ') else 0.2 if code[:2] in ('30', '68') else 0.1
        return {'lastPrice': round(last, 2), 'lastClose': pre, 'open': float(b['open']), 'high': max(float(b['high']), last),
                'low': min(float(b['low']), last), 'volume': vol, 'amount': vol * last * 100,
                'timetag': now.strftime('%Y%m%d %H:%M:%S'), 'openInt': 0,
                'upperLimit': round(pre * (1 + pct) + 1e-9, 2), 'lowerLimit': round(pre * (1 - pct) + 1e-9, 2),
                'bidPrice': [round(last - 0.01, 2)] + [0] * 4, 'askPrice': [round(last, 2)] + [0] * 4,
                'bidVol': [1000] + [0] * 4, 'askVol': [1000] + [0] * 4}

    def get_full_tick(self, codes):
        out = {}
        for code in codes:
            q = self.quote(code)
            if q:
                out[code] = q
        return out

    def get_instrument_detail(self, code):
        out = MockMarket.get_instrument_detail(self, code)
        if out:
            num, exch = code.split('.')
            out['InstrumentID'], out['ExchangeID'] = num, exch
            q = self.quote(code)
            if q:
                out.update(UpStopPrice=q['upperLimit'], DownStopPrice=q['lowerLimit'], PreClose=q['lastClose'])
        return out

    def get_market_data_ex(self, fields, codes, period='1d', start_time='', end_time='', count=-1,
                           dividend_type='none', fill_data=False, subscribe=False):
        if period != '1m':
            vday = self.vnow().strftime('%Y%m%d')
            data = MockMarket.get_market_data_ex(self, fields, codes, period, start_time, min(end_time or vday, vday + '235959'),
                                                 count, dividend_type, fill_data, subscribe)
            return data
        now = self.vnow()
        day = now.strftime('%Y%m%d')
        out = {}
        for code in codes:
            b = self.bar(code, day)
            if b is None:
                continue
            idx, rows = [], []
            for m in list(range(571, 691)) + list(range(781, 901)):
                stamp = dt.datetime.strptime(day, '%Y%m%d').replace(hour=m // 60, minute=m % 60, tzinfo=BJ)
                if stamp > now:
                    break
                frac = (m - 570 - (90 if m > 700 else 0)) / 240.0
                px = float(b['open']) + (float(b['close']) - float(b['open'])) * frac
                rows.append({'open': px, 'high': px, 'low': px, 'close': px, 'volume': float(b['volume']) / 240,
                             'amount': float(b['amount']) / 240})
                idx.append(stamp.strftime('%Y%m%d%H%M%S'))
            out[code] = pd.DataFrame(rows, index=idx, columns=['open', 'high', 'low', 'close', 'volume', 'amount'])
        return out

    def subscribe_quote(self, code, period='1m', dividend_type='none'):
        self.subs[code] = len(self.subs) + 1
        return self.subs[code]

    def unsubscribe_quote(self, seq):
        return True

    def set_account(self, account):
        self.account = account

    def run_time(self, *args):
        return None


def main():
    path, start, end, speed, out = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), sys.argv[5]
    clock = qn_vclock.QnVirtualClock(bj_epoch(start), speed)
    kv = {}
    C = VMarket(clock)
    ns = {'__name__': '__main__', 'download_history_data': lambda *a: None,
          'get_py_key': lambda k: (kv.get(k, ''), 0, ''),
          'save_py_key': lambda k, v: (kv.__setitem__(k, v) or (0, '')) if len(v) <= 5000 else (-1, 'too long')}
    exec(compile(open(path, encoding='gbk').read(), path, 'exec'), ns)
    ns['QN_ENVIRON']['QN_TEST_MISSING_TICKS_ARE_UNSEALED'] = '1'
    ns['QN_DEPLOY']['debug_export_dir'] = out
    overrides = ns['QN_DEPLOY'].setdefault('config_overrides', {})
    overrides['equity_curve_stop.enabled'] = False  # drill: exercise entries regardless of the stop
    qn_vclock.install_virtual_clock(ns['QnLoader'], clock)
    old_factory = logging.getLogRecordFactory()

    def factory(*a, **k):
        rec = old_factory(*a, **k)
        rec.created = clock.now()
        return rec
    logging.setLogRecordFactory(factory)
    real_paper = ns['QnPaperBroker']

    class VPaper(real_paper):
        def __init__(self, *a, **k):
            k['now_fn'] = lambda: dt.datetime.fromtimestamp(clock.now(), BJ)
            real_paper.__init__(self, *a, **k)
    ns['QnPaperBroker'] = VPaper
    # Accelerated time only: run QMT data jobs on their own thread so a slow mock call does not
    # freeze the engine heartbeat (1 real second = 1 virtual minute here).
    svc = ns['QN_NATIVE_SERVICE']
    real_pump = svc.pump
    svc.pump = lambda budget_seconds=0.08: None
    import threading

    def data_jobs():
        while True:
            real_pump(0.5)
            time.sleep(0.005)
    ns['init'](C)
    svc.context = C
    threading.Thread(target=data_jobs, daemon=True).start()
    end_epoch = bj_epoch(end)
    last = 0
    fast = speed
    while clock.now() < end_epoch:
        # real time while any pipeline script runs (their production timeouts are wall-clock budgets)
        busy = any(p.poll() is None for p in list(ns['QN_PROCESSES'].values()))
        clock.set_speed(1.0 if busy else fast)
        ns['a_system_pump'](C)
        time.sleep(0.02)
        if time.time() - last > 20:
            last = time.time()
            host = ns['QN_HOST'][0]
            print('virtual', dt.datetime.fromtimestamp(clock.now(), BJ).strftime('%m-%d %H:%M'),
                  'daemon_alive', host.daemon_thread.is_alive(), 'log', len(ns['QN_LOG_LINES']), flush=True)
    host = ns['QN_HOST'][0]
    host._debug_export()
    print('broker', host.broker.snapshot()['cash'], host.broker.positions)
    ns['stop'](C)


if __name__ == '__main__':
    main()
