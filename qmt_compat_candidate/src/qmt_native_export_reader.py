"""国金 Full 原生自动导出 CSV 的只读、保真读取器。

合同来自 2026-10-19 实机四表表头；不解释状态、买卖标记、数量单位或身份别名。
文件稳定、mtime 新近和 Account 交易日正确，只是本地文件观察，均不能证明柜台
新鲜度、导出完整性、跨表原子性或空仓。此模块不发送委托、不写入源目录。
"""
import codecs
import csv
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import io
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType


HEADERS = MappingProxyType({
    'Account.csv': ('证券公司', '资金账号', '账号名称', '账号启用', '账号备注', '冻结金额', '总资产', '可用金额', '手续费', '盈亏', '总市值', '可取金额', '交易日', '股票总市值', '债券总市值', '基金总市值', '回购总市值', '净资产', '总负债', '账号状态', '账号key', '数据key'),
    'Deal.csv': ('证券公司', '资金账号', '账号名称', '账号启用', '账号备注', '任务编号', '任务来源', '报单来源', '下单方式', '投资备注', '证券市场', '交易市场', '证券代码', '证券名称', '成交编号', '合同编号', '操作', '成交价格', '成交数量', '成交日期', '成交时间', '手续费', '成交金额', '买卖标记', '委托类别', '成交类型', '订单编号', '股东号', '投资备注1', '账号key', '数据key'),
    'Order.csv': ('证券公司', '资金账号', '账号名称', '账号启用', '账号备注', '任务编号', '任务来源', '报单来源', '下单方式', '投资备注', '交易市场', '证券代码', '证券名称', '委托价格', '委托量', '合同编号', '委托状态', '成交数量', '委托剩余量', '冻结金额', '冻结手续费', '委托日期', '委托时间', '成交均价', '已撤数量', '买卖标记', '委托类别', '废单原因', '委托来源', '订单编号', '今日盈亏', '股东号', '账号key', '数据key'),
    'PositionStatics.csv': ('证券公司', '资金账号', '账号名称', '账号启用', '账号备注', '市场代码', '市场名称', '证券代码', '证券名称', '当前拥股', '持仓成本', '成本价', '盈亏', '市值', '股东账号', '可用数量', '在途股份', '盈亏比例', '最新价', '到期日', 'etf申赎', '当日盈亏', '资产占比', '市值占比', '状态', '当日涨幅', '昨夜拥股', '账号key', '数据key'),
})
BEIJING = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc
NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')


class NativeExportReadError(ValueError):
    """仅固定错误码；不包含路径、账号、字段值或底层异常正文。"""


@dataclass(frozen=True)
class NativeExportLimits:
    max_file_bytes: int = 16 * 1024 * 1024
    max_rows: int = 100000
    max_field_chars: int = 4096
    max_cells: int = 1000000

    def __post_init__(self):
        caps = dict(max_file_bytes=64 * 1024 * 1024, max_rows=1000000,
                    max_field_chars=65536, max_cells=5000000)
        if any(type(getattr(self, key)) is not int or not 1 <= getattr(self, key) <= cap
               for key, cap in caps.items()):
            raise NativeExportReadError('EXPORT_LIMITS_INVALID')


@dataclass(frozen=True)
class NativeExportTable:
    name: str
    columns: tuple
    rows: tuple = field(repr=False)
    sha256: str
    size_bytes: int
    mtime_utc: object = None

    @property
    def observed_empty(self):
        return not self.rows

    def decimal(self, row_index, column, *, allow_empty=False):
        """仅显式调用时精确解析；不去空格、不猜千分位、单位或默认零。"""
        if type(row_index) is not int or not 0 <= row_index < len(self.rows) or column not in self.columns:
            raise NativeExportReadError('EXPORT_CELL_REFERENCE_INVALID')
        value = self.rows[row_index][column]
        if value == '' and allow_empty:
            return None
        if len(value) > 128 or not NUMBER.fullmatch(value):
            raise NativeExportReadError('EXPORT_DECIMAL_INVALID')
        try:
            result = Decimal(value)
        except InvalidOperation:
            raise NativeExportReadError('EXPORT_DECIMAL_INVALID') from None
        if not result.is_finite() or abs(result.as_tuple().exponent) > 1000 or abs(result.adjusted()) > 1000:
            raise NativeExportReadError('EXPORT_DECIMAL_UNSUPPORTED')
        return result


@dataclass(frozen=True)
class NativeExportSnapshot:
    tables: object = field(repr=False)
    target_account: str = field(repr=False)
    trade_date: dt.date
    read_started_at: dt.datetime
    read_completed_at: dt.datetime
    native_export_readonly_verified: bool = field(default=True, init=False)
    file_age_verified: bool = field(default=True, init=False)
    broker_freshness_verified: bool = field(default=False, init=False)
    account_query_equivalence_certified: bool = field(default=False, init=False)
    source_equivalence_certified: bool = field(default=False, init=False)
    cross_table_snapshot_verified: bool = field(default=False, init=False)
    position_emptiness_verified: bool = field(default=False, init=False)
    order_emptiness_verified: bool = field(default=False, init=False)
    deal_emptiness_verified: bool = field(default=False, init=False)
    automatic_trading_ready: bool = field(default=False, init=False)

    def safe_summary(self):
        return dict(scope='LOCAL_CSV_SCHEMA_ACCOUNT_DATE_AGE_AND_STABILITY_ONLY',
                    trade_date=self.trade_date.isoformat(),
                    rows={key: len(value.rows) for key, value in self.tables.items()},
                    observed_empty={key: value.observed_empty for key, value in self.tables.items()},
                    native_export_readonly_verified=True, file_age_verified=True,
                    broker_freshness_verified=False, account_query_equivalence_certified=False,
                    source_equivalence_certified=False, cross_table_snapshot_verified=False,
                    position_emptiness_verified=False, order_emptiness_verified=False,
                    deal_emptiness_verified=False, automatic_trading_ready=False)


@dataclass(frozen=True)
class NativeAccountObservation:
    """仅当前本地 Account 文件；不补造任何其他表或柜台完整性证明。"""
    table: NativeExportTable = field(repr=False)
    target_account: str = field(repr=False)
    trade_date: dt.date
    read_started_at: dt.datetime
    read_completed_at: dt.datetime
    local_current_observation: bool = field(default=True, init=False)
    native_export_readonly_verified: bool = field(default=True, init=False)
    file_age_verified: bool = field(default=True, init=False)
    broker_freshness_verified: bool = field(default=False, init=False)
    account_completeness_verified: bool = field(default=False, init=False)
    account_query_equivalence_certified: bool = field(default=False, init=False)
    source_equivalence_certified: bool = field(default=False, init=False)
    cross_table_snapshot_verified: bool = field(default=False, init=False)
    automatic_trading_ready: bool = field(default=False, init=False)

    @property
    def row(self):
        return self.table.rows[0]

    def decimal(self, column, *, allow_empty=False):
        return self.table.decimal(0, column, allow_empty=allow_empty)

    def safe_summary(self):
        return dict(scope='LOCAL_CURRENT_ACCOUNT_FILE_OBSERVATION_ONLY',
                    trade_date=self.trade_date.isoformat(), account_rows=1,
                    local_current_observation=True, native_export_readonly_verified=True,
                    file_age_verified=True, broker_freshness_verified=False,
                    account_completeness_verified=False, account_query_equivalence_certified=False,
                    source_equivalence_certified=False, cross_table_snapshot_verified=False,
                    automatic_trading_ready=False)


def _limits(value):
    if value is None:
        return NativeExportLimits()
    if type(value) is not NativeExportLimits:
        raise NativeExportReadError('EXPORT_LIMITS_INVALID')
    return value


def _trade_date(value):
    if type(value) is dt.date:
        return value
    if type(value) is str and (re.fullmatch(r'[0-9]{8}', value) or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value)):
        try:
            return dt.datetime.strptime(value, '%Y%m%d' if '-' not in value else '%Y-%m-%d').date()
        except ValueError:
            pass
    raise NativeExportReadError('EXPORT_TRADE_DATE_INVALID')


def _utc(value):
    if type(value) is not dt.datetime or value.tzinfo is None or value.utcoffset() is None:
        raise NativeExportReadError('EXPORT_AWARE_CLOCK_REQUIRED')
    return value.astimezone(UTC)


def _csv_grammar(text):
    # Python csv.strict alone accepts a quote in an unquoted field. Reject it.
    mode, index = 'start', 0
    while index < len(text):
        char = text[index]
        if mode == 'quoted':
            if char == '"':
                mode = 'after_quote'
        elif char in '\r\n':
            if char == '\r':
                if index + 1 >= len(text) or text[index + 1] != '\n':
                    raise NativeExportReadError('EXPORT_CSV_LINE_ENDING_INVALID')
                index += 1
            mode = 'start'
        elif mode == 'after_quote':
            if char == '"':
                mode = 'quoted'
            elif char == ',':
                mode = 'start'
            else:
                raise NativeExportReadError('EXPORT_CSV_QUOTE_INVALID')
        elif char == ',':
            mode = 'start'
        elif char == '"':
            if mode != 'start':
                raise NativeExportReadError('EXPORT_CSV_QUOTE_INVALID')
            mode = 'quoted'
        else:
            mode = 'unquoted'
        index += 1
    if mode != 'start':
        raise NativeExportReadError('EXPORT_CSV_TRUNCATED')


def parse_native_export_bytes(name, data, *, encoding, limits=None):
    """同一不可变 bytes 解析；不读取文件，也不认证账户、日期或原生来源。"""
    limits = _limits(limits)
    if name not in HEADERS or type(data) is not bytes:
        raise NativeExportReadError('EXPORT_INPUT_INVALID')
    try:
        if codecs.lookup(encoding).name != 'gb18030':
            raise LookupError
    except (LookupError, TypeError):
        raise NativeExportReadError('EXPORT_ENCODING_CONTRACT_MISMATCH') from None
    if not data or len(data) > limits.max_file_bytes:
        raise NativeExportReadError('EXPORT_FILE_SIZE_INVALID')
    if not data.endswith(b'\n'):
        raise NativeExportReadError('EXPORT_CSV_TRUNCATED')
    try:
        text = data.decode('gb18030', errors='strict')
    except UnicodeError:
        raise NativeExportReadError('EXPORT_ENCODING_INVALID') from None
    _csv_grammar(text)
    reader = csv.reader(io.StringIO(text, newline=''), delimiter=',', quotechar='"', strict=True)
    rows = []
    try:
        header = tuple(next(reader))
        if len(set(header)) != len(header):
            raise NativeExportReadError('EXPORT_DUPLICATE_HEADER')
        if header != HEADERS[name]:
            raise NativeExportReadError('EXPORT_HEADER_MISMATCH')
        for cells in reader:
            if len(cells) != len(header):
                raise NativeExportReadError('EXPORT_ROW_WIDTH_INVALID')
            if (len(rows) >= limits.max_rows or (len(rows) + 1) * len(header) > limits.max_cells):
                raise NativeExportReadError('EXPORT_ROW_OR_CELL_LIMIT_EXCEEDED')
            if any(len(cell) > limits.max_field_chars or any(ord(c) < 32 or ord(c) == 127 for c in cell) for cell in cells):
                raise NativeExportReadError('EXPORT_FIELD_INVALID')
            rows.append(MappingProxyType(dict(zip(header, cells))))
    except (csv.Error, StopIteration):
        raise NativeExportReadError('EXPORT_CSV_INVALID') from None
    return NativeExportTable(name, header, tuple(rows), hashlib.sha256(data).hexdigest(), len(data))


def _open_shared_binary(path):
    if os.name != 'nt':
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            return os.fdopen(descriptor, 'rb')
        except Exception:
            os.close(descriptor)
            raise
    # Read-only handle, sharing read/write/delete so an exporter is never locked
    # out by this observer. Sharing permits concurrent change; stat/bytes reject it.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x80000000, 1 | 2 | 4, None, 3, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise NativeExportReadError('EXPORT_SHARED_OPEN_FAILED')
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, 'rb')
    except Exception:
        os.close(descriptor)
        raise


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _capture(directory, limits, names=None):
    captured = {}
    for name in HEADERS if names is None else names:
        path = directory / name
        before = os.stat(str(path), follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limits.max_file_bytes:
            raise NativeExportReadError('EXPORT_FILE_TYPE_OR_SIZE_INVALID')
        with _open_shared_binary(path) as stream:
            opened = os.fstat(stream.fileno())
            data = stream.read(limits.max_file_bytes + 1)
            closed = os.fstat(stream.fileno())
        after = os.stat(str(path), follow_symlinks=False)
        if any(_signature(value) != _signature(before) for value in (opened, closed, after)):
            raise NativeExportReadError('EXPORT_CHANGED_DURING_READ')
        if len(data) != before.st_size:
            raise NativeExportReadError('EXPORT_FILE_LENGTH_MISMATCH')
        captured[name] = (data, _signature(after))
    return captured


def _read_native_tables(directory, *, target_account, trade_date, encoding,
                        max_age_seconds, clock=None, limits=None, names=None):
    """两种公开 API 共用同一绑定、文件稳定性和最终时点年龄校验。"""
    limits = _limits(limits)
    if (type(target_account) is not str or not target_account or len(target_account) > 128
            or target_account != target_account.strip()
            or any(ord(c) < 33 or ord(c) == 127 for c in target_account)):
        raise NativeExportReadError('EXPORT_TARGET_ACCOUNT_REQUIRED')
    date = _trade_date(trade_date)
    if (type(max_age_seconds) not in (int, float) or not math.isfinite(max_age_seconds)
            or not 0 < max_age_seconds <= 3600):
        raise NativeExportReadError('EXPORT_MAX_AGE_INVALID')
    clock = clock or (lambda: dt.datetime.now(UTC))
    started = _utc(clock())
    try:
        arguments = (Path(directory), limits) if names is None else (Path(directory), limits, names)
        first = _capture(*arguments)
        second = _capture(*arguments)
    except NativeExportReadError:
        raise
    except (OSError, ValueError, TypeError):
        raise NativeExportReadError('EXPORT_FILES_UNAVAILABLE') from None
    if first != second:
        raise NativeExportReadError('EXPORT_INDEPENDENT_READS_DIFFER')
    tables, mtime_ns_by_name = {}, {}
    for name, (data, signature) in second.items():
        table = parse_native_export_bytes(name, data, encoding=encoding, limits=limits)
        if any(row['资金账号'] != target_account for row in table.rows):
            raise NativeExportReadError('EXPORT_ACCOUNT_MISMATCH')
        try:
            mtime = dt.datetime(1970, 1, 1, tzinfo=UTC) + dt.timedelta(microseconds=signature[3] // 1000)
        except (OverflowError, ValueError):
            raise NativeExportReadError('EXPORT_MTIME_INVALID') from None
        mtime_ns_by_name[name] = signature[3]
        tables[name] = NativeExportTable(table.name, table.columns, table.rows,
                                        table.sha256, table.size_bytes, mtime)
    account = tables['Account.csv']
    if len(account.rows) != 1:
        raise NativeExportReadError('EXPORT_ACCOUNT_ROW_COUNT_INVALID')
    if _trade_date(account.rows[0]['交易日']) != date:
        raise NativeExportReadError('EXPORT_ACCOUNT_TRADE_DATE_MISMATCH')
    completed = _utc(clock())
    if completed < started:
        raise NativeExportReadError('EXPORT_CLOCK_MOVED_BACKWARD')
    elapsed = completed - dt.datetime(1970, 1, 1, tzinfo=UTC)
    completed_ns = ((elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds) * 1000
    max_age_ns = Decimal(str(max_age_seconds)) * 1000000000
    for table in tables.values():
        age_ns = completed_ns - mtime_ns_by_name[table.name]
        if age_ns < 0 or age_ns > max_age_ns or table.mtime_utc.astimezone(BEIJING).date() != date:
            raise NativeExportReadError('EXPORT_FILE_AGE_OR_DAY_INVALID')
    return MappingProxyType(tables), date, started, completed


def read_native_exports(directory, *, target_account, trade_date, encoding,
                        max_age_seconds, clock=None, limits=None):
    """两轮独立共享读取四表；任一不稳定、跨账户、日期/年龄不符整体拒绝。"""
    tables, date, started, completed = _read_native_tables(
        directory, target_account=target_account, trade_date=trade_date, encoding=encoding,
        max_age_seconds=max_age_seconds, clock=clock, limits=limits)
    return NativeExportSnapshot(tables, target_account, date, started, completed)


def read_native_account(directory, *, target_account, trade_date, encoding,
                        max_age_seconds, clock=None, limits=None):
    """仅 Account.csv 当前本地观察；不读取、不推断其他三表。"""
    tables, date, started, completed = _read_native_tables(
        directory, target_account=target_account, trade_date=trade_date, encoding=encoding,
        max_age_seconds=max_age_seconds, clock=clock, limits=limits, names=('Account.csv',))
    return NativeAccountObservation(tables['Account.csv'], target_account, date, started, completed)
