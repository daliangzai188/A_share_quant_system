"""维修后的只读发布前验收；保留停机标记，仅发送用户要求的验收通知。"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from qmt_inner.engine import ENGINE_REVISION
from src.qmt_inner_start_gate import assert_selected_transport_ready
from src.qmt_single_owner import assert_standalone_qmt_allowed
from scripts.show_trading_status import local_runtime_status
from src.utils.config import load_json_config


def main():
    assert_standalone_qmt_allowed(ROOT, caller='verify_qmt_inner_live_readiness')
    marker = ROOT / '.manual_stop.json'
    before = marker.read_bytes()
    load_dotenv(ROOT / '.env', override=False)
    gate = assert_selected_transport_ready(ROOT)
    if not gate.get('checked') or gate.get('engine_revision') != ENGINE_REVISION:
        raise RuntimeError('Expected live inner engine was not verified')
    from scripts import trading_daemon as daemon
    counts = daemon._validate_local_execution_state_for_startup()
    active = [p for p in daemon.load_positions()
              if p.get('status') in ('open', 'sell_pending')]
    result = dict(time=datetime.now().isoformat(), status='READ_ONLY_READY',
                  gate=gate, local_state=counts, financial_calls=False,
                  runtime=local_runtime_status(load_json_config(ROOT / 'config/config.json')),
                  positions=[dict(ts_code=p['ts_code'], shares=p['shares'],
                      strategy_leg=p.get('strategy_leg'), buy_date=p.get('buy_date'),
                      planned_exit_time=p.get('planned_exit_time')) for p in active])
    if marker.read_bytes() != before:
        raise RuntimeError('Manual stop marker changed')
    from src.notify import notify
    result['notification_service_accepted'] = notify(
        'maintenance_acceptance', '内置QMT修复验收通知',
        '委托、成交和100股分仓已核对。此消息仅验证原通知通道；自动交易仍处于维修停机状态，不涉及买卖或撤单。')
    result['manual_stop_retained'] = marker.read_bytes() == before
    path = ROOT / 'reports/incident_20260917_qmt_order_converter/live_readiness.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['notification_service_accepted'] else 1


if __name__ == '__main__':
    sys.exit(main())
