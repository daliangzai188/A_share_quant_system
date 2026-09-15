# coding: utf-8
"""QMT callback-thread execution engine. Python 3.6, standard library only.

Every broker operation runs in pump(), never in a socket/worker thread.
The durable submit marker precedes passorder; an uncertain submission is never replayed.
"""
import csv
import datetime as dt
import hashlib
import hmac
import math
import os
import re
import queue
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from qmt_inner.protocol import PROTOCOL, atomic_json, canonical, read_json, signature, lock_owner


class PendingSubmission(Exception):
    pass


def plain(value):
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Non-finite broker value')
        return value
    if hasattr(value, 'tolist'):
        return plain(value.tolist())
    if hasattr(value, 'item'):
        return plain(value.item())
    result = {}
    for key in dir(value):
        if key.startswith('m_'):
            item = getattr(value, key)
            if not callable(item):
                try:
                    result[key] = plain(item)
                except (TypeError, ValueError):
                    continue
    if not result:
        raise ValueError('Unsupported or empty broker object')
    return result


def code_of(row):
    code = str(row.get('m_strInstrumentID', '')).strip().upper()
    exchange = str(row.get('m_strExchangeID', '')).strip().upper()
    if '.' not in code:
        if exchange not in ('SH', 'SZ', 'BJ'):
            raise ValueError('Unknown exchange; cannot normalize security')
        code += '.' + exchange
    if not re.match(r'^\d{6}\.(SH|SZ|BJ)$', code):
        raise ValueError('Invalid stock code')
    return code


def side_of(row):
    op = row.get('m_nOpType')
    flag = row.get('m_nOffsetFlag')
    by_op = {23: 'BUY', 24: 'SELL'}.get(op)
    by_flag = {48: 'BUY', 49: 'SELL'}.get(flag)
    if by_op and by_flag and by_op != by_flag:
        raise ValueError('Conflicting stock operation fields')
    side = by_op or by_flag
    if not side:
        raise ValueError('Unknown stock operation; m_nDirection is not a stock side')
    return side


class Engine:
    def __init__(self, context, api, settings):
        self.C = context
        self.api = api
        self.cfg = settings
        self.root = Path(settings['spool_dir'])
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ('requests', 'responses'):
            (self.root / name).mkdir(exist_ok=True)
        self.owner = (self.root / 'owner.lock').open('a+b')
        self.owner.seek(0)
        try:
            self._lock_owner()
        except BaseException:
            self.owner.close()
            raise
        self.instance = uuid.uuid4().hex
        self.account = settings['account_id']
        self.lock = threading.RLock()
        self.closed = False
        self.pending = {}
        self.callback_queue = queue.Queue(maxsize=int(settings.get("callback_queue_limit", 10000)))
        self.callback_fault = ""
        self.subscriptions = {}
        self.downloaded_windows = set()
        self.db = sqlite3.connect(str(self.root / 'journal.sqlite3'), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS submissions (tag TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, sysid TEXT NOT NULL DEFAULT \'\', created REAL NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS callbacks (id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, created REAL)')
        self.db.commit()
        self.heartbeat()

    def _lock_owner(self):
        lock_owner(self.owner)

    def heartbeat(self):
        body = dict(protocol=PROTOCOL, instance=self.instance, account_id=self.account,
                    mode=self.cfg.get('mode', 'read_only'), time=time.time(),
                    pending=len(self.pending), python=__import__('sys').version.split()[0])
        atomic_json(self.root / 'heartbeat.json', dict(body=body, signature=signature(body, self.cfg['token'])))

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            # stop() is invoked after QMT disconnects. Never submit/cancel here.
            try:
                self.drain_callbacks()
            finally:
                self.db.close()
                self.owner.close()
            try:
                hb = read_json(self.root / 'heartbeat.json')
                if hb['body']['instance'] == self.instance:
                    (self.root / 'heartbeat.json').unlink()
            except (OSError, ValueError, KeyError):
                pass

    def query(self, kind):
        rows = self.api['get_trade_detail_data'](self.account, 'stock', kind)
        if rows is None or not isinstance(rows, (list, tuple)):
            raise RuntimeError('Broker query failed, not an empty account: ' + kind)
        result = [plain(row) for row in rows]
        for row in result:
            if row.get('m_strAccountID') != self.account:
                raise RuntimeError('Broker account identity missing or mismatched')
        return result

    def account_snapshot(self):
        rows = self.query('account')
        if len(rows) != 1:
            raise RuntimeError('Expected exactly one account snapshot')
        row = rows[0]
        # Required amounts must not silently become zero.
        market_key = 'm_dInstrumentValue' if 'm_dInstrumentValue' in row else 'm_dMarketValue'
        for key in ('m_dAvailable', 'm_dBalance', market_key):
            if key not in row or not math.isfinite(float(row[key])):
                raise RuntimeError('Missing account field: ' + key)
        return dict(row, cash=row['m_dAvailable'], available_cash=row['m_dAvailable'],
                    total_asset=row['m_dBalance'], market_value=row[market_key],
                    frozen_cash=row.get('m_dFrozenCash', 0))

    def positions(self):
        result = []
        for row in self.query('position'):
            for key in ('m_nVolume', 'm_nCanUseVolume', 'm_dOpenPrice', 'm_dMarketValue'):
                if key not in row:
                    raise RuntimeError('Missing position field: ' + key)
            result.append(dict(row, stock_code=code_of(row), volume=row['m_nVolume'],
                               can_use_volume=row['m_nCanUseVolume'], open_price=row['m_dOpenPrice']))
        return result

    def normalize(self, row, kind):
        code = code_of(row)
        tag = str(row.get('m_strRemark', ''))
        saved = self.db.execute('SELECT * FROM submissions WHERE tag=?', (tag,)).fetchone()
        payload = __import__('json').loads(saved['payload']) if saved else {}
        sysid = str(row.get('m_strOrderSysID', '')).strip()
        # Existing miniQMT orders use the internal reference. Do not silently
        # replace their identity with the exchange contract number at cutover.
        oid = sysid if saved else str(row.get('m_strOrderRef', '')).strip() or sysid
        remark = str(payload.get('remark', tag))
        result = dict(row, stock_code=code, ts_code=code, order_id=oid, order_sysid=sysid,
                      execution_intent_id=payload.get('metadata', {}).get('_execution_intent_id', ''),
                      side=side_of(row), order_type=23 if side_of(row) == 'BUY' else 24,
                      order_remark=remark, remark=remark,
                      strategy_name=payload.get('strategy_name', row.get('m_strSource', '')))
        if kind == 'order':
            for key in ('m_nOrderStatus', 'm_nVolumeTotalOriginal', 'm_nVolumeTraded'):
                if key not in row:
                    raise RuntimeError('Missing order field: ' + key)
            result.update(order_status=row['m_nOrderStatus'], order_volume=row['m_nVolumeTotalOriginal'],
                          traded_volume=row['m_nVolumeTraded'], traded_price=row.get('m_dTradedPrice', 0),
                          price=row.get('m_dLimitPrice', 0), order_time=row.get('m_strInsertTime', ''))
        else:
            result.update(traded_id=row['m_strTradeID'], traded_volume=row['m_nVolume'],
                          traded_price=row['m_dPrice'], traded_time=row.get('m_strTradeTime', ''))
        return result

    def orders(self):
        rows = [self.normalize(row, 'order') for row in self.query('order')]
        ids = [r['order_id'] for r in rows if r['order_id']]
        if len(ids) != len(set(ids)):
            raise RuntimeError('Order identity collision; cutover reconciliation required')
        return rows

    def trades(self):
        result = {}
        for row in self.query('deal'):
            normalized = self.normalize(row, 'deal')
            key = (row.get('m_strTradeDate', ''), normalized['stock_code'], normalized['traded_id'])
            if not key[2]:
                raise RuntimeError('Missing trade identity')
            if key in result and result[key] != normalized:
                raise RuntimeError('Conflicting duplicate trade identity')
            result[key] = normalized
        return list(result.values())

    def callback(self, kind, value):
        # Native QMT callbacks must never wait on a lock held by passorder/query.
        # Persist on the next timer turn, while reconciliation still queries QMT.
        if self.closed:
            return
        try:
            row = plain(value)
            if row.get('m_strAccountID') not in (None, self.account):
                return
            self.callback_queue.put_nowait((kind, canonical(row), time.time()))
        except Exception as exc:
            self.callback_fault = 'Callback audit unavailable: ' + str(exc)

    def drain_callbacks(self):
        batch = []
        for _ in range(500):
            try:
                batch.append(self.callback_queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self.db.executemany('INSERT INTO callbacks(kind,payload,created) VALUES(?,?,?)', batch)
            self.db.commit()

    def quotes(self, codes):
        raw = self.C.get_full_tick(codes)
        if not isinstance(raw, dict):
            raise RuntimeError('Full tick query failed')
        result = {}
        for code in codes:
            if not raw.get(code):
                raise RuntimeError('Missing tick for ' + code)
            row = plain(raw[code])
            # Inner QMT publishes timetag, miniQMT often publishes epoch ms.
            # Preserve both original timestamps and units; never stamp old data as now.
            if 'time' not in row and row.get('timetag'):
                stamp = dt.datetime.strptime(row['timetag'], '%Y%m%d %H:%M:%S')
                row['time'] = int(stamp.replace(tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp() * 1000)
            if 'suspendFlag' not in row and 'openInt' in row:
                row['suspendFlag'] = int(row['openInt']) in (1, 17, 20)
            result[code] = row
        return result

    def minutes(self, params):
        codes = list(dict.fromkeys(params['ts_codes']))
        if len(codes) > int(self.cfg.get('max_minute_subscriptions', 50)):
            raise RuntimeError('Minute subscription capacity exceeded')
        for code in list(self.subscriptions):
            if code not in codes:
                self.C.unsubscribe_quote(self.subscriptions[code])
                del self.subscriptions[code]
        for code in codes:
            if code not in self.subscriptions:
                seq = self.C.subscribe_quote(code, period='1m', dividend_type='none')
                if seq is None or int(seq) < 0:
                    raise RuntimeError('Minute subscription failed: ' + code)
                self.subscriptions[code] = seq
            window = (code, params['start_time'])
            if window not in self.downloaded_windows:
                self.api['download_history_data'](code, '1m', params['start_time'], params['end_time'])
                self.downloaded_windows.add(window)
        if not codes:
            return {}
        raw = self.C.get_market_data_ex(['open', 'high', 'low', 'close', 'volume', 'amount'], codes,
                                        period='1m', start_time=params['start_time'], end_time=params['end_time'],
                                        count=-1, dividend_type='none', fill_data=True, subscribe=False)
        if not isinstance(raw, dict):
            raise RuntimeError('Minute query failed')
        result = {}
        for code in codes:
            frame = raw.get(code)
            if frame is None or not hasattr(frame, 'iterrows'):
                raise RuntimeError('Missing minute frame: ' + code)
            result[code] = []
            for index, row in frame.iterrows():
                value = row.get('time', index)
                if hasattr(value, 'item'):
                    value = value.item()
                # Preserve epoch milliseconds as numbers. Stringifying a pandas
                # float would append .0 and silently corrupt the minute label.
                stamp = value if isinstance(value, (int, float)) else str(value)
                result[code].append(dict(plain(row.to_dict()), bar_time=stamp))
        return result

    def runtime_gate(self, side):
        if self.callback_fault:
            raise RuntimeError(self.callback_fault)
        if self.cfg.get('mode', 'read_only') != 'live':
            raise RuntimeError('Inner bridge is read_only; broker mutation disabled')
        stop_path = self.cfg.get('manual_stop_path')
        if not stop_path:
            # runtime_config 固定位于 <project>/config/config.json。旧版私有配置
            # 没有 manual_stop_path 时也要自动兼容，避免升级窗口留下旁路。
            stop_path = str(Path(self.cfg['runtime_config']).parent.parent / '.manual_stop.json')
        if Path(stop_path).exists():
            raise RuntimeError('A_System manual stop is active; broker mutation disabled')
        runtime = read_json(self.cfg['runtime_config'])
        live = runtime.get('live_trade', {})
        broker = runtime.get('broker', {})
        if not (runtime.get('trade_mode') == 'live' and runtime.get('broker_adapter_enabled')
                and runtime.get('qmt_enabled') and broker.get('enabled')
                and live.get('enabled') and live.get('real_order_enabled')):
            raise RuntimeError('Runtime real-order gates are closed')
        if side in ('BUY', 'SELL') and not live.get('allow_' + side.lower()):
            raise RuntimeError('Runtime side gate is closed: ' + side)
        return runtime

    def history(self, p):
        if p['period'] not in ('tick', '1m', '5m', '1d'):
            raise ValueError('Unsupported research period')
        raw = self.C.get_market_data_ex(p['fields'], p['codes'], period=p['period'],
                    start_time=p['start_time'], end_time=p['end_time'], count=p['count'],
                    dividend_type=p['dividend_type'], fill_data=p['fill_data'], subscribe=False)
        if not isinstance(raw, dict):
            raise RuntimeError('Historical data query failed')
        def cell(value):
            if hasattr(value, 'tolist'):
                return cell(value.tolist())
            if isinstance(value, (list, tuple)):
                return [cell(x) for x in value]
            # Preserve missing observations as null/NaN, never as zero prices.
            if isinstance(value, float) and not math.isfinite(value):
                return None
            return plain(value)
        result = {}
        for code in p['codes']:
            frame = raw.get(code)
            if frame is None or not hasattr(frame, 'iterrows'):
                raise RuntimeError('Missing historical frame: ' + code)
            result[code] = dict(columns=list(frame.columns), index=[cell(x) for x in frame.index],
                                rows=[[cell(x) for x in row.tolist()] for _, row in frame.iterrows()])
        return result

    def trading_time(self, cancel=False):
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
        day = now.strftime('%Y%m%d')
        # Actual exchange calendar, not a weekday approximation.
        with open(self.cfg['trade_calendar'], encoding='utf-8-sig') as stream:
            days = {str(r['cal_date']).replace('-', '') for r in csv.DictReader(stream)
                    if str(r.get('is_open', '')) in ('1', '1.0') and r.get('exchange', 'SSE') == 'SSE'}
        if day not in days:
            raise RuntimeError('Not a verified trading day')
        t = now.strftime('%H%M%S')
        valid = ('091500' <= t < '092500' or '093000' <= t < '113000' or '130000' <= t < '150000')
        if cancel and ('092000' <= t < '092500' or '145700' <= t < '150000'):
            valid = False
        if not valid:
            raise RuntimeError('Outside permitted order/cancel session')
        return day

    def verify_intent(self, p, day):
        intent = p.get('metadata', {}).get('_execution_intent_id', '')
        if not intent:
            raise RuntimeError('Missing durable execution intent')
        path = Path(self.cfg['intent_database'])
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute('SELECT * FROM trade_intents WHERE intent_id=?', (intent,)).fetchone()
        finally:
            db.close()
        if not row or row['status'] != 'SUBMITTING' or row['business_date'] != day:
            raise RuntimeError('Intent is not a current SUBMITTING intent')
        if (row['ts_code'] != p['ts_code'] or row['side'] != p['side'] or
                row['target_qty'] != p['quantity'] or row['price_type'] != p['price_type'] or
                abs(row['limit_price'] - p['price']) > 1e-8):
            raise RuntimeError('Order differs from durable intent')
        expected = hashlib.sha256(self.account.encode('utf-8')).hexdigest()[:16]
        if row['account_fingerprint'] != expected:
            raise RuntimeError('Intent belongs to a different account')
        return intent

    def submit(self, p, body):
        try:
            return self._submit(p, body)
        except PendingSubmission:
            raise
        except Exception as exc:
            intent = p.get('metadata', {}).get('_execution_intent_id', '')
            tag = 'AS' + hashlib.sha256((self.account + ':' + intent).encode()).hexdigest()[:28]
            saved = self.db.execute('SELECT state FROM submissions WHERE tag=?', (tag,)).fetchone()
            if saved is None:
                # There is proof that passorder was never reached. Distinguish a
                # local validation rejection from a potentially executed order.
                return dict(accepted=False, order_id='', message='NOT_SUBMITTED: ' + str(exc))
            raise

    def _submit(self, p, body):
        intent = p.get('metadata', {}).get('_execution_intent_id', '')
        if not intent:
            raise RuntimeError('Missing execution intent id')
        tag = 'AS' + hashlib.sha256((self.account + ':' + intent).encode()).hexdigest()[:28]
        saved = self.db.execute('SELECT * FROM submissions WHERE tag=?', (tag,)).fetchone()
        if saved:
            if saved['payload'] != canonical(p):
                raise RuntimeError('Idempotency conflict')
            return self.confirm_submission(tag, p)
        self.runtime_gate(p['side'])
        day = self.trading_time()
        self.verify_intent(p, day)
        price_types = {'FIXED_PRICE': 11, 'FIX_PRICE': 11, 'LATEST_PRICE': 5}
        if p['side'] not in ('BUY', 'SELL') or p['price_type'] not in price_types:
            raise RuntimeError('Unsupported stock side or price type')
        native_price_type = price_types[p['price_type']]
        qty, price = p['quantity'], p['price']
        if (isinstance(qty, bool) or int(qty) != qty or qty <= 0 or not math.isfinite(price)
                or (native_price_type == 11 and price <= 0)):
            raise RuntimeError('Invalid quantity or limit price')
        if p['broker_code'] != p['ts_code'] or not re.match(r'^\d{6}\.(SH|SZ|BJ)$', p['broker_code']):
            raise RuntimeError('Invalid security identity')
        account = self.account_snapshot()
        risk_price = price
        if native_price_type == 5:
            # Preserve the original gateway's latest-price orders. Never silently
            # turn an explicit limit into latest price, or budget at price=0.
            tick = self.quotes([p['ts_code']])[p['ts_code']]
            risk_price = float(tick.get('lastPrice', 0))
            if not math.isfinite(risk_price) or risk_price <= 0:
                raise RuntimeError('Latest-price order requires a valid broker quote')
        notional = qty * risk_price
        max_order_notional = float(self.cfg.get('max_order_notional', 0))
        if max_order_notional > 0 and notional > max_order_notional:
            raise RuntimeError('Local order notional cap exceeded')
        if p['side'] == 'BUY' and notional + max(notional * float(self.cfg.get('buy_fee_reserve_rate', 0.001)),
                                                   float(self.cfg.get('minimum_fee_reserve', 5))) > account['available_cash']:
            raise RuntimeError('Insufficient available cash including fee reserve')
        if p['side'] == 'SELL':
            available = sum(r['can_use_volume'] for r in self.positions() if r['stock_code'] == p['ts_code'])
            if qty > available:
                raise RuntimeError('T+1 / available position gate rejected sell')
        # Persist BEFORE calling passorder. Crash after this line is uncertain,
        # even if the call never reached QMT. This uncertainty is never replayed.
        self.db.execute('INSERT INTO submissions(tag,payload,state,created) VALUES(?,?,?,?)',
                        (tag, canonical(p), 'SUBMITTING', time.time()))
        self.db.commit()
        if time.time() >= body['expires']:
            raise RuntimeError('Order expired before broker call; requires reconciliation')
        self.api['passorder'](23 if p['side'] == 'BUY' else 24, 1101, self.account,
                              p['broker_code'], native_price_type, float(price), int(qty), p['strategy_name'], 2, tag, self.C)
        # passorder has NO order-id return. Do not invent acceptance from None.
        return self.confirm_submission(tag, p)

    def confirm_submission(self, tag, p):
        matches = [r for r in self.query('order') if str(r.get('m_strRemark', '')) == tag]
        if len(matches) > 1:
            raise RuntimeError('Multiple broker orders match one execution intent')
        if not matches:
            raise PendingSubmission(tag)
        row = matches[0]
        if code_of(row) != p['ts_code'] or side_of(row) != p['side'] or int(row['m_nVolumeTotalOriginal']) != p['quantity']:
            raise RuntimeError('Broker acknowledgement differs from request')
        sysid = str(row.get('m_strOrderSysID', '')).strip()
        if row.get('m_nOrderStatus') == 57:
            self.db.execute("UPDATE submissions SET state='REJECTED',sysid=? WHERE tag=?", (sysid, tag))
            self.db.commit()
            return dict(accepted=False, order_id=sysid, message='BROKER_REJECTED')
        if not sysid:
            raise PendingSubmission(tag)
        self.db.execute("UPDATE submissions SET state='ACKNOWLEDGED',sysid=? WHERE tag=?", (sysid, tag))
        self.db.commit()
        return dict(accepted=True, order_id=sysid, message='BROKER_ACKNOWLEDGED')

    def cancel(self, p, body=None):
        self.runtime_gate('CANCEL')
        self.trading_time(cancel=True)
        matches = [r for r in self.orders() if r['order_id'] == str(p['order_id'])]
        if len(matches) != 1 or not matches[0]['order_sysid']:
            raise RuntimeError('Cannot uniquely resolve cancel contract id')
        row = matches[0]
        if row['order_status'] in (53, 54, 56, 57):
            return False
        if body is not None and time.time() >= body['expires']:
            raise RuntimeError('Cancel expired before broker call')
        return self.api['cancel'](row['order_sysid'], self.account, 'STOCK', self.C) is True

    def dispatch(self, body):
        p, method = body['params'], body['method']
        if method == 'hello':
            self.account_snapshot()
            return dict(protocol=PROTOCOL, mode=self.cfg.get('mode', 'read_only'), transport='qmt_inner')
        if method == 'account':
            return self.account_snapshot()
        if method == 'positions':
            return self.positions()
        if method == 'orders':
            return self.orders()
        if method == 'trades':
            return self.trades()
        if method == 'quotes':
            return self.quotes(p['ts_codes'])
        if method == 'minutes':
            return self.minutes(p)
        if method == 'download_history':
            if p['period'] not in ('tick', '1m', '5m', '1d'):
                raise ValueError('Unsupported research period')
            self.api['download_history_data'](p['code'], p['period'], p['start_time'], p['end_time'])
            return True
        if method == 'history':
            return self.history(p)
        if method == 'submit':
            return self.submit(p, body)
        if method == 'cancel':
            return self.cancel(p, body)
        raise RuntimeError('Unsupported bridge operation')

    def reply(self, body, result=None, error=None):
        self.heartbeat()
        payload = dict(id=body['id'], instance=self.instance, ok=error is None, result=result, error=error)
        atomic_json(self.root / 'responses' / (body['id'] + '.json'),
                    dict(body=payload, signature=signature(payload, self.cfg['token'])))

    def pump(self):
        if not self.lock.acquire(False):
            return
        try:
            if self.closed:
                return
            self.drain_callbacks()
            self.heartbeat()
            for rid, pair in list(self.pending.items()):
                body, tag = pair
                try:
                    result = self.confirm_submission(tag, body['params'])
                    self.reply(body, result=result)
                    del self.pending[rid]
                except PendingSubmission:
                    if time.time() >= body['expires']:
                        self.reply(body, error='Submission UNKNOWN; reconcile before any retry')
                        del self.pending[rid]
                except Exception as exc:
                    self.reply(body, error=str(exc))
                    del self.pending[rid]
            for path in sorted((self.root / 'requests').glob('*.json'))[:16]:
                body = None
                try:
                    envelope = read_json(path)
                    body = envelope['body']
                    if not hmac.compare_digest(envelope['signature'], signature(body, self.cfg['token'])):
                        raise ValueError('Bad request signature')
                    if not re.match(r'^[a-f0-9]{32}$', body['id']) or path.stem != body['id']:
                        raise ValueError('Invalid request identity')
                    if (body['protocol'] != PROTOCOL or body['instance'] != self.instance or
                            body['account_id'] != self.account):
                        raise ValueError('Request session/account mismatch')
                    now = time.time()
                    if not body['created'] - 2 <= now < body['expires'] or body['expires'] - body['created'] > 30:
                        raise ValueError('Expired or invalid request deadline')
                    result = self.dispatch(body)
                    self.reply(body, result=result)
                except PendingSubmission as exc:
                    self.pending[body['id']] = (body, str(exc))
                except Exception as exc:
                    if body and re.match(r'^[a-f0-9]{32}$', str(body.get('id', ''))):
                        self.reply(body, error=str(exc))
                finally:
                    path.unlink()
        finally:
            self.lock.release()
