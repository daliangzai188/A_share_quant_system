"""One-shot native Account observation; four-table acceptance stays independent."""
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

READER_SHA = 'e775a2d9e7130dff2c1547e6079062ce933a3dbd5dda73344e7030cf97c2b8ee'
PROTOCOL_SHA = '8c5ba839faebabd98512642ffc401b29d6e4d09d43794fb2727bdd7ec6b63141'

def module(name, path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError('PUBLIC_MODULE_MISMATCH')
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

def main():
    if sys.platform != 'win32':
        raise SystemExit('WINDOWS_ONLY')
    local = Path(os.environ['LOCALAPPDATA']) / 'A_System'
    directory = local / 'native_broker_snapshots' / 'Stock'
    report = dict(scope='NATIVE_ACCOUNT_TARGET_LOCAL_OBSERVATION_ONLY', result='FAIL',
                  observed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  calls=[], sends_orders=False, cancels_orders=False,
                  automatic_trading_ready=False, account_query_equivalence_certified=False,
                  broker_freshness_verified=False, account_completeness_verified=False,
                  source_equivalence_certified=False, cross_table_snapshot_verified=False,
                  position_emptiness_verified=False, order_emptiness_verified=False,
                  deal_emptiness_verified=False, four_table_local_read_pass=False)
    try:
        reader = module('native_export_candidate_acceptance',
                        Path(__file__).resolve().parent.parent / 'src' / 'qmt_native_export_reader.py', READER_SHA)
        cfg = json.loads((local / 'qmt_inner' / 'config.json').read_text(encoding='utf-8-sig'))
        target = cfg.get('account_id')
        if not isinstance(target, str) or not target.strip():
            raise ValueError('EXISTING_ACCOUNT_BINDING_MISSING')
        day = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
        before = reader.read_native_account(directory, target_account=target, trade_date=day,
                                           encoding='gb18030', max_age_seconds=30)
        report['native_account_before'] = before.safe_summary()
        # Independently attempt the unchanged, strict four-file contract. An old
        # header-only order/deal file must remain a failure, not an empty account.
        try:
            four = reader.read_native_exports(directory, target_account=target, trade_date=day,
                                              encoding='gb18030', max_age_seconds=30)
            report['four_table_local_read_pass'] = True
            report['four_table_local_observation'] = four.safe_summary()
        except reader.NativeExportReadError as exc:
            report['four_table_local_error_code'] = str(exc)
        try:
            protocol = module('existing_readonly_bridge_acceptance',
                              Path('C:/A_System/qmt_inner/protocol.py'), PROTOCOL_SHA)
            client = protocol.FileClient(cfg)
            client.connect()
            report['calls'].append('hello')
            asset = client.call('account', {})
            report['calls'].append('account')
            cents = lambda value: Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            pairs = (('available_cash', '\u53ef\u7528\u91d1\u989d'),
                     ('total_asset', '\u603b\u8d44\u4ea7'), ('market_value', '\u603b\u5e02\u503c'))
            report['current_account_amounts_equal_at_cents'] = {
                key: cents(asset[key]) == cents(before.decimal(column))
                for key, column in pairs}
        except Exception as exc:
            report['existing_query_error_type'] = type(exc).__name__
        time.sleep(11)
        after = reader.read_native_account(directory, target_account=target, trade_date=day,
                                          encoding='gb18030', max_age_seconds=30)
        report['account_mtime_advanced_over_11_seconds'] = after.table.mtime_utc > before.table.mtime_utc
        report['native_account_after'] = after.safe_summary()
        report['result'] = 'LOCAL_ACCOUNT_READ_PASS'
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        allowed = ('EXPORT_', 'PUBLIC_MODULE_', 'EXISTING_ACCOUNT_')
        report['error_code'] = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(allowed) else 'TARGET_READ_UNVERIFIED'
    report['finished_at_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    destination = local / 'native_broker_snapshots' / 'account_observation_acceptance.json'
    destination.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, separators=(',', ':')))
    return 0 if report['result'] == 'LOCAL_ACCOUNT_READ_PASS' else 1

if __name__ == '__main__':
    sys.exit(main())
