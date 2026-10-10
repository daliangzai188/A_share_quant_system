"""在隔离目录查询通达信历史分笔；留存协议原件并严格区分采集与认证。"""
from pathlib import Path
import datetime as dt
import hashlib
import json
import sys
import time

import pandas as pd

BASE = Path(__file__).resolve().parent
DEPENDENCY = BASE / 'dependencies/pytdx-1.72'
sys.path.insert(0, str(DEPENDENCY))
from pytdx.hq import TdxHq_API
from pytdx.config.hosts import hq_hosts
from pytdx.parser.get_history_transaction_data import GetHistoryTransactionData

TARGETS = [('601011.SH', '20180807'), ('000610.SZ', '20180807'), ('603598.SH', '20180409'),
           ('000001.SZ', '20261008'), ('601011.SH', '20180805')]
SERVER_NAMES = ('上海电信主站Z1', '招商证券深圳行情', '上证云成都电信一', '上海电信主站Z80')
PAGE_SIZE = 1800
MAX_PAGES = 12


def sha(data):
    return hashlib.sha256(data).hexdigest()


def select_server():
    attempts = []
    for name in SERVER_NAMES:
        host = next(item for item in hq_hosts if item[0] == name)
        api = TdxHq_API(raise_exception=True, auto_retry=False, heartbeat=False)
        started = time.monotonic()
        attempt = {'name': name, 'host': host[1], 'port': host[2]}
        try:
            if not api.connect(host[1], host[2], time_out=3):
                raise RuntimeError('行情连接失败')
            count = api.get_security_count(0)
            if not isinstance(count, int) or count < 1000:
                raise ValueError('服务器证券列表控制返回异常')
            attempt.update(status='READ_ONLY_MARKET_CONNECTION_VERIFIED', security_count_control=count,
                           elapsed_seconds=round(time.monotonic()-started, 3))
            attempts.append(attempt)
            return api, attempt, attempts
        except Exception as error:
            attempt.update(status='CONNECTION_OR_MARKET_CONTROL_FAILED', error_type=type(error).__name__,
                           elapsed_seconds=round(time.monotonic()-started, 3))
            attempts.append(attempt)
            try:
                api.disconnect()
            except Exception:
                pass
    return None, None, attempts


def query(api, code, date):
    result = {'ts_code': code, 'trade_date_requested': date, 'pages': [],
              'queried_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
              'source_time_resolution': 'HH:MM', 'strategy_rows_accepted': 0,
              'price_semantics_certified': False, 'coverage_certified': False}
    rows = []
    original = GetHistoryTransactionData.parseResponse
    captured = []

    def capture(parser, body):
        captured.append(bytes(body))
        return original(parser, body)

    GetHistoryTransactionData.parseResponse = capture
    try:
        seen_page_hashes = set()
        for page in range(MAX_PAGES):
            captured.clear()
            offset = page * PAGE_SIZE
            response = api.get_history_transaction_data(1 if code.endswith('.SH') else 0,
                                                         code[:6], offset, PAGE_SIZE, int(date))
            if not isinstance(response, list) or len(captured) != 1 or len(response) > PAGE_SIZE:
                raise ValueError('分笔回包结构或原件留存不符')
            raw = BASE / 'raw' / f'tdx_{code}_{date}_{offset}.bin'
            raw.write_bytes(captured[0])
            raw.chmod(0o600)
            page_hash = sha(captured[0])
            result['pages'].append({'offset': offset, 'requested_count': PAGE_SIZE, 'rows': len(response),
                                    'file': str(raw.relative_to(BASE)), 'sha256': page_hash})
            if not response:
                result['pagination_terminal_received'] = True
                break
            if page_hash in seen_page_hashes:
                raise ValueError('服务器重复同一页，分页覆盖无法确认')
            seen_page_hashes.add(page_hash)
            rows.extend(response)
        else:
            result['pagination_terminal_received'] = False
        frame = pd.DataFrame(rows, columns=['time', 'price', 'vol', 'buyorsell'])
        if len(frame):
            valid = frame.time.str.fullmatch(r'\d{2}:\d{2}', na=False)
            if not valid.all() or frame.price.le(0).any() or frame.vol.lt(0).any():
                raise ValueError('时间或成交数值异常')
            frame.insert(0, 'trade_date_requested', date)
            frame.insert(0, 'ts_code', code)
            frame['source_row_sequence'] = range(len(frame))
            path = BASE / 'raw' / f'tdx_{code}_{date}.csv'
            frame.to_csv(path, index=False, encoding='utf-8-sig')
            path.chmod(0o600)
            result.update(candidate_file=str(path.relative_to(BASE)), candidate_sha256=sha(path.read_bytes()),
                          first_time=frame.time.min(), last_time=frame.time.max(),
                          prices={'high': float(frame.price.max()), 'low': float(frame.price.min())})
        result.update(status='EMPTY_HISTORICAL_SOURCE' if not len(frame) else 'HISTORY_CAPTURED_PENDING_CERTIFICATION',
                      source_rows_captured=len(frame))
    except Exception as error:
        result.update(status='QUERY_OR_VALIDATION_FAILED', error_type=type(error).__name__,
                      error=str(error)[:200], source_rows_captured=len(rows))
    finally:
        GetHistoryTransactionData.parseResponse = original
    return result


def main():
    for folder in ('raw', 'evidence'):
        (BASE / folder).mkdir(exist_ok=True)
    receipt = BASE / 'evidence/tdx_history_sources.json'
    if receipt.exists():
        print(json.dumps({'status': 'EXISTING_RECEIPT_REUSED', 'file': str(receipt)}, ensure_ascii=False))
        return
    api, server, attempts = select_server()
    report = {'server_attempts': attempts, 'server_used': server, 'queries': [],
              'documentation': 'https://github.com/rainx/pytdx/blob/master/docs/pytdx_hq.md',
              'dependency_sha256': sha((BASE / 'dependencies/pytdx-1.72.tar.gz').read_bytes()),
              'source_parser_sha256': sha((DEPENDENCY / 'pytdx/parser/get_history_transaction_data.py').read_bytes()),
              'production_writes': False, 'broker_account_queries_sent': False,
              'orders_or_cancellations_sent': False, 'strategy_rows_accepted': 0}
    try:
        if api is not None:
            for code, date in TARGETS:
                item = query(api, code, date)
                report['queries'].append(item)
                print(json.dumps({k: item[k] for k in ('ts_code', 'trade_date_requested', 'status', 'source_rows_captured')}, ensure_ascii=False), flush=True)
                if item['status'] == 'QUERY_OR_VALIDATION_FAILED':
                    break
        report['status'] = 'NO_USABLE_MARKET_CONNECTION' if api is None else 'READ_ONLY_MARKET_PROBE_COMPLETED'
    finally:
        if api is not None:
            try:
                api.disconnect()
            except Exception:
                pass
        report['completed_at_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'server_attempts': attempts,
                      'new_historical_source_rows_captured': sum(x['source_rows_captured'] for x in report['queries'] if x['trade_date_requested'] < '2020')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
