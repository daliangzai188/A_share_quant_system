"""One-shot, GET-only inspection of the existing C:\\A_System sync folder."""
import datetime
import http.client
import json
import ntpath
import os
from pathlib import Path
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

MAX_BODY = 1024 * 1024
COUNT_FIELDS = (
    'globalBytes', 'globalDeleted', 'globalDirectories', 'globalFiles',
    'globalSymlinks', 'globalTotalItems', 'localBytes', 'localDeleted',
    'localDirectories', 'localFiles', 'localSymlinks', 'localTotalItems',
    'inSyncBytes', 'inSyncFiles', 'needBytes', 'needDeletes', 'needDirectories',
    'needFiles', 'needSymlinks', 'needTotalItems', 'pullErrors',
    'receiveOnlyChangedBytes', 'receiveOnlyChangedDeletes',
    'receiveOnlyChangedDirectories', 'receiveOnlyChangedFiles',
    'receiveOnlyChangedSymlinks', 'receiveOnlyTotalItems', 'sequence',
)
STATES = frozenset(('idle', 'scanning', 'scan-waiting', 'scan-paused',
                    'sync-waiting', 'sync-preparing', 'syncing', 'sync-paused',
                    'cleaning', 'clean-waiting', 'error'))
SENSITIVE_NAMES = (
    'secret', 'credential', 'password', 'passwd', 'private', 'token',
    'apikey', 'api_key', 'account', 'asset', 'equity', 'ledger', 'trade_intent',
    'order_intent', 'fill', 'broker_health', 'fund', 'position', 'wallet',
    'balance', 'bank', 'config', 'userdata', 'database',
    '\u51c0\u503c', '\u8d44\u91d1', '\u8d26\u6237', '\u8d26\u53f7',
    '\u5bc6\u7801', '\u5bc6\u94a5', '\u51ed\u636e', '\u6210\u4ea4',
    '\u6301\u4ed3', '\u59d4\u6258', '\u8d26\u672c', '\u6d41\u6c34',
)
SAFE_EXTENSIONS = frozenset(('.py', '.ps1', '.cmd', '.bat', '.md', '.txt', '.json'))


class InspectionError(Exception):
    pass


def local_contract(local):
    path = Path(local) / 'Syncthing' / 'config.xml'
    with path.open('rb') as stream:
        raw = stream.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY or b'<!DOCTYPE' in raw.upper():
        raise InspectionError('EXISTING_CONFIG_UNSUPPORTED')
    root = ET.fromstring(raw)
    gui = root.find('gui')
    if (gui is None or gui.get('enabled', 'true').lower() != 'true'
            or gui.get('tls', 'false').lower() != 'false'
            or (gui.findtext('address') or '').strip() != '127.0.0.1:8384'):
        raise InspectionError('EXISTING_GUI_UNSUPPORTED')
    api_key = (gui.findtext('apikey') or '').strip()
    if not api_key or len(api_key) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
        raise InspectionError('EXISTING_API_AUTH_UNAVAILABLE')
    target = ntpath.normcase(ntpath.normpath('C:\\A_System'))
    folders = [f for f in root.findall('folder')
               if ntpath.normcase(ntpath.normpath(f.get('path', ''))) == target]
    if len(folders) != 1 or not folders[0].get('id'):
        raise InspectionError('EXISTING_PROJECT_FOLDER_NOT_UNIQUE')
    return api_key, folders[0].get('id')


def get_json(api_key, endpoint, folder, extra=None):
    params = {'folder': folder}
    params.update(extra or {})
    connection = http.client.HTTPConnection('127.0.0.1', 8384, timeout=5)
    try:
        connection.request('GET', endpoint + '?' + urllib.parse.urlencode(params),
                           headers={'X-API-Key': api_key, 'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status in (401, 403):
            raise InspectionError('LOCAL_REST_AUTH_DENIED')
        if response.status != 200:
            raise InspectionError('LOCAL_REST_HTTP_FAILURE')
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise InspectionError('LOCAL_REST_BODY_TOO_LARGE')
        result = json.loads(raw.decode('utf-8'))
        if not isinstance(result, dict):
            raise InspectionError('LOCAL_REST_SCHEMA_UNSUPPORTED')
        return result
    finally:
        connection.close()


def safe_relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        return None
    value = value.replace('\\', '/')
    if (ntpath.splitdrive(value)[0] or value.startswith('/') or ':' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or re.search(r'\d{9,}', value)):
        return None
    parts = value.split('/')
    if any(not p or p in ('.', '..') or p.startswith('.') for p in parts):
        return None
    lowered = value.casefold()
    if (any(name in lowered for name in SENSITIVE_NAMES)
            or ntpath.splitext(value)[1].lower() not in SAFE_EXTENSIONS):
        return None
    return value


def error_class(value):
    value = str(value).casefold()
    groups = (
        ('ACCESS_DENIED', ('permission denied', 'access denied', 'not permitted')),
        ('FILE_IN_USE', ('being used by another process', 'sharing violation', 'file is in use', 'resource busy')),
        ('DISK_FULL', ('no space left', 'disk full', 'not enough space')),
        ('INVALID_NAME', ('invalid filename', 'invalid name', 'filename syntax', 'not valid filename')),
        ('PATH_NOT_FOUND', ('no such file', 'cannot find', 'not found')),
        ('IO_TIMEOUT', ('timed out', 'timeout')),
        ('FILE_CHANGED', ('modified during', 'changed during')),
    )
    for label, patterns in groups:
        if any(pattern in value for pattern in patterns):
            return label
    return 'UNCLASSIFIED'


def inspect(local, report):
    api_key, folder = local_contract(local)
    status = get_json(api_key, '/rest/db/status', folder)
    report['status'] = {key: value if type(value) is int and value >= 0 else None
                        for key in COUNT_FIELDS for value in (status.get(key),)}
    report['status']['state'] = status.get('state') if status.get('state') in STATES else 'UNKNOWN'
    need = get_json(api_key, '/rest/db/need', folder, {'page': 1, 'perpage': 25})
    rows = []
    for section in ('progress', 'queued', 'rest'):
        items = need.get(section)
        if not isinstance(items, list):
            raise InspectionError('LOCAL_NEED_SCHEMA_UNSUPPORTED')
        rows.extend((section, row) for row in items)
    if len(rows) > 25:
        raise InspectionError('LOCAL_NEED_PAGE_LIMIT_EXCEEDED')
    report['need_page_rows'] = len(rows)
    for section, row in rows:
        if not isinstance(row, dict):
            raise InspectionError('LOCAL_NEED_SCHEMA_UNSUPPORTED')
        name = safe_relative_path(row.get('name'))
        if name is None:
            report['need_paths_suppressed'] += 1
            continue
        size = row.get('size')
        report['need_paths'].append({'path': name, 'section': section,
                                     'bytes': size if type(size) is int and size >= 0 else None})
    if report['status']['pullErrors'] or report['status']['state'] == 'error':
        errors = get_json(api_key, '/rest/folder/errors', folder, {'page': 1, 'perpage': 25}).get('errors')
        if not isinstance(errors, list) or len(errors) > 25:
            raise InspectionError('LOCAL_ERRORS_SCHEMA_UNSUPPORTED')
        report['error_page_rows'] = len(errors)
        for row in errors:
            if not isinstance(row, dict):
                raise InspectionError('LOCAL_ERRORS_SCHEMA_UNSUPPORTED')
            label = error_class(row.get('error', ''))
            report['error_classes'][label] = report['error_classes'].get(label, 0) + 1
    report['result'] = 'PASS'


def main():
    report = {'schema_version': 1, 'scope': 'EXISTING_PROJECT_SYNC_GET_ONLY',
              'observed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'result': 'FAIL', 'error_code': None, 'error_type': None, 'status': {},
              'need_page_rows': None, 'need_paths': [], 'need_paths_suppressed': 0,
              'need_paths_limit': 25, 'error_page_rows': None, 'error_classes': {},
              'configuration_changed': False, 'processes_restarted': False,
              'folder_sync_certified': False, 'report_saved': False}
    local = os.environ.get('LOCALAPPDATA')
    try:
        if sys.platform != 'win32' or not local:
            raise InspectionError('WINDOWS_LOCAL_PROFILE_REQUIRED')
        inspect(local, report)
    except Exception as exc:
        report['error_code'] = str(exc) if isinstance(exc, InspectionError) else 'LOCAL_INSPECTION_FAILED'
        report['error_type'] = type(exc).__name__
    try:
        if local and sys.platform == 'win32':
            destination = Path(local) / 'A_System' / 'sync_tools' / 'sync_acceptance.json'
            report['report_saved'] = True
            destination.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
    except Exception:
        report['report_saved'] = False
        report['result'] = 'FAIL'
        report['error_code'] = 'PRIVATE_REPORT_SAVE_FAILED'
    print(json.dumps(report, ensure_ascii=True, separators=(',', ':')))
    return 0 if report['result'] == 'PASS' and report['report_saved'] else 1


if __name__ == '__main__':
    sys.exit(main())
