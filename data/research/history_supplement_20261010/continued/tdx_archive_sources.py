"""读取通达信公开下载索引，按索引声明的历史范围选择原件；不推猜隐藏接口。"""
from pathlib import Path
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import io
import json
import re
import zipfile

import requests

BASE = Path(__file__).resolve().parent
INDEX_URLS = ('https://www.tdx.com.cn/downit.zip', 'http://www.tdx.com.cn/downit.zip')
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024


def download(url, name):
    report = {'url': url, 'queried_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    session = requests.Session()
    session.trust_env = False
    try:
        with session.get(url, timeout=(5, 15), stream=True) as response:
            report.update(http_status=response.status_code, final_url=response.url,
                          content_type=response.headers.get('Content-Type'))
            chunks, count = [], 0
            for chunk in response.iter_content(65536):
                count += len(chunk)
                if count > MAX_DOWNLOAD_BYTES:
                    raise ValueError('原件超出单日补采大小上限')
                chunks.append(chunk)
            raw = b''.join(chunks)
            path = BASE / 'raw' / name
            path.write_bytes(raw)
            path.chmod(0o600)
            report.update(file=str(path.relative_to(BASE)), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
            if response.status_code != 200:
                report['status'] = 'HTTP_ERROR'
                return report, None
            if not zipfile.is_zipfile(io.BytesIO(raw)):
                report['status'] = 'NON_ARCHIVE_DOCUMENT'
                return report, None
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 512 * 1024 * 1024:
                    raise ValueError('解压大小超过上限')
                if archive.testzip() is not None:
                    raise ValueError('来源压缩包CRC错误')
                report.update(status='PUBLIC_ARCHIVE_CAPTURED', entries=[x.filename for x in archive.infolist()])
            return report, raw
    except Exception as error:
        report.update(status='NETWORK_OR_ARCHIVE_ERROR', error_type=type(error).__name__)
        return report, None
    finally:
        session.close()


def query_market_samples(current_format=False):
    """只查询下载索引实际公开列出的旧版与新版分笔路径。"""
    index_path = BASE / 'evidence/tdx_archive_sources.json'
    source = json.loads(index_path.read_text())
    raw_index = BASE / source['index_requests'][-1]['file']
    if hashlib.sha256(raw_index.read_bytes()).hexdigest() != source['index_requests'][-1]['sha256']:
        raise ValueError('官方公开索引原件哈希不符')
    configuration = (BASE / 'raw/tdx_downit5.cfg').read_bytes()
    if hashlib.sha256(configuration).hexdigest() != source['index_configuration_sha256']:
        raise ValueError('官方公开索引配置哈希不符')
    text = configuration.decode('gb18030')
    paths = dict(re.findall(r'^(PATH\d+)=([^\r\n]*)', text, re.M))
    templates = dict(re.findall(r'^(FILE\d+)=([^\r\n]*)', text, re.M))
    targets = []
    formats = (('12', ('20180807', '20261008')),) if current_format else (
        ('02', ('20180807', '20180409')), ('09', ('20180807',)))
    for number, dates in formats:
        path, template = paths['PATH' + number], templates['FILE' + number]
        if not re.fullmatch(r'products/data/data/[a-z0-9]+/', path) or template != 'YYYYMMDD.zip':
            raise ValueError('官方索引路径或文件模板不符合白名单')
        for date in dates:
            targets.append(('https://www.tdx.com.cn/' + path + template.replace('YYYYMMDD', date),
                            f'tdx_archive_{number}_{date}.response', date, path))
    receipt = BASE / 'evidence' / ('tdx_archive_current_format_sources.json' if current_format else 'tdx_archive_market_sources.json')
    if receipt.exists():
        print(json.dumps({'status': 'EXISTING_MARKET_RECEIPT_REUSED'}, ensure_ascii=False))
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: download(item[0], item[1])[0], targets))
    for result, target in zip(results, targets):
        result.update(trade_date_requested=target[2], index_declared_path=target[3],
                      parsed_tick_rows=0, strategy_rows_accepted=0, price_semantics_certified=False)
    report = {'queries': results, 'download_index_sha256': hashlib.sha256(raw_index.read_bytes()).hexdigest(),
              'production_writes': False, 'strategy_rows_accepted': 0, 'historical_price_conflicts_repaired': 0,
              'completed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--market-samples', action='store_true')
    parser.add_argument('--current-format', action='store_true')
    args = parser.parse_args()
    (BASE / 'raw').mkdir(exist_ok=True)
    (BASE / 'evidence').mkdir(exist_ok=True)
    if args.market_samples or args.current_format:
        query_market_samples(current_format=args.current_format)
        return
    receipt = BASE / 'evidence/tdx_archive_sources.json'
    if receipt.exists():
        print(json.dumps({'status': 'EXISTING_RECEIPT_REUSED'}, ensure_ascii=False))
        return
    records, index = [], None
    for number, url in enumerate(INDEX_URLS):
        result, raw = download(url, f'tdx_download_index_{number}.response')
        records.append(result)
        if raw is not None:
            index = raw
            break
    report = {'index_requests': records, 'market_requests': [], 'production_writes': False,
              'credentials_used': False, 'strategy_rows_accepted': 0, 'source_rows_certified': 0}
    if index is not None:
        with zipfile.ZipFile(io.BytesIO(index)) as archive:
            filename = next((x for x in archive.namelist() if x.lower().endswith('downit5.cfg')), None)
            if filename:
                data = archive.read(filename)
                (BASE / 'raw/tdx_downit5.cfg').write_bytes(data)
                text = data.decode('gb18030', errors='replace')
                # 只记录索引中公开声明的分笔资源和范围，先核验它确实覆盖2018。
                lines = text.splitlines()
                relevant = [i for i, line in enumerate(lines) if re.search(r'2ktic|g3tic|分笔', line, re.I)]
                report['historical_tick_index_sections'] = [lines[max(0, i-4):i+8] for i in relevant]
                report['index_configuration_sha256'] = hashlib.sha256(data).hexdigest()
                report['status'] = 'INDEX_CAPTURED_REQUIRES_RANGE_VERIFICATION'
            else:
                report['status'] = 'INDEX_ARCHIVE_HAS_NO_KNOWN_CONFIGURATION'
    else:
        report['status'] = 'PUBLIC_INDEX_UNAVAILABLE'
    report['completed_at_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
