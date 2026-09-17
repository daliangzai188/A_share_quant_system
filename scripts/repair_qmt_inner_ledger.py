"""维修停机期间用原启动恢复器补账；禁止下单、撤单，不清除停机标记。"""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from qmt_inner.engine import ENGINE_REVISION
from qmt_inner.protocol import FileClient, load_settings
from scripts.qmt_inner_backup import backup
from src.qmt_single_owner import assert_standalone_qmt_allowed
from src.runtime_stop_state import load_manual_stop


def repair_ledger(root=ROOT):
    assert_standalone_qmt_allowed(root, caller='repair_qmt_inner_ledger')
    if load_manual_stop(root) is None:
        raise RuntimeError('Ledger repair requires manual stop')
    marker = root / '.manual_stop.json'
    marker_bytes = marker.read_bytes()
    load_dotenv(root / '.env', override=False)
    client = FileClient(load_settings())
    client.connect()
    revision = client.heartbeat().get('engine_revision', '')
    if revision != ENGINE_REVISION:
        raise RuntimeError('Running engine revision does not match repair source')
    saved = backup(root, require_stopped=True)
    from scripts import trading_daemon as daemon
    from src.qmt_inner_adapter import QMTInnerBrokerAdapter
    from src.qmt_adapter import QMTBrokerAdapter

    def forbidden(*args, **kwargs):
        raise RuntimeError('Financial operation forbidden during ledger repair')

    daemon.setup()
    # Use the original authoritative recovery path, with execution calls
    # explicitly disabled even if a future refactor introduces one.
    with (patch.object(QMTInnerBrokerAdapter, 'place_order', forbidden),
          patch.object(QMTInnerBrokerAdapter, 'cancel_order', forbidden),
          patch.object(QMTBrokerAdapter, 'place_order', forbidden),
          patch.object(QMTBrokerAdapter, 'cancel_order', forbidden)):
        outcome = daemon._recover_trade_execution_state_once()
        if outcome.status != 'PASS':
            raise RuntimeError('Ledger reconciliation blocked: ' + str(asdict(outcome)))
        counts = daemon._validate_local_execution_state_for_startup()
        active = [p for p in daemon.load_positions()
                  if str(p.get('status', '')).lower() in ('open', 'sell_pending')]
    if marker.read_bytes() != marker_bytes:
        raise RuntimeError('Manual stop changed during ledger repair')
    result = dict(status='LEDGER_RECOVERY_PASS_MANUAL_STOP_RETAINED',
                  engine_revision=revision, backup=saved['directory'],
                  recovery=asdict(outcome), local_state=counts,
                  positions=[dict(ts_code=p['ts_code'], strategy_leg=p.get('strategy_leg', ''),
                      shares=p['shares'], buy_date=p.get('buy_date', ''),
                      buy_price=p.get('buy_price', 0),
                      planned_exit_date=p.get('planned_exit_date', ''),
                      planned_exit_time=p.get('planned_exit_time', ''),
                      status=p.get('status', '')) for p in active],
                  financial_calls=False, manual_stop=True)
    output = root / 'reports/incident_20260917_qmt_order_converter/ledger_recovery.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    print(json.dumps(repair_ledger(), ensure_ascii=True))
