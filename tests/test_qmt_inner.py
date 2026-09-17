from __future__ import annotations
import ast
from dataclasses import replace
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pandas as pd

from qmt_inner.engine import Engine, PendingSubmission, side_of, plain
from qmt_inner.protocol import FileClient, atomic_json, read_json, remove_file, signature
from src.broker_adapter import OrderRequest, BrokerConnectionConfig
from src.qmt_inner_adapter import QMTInnerBrokerAdapter
from src.trade_intent_store import TradeIntentStore
from src.broker_execution_service import IntentBrokerExecutionService


class Broker:
    def __init__(self):
        self.order_rows = []
        self.deal_rows = []
        self.position_rows = []
        self.calls = []
        self.defer_ack = False
        self.empty_kind = None
        self.C = self
        self.threads = []

    def query(self, account, account_type, kind):
        self.threads.append(threading.get_ident())
        if self.empty_kind == kind:
            return None
        return {'account': [dict(m_strAccountID='TEST', m_dAvailable=100000,
                                 m_dBalance=100000, m_dInstrumentValue=0)],
                'order': self.order_rows, 'deal': self.deal_rows, 'position': self.position_rows}[kind]

    def passorder(self, *args):
        self.calls.append(args)
        if not self.defer_ack:
            self.ack(args)
        return None

    def ack(self, args):
        op, _, account, code, price_type, price, qty, name, quick, tag, _ = args
        self.order_rows.append(dict(m_strAccountID=account, m_strInstrumentID=code.split('.')[0],
                                    m_strExchangeID=code.split('.')[1], m_nOffsetFlag=48 if op == 23 else 49,
                                    m_nOpType=op, m_nOrderStatus=50, m_nVolumeTotalOriginal=qty,
                                    m_nVolumeTraded=0, m_dTradedPrice=0, m_dLimitPrice=price,
                                    m_strOrderSysID=str(len(self.order_rows)+1), m_strOrderRef='REF', m_strRemark=tag))

    def cancel(self, sysid, *args):
        for row in self.order_rows:
            if row['m_strOrderSysID'] == sysid:
                row['m_nOrderStatus'] = 53 if row['m_nVolumeTraded'] else 54
        return True

    def get_full_tick(self, codes):
        return {c: dict(lastPrice=10, open=10, high=10, low=10, lastClose=9.8,
                        amount=100000, bidPrice=[10], askPrice=[10.01], bidVol=[5], askVol=[6],
                        timetag='20260915 10:00:00', openInt=13) for c in codes}

    def get_instrument_detail(self, code):
        self.detail_calls = getattr(self, 'detail_calls', 0) + 1
        instrument, exchange = code.split('.')
        return dict(InstrumentID=instrument, ExchangeID=exchange,
                    PreClose=9.8, UpStopPrice=10.78, DownStopPrice=8.82)

    def subscribe_quote(self, code, **kwargs):
        return 1

    def unsubscribe_quote(self, seq):
        pass

    def get_market_data_ex(self, fields, codes, **kwargs):
        self.minute_kwargs = kwargs
        return {c: pd.DataFrame([dict(open=10, high=10, low=10, close=10, volume=100, amount=100000)],
                                index=['20260915100100']) for c in codes}


class InnerTests(unittest.TestCase):
    def test_quotes_fill_actual_broker_limits_and_cache_same_day(self):
        for _ in range(2):
            quote = self.engine.quotes(['000001.SZ'])['000001.SZ']
            self.assertEqual(quote['upperLimit'], 10.78)
            self.assertEqual(quote['lowerLimit'], 8.82)
            self.assertEqual(quote['lastPrice'], 10)
        self.assertEqual(self.broker.detail_calls, 1)

    def test_quotes_reject_wrong_security_or_stale_price_bands(self):
        for changes in ({'InstrumentID': '000002'}, {'PreClose': 9.5}, {'UpStopPrice': 0}):
            with patch.object(self.broker, 'get_instrument_detail', return_value=dict(
                    InstrumentID='000001', ExchangeID='SZ', PreClose=9.8,
                    UpStopPrice=10.78, DownStopPrice=8.82)) as getter:
                getter.return_value.update(changes)
                with self.assertRaises(RuntimeError):
                    self.engine.quotes(['000001.SZ'])
            self.assertEqual(self.engine.instrument_limits, {})

    def test_quotes_preserve_existing_tick_limits_without_extra_query(self):
        raw = self.broker.get_full_tick(['000001.SZ'])
        raw['000001.SZ'].update(upperLimit=10.78, lowerLimit=8.82)
        with patch.object(self.broker, 'get_full_tick', return_value=raw), \
             patch.object(self.broker, 'get_instrument_detail', side_effect=AssertionError('unused')):
            self.assertEqual(self.engine.quotes(['000001.SZ'])['000001.SZ']['upperLimit'], 10.78)

    def test_native_order_opaque_tag_does_not_break_submit_or_reconciliation(self):
        class NativeOrder:
            @property
            def m_pOrderTag(self):
                raise TypeError('No to_python (by-value) converter found for C++ type: class boost::shared_ptr<class se::CXtOrderTag>')

        original_query = self.broker.query
        def native_query(account, account_type, kind):
            rows = original_query(account, account_type, kind)
            if kind != 'order':
                return rows
            converted = []
            for row in rows:
                native = NativeOrder()
                native.__dict__.update(row)
                converted.append(native)
            return converted
        self.engine.api['get_trade_detail_data'] = native_query
        with self.gates():
            first = self.engine.submit(self.request, self.body())
            second = self.engine.submit(self.request, self.body())
        self.assertTrue(first['accepted'])
        self.assertEqual(first['order_id'], second['order_id'])
        self.assertEqual(len(self.broker.calls), 1)
        orders = self.engine.orders()
        self.assertEqual(orders[0]['execution_intent_id'], 'intent-1')
        self.assertEqual(orders[0]['order_volume'], 100)
        self.assertEqual(orders[0]['order_remark'], 'slice-1')
        self.engine.callback('order', native_query('TEST', 'stock', 'order')[0])
        self.engine.drain_callbacks()
        self.assertEqual(self.engine.callback_fault, '')

    def test_native_property_unrelated_error_and_identity_converter_fail_closed(self):
        class Fault:
            @property
            def m_anything(self):
                raise RuntimeError('broker unavailable')
        class IdentityFault:
            @property
            def m_strRemark(self):
                raise RuntimeError('No to_python (by-value) converter found for C++ type: class boost::shared_ptr<class se::CXtOrderTag>')
        for value in (Fault(), IdentityFault()):
            with self.assertRaises(RuntimeError):
                plain(value)

        class TypeIdentityFault:
            @property
            def m_strRemark(self):
                raise TypeError('No to_python (by-value) converter found for C++ type: class boost::shared_ptr<class se::CXtOrderTag>')
        with self.assertRaisesRegex(TypeError, 'Broker property m_strRemark'):
            plain(TypeIdentityFault())

    def test_native_query_and_getter_failure_report_original_stage(self):
        def fail_query(*args):
            raise TypeError('No to_python (by-value) converter found for C++ type: class boost::shared_ptr<class se::CXtOrderTag>')
        self.engine.api['get_trade_detail_data'] = fail_query
        with self.assertRaisesRegex(RuntimeError, 'Native query order.*TypeError'):
            self.engine.orders()

        class Fault:
            @property
            def m_nVolumeTotalOriginal(self):
                raise TypeError('broker scalar unreadable')
        self.engine.api['get_trade_detail_data'] = lambda *args: [Fault()]
        with self.assertRaisesRegex(RuntimeError, 'Native serialization order.*m_nVolumeTotalOriginal'):
            self.engine.orders()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = dict(spool_dir=str(self.root / 'spool'), account_id='TEST', token='x'*64,
                        mode='read_only', request_timeout_seconds=8, heartbeat_max_age_seconds=5,
                        max_order_notional=100000)
        self.broker = Broker()
        self.engine = Engine(self.broker, dict(get_trade_detail_data=self.broker.query,
                            passorder=self.broker.passorder, cancel=self.broker.cancel,
                            download_history_data=lambda *a: None), self.cfg)
        self.request = dict(ts_code='000001.SZ', broker_code='000001.SZ', side='BUY', quantity=100,
                            price_type='FIXED_PRICE', price=10., strategy_name='A_SYSTEM_A', remark='slice-1',
                            metadata={'_execution_intent_id':'intent-1'})

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def body(self, params=None):
        now = time.time()
        return dict(id='a'*32, protocol=1, account_id='TEST', instance=self.engine.instance,
                    created=now, expires=now+2, method='submit', params=params or self.request)

    def gates(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(self.engine, 'runtime_gate'))
        stack.enter_context(patch.object(self.engine, 'trading_time', return_value='20260915'))
        stack.enter_context(patch.object(self.engine, 'verify_intent'))
        return stack

    def test_protocol_file_operations_retry_windows_sharing_violation(self):
        target = self.root / 'retry.json'
        real_replace = os.replace
        replace_calls = []

        def flaky_replace(source, destination):
            replace_calls.append((source, destination))
            if len(replace_calls) == 1:
                exc = PermissionError('temporary sharing violation')
                exc.winerror = 32
                raise exc
            return real_replace(source, destination)

        with patch('qmt_inner.protocol.os.replace', side_effect=flaky_replace), \
             patch('qmt_inner.protocol.time.sleep') as sleeper:
            atomic_json(target, {'ok': True})
        self.assertEqual(read_json(target), {'ok': True})
        self.assertEqual(len(replace_calls), 2)
        sleeper.assert_called_once_with(0.01)

    def test_read_json_and_remove_retry_windows_access_denied(self):
        target = self.root / 'busy.json'
        target.write_text('{"ok":true}', encoding='utf-8')
        real_open = Path.open
        open_calls = []

        def flaky_open(path, *args, **kwargs):
            if path == target:
                open_calls.append(path)
                if len(open_calls) == 1:
                    exc = PermissionError('access denied')
                    exc.winerror = 5
                    raise exc
            return real_open(path, *args, **kwargs)

        with patch('qmt_inner.protocol.Path.open', new=flaky_open), \
             patch('qmt_inner.protocol.time.sleep') as sleeper:
            self.assertEqual(read_json(target), {'ok': True})
        self.assertEqual(len(open_calls), 2)
        sleeper.assert_called_once_with(0.01)

        real_unlink = Path.unlink
        unlink_calls = []

        def flaky_unlink(path, *args, **kwargs):
            if path == target:
                unlink_calls.append(path)
                if len(unlink_calls) == 1:
                    exc = PermissionError('sharing violation')
                    exc.winerror = 33
                    raise exc
            return real_unlink(path, *args, **kwargs)

        with patch('qmt_inner.protocol.Path.unlink', new=flaky_unlink), \
             patch('qmt_inner.protocol.time.sleep') as sleeper:
            self.assertTrue(remove_file(target))
        self.assertEqual(len(unlink_calls), 2)
        sleeper.assert_called_once_with(0.01)
        self.assertFalse(target.exists())

    def test_successful_response_is_not_lost_when_cleanup_stays_locked(self):
        stop = threading.Event()

        def pump():
            while not stop.wait(.01):
                self.engine.pump()

        thread = threading.Thread(target=pump)
        thread.start()
        client = FileClient(self.cfg)
        client.connect()
        original_remove = remove_file

        def locked_response_cleanup(path):
            if Path(path).parent.name == 'responses':
                exc = PermissionError('response still held by scanner')
                exc.winerror = 32
                raise exc
            return original_remove(path)

        try:
            with patch('qmt_inner.protocol.remove_file', side_effect=locked_response_cleanup):
                result = client.call('account', {})
            self.assertEqual(result['m_dAvailable'], 100000)
        finally:
            stop.set()
            thread.join()

    def test_read_only_does_not_call_broker(self):
        result = self.engine.submit(self.request, self.body())
        self.assertFalse(result['accepted'])
        self.assertIn('read_only', result['message'])
        self.assertEqual(self.broker.calls, [])

    def test_manual_stop_blocks_live_submit_and_cancel(self):
        runtime = self.root / 'runtime.json'
        stop = self.root / '.manual_stop.json'
        atomic_json(runtime, dict(trade_mode='live', broker_adapter_enabled=True,
                    qmt_enabled=True, broker={'enabled': True},
                    live_trade=dict(enabled=True, real_order_enabled=True,
                                    allow_buy=True, allow_sell=True)))
        atomic_json(stop, {'status': 'MANUAL_STOP'})
        self.cfg.update(mode='live', runtime_config=str(runtime), manual_stop_path=str(stop))
        with patch.object(self.engine, 'trading_time', return_value='20260915'), \
             patch.object(self.engine, 'verify_intent'):
            result = self.engine.submit(self.request, self.body())
            with self.assertRaisesRegex(RuntimeError, 'manual stop'):
                self.engine.cancel({'order_id': '1'})
        self.assertFalse(result['accepted'])
        self.assertIn('manual stop', result['message'])
        self.assertEqual(self.broker.calls, [])

    def test_none_query_is_not_empty_account(self):
        for kind, method in [('account', self.engine.account_snapshot), ('position', self.engine.positions),
                             ('order', self.engine.orders), ('deal', self.engine.trades)]:
            self.broker.empty_kind = kind
            with self.assertRaises(RuntimeError):
                method()

    def test_stock_side_uses_offset_not_direction(self):
        self.assertEqual(side_of(dict(m_nDirection=48, m_nOffsetFlag=49)), 'SELL')
        with self.assertRaises(ValueError):
            side_of(dict(m_nDirection=48))

    def test_submit_idempotent_and_fixed_price(self):
        with self.gates():
            first = self.engine.submit(self.request, self.body())
            again = self.engine.submit(self.request, self.body())
        self.assertEqual(first, again)
        self.assertEqual(len(self.broker.calls), 1)
        self.assertEqual(self.broker.calls[0][4], 11)
        self.assertEqual(self.broker.calls[0][8], 2)
        self.assertEqual(self.engine.orders()[0]['remark'], 'slice-1')
        changed = dict(self.request, quantity=200)
        with self.assertRaisesRegex(RuntimeError, 'Idempotency conflict'):
            self.engine.submit(changed, self.body(changed))

    def test_pending_ack_and_restart_never_resubmits(self):
        self.broker.defer_ack = True
        with self.gates(), self.assertRaises(PendingSubmission):
            self.engine.submit(self.request, self.body())
        self.engine.close()
        self.engine = Engine(self.broker, dict(get_trade_detail_data=self.broker.query,
                             passorder=self.broker.passorder, cancel=self.broker.cancel), self.cfg)
        with self.assertRaises(PendingSubmission):
            self.engine.submit(self.request, self.body())
        self.assertEqual(len(self.broker.calls), 1)
        self.broker.ack(self.broker.calls[0])
        self.assertTrue(self.engine.submit(self.request, self.body())['accepted'])
        self.assertEqual(len(self.broker.calls), 1)

    def test_expired_request_cannot_submit(self):
        body = self.body()
        body['expires'] = time.time()-1
        atomic_json(self.engine.root/'requests'/(body['id']+'.json'),
                    dict(body=body, signature=signature(body, self.cfg['token'])))
        self.engine.pump()
        self.assertEqual(self.broker.calls, [])
        reply = json.loads((self.engine.root/'responses'/(body['id']+'.json')).read_text())
        self.assertFalse(reply['body']['ok'])

    def test_forged_signature_never_calls_broker(self):
        body = self.body()
        atomic_json(self.engine.root/'requests'/(body['id']+'.json'),
                    dict(body=body, signature=signature(body, 'wrong-token')))
        self.engine.pump()
        self.assertEqual(self.broker.calls, [])

    def test_previous_session_request_never_calls_broker(self):
        body = self.body()
        body['instance'] = 'previous-qmt-session'
        atomic_json(self.engine.root/'requests'/(body['id']+'.json'),
                    dict(body=body, signature=signature(body, self.cfg['token'])))
        self.engine.pump()
        self.assertEqual(self.broker.calls, [])

    def test_float_epoch_minute_keeps_correct_clock(self):
        frame = pd.DataFrame([dict(time=1789437660000.0, open=10., high=10., low=10.,
                                  close=10., volume=100., amount=1000.)])
        with patch.object(self.broker, 'get_market_data_ex', return_value={'000001.SZ': frame}):
            rows = self.engine.minutes(dict(ts_codes=['000001.SZ'], start_time='20260915093000',
                                            end_time='20260915110000'))
        from src.qmt_adapter import qmt_bar_hhmm
        self.assertEqual(rows['000001.SZ'][0]['bar_time'], 1789437660000.0)
        expected = dt.datetime.fromtimestamp(1789437660, dt.timezone(dt.timedelta(hours=8)))
        self.assertEqual(qmt_bar_hhmm(rows['000001.SZ'][0]['bar_time']), expected.hour*100 + expected.minute)

    def test_callback_is_nonblocking_and_audited_on_pump(self):
        completed = threading.Event()
        with self.engine.lock:
            thread = threading.Thread(target=lambda: (self.engine.callback('order', {'m_strAccountID':'TEST'}), completed.set()))
            thread.start()
            self.assertTrue(completed.wait(2), 'Callback must not wait on broker pump lock')
        thread.join()
        self.engine.pump()
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM callbacks').fetchone()[0], 1)

    def test_actual_readonly_server_blocks_live_daemon_start(self):
        runtime = self.root / 'runtime.json'
        atomic_json(runtime, {'trade_mode':'live'})
        adapter = QMTInnerBrokerAdapter(BrokerConnectionConfig('test','TEST','STOCK','unused',0),
                                        dict(self.cfg, runtime_config=str(runtime), mode='live'))
        adapter.server_info = {'mode':'read_only'}
        with self.assertRaisesRegex(RuntimeError, '只读'):
            adapter.assert_execution_ready()

    def test_t_plus_one_gate(self):
        sell = dict(self.request, side='SELL')
        with self.gates():
            result = self.engine.submit(sell, self.body(sell))
        self.assertFalse(result['accepted'])
        self.assertIn('T+1', result['message'])
        self.assertFalse(self.broker.calls)

    def test_legacy_latest_price_gateway_retains_native_price_type(self):
        request = dict(self.request, price_type='LATEST_PRICE', price=0.)
        with self.gates():
            result = self.engine.submit(request, self.body(request))
        self.assertTrue(result['accepted'])
        self.assertEqual(self.broker.calls[0][4], 5)

    def test_latest_price_does_not_bypass_notional_cap_with_zero_price(self):
        self.cfg['max_order_notional'] = 500
        request = dict(self.request, price_type='LATEST_PRICE', price=0.)
        with self.gates():
            result = self.engine.submit(request, self.body(request))
        self.assertFalse(result['accepted'])
        self.assertIn('notional cap', result['message'])
        self.assertEqual(self.broker.calls, [])

    def test_zero_notional_cap_uses_original_portfolio_risk_limits(self):
        self.cfg['max_order_notional'] = 0
        with self.gates():
            result = self.engine.submit(self.request, self.body())
        self.assertTrue(result['accepted'])
        self.assertEqual(len(self.broker.calls), 1)

    def test_research_history_preserves_missing_and_depth_arrays(self):
        import numpy as np
        frame = pd.DataFrame({'close':[float('nan')], 'bidPrice':[np.array([10., 9.99])]},
                              index=['20260915100100'])
        params = dict(fields=[], codes=['000001.SZ'], period='tick', start_time='20260915',
                      end_time='20260915', count=-1, dividend_type='none', fill_data=True)
        with patch.object(self.broker, 'get_market_data_ex', return_value={'000001.SZ':frame}):
            value = self.engine.history(params)['000001.SZ']
        self.assertEqual(value['index'], ['20260915100100'])
        self.assertEqual(value['rows'], [[None, [10., 9.99]]])
        self.assertEqual(self.broker.calls, [])

    def test_partial_fill_cancel_and_normalization(self):
        with self.gates():
            self.engine.submit(self.request, self.body())
            row = self.broker.order_rows[0]
            row.update(m_nOrderStatus=55, m_nVolumeTraded=40, m_dTradedPrice=10.)
            self.assertTrue(self.engine.cancel({'order_id':'1'}))
        normalized = self.engine.orders()[0]
        self.assertEqual(normalized['order_status'], 53)
        self.assertEqual(normalized['traded_volume'], 40)
        self.assertEqual(normalized['order_volume'], 100)

    def test_two_instances_cannot_own_same_spool(self):
        with self.assertRaises(OSError):
            Engine(self.broker, {}, self.cfg)

    def test_account_identity_is_checked(self):
        self.engine.account = 'OTHER'
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.engine.account_snapshot()

    def test_durable_intent_must_match_order(self):
        self.cfg['intent_database'] = str(self.root / 'intents.sqlite3')
        TradeIntentStore(self.cfg['intent_database'])
        with self.assertRaisesRegex(RuntimeError, 'SUBMITTING'):
            self.engine.verify_intent(self.request, '20260915')

    def test_full_transport_and_minute_normalization(self):
        stop = threading.Event()
        def pump():
            while not stop.wait(.01):
                self.engine.pump()
        thread = threading.Thread(target=pump)
        thread.start()
        adapter = QMTInnerBrokerAdapter(BrokerConnectionConfig('test','TEST','STOCK','',0), self.cfg)
        try:
            adapter.connect()
            self.assertEqual(adapter.query_account().available_cash, 100000)
            self.assertEqual(adapter.query_positions(), [])
            quote = adapter.get_full_tick(['000001.SZ'])['000001.SZ']
            self.assertEqual(quote.raw['time'], int(dt.datetime(2026,9,15,10,tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()*1000))
            bars = adapter.get_minute_bars(['000001.SZ'], start_time='20260915093000', end_time='20260915103000')
            self.assertEqual(bars['000001.SZ'][0]['hhmm'], 1001)
            self.assertEqual(self.broker.minute_kwargs['dividend_type'], 'none')
            from src.qmt_market_data import InnerMarketData
            research = InnerMarketData(self.cfg)
            self.assertTrue(research.download_history_data('000001.SZ', '5m', '20260915', '20260915'))
            frame = research.get_market_data_ex(['close'], ['000001.SZ'], period='5m',
                        start_time='20260915093000', end_time='20260915103000')['000001.SZ']
            self.assertEqual(frame.index.tolist(), ['20260915100100'])
            self.assertEqual(frame.iloc[0]['close'], 10)
            self.assertEqual(set(self.broker.threads), {thread.ident})
        finally:
            adapter.disconnect()
            stop.set()
            thread.join()

    def test_original_intent_service_partial_cancel_restart_and_exit(self):
        """原串行交易服务经真实文件协议完成开仓、部分成交、撤单、重启恢复、平仓。"""
        import hashlib
        from src.trade_recovery import TradeRecoveryCoordinator
        self.cfg.update(mode='live', intent_database=str(self.root/'intent.sqlite3'),
                        runtime_config=str(self.root/'runtime.json'))
        atomic_json(self.cfg['runtime_config'], dict(trade_mode='live', broker_adapter_enabled=True,
                    qmt_enabled=True, broker={'enabled':True},
                    live_trade=dict(enabled=True, real_order_enabled=True, allow_buy=True, allow_sell=True)))
        fingerprint = hashlib.sha256(b'TEST').hexdigest()[:16]
        store = TradeIntentStore(self.cfg['intent_database'])
        service = IntentBrokerExecutionService(intent_store=store,
                    account_fingerprint_provider=lambda:fingerprint,
                    business_date_provider=lambda:'20260915', default_timeout=3)
        adapter = QMTInnerBrokerAdapter(BrokerConnectionConfig('test','TEST','STOCK','',0),self.cfg)
        stop = threading.Event()
        def pump():
            while not stop.wait(.01):
                self.engine.pump()
        thread = threading.Thread(target=pump)
        thread.start()
        time_patch = patch.object(self.engine,'trading_time',return_value='20260915')
        time_patch.start()
        try:
            service.call_function(adapter.connect, operation='connect')
            proxy = service.proxy(lambda:adapter)
            request = OrderRequest(ts_code='000001.SZ', broker_code='000001.SZ', side='BUY',
                        quantity=100, price_type='FIXED_PRICE', price=10, strategy_leg='A',
                        business_date='20260915', purpose='OPEN', source_key='A-slice-1', remark='A-slice-1')
            result = proxy.place_order(request)
            self.assertTrue(result.accepted)
            self.assertEqual(store.get_intent(result.intent_id)['status'],'SUBMITTED')
            with self.engine.lock:
                row = self.broker.order_rows[0]
                row.update(m_nOrderStatus=55,m_nVolumeTraded=40,m_dTradedPrice=10.)
                deal = dict(row, m_strTradeID='deal-1',m_nVolume=40,m_dPrice=10.,
                            m_strTradeDate='20260915',m_strTradeTime='100000')
                self.broker.deal_rows=[deal,dict(deal)]
            self.assertEqual(proxy.get_order_fill(result.order_id).filled_qty,40)
            self.assertTrue(proxy.cancel_order(result.order_id))
            self.assertTrue(proxy.get_order_fill(result.order_id).is_terminal)
            self.assertEqual(store.get_intent(result.intent_id)['filled_qty'],40)
            # A restart with the same intent must not issue another broker order.
            service.shutdown()
            service = IntentBrokerExecutionService(intent_store=TradeIntentStore(self.cfg['intent_database']),
                        account_fingerprint_provider=lambda:fingerprint,
                        business_date_provider=lambda:'20260915', default_timeout=3)
            proxy = service.proxy(lambda:adapter)
            proxy.place_order(request)
            self.assertEqual(len(self.broker.calls),1)
            with self.engine.lock:
                self.broker.position_rows=[dict(m_strAccountID='TEST',m_strInstrumentID='000001',
                    m_strExchangeID='SZ',m_nVolume=40,m_nCanUseVolume=40,m_dOpenPrice=10.,m_dMarketValue=400.)]
            exit_request = replace(request, side='SELL',quantity=40,purpose='CLOSE',
                                    source_key='A-exit-1',remark='A-exit-1')
            sold = proxy.place_order(exit_request)
            self.assertTrue(sold.accepted)
            self.assertEqual(self.broker.calls[-1][0],24)
            with self.engine.lock:
                self.broker.order_rows[-1].update(m_nOrderStatus=56,m_nVolumeTraded=40,m_dTradedPrice=10.)
            self.assertTrue(proxy.get_order_fill(sold.order_id).is_filled)
            self.assertEqual(store.get_intent(sold.intent_id)['status'],'FILLED')
        finally:
            time_patch.stop()
            service.shutdown()
            stop.set()
            thread.join()
            adapter.disconnect()

    def test_lost_ack_recovery_binds_exact_intent_even_with_same_remark(self):
        from src.trade_recovery import TradeRecoveryCoordinator
        from tests.test_trade_recovery import advance_to_prepared
        store=TradeIntentStore(self.root/'recover.sqlite3')
        intent=advance_to_prepared(store)
        store.transition_intent(intent['intent_id'],'SUBMITTING')
        p=dict(self.request,quantity=1000,remark='PREMARKET-A-000001',
                metadata={'_execution_intent_id':intent['intent_id']})
        with self.gates():
            self.engine.submit(p,self.body(p))
        # Same symbol/side/qty/remark from a different slice is not this intent.
        other=dict(p,metadata={'_execution_intent_id':'different-intent'})
        with self.gates():
            self.engine.submit(other,self.body(other))
        recovered=TradeRecoveryCoordinator(store).recover(daemon_boot_id='new-boot',
            account_fingerprint='acct',business_date='20260817',positions=[],
            orders=self.engine.orders(),trades=[])
        self.assertEqual(recovered.unresolved_count,0)
        self.assertEqual(store.get_intent(intent['intent_id'])['broker_order_id'],'1')

    def test_python36_syntax_for_entire_inner_package(self):
        for path in (Path(__file__).resolve().parents[1]/'qmt_inner').glob('*.py'):
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path), feature_version=(3,6))


if __name__ == '__main__':
    unittest.main()
