"""读取现有心跳和日志观察5分钟，不建立券商查询连接，不发单撤单。"""
from datetime import datetime
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from qmt_inner.engine import ENGINE_REVISION
from qmt_inner.protocol import FileClient, load_settings, atomic_json
from scripts.show_trading_status import local_runtime_status
from src.utils.config import load_json_config


def main():
    load_dotenv(ROOT / '.env', override=False)
    from scripts import trading_daemon as daemon
    client = FileClient(load_settings())
    samples = []
    started = time.monotonic()
    output = ROOT / 'reports/incident_20260917_qmt_order_converter/runtime_observation.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    for index in range(12):
        runtime = local_runtime_status(load_json_config(ROOT / 'config/config.json'))
        pid = runtime.get('daemon_pid')
        health = daemon._periodic_health_snapshot(current_pid=pid or -1)
        sample = dict(time=datetime.now().isoformat(), elapsed_sec=time.monotonic()-started,
                      runtime=runtime, health_ok=health['ok'], errors=health['errors'],
                      broker_pid=health['broker_pid'], broker_status=health['broker_status'],
                      broker_age_sec=(health['broker_age_sec']
                          if math.isfinite(health['broker_age_sec']) else None))
        try:
            hb = client.heartbeat()  # Only read signed heartbeat, no RPC/connect.
            sample['engine_revision'] = hb.get('engine_revision', '')
            sample['engine_instance'] = hb.get('instance', '')
            sample['inner_heartbeat_ok'] = bool(sample['engine_instance']
                                               and sample['engine_revision'] == ENGINE_REVISION)
        except Exception as exc:
            sample['inner_heartbeat_ok'] = False
            sample['errors'].append('Inner heartbeat: ' + type(exc).__name__)
        sample['ok'] = bool(health['ok'] and runtime['daemon_running']
                            and runtime['keeper_running'] and not runtime['manual_stop']
                            and sample['inner_heartbeat_ok'])
        pov = json.loads((ROOT / 'data/state/pov_state.json').read_text(encoding='utf-8-sig'))
        sample['pov'] = dict(date=pov.get('date'), items=[dict(
            ts_code=it.get('ts_code'), done=it.get('done'), finalized=it.get('finalized'),
            confirmed_actual_amount=it.get('confirmed_actual_amount'),
            target_actual_amount=it.get('target_actual_amount'), done_reason=it.get('done_reason'))
            for it in pov.get('items', [])])
        samples.append(sample)
        log_path = ROOT / 'logs/trading_daemon.log'
        with log_path.open('rb') as stream:
            stream.seek(max(0, log_path.stat().st_size - 150000))
            log = stream.read().decode('utf-8', errors='replace')
        boundary = log.rfind('A_System 守护进程启动')
        current_log = log[boundary:] if boundary >= 0 else ''
        evidence = [line for line in current_log.splitlines() if any(k in line for k in (
            '交易恢复门禁通过', '启动检查：扫描逾期', '现恢复普通买入POV',
            'POV平滑执行线程启动', '已有持仓，跳过D监控',
            'notify: 已推送 [connection]', 'notify: 已推送 [buy_result]',
            'POV恢复', 'POV恢复收口', '10:30'))]
        startup_gate = '交易恢复门禁通过' in current_log
        ready_notice = 'notify: 已推送 [connection]' in current_log and '程序与账户已恢复正常' in current_log
        failures = sum(' | ERROR | ' in line or ' | CRITICAL | ' in line
                       for line in current_log.splitlines())
        stable = len({s['runtime']['daemon_pid'] for s in samples}) == 1
        keeper_stable = len({s['runtime']['keeper_pid'] for s in samples}) == 1
        inner_stable = len({s.get('engine_instance') for s in samples}) == 1
        complete = index == 11
        passed = bool(complete and all(s['ok'] for s in samples) and stable and keeper_stable
                      and inner_stable and startup_gate and ready_notice and failures == 0)
        report = dict(status=('PASS_5_MINUTE_OBSERVATION' if passed else
                             'FAILED_OBSERVATION' if complete else 'OBSERVING'),
                      financial_calls=False, samples=samples, daemon_pid_stable=stable,
                      keeper_pid_stable=keeper_stable, inner_instance_stable=inner_stable,
                      startup_gate_passed=startup_gate, ready_notification_service_accepted=ready_notice,
                      error_log_count=failures, log_evidence=evidence)
        atomic_json(output, report)
        print(json.dumps(sample, ensure_ascii=True), flush=True)
        if not complete:
            time.sleep(30)
    print(json.dumps(dict(status=report['status'], samples=len(samples)), ensure_ascii=True), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
