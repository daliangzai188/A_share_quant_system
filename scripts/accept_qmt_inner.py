"""目标 Windows 的只读联调报告；不调用下单、撤单或修改策略状态。"""
import json
from datetime import datetime
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_qmt_inner import probe
from scripts.qmt_inner_audit import audit
from src.qmt_inner_adapter import QMTInnerBrokerAdapter
from src.qmt_market_data import InnerMarketData


def main():
    report = dict(time=datetime.now().isoformat(), status='FAILED', financial_calls=False)
    try:
        report['probe'] = probe()
        report['audit'] = audit()
        adapter = QMTInnerBrokerAdapter.from_config({})
        adapter.connect()
        try:
            report['server_instance'] = adapter.client.instance
            positions = adapter.query_positions()
            codes = [p.ts_code for p in positions][:1]
            if codes:
                date = datetime.now().strftime('%Y%m%d')
                start, end = date + '093000', date + '113000'
                bars = adapter.get_minute_bars(codes, start_time=start, end_time=end)
                report['minutes'] = {code: dict(count=len(rows), first=rows[0]['bar_time'] if rows else None,
                    last=rows[-1]['bar_time'] if rows else None) for code, rows in bars.items()}
                market = InnerMarketData()
                frames = market.get_market_data_ex(['open', 'high', 'low', 'close', 'volume', 'amount'],
                    codes, period='1m', start_time=start, end_time=end)
                report['history'] = {code: dict(rows=len(frame), columns=list(frame.columns),
                    missing_values=int(frame.isna().sum().sum())) for code, frame in frames.items()}
        finally:
            adapter.disconnect()
        report['status'] = 'READ_ONLY_CHECKED'
    except Exception:
        report['error'] = traceback.format_exc()
    output = ROOT / 'reports/qmt_inner_migration/native_acceptance.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report['status'] == 'READ_ONLY_CHECKED' else 1


if __name__ == '__main__':
    sys.exit(main())
