# coding: utf-8
"""Import this file as a QMT Python strategy; configuration is local to Windows.

The installer generates a standalone copy with its module path bootstrap.
handlebar never submits historical signals. Only the live timer pumps commands.
"""
import os
import sys
from pathlib import Path

# The installer replaces this literal in the generated, GBK-compatible entry.
PROJECT_ROOT = r'__PROJECT_ROOT__'
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from qmt_inner.protocol import load_settings
from qmt_inner.engine import Engine

_ENGINE = None


def init(ContextInfo):
    global _ENGINE
    if _ENGINE is not None:
        raise RuntimeError('Inner execution engine already initialized')
    cfg = load_settings()
    if bool(getattr(ContextInfo, 'do_back_test', False)):
        raise RuntimeError('Bridge must not run in backtest mode')
    ContextInfo.set_account(cfg['account_id'])
    api = {name: globals()[name] for name in ('get_trade_detail_data', 'passorder', 'cancel', 'download_history_data')}
    _ENGINE = Engine(ContextInfo, api, cfg)
    ContextInfo.run_time('a_system_pump', str(cfg.get('poll_interval_ms', 100)) + 'nMilliSecond',
                         '2020-01-01 00:00:00')
    print('A_SYSTEM QMT INNER STARTED; mode=' + cfg.get('mode', 'read_only'))


def a_system_pump(ContextInfo):
    if _ENGINE is not None:
        _ENGINE.pump()


def handlebar(ContextInfo):
    # Deliberately empty: QMT replays historical bars at startup.
    pass


def order_callback(ContextInfo, orderInfo):
    if _ENGINE is not None:
        _ENGINE.callback('order', orderInfo)


def deal_callback(ContextInfo, dealInfo):
    if _ENGINE is not None:
        _ENGINE.callback('deal', dealInfo)


def orderError_callback(ContextInfo, passOrderInfo, msg):
    if _ENGINE is not None:
        _ENGINE.callback('error', {'message': str(msg), 'request': str(passOrderInfo)})


def stop(ContextInfo):
    global _ENGINE
    if _ENGINE is not None:
        _ENGINE.close()
        _ENGINE = None
