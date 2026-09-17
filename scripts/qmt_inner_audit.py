"""只读核对内置 QMT 与原策略分仓、未完成意图；不自动改账或下单。"""
from __future__ import annotations
import collections
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from qmt_inner.protocol import config_path, read_json
from src.qmt_inner_adapter import QMTInnerBrokerAdapter
from src.qmt_single_owner import assert_standalone_qmt_allowed
from src.trade_recovery import normalize_broker_orders, normalize_broker_trades


def audit(root=ROOT):
    assert_standalone_qmt_allowed(root, caller='qmt_inner_audit')
    load_dotenv(root / '.env', override=False)
    positions_path = root / 'data/processed/positions.json'
    local = read_json(positions_path)
    if not isinstance(local, list):
        raise RuntimeError('Original position file must be a list')
    active = [row for row in local if str(row.get('status', '')).lower() in ('open', 'sell_pending')]
    quantities = collections.Counter()
    for row in active:
        quantities[row['ts_code']] += int(row['shares'])
    adapter = QMTInnerBrokerAdapter.from_config({})
    adapter.connect()
    try:
        account = adapter.query_account()
        broker_positions = adapter.query_positions()
        orders = adapter.query_orders()
        trades = adapter.query_trades()
        quotes = adapter.get_full_tick([p.ts_code for p in broker_positions]) if broker_positions else {}
        broker_qty = collections.Counter()
        for row in broker_positions:
            broker_qty[row.ts_code] += row.volume
        differences = [dict(ts_code=code, local=quantities[code], broker=broker_qty[code])
                       for code in sorted(set(quantities) | set(broker_qty))
                       if quantities[code] != broker_qty[code]]
        pending_orders = [o for o in orders if int(o['order_status']) not in (53, 54, 56, 57)]
        database = root / 'data/state/execution_events.sqlite3'
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
            pending_intents = conn.execute("SELECT intent_id,status,ts_code,side,broker_order_id FROM trade_intents WHERE status IN ('PREPARED','SUBMITTING','SUBMITTED','PARTIALLY_FILLED','CANCEL_REQUESTED','RECOVERY_REQUIRED')").fetchall()
        report = dict(time=datetime.now().isoformat(), status='READ_ONLY_AUDIT',
                      server=adapter.server_info, account_suffix=account.account_id[-2:],
                      engine_revision=adapter.client.heartbeat().get('engine_revision', ''),
                      local_active_slices=active, broker_positions=[dict(ts_code=p.ts_code, volume=p.volume,
                          can_use_volume=p.can_use_volume) for p in broker_positions],
                      position_differences=differences, pending_orders=pending_orders,
                      pending_intents=pending_intents, order_count=len(orders), trade_count=len(trades),
                      order_snapshots=normalize_broker_orders(orders),
                      trade_snapshots=normalize_broker_trades(trades),
                      quotes={code: dict(last_price=q.last_price, upper_limit=q.upper_limit,
                          lower_limit=q.lower_limit, volume=q.raw.get('volume'), amount=q.amount,
                          timestamp=q.raw.get('time', q.raw.get('timetag', '')))
                          for code, q in quotes.items()},
                      requires_reconciliation=bool(differences or pending_orders or pending_intents))
        # Private account/state details remain outside the shared project directory.
        output = config_path().parent / 'audits' / (datetime.now().strftime('%Y%m%d_%H%M%S') + '.json')
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
        return dict(status=report['status'], report=str(output), local_active_slices=len(active),
                    differences=len(differences), pending_orders=len(pending_orders),
                    pending_intents=len(pending_intents), requires_reconciliation=report['requires_reconciliation'],
                    engine_revision=report['engine_revision'], order_count=len(orders), trade_count=len(trades),
                    broker_positions=report['broker_positions'],
                    quotes=report['quotes'],
                    orders=[{key: row[key] for key in ('order_id', 'ts_code', 'side',
                        'order_qty', 'filled_qty', 'status_code', 'execution_intent_id')}
                        for row in report['order_snapshots']],
                    trades=[{key: row[key] for key in ('order_id', 'ts_code', 'side',
                        'filled_qty', 'filled_amount', 'trade_count')}
                        for row in report['trade_snapshots']],
                    local_positions=[dict(ts_code=p['ts_code'], shares=p['shares'],
                        strategy_leg=p.get('strategy_leg', ''),
                        planned_exit_date=p.get('planned_exit_date', '')) for p in active])
    finally:
        adapter.disconnect()


if __name__ == '__main__':
    print(json.dumps(audit(), ensure_ascii=True))
