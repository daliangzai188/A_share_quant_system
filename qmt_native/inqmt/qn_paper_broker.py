# coding: utf-8
# Paper broker with QMT's trading API surface (get_trade_detail_data / passorder / cancel).
# Shadow mode inside QMT: the unchanged daemon and qmt_inner engine run their live logic,
# orders are filled here against real QMT quotes instead of reaching the exchange.
# Offline: the same class backs the mock QMT used for local integration tests.
import datetime as _pb_dt
import threading as _pb_thr

_PB_BJ = _pb_dt.timezone(_pb_dt.timedelta(hours=8))


class _PbRow(object):
    def __init__(self, **values):
        self.__dict__.update(values)


class QnPaperBroker(object):
    """Order status codes follow QMT: 50 reported, 54 cancelled, 55 part filled, 56 filled, 57 rejected."""

    def __init__(self, account_id, cash, quote_fn, now_fn=None, fee_rate=0.00025, min_fee=5.0, stamp_rate=0.0005):
        self.account_id = str(account_id)
        self.cash = float(cash)
        self.frozen = 0.0
        self.positions = {}  # code -> dict(volume, today_buy, cost)
        self.orders = []
        self.deals = []
        self.quote_fn = quote_fn
        self.now_fn = now_fn or (lambda: _pb_dt.datetime.now(_PB_BJ))
        self.fee_rate, self.min_fee, self.stamp_rate = fee_rate, min_fee, stamp_rate
        self.lock = _pb_thr.RLock()
        self._seq = 0
        self.day = self.now_fn().strftime('%Y%m%d')
        self.log = []

    # ------------------------------------------------------------------ helpers
    def _roll_day(self):
        today = self.now_fn().strftime('%Y%m%d')
        if today != self.day:
            self.day = today
            for p in self.positions.values():
                p['today_buy'] = 0
            for o in self.orders:
                if o['status'] in (50, 55):
                    o['status'] = 54 if o['traded'] == 0 else 53
            self.frozen = 0.0

    @staticmethod
    def _split(code):
        num, exch = code.split('.')
        return num, exch

    def _fee(self, amount, side):
        fee = max(amount * self.fee_rate, self.min_fee)
        if side == 'SELL':
            fee += amount * self.stamp_rate
        return fee

    # ------------------------------------------------------------------ QMT API
    def get_trade_detail_data(self, account, market, kind):
        with self.lock:
            self._roll_day()
            if str(account) != self.account_id:
                return []
            if kind == 'account':
                mv = 0.0
                for code, p in self.positions.items():
                    q = self.quote_fn(code) or {}
                    mv += p['volume'] * float(q.get('lastPrice') or p['cost'])
                return [_PbRow(m_strAccountID=self.account_id, m_dAvailable=self.cash - self.frozen,
                               m_dBalance=self.cash + mv, m_dInstrumentValue=mv, m_dFrozenCash=self.frozen)]
            if kind == 'position':
                out = []
                for code, p in sorted(self.positions.items()):
                    if p['volume'] <= 0:
                        continue
                    num, exch = self._split(code)
                    q = self.quote_fn(code) or {}
                    price = float(q.get('lastPrice') or p['cost'])
                    out.append(_PbRow(m_strAccountID=self.account_id, m_strInstrumentID=num, m_strExchangeID=exch,
                                      m_nVolume=p['volume'], m_nCanUseVolume=p['volume'] - p['today_buy'],
                                      m_dOpenPrice=p['cost'], m_dMarketValue=p['volume'] * price,
                                      m_strInstrumentName=''))
                return out
            if kind == 'order':
                return [_PbRow(**o['row']) for o in self.orders]
            if kind == 'deal':
                return [_PbRow(**d) for d in self.deals]
            return []

    def passorder(self, op_type, order_type, account, code, price_type, price, volume, strategy, quick, remark, C=None):
        with self.lock:
            self._roll_day()
            self._seq += 1
            side = 'BUY' if int(op_type) == 23 else 'SELL'
            num, exch = self._split(code)
            now = self.now_fn()
            sysid = 'P%s%06d' % (now.strftime('%H%M%S'), self._seq)
            q = self.quote_fn(code) or {}
            limit = float(price) if int(price_type) == 11 else float(q.get('lastPrice') or 0)
            row = dict(m_strAccountID=self.account_id, m_strInstrumentID=num, m_strExchangeID=exch,
                       m_nOpType=int(op_type), m_nOffsetFlag=48 if side == 'BUY' else 49,
                       m_nOrderStatus=50, m_nVolumeTotalOriginal=int(volume), m_nVolumeTraded=0,
                       m_dTradedPrice=0.0, m_dLimitPrice=limit, m_strInsertTime=now.strftime('%H%M%S'),
                       m_strOrderSysID=sysid, m_strOrderRef=sysid, m_strRemark=str(remark), m_strSource=str(strategy))
            order = dict(row=row, side=side, code=code, limit=limit, volume=int(volume), traded=0, status=50)
            reject = ''
            if side == 'BUY':
                need = limit * int(volume)
                if need + self._fee(need, side) > self.cash - self.frozen:
                    reject = 'cash'
                else:
                    self.frozen += need + self._fee(need, side)
            else:
                p = self.positions.get(code)
                if not p or p['volume'] - p['today_buy'] < int(volume):
                    reject = 'position'
            if reject:
                order['status'] = row['m_nOrderStatus'] = 57
            self.orders.append(order)
            self.log.append('%s passorder %s %s %d @ %.3f -> %s %s' % (now.strftime('%H:%M:%S'), side, code, volume,
                                                                     limit, sysid, reject or 'reported'))
            return None

    def cancel(self, sysid, account, market, C=None):
        with self.lock:
            for o in self.orders:
                if o['row']['m_strOrderSysID'] == str(sysid) and o['status'] in (50, 55):
                    self._release(o)
                    o['status'] = o['row']['m_nOrderStatus'] = 54 if o['traded'] == 0 else 53
                    self.log.append('cancel %s' % sysid)
                    return True
            return False

    def _release(self, o):
        if o['side'] == 'BUY':
            rest = (o['volume'] - o['traded']) * o['limit']
            self.frozen = max(0.0, self.frozen - rest - self._fee(rest, 'BUY'))

    # ------------------------------------------------------------------ matching
    def match(self, auction_open=False):
        """Fill open orders against the latest quote: buys at or above the last price, sells at
        or below it, at the order's limit (conservative)."""
        fills = []
        with self.lock:
            self._roll_day()
            now = self.now_fn()
            t = now.strftime('%H%M%S')
            if not ('092500' <= t <= '113000' or '130000' <= t <= '150000'):
                return fills
            for o in self.orders:
                if o['status'] not in (50, 55):
                    continue
                q = self.quote_fn(o['code']) or {}
                last = float(q.get('lastPrice') or 0)
                if last <= 0:
                    continue
                ok = (o['side'] == 'BUY' and last <= o['limit'] + 1e-9) or (o['side'] == 'SELL' and last >= o['limit'] - 1e-9)
                if not ok:
                    continue
                qty = o['volume'] - o['traded']
                price = last if o['side'] == 'BUY' else max(last, o['limit'])
                price = min(price, o['limit']) if o['side'] == 'BUY' else price
                amount = qty * price
                fee = self._fee(amount, o['side'])
                p = self.positions.setdefault(o['code'], dict(volume=0, today_buy=0, cost=price))
                if o['side'] == 'BUY':
                    self._release(o)
                    self.cash -= amount + fee
                    p['cost'] = (p['cost'] * p['volume'] + amount) / float(p['volume'] + qty)
                    p['volume'] += qty
                    p['today_buy'] += qty
                else:
                    self.cash += amount - fee
                    p['volume'] -= qty
                o['traded'] += qty
                o['status'] = o['row']['m_nOrderStatus'] = 56
                o['row']['m_nVolumeTraded'] = o['traded']
                o['row']['m_dTradedPrice'] = price
                self._seq += 1
                num, exch = self._split(o['code'])
                deal = dict(m_strAccountID=self.account_id, m_strInstrumentID=num, m_strExchangeID=exch,
                            m_nOpType=o['row']['m_nOpType'], m_nOffsetFlag=o['row']['m_nOffsetFlag'],
                            m_strTradeID='T%s%06d' % (t, self._seq), m_nVolume=qty, m_dPrice=price,
                            m_strTradeTime=t, m_strTradeDate=self.day, m_strRemark=o['row']['m_strRemark'],
                            m_strOrderSysID=o['row']['m_strOrderSysID'], m_dComssion=fee)
                self.deals.append(deal)
                fills.append(deal)
                self.log.append('%s fill %s %s %d @ %.3f' % (now.strftime('%H:%M:%S'), o['side'], o['code'], qty, price))
        return fills

    def snapshot(self):
        with self.lock:
            return dict(account_id=self.account_id, cash=self.cash, positions=self.positions, day=self.day,
                        orders=[o['row'] for o in self.orders[-200:]], deals=self.deals[-200:])
