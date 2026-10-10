"""直接拉取旧 SDK 所列的公开逐笔下载入口，拒绝把错误页认作行情。"""
from pathlib import Path
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import io
import json
import re

import pandas as pd
import requests

BASE = Path(__file__).resolve().parent
PRODUCTION = Path('/Users/eass/Desktop/A_System')
EARLY = Path('/Users/eass/A_System_复核/2026-10-09_甲临时复核_独立核验/data_recovery/early_research_supplement')
SDK = Path('/Users/eass/A_System_Rebuild/2026-10-06/test_venv/lib/python3.12/site-packages/tushare/stock')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def urls():
    # 地址来自已安装 Tushare 的 trading.py:get_tick_data 和 cons.py。
    result = []
    for provider in ('sina', 'tencent'):
        for code, date in (('601011.SH', '20180807'), ('000001.SZ', '20261008')):
            symbol = ('sh' if code.endswith('.SH') else 'sz') + code[:6]
            dashed = dt.datetime.strptime(date, '%Y%m%d').strftime('%Y-%m-%d')
            if provider == 'sina':
                url = 'https://market.finance.sina.com.cn/downxls.php?date=' + dashed + '&symbol=' + symbol
            else:
                url = 'https://stock.gtimg.cn/data/index.php?appn=detail&action=download&c=' + symbol + '&d=' + date
            result.append({'provider': provider, 'code': code, 'trade_date': date, 'url': url})
    result.append({'provider': 'netease', 'code': '601011.SH', 'trade_date': '20180807',
                   'url': 'https://quotes.money.163.com/cjmx/2018/20180807/0601011.xls'})
    return result


def parse_text(data):
    """只接受时间、价格、手数、金额全有效的六字段下载表。"""
    text = data.decode('gb18030', errors='replace')
    if text.strip() == '服务已下线':
        return None, 'PROVIDER_SERVICE_RETIRED'
    if text.strip() == '暂无数据':
        return None, 'PROVIDER_NO_DATA'
    lower = text.lower()
    if any(value in lower for value in ('<html', '<script', '<!doctype', '<?xml')):
        return None, 'NON_MARKET_DOCUMENT'
    try:
        frame = pd.read_csv(io.StringIO(text), sep='\t', header=0, dtype=str)
    except Exception:
        return None, 'UNPARSEABLE_RESPONSE'
    expected = ['成交时间', '成交价', '价格变动', '成交量(手)', '成交额(元)', '性质']
    if len(frame.columns) != 6 or not len(frame):
        return None, 'NO_VALID_TICK_TABLE'
    frame.columns = expected
    valid_time = frame['成交时间'].str.fullmatch(r'\d{2}:\d{2}:\d{2}', na=False)
    numeric = frame[['成交价', '成交量(手)', '成交额(元)']].apply(pd.to_numeric, errors='coerce')
    if not valid_time.all() or numeric.isna().any().any() or not numeric.gt(0).all().all():
        return None, 'INVALID_TICK_FIELDS'
    return frame, 'TICK_TABLE_CAPTURED_PENDING_CERTIFICATION'


def daily_compare(frame, record):
    path = (EARLY / 'data/raw/daily' / (record['trade_date'] + '.csv')
            if record['trade_date'] < '2020' else PRODUCTION / 'data/raw/daily' / (record['trade_date'] + '.csv'))
    if not path.exists():
        return {'status': 'DAILY_CONTROL_NOT_AVAILABLE'}
    daily = pd.read_csv(path, dtype={'ts_code': str})
    selection = daily.loc[daily.ts_code.eq(record['code'])]
    if len(selection) != 1:
        return {'status': 'DAILY_CONTROL_KEY_ERROR'}
    ordered = frame.sort_values('成交时间', kind='stable')
    prices = pd.to_numeric(ordered['成交价'])
    found = {'open': float(prices.iloc[0]), 'high': float(prices.max()),
             'low': float(prices.min()), 'close': float(prices.iloc[-1])}
    differences = {key: round(value - float(selection.iloc[0][key]), 8) for key, value in found.items()}
    return {'daily_sha256': sha(path.read_bytes()), 'tick_ohlc': found,
            'daily_differences': differences, 'all_ohlc_match': all(abs(x) < 1e-8 for x in differences.values())}


def fetch(record, refresh=False):
    name = record['provider'] + '_' + record['code'] + '_' + record['trade_date']
    receipt = BASE / 'evidence' / (name + '.json')
    if receipt.exists() and not refresh:
        existing = json.loads(receipt.read_text())
        if existing.get('raw_file') and sha((BASE / existing['raw_file']).read_bytes()) != existing['raw_sha256']:
            raise ValueError('已保存的来源响应哈希不符')
        if existing.get('http_status') == 200 and existing['provider'] != 'netease':
            frame, status = parse_text((BASE / existing['raw_file']).read_bytes())
            existing['status'] = status
            existing['parsed_tick_rows'] = len(frame) if frame is not None else 0
            receipt.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + '\n')
        return existing
    result = dict(record, queried_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  request_authenticated=False, source_rows_accepted_for_strategy=0, repaired_price_conflicts=0)
    session = requests.Session()
    session.trust_env = False
    try:
        with session.get(record['url'], timeout=(5, 12), stream=True,
                         headers={'User-Agent': 'Mozilla/5.0'}) as response:
            chunks, count = [], 0
            for chunk in response.iter_content(65536):
                count += len(chunk)
                if count > 16 * 1024 * 1024:
                    raise ValueError('响应超出逐笔原件大小上限')
                chunks.append(chunk)
            data = b''.join(chunks)
            raw = BASE / 'raw' / (name + '.response')
            raw.write_bytes(data)
            raw.chmod(0o600)
            result.update(http_status=response.status_code, final_url=response.url,
                          content_type=response.headers.get('Content-Type'), raw_bytes=len(data),
                          raw_file=str(raw.relative_to(BASE)), raw_sha256=sha(data))
            if response.status_code != 200:
                result.update(status='HTTP_ERROR', parsed_tick_rows=0)
            elif record['provider'] == 'netease':
                # 不把二进制下载文件或 HTML 错误页未经解码直接认作成交。
                result.update(status='BINARY_RESPONSE_PENDING_FORMAT_CHECK' if data.startswith(bytes.fromhex('d0cf11e0a1b11ae1'))
                              else 'NON_MARKET_DOCUMENT', parsed_tick_rows=0)
            else:
                frame, status = parse_text(data)
                result.update(status=status, parsed_tick_rows=len(frame) if frame is not None else 0)
                if frame is not None:
                    candidate = BASE / 'raw' / (name + '.csv')
                    frame.to_csv(candidate, index=False, encoding='utf-8-sig')
                    candidate.chmod(0o600)
                    result.update(candidate_file=str(candidate.relative_to(BASE)), candidate_sha256=sha(candidate.read_bytes()),
                                  daily_control=daily_compare(frame, record),
                                  coverage_certified=False, price_semantics_certified=False)
    except Exception as error:
        result.update(status='NETWORK_OR_READ_ERROR', error_type=type(error).__name__, parsed_tick_rows=0)
    finally:
        session.close()
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh', action='store_true', help='明确请求时才重抓已留存的探测结果')
    args = parser.parse_args()
    (BASE / 'raw').mkdir(parents=True, exist_ok=True)
    (BASE / 'evidence').mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda item: fetch(item, args.refresh), urls()))
    report = {'completed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'queries': results,
              'sdk_source_hashes': {name: sha((SDK / name).read_bytes()) for name in ('trading.py', 'cons.py')},
              'new_historical_tick_rows_captured': sum(x['parsed_tick_rows'] for x in results if x['trade_date'] < '2020'),
              'strategy_rows_accepted': 0, 'production_writes': False, 'credentials_used': False,
              'historical_price_conflicts_repaired': 0}
    (BASE / 'evidence/legacy_tick_sources.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'queries': [{k: x.get(k) for k in ('provider', 'trade_date', 'status', 'http_status', 'parsed_tick_rows')} for x in results],
                      'new_historical_tick_rows_captured': report['new_historical_tick_rows_captured']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
