"""保持现有交易/分仓/恢复接口，券商调用实际由 QMT 内置 Python 执行。"""
from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any

from qmt_inner.protocol import FileClient, load_settings
from src.broker_adapter import BrokerConnectionConfig, OrderRequest, OrderResult
from src.qmt_adapter import QMTBrokerAdapter, qmt_bar_hhmm, to_float


class _InnerQueries:
    """仅提供现有归一化代码需要的查询方法；不包含 xtquant。"""
    def __init__(self, client):
        self.client = client

    def query_stock_asset(self, account):
        return self.client.call('account', {})

    def query_stock_positions(self, account):
        return self.client.call('positions', {})

    def query_stock_orders(self, account):
        return self.client.call('orders', {})

    def query_stock_trades(self, account):
        return self.client.call('trades', {})

    def get_full_tick(self, codes):
        return self.client.call('quotes', {'ts_codes': codes})


class QMTInnerBrokerAdapter(QMTBrokerAdapter):
    def __init__(self, config: BrokerConnectionConfig, settings: dict[str, Any]):
        super().__init__(config)
        self.settings = settings
        self.client = FileClient(settings)
        self._active_qmt_path = ''  # 内置模式不污染 miniQMT path/session 成功缓存。
        self._active_session_id = 0

    @classmethod
    def from_config(cls, broker_config):
        settings = load_settings(broker_config.get('inner_config_path') or None)
        expected = os.getenv(str(broker_config.get('account_id_env', 'QMT_ACCOUNT_ID')), '').strip()
        if expected and expected != settings['account_id']:
            raise RuntimeError('原项目与内置配置的账户不同，禁止切换')
        config = BrokerConnectionConfig(
            broker_name='guojin_qmt_inner', account_id=settings['account_id'],
            account_type='STOCK', qmt_path=settings['spool_dir'], session_id=0,
        )
        return cls(config, settings)

    def _load_xtquant(self):
        if self.trader is None:
            raise RuntimeError('QMT 内置执行端尚未连接')

    def connect(self, preferred_only=False):
        self.server_info = self.client.connect()
        queries = _InnerQueries(self.client)
        self.trader = queries
        self.account = self.config.account_id
        self.xtdata_module = queries

    def assert_execution_ready(self):
        from qmt_inner.protocol import read_json
        runtime = read_json(self.settings['runtime_config'])
        if runtime.get('trade_mode') == 'live' and self.server_info.get('mode') != 'live':
            raise RuntimeError('QMT 内置端为只读模式，不能启动实盘执行；请先完成迁移验收')

    def disconnect(self):
        self.trader = None
        self.account = None
        self.xtdata_module = None
        self.client.instance = None

    def place_order(self, request: OrderRequest) -> OrderResult:
        if self.trader is None:
            raise RuntimeError('QMT 内置执行端尚未连接')
        response = self.client.call('submit', asdict(request))
        return OrderResult(
            ts_code=request.ts_code, broker_code=request.broker_code,
            side=request.side, quantity=request.quantity,
            accepted=response['accepted'], order_id=response.get('order_id', ''),
            message=response['message'], raw=response,
        )

    def cancel_order(self, order_id: str) -> bool:
        if self.trader is None:
            raise RuntimeError('QMT 内置执行端尚未连接')
        # 异常必须上抛：超时不等于撤单失败，更不等于委托已终止。
        return self.client.call('cancel', {'order_id': str(order_id)}) is True

    def get_minute_bars(self, ts_codes, *, start_time, end_time):
        raw = self.client.call('minutes', dict(ts_codes=list(dict.fromkeys(ts_codes)),
                                              start_time=str(start_time), end_time=str(end_time)))
        result = {}
        for code, rows in raw.items():
            normalized = []
            for row in rows:
                hhmm = qmt_bar_hhmm(row['bar_time'])
                if hhmm <= 0:
                    raise RuntimeError('QMT 内置分钟线时间无法解析')
                normalized.append(dict(
                    ts_code=code, bar_time=str(row['bar_time']), hhmm=hhmm,
                    **{field: to_float(row[field]) for field in ('open', 'high', 'low', 'close', 'volume', 'amount')},
                ))
            result[code] = sorted(normalized, key=lambda row: row['hhmm'])
        return result
