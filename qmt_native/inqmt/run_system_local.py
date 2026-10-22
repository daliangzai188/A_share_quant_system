# coding: utf-8
"""Boot the whole bundled system (host + engine + daemon thread) against the mock QMT,
in real time, and report what the daemon does. Python 3.6 compatible.

Usage: python run_system_local.py CORE_FILE SECONDS
"""
import base64
import os
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
REPO = '/Users/eass/Desktop/A_System'
sys.path.insert(0, HERE)
from mock_qmt import MockMarket, mock_globals  # noqa: E402


class MockContext(MockMarket):
    """Adds the trading-side ContextInfo calls the engine uses."""

    def __init__(self):
        MockMarket.__init__(self)
        self.subs = {}

    def quote(self, code):
        g = self.bars.get(code)
        if g is None or not len(g):
            return None
        last = g.iloc[-1]
        return {'lastPrice': float(last['close']), 'lastClose': float(last['preClose']),
                'open': float(last['open']), 'high': float(last['high']), 'low': float(last['low']),
                'volume': float(last['volume']), 'amount': float(last['amount']),
                'timetag': '%s 15:00:00' % last['date'], 'openInt': 0}

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
            out['InstrumentID'] = num
            out['ExchangeID'] = exch
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


def pack(data):
    return base64.b64encode(zlib.compress(data)).decode('ascii')


def seed_files():
    seed = {}

    def add(rel, path):
        seed[rel] = pack(open(path, 'rb').read())
    add('data/raw/trade_calendar.csv', os.path.join(BASE, 'vm_raw/data/raw/trade_calendar.csv'))
    for sub in ('limit_list', 'daily', 'daily_basic'):
        src = os.path.join(BASE, 'vm_raw/data/raw', sub)
        keep = 60 if sub == 'limit_list' else 8
        for f in sorted(os.listdir(src))[-keep:]:
            add('data/raw/%s/%s' % (sub, f), os.path.join(src, f))
    for f in ('fill_rate_table.csv', 'fill_rate_fallback.csv'):
        add('data/processed/' + f, os.path.join(REPO, 'data/processed', f))
    add('data/processed/stock_theme_reference.csv', os.path.join(BASE, 'ref15/data/processed/stock_theme_reference.csv'))
    add('data/state/equity_curve_stop_decision.json', os.path.join(REPO, 'data/state/equity_curve_stop_decision.json'))
    add('reports/equity_curve_stop/shadow_nav_latest.csv', os.path.join(REPO, 'reports/equity_curve_stop/shadow_nav_latest.csv'))
    for f in ('e_signal_runs_recent.json', 'e_signals_recent.json'):
        p = os.path.join(REPO, 'reports/strategy_e', f)
        if os.path.exists(p):
            add('reports/strategy_e/' + f, p)
    return seed


def main():
    core, seconds = sys.argv[1], float(sys.argv[2])
    ns = {'__name__': 'qn_core'}
    for name in ('qn_runtime.py', core, 'qn_paper_broker.py', 'qn_host.py'):
        exec(compile(open(os.path.join(HERE, name), encoding='utf-8').read(), name, 'exec'), ns)
    C = MockContext()
    g = mock_globals()
    kv = {}
    g['get_py_key'] = lambda key: (kv.get(key, ''), 0, '')
    g['save_py_key'] = lambda key, value: kv.__setitem__(key, value)
    account = '8880000001'
    broker = ns['QnPaperBroker'](account, 1000000.0, C.quote)
    store = ns['QnStateStore'](g['get_py_key'], g['save_py_key'])
    deploy = {'release': 'local-test', 'account_id': account, 'mode': 'shadow', 'seed': seed_files()}
    ns['QN_ENVIRON']['QN_TEST_MISSING_TICKS_ARE_UNSEALED'] = '1'
    host = ns['QnHost'](C, g, deploy, ns['QN_MODULES'], ns['QN_STATIC_FILES'], market=C, broker=broker, store=store)
    t0 = time.time()
    host.boot()
    print('boot %.1fs' % (time.time() - t0))
    last_report = 0
    while time.time() - t0 < seconds:
        host.pump()
        time.sleep(host.PUMP_MS / 1000.0)
        if time.time() - last_report > 15:
            last_report = time.time()
            print('--- t=%.0fs daemon_alive=%s log_lines=%d persist=%s err=%s' % (
                time.time() - t0, host.daemon_thread.is_alive(), len(ns['QN_LOG_LINES']),
                (host.persist_meta or {}).get('bytes'), host.persist_error))
    out = ns['QN_LOG_LINES'][-160:] + ['--- host messages'] + host.messages[-20:] + ['--- broker'] + broker.log[-20:]
    sys.stdout.buffer.write(('\n'.join(out) + '\n').encode('utf-8'))
    sys.stdout.flush()
    host.stop()


if __name__ == '__main__':
    main()
