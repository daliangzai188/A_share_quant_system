import contextlib
import csv
import datetime as dt
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src import qmt_native_export_reader as reader


ACCOUNT = '00000001'
NOW = dt.datetime(2026, 10, 19, 7, 50, tzinfo=dt.timezone.utc)


def csv_bytes(name, rows=(), header=None):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\r\n')
    fields = header if header is not None else reader.HEADERS[name]
    writer.writerow(fields)
    for row in rows:
        writer.writerow([row.get(field, '') for field in fields] if isinstance(row, dict) else row)
    return stream.getvalue().encode('gb18030')


class NativeExportFixture:
    def __init__(self, directory, now=NOW):
        self.directory, self.now = Path(directory), now
        self.rows = {name: [] for name in reader.HEADERS}
        self.rows['Account.csv'] = [{'资金账号': ACCOUNT, '交易日': '20261019',
                                     '总资产': '100000.10000000000000000001'}]
        self.write()

    def write(self):
        for name, rows in self.rows.items():
            self.write_one(name, csv_bytes(name, rows))

    def write_one(self, name, data):
        path = self.directory / name
        path.write_bytes(data)
        os.utime(path, (self.now.timestamp() - 1, self.now.timestamp() - 1))

    def read(self, **kwargs):
        params = dict(target_account=ACCOUNT, trade_date='20261019', encoding='gb18030',
                      max_age_seconds=30, clock=lambda: self.now)
        params.update(kwargs)
        return reader.read_native_exports(self.directory, **params)

    def read_account(self, **kwargs):
        params = dict(target_account=ACCOUNT, trade_date='20261019', encoding='gb18030',
                      max_age_seconds=30, clock=lambda: self.now)
        params.update(kwargs)
        return reader.read_native_account(self.directory, **params)


class TestQmtNativeExportReader(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = NativeExportFixture(self.temporary.name)

    def assert_code(self, code, operation):
        with self.assertRaises(reader.NativeExportReadError) as caught:
            operation()
        self.assertEqual(str(caught.exception), code)

    def test_real_header_contract_and_empty_tables_do_not_certify_empty_account(self):
        snapshot = self.fixture.read()
        self.assertEqual({name: len(table.columns) for name, table in snapshot.tables.items()},
                         {'Account.csv': 22, 'Deal.csv': 31, 'Order.csv': 34, 'PositionStatics.csv': 29})
        self.assertTrue(snapshot.native_export_readonly_verified)
        self.assertTrue(snapshot.file_age_verified)
        summary = snapshot.safe_summary()
        self.assertEqual(summary['rows']['Account.csv'], 1)
        self.assertTrue(summary['observed_empty']['PositionStatics.csv'])
        for key in ('broker_freshness_verified', 'source_equivalence_certified',
                    'cross_table_snapshot_verified', 'position_emptiness_verified',
                    'order_emptiness_verified', 'deal_emptiness_verified', 'automatic_trading_ready'):
            self.assertFalse(summary[key])
        self.assertNotIn(ACCOUNT, json.dumps(summary))
        self.assertNotIn(ACCOUNT, repr(snapshot))

    def test_strings_leading_zero_ids_and_exact_decimal_and_quoted_text(self):
        self.fixture.rows['Order.csv'] = [{'资金账号': ACCOUNT, '证券代码': '000001',
                                          '合同编号': '0000000000009', '委托价格': '0.100000000000000000001',
                                          '投资备注': '原样,含"引号"', '证券名称': '样本股份'}]
        self.fixture.write()
        table = self.fixture.read().tables['Order.csv']
        self.assertEqual(table.rows[0]['证券代码'], '000001')
        self.assertEqual(table.rows[0]['合同编号'], '0000000000009')
        self.assertEqual(table.rows[0]['投资备注'], '原样,含"引号"')
        self.assertTrue(all(type(value) is str for value in table.rows[0].values()))
        self.assertEqual(table.decimal(0, '委托价格'), Decimal('0.100000000000000000001'))
        with self.assertRaises(TypeError):
            table.rows[0]['证券代码'] = 'changed'
        self.assertIsNone(table.decimal(0, '成交均价', allow_empty=True))
        self.assert_code('EXPORT_DECIMAL_INVALID', lambda: table.decimal(0, '成交均价'))

    def test_decimal_never_guesses_units_whitespace_or_nonfinite(self):
        for value in (' 1', '1,000', 'NaN', 'Infinity', '1e1001'):
            with self.subTest(value=value):
                table = reader.parse_native_export_bytes('Account.csv', csv_bytes('Account.csv', [{'总资产': value}]), encoding='gb18030')
                with self.assertRaises(reader.NativeExportReadError):
                    table.decimal(0, '总资产')

    def test_mixed_or_other_account_fails_entire_snapshot_without_exposing_account(self):
        for name in reader.HEADERS:
            with self.subTest(name=name):
                self.fixture.rows = {key: [] for key in reader.HEADERS}
                self.fixture.rows['Account.csv'] = [{'资金账号': ACCOUNT, '交易日': '20261019'}]
                self.fixture.rows[name].append({'资金账号': 'OTHER_ACCOUNT', '交易日': '20261019'})
                self.fixture.write()
                self.assert_code('EXPORT_ACCOUNT_MISMATCH', self.fixture.read)

    def test_explicit_target_account_and_single_account_row_required(self):
        for value in (None, 1, '', ' ' + ACCOUNT, ACCOUNT + '\n'):
            with self.subTest(value=value):
                self.assert_code('EXPORT_TARGET_ACCOUNT_REQUIRED', lambda: self.fixture.read(target_account=value))
        for count in (0, 2):
            self.fixture.rows['Account.csv'] = [{'资金账号': ACCOUNT, '交易日': '20261019'}] * count
            self.fixture.write()
            self.assert_code('EXPORT_ACCOUNT_ROW_COUNT_INVALID', self.fixture.read)

    def test_account_trade_date_exact_and_iso_supported_without_changing_raw_value(self):
        self.fixture.rows['Account.csv'][0]['交易日'] = '2026-10-19'
        self.fixture.write()
        self.assertEqual(self.fixture.read().tables['Account.csv'].rows[0]['交易日'], '2026-10-19')
        self.fixture.rows['Account.csv'][0]['交易日'] = '20261018'
        self.fixture.write()
        self.assert_code('EXPORT_ACCOUNT_TRADE_DATE_MISMATCH', self.fixture.read)
        self.assert_code('EXPORT_TRADE_DATE_INVALID', lambda: self.fixture.read(trade_date='2026/10/19'))

    def test_header_extra_reorder_and_duplicate_are_not_accepted(self):
        name = 'Order.csv'
        headers = list(reader.HEADERS[name])
        for changed, code in ((headers + ['未知列'], 'EXPORT_HEADER_MISMATCH'),
                              (list(reversed(headers)), 'EXPORT_HEADER_MISMATCH'),
                              (headers[:-1] + [headers[0]], 'EXPORT_DUPLICATE_HEADER')):
            with self.subTest(code=code):
                self.assert_code(code, lambda: reader.parse_native_export_bytes(name, csv_bytes(name, header=changed), encoding='gb18030'))

    def test_truncation_bad_quotes_width_and_control_characters_fail(self):
        data = csv_bytes('Account.csv', self.fixture.rows['Account.csv'])
        variants = [data[:-1], data + b'"unterminated', data + b'bad"quote\r\n',
                    data + b'too,few\r\n', data + b'\r\n']
        for raw in variants:
            with self.subTest(raw_tail=raw[-12:]):
                with self.assertRaises(reader.NativeExportReadError):
                    reader.parse_native_export_bytes('Account.csv', raw, encoding='gb18030')
        for cell in ('nul\x00', 'tab\t', 'line\nfeed'):
            self.assert_code('EXPORT_FIELD_INVALID', lambda: reader.parse_native_export_bytes(
                'Account.csv', csv_bytes('Account.csv', [{'账号备注': cell}]), encoding='gb18030'))

    def test_explicit_encoding_and_resource_limits(self):
        data = csv_bytes('Account.csv', self.fixture.rows['Account.csv'])
        self.assert_code('EXPORT_ENCODING_CONTRACT_MISMATCH', lambda: reader.parse_native_export_bytes('Account.csv', data, encoding='utf-8'))
        self.assert_code('EXPORT_ENCODING_INVALID', lambda: reader.parse_native_export_bytes('Account.csv', b'\xff\n', encoding='gb18030'))
        for limits in (reader.NativeExportLimits(max_file_bytes=10),
                       reader.NativeExportLimits(max_field_chars=2),
                       reader.NativeExportLimits(max_cells=1)):
            with self.subTest(limits=limits):
                with self.assertRaises(reader.NativeExportReadError):
                    reader.parse_native_export_bytes('Account.csv', data, encoding='gb18030', limits=limits)
        self.assert_code('EXPORT_ROW_OR_CELL_LIMIT_EXCEEDED', lambda: reader.parse_native_export_bytes(
            'Account.csv', csv_bytes('Account.csv', self.fixture.rows['Account.csv'] * 2), encoding='gb18030',
            limits=reader.NativeExportLimits(max_rows=1)))

    def test_no_source_bytes_or_metadata_are_modified(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.fixture.directory.iterdir()}
        self.fixture.read()
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.fixture.directory.iterdir()}
        self.assertEqual(before, after)

    def test_independent_round_change_is_rejected_even_if_parseable(self):
        capture = reader._capture
        calls = []

        def changing(directory, limits):
            if calls:
                self.fixture.rows['Account.csv'][0]['总资产'] = '999'
                self.fixture.write()
            calls.append(True)
            return capture(directory, limits)

        with mock.patch.object(reader, '_capture', side_effect=changing):
            self.assert_code('EXPORT_INDEPENDENT_READS_DIFFER', self.fixture.read)

    def test_change_while_handle_is_open_is_rejected(self):
        original = reader._open_shared_binary
        changed = []

        @contextlib.contextmanager
        def open_changing(path):
            with original(path) as stream:
                class ChangingStream:
                    def fileno(self):
                        return stream.fileno()

                    def read(self, size):
                        data = stream.read(size)
                        if not changed:
                            changed.append(True)
                            with path.open('ab') as writer:
                                writer.write(b'\r\n')
                        return data
                yield ChangingStream()

        with mock.patch.object(reader, '_open_shared_binary', side_effect=open_changing):
            self.assert_code('EXPORT_CHANGED_DURING_READ', self.fixture.read)

    def test_file_age_is_checked_at_end_not_start_and_future_files_rejected(self):
        moments = iter((NOW, NOW + dt.timedelta(seconds=31)))
        self.assert_code('EXPORT_FILE_AGE_OR_DAY_INVALID', lambda: self.fixture.read(clock=lambda: next(moments)))
        path = self.fixture.directory / 'Deal.csv'
        os.utime(path, ns=(int(NOW.timestamp()) * 1000000000 + 1,) * 2)
        self.assert_code('EXPORT_FILE_AGE_OR_DAY_INVALID', self.fixture.read)

    def test_timezone_aware_clock_and_beijing_date_at_utc_day_boundary(self):
        now = dt.datetime(2026, 10, 18, 16, 0, 2, tzinfo=dt.timezone.utc)
        fixture = NativeExportFixture(self.temporary.name, now=now)
        self.assertEqual(fixture.read().trade_date, dt.date(2026, 10, 19))
        self.assert_code('EXPORT_AWARE_CLOCK_REQUIRED', lambda: fixture.read(clock=lambda: now.replace(tzinfo=None)))
        moments = iter((now, now - dt.timedelta(seconds=1)))
        self.assert_code('EXPORT_CLOCK_MOVED_BACKWARD', lambda: fixture.read(clock=lambda: next(moments)))
        for value in (True, 0, float('nan'), float('inf'), 3601):
            self.assert_code('EXPORT_MAX_AGE_INVALID', lambda: fixture.read(max_age_seconds=value))

    def test_missing_and_nonregular_file_errors_do_not_expose_paths(self):
        path = self.fixture.directory / 'Order.csv'
        path.unlink()
        self.assert_code('EXPORT_FILES_UNAVAILABLE', self.fixture.read)
        path.mkdir()
        self.assert_code('EXPORT_FILE_TYPE_OR_SIZE_INVALID', self.fixture.read)

    def test_account_only_reads_exactly_account_when_other_files_are_missing(self):
        for name in reader.HEADERS:
            if name != 'Account.csv':
                (self.fixture.directory / name).unlink()
        opened = []
        original = reader._open_shared_binary

        def account_open(path):
            opened.append(path.name)
            return original(path)

        with mock.patch.object(reader, '_open_shared_binary', side_effect=account_open):
            observation = self.fixture.read_account()
        self.assertIs(type(observation), reader.NativeAccountObservation)
        self.assertEqual(opened, ['Account.csv', 'Account.csv'])
        self.assertFalse(hasattr(observation, 'tables'))
        self.assertEqual(observation.row['资金账号'], ACCOUNT)
        self.assertEqual(observation.decimal('总资产'), Decimal('100000.10000000000000000001'))
        with self.assertRaises(TypeError):
            observation.row['总资产'] = 'changed'
        self.assertNotIn(ACCOUNT, repr(observation))
        summary = observation.safe_summary()
        self.assertEqual(summary['scope'], 'LOCAL_CURRENT_ACCOUNT_FILE_OBSERVATION_ONLY')
        self.assertTrue(summary['local_current_observation'])
        self.assertNotIn(ACCOUNT, json.dumps(summary))
        for key in ('broker_freshness_verified', 'account_completeness_verified',
                    'account_query_equivalence_certified', 'source_equivalence_certified',
                    'cross_table_snapshot_verified', 'automatic_trading_ready'):
            self.assertFalse(getattr(observation, key))
            self.assertFalse(summary[key])

    def test_account_only_does_not_relax_four_table_stale_empty_file_contract(self):
        for name in reader.HEADERS:
            if name != 'Account.csv':
                os.utime(self.fixture.directory / name, (NOW.timestamp() - 60,) * 2)
        self.assertTrue(self.fixture.read_account().local_current_observation)
        self.assert_code('EXPORT_FILE_AGE_OR_DAY_INVALID', self.fixture.read)

    def test_account_only_still_requires_target_single_row_and_matching_day(self):
        self.assert_code('EXPORT_TARGET_ACCOUNT_REQUIRED', lambda: self.fixture.read_account(target_account=''))
        self.fixture.rows['Account.csv'][0]['资金账号'] = 'OTHER_ACCOUNT'
        self.fixture.write()
        self.assert_code('EXPORT_ACCOUNT_MISMATCH', self.fixture.read_account)
        self.fixture.rows['Account.csv'] = [{'资金账号': ACCOUNT, '交易日': '20261018'}]
        self.fixture.write()
        self.assert_code('EXPORT_ACCOUNT_TRADE_DATE_MISMATCH', self.fixture.read_account)
        self.fixture.rows['Account.csv'] = []
        self.fixture.write()
        self.assert_code('EXPORT_ACCOUNT_ROW_COUNT_INVALID', self.fixture.read_account)
        self.fixture.rows['Account.csv'] = [{'资金账号': ACCOUNT, '交易日': '20261019'}] * 2
        self.fixture.write()
        self.assert_code('EXPORT_ACCOUNT_ROW_COUNT_INVALID', self.fixture.read_account)

    def test_account_only_keeps_final_age_future_and_aware_clock_checks(self):
        moments = iter((NOW, NOW + dt.timedelta(seconds=31)))
        self.assert_code('EXPORT_FILE_AGE_OR_DAY_INVALID', lambda: self.fixture.read_account(clock=lambda: next(moments)))
        self.assert_code('EXPORT_AWARE_CLOCK_REQUIRED', lambda: self.fixture.read_account(clock=lambda: NOW.replace(tzinfo=None)))
        path = self.fixture.directory / 'Account.csv'
        os.utime(path, ns=(int(NOW.timestamp()) * 1000000000 + 1,) * 2)
        self.assert_code('EXPORT_FILE_AGE_OR_DAY_INVALID', self.fixture.read_account)

    def test_account_only_change_between_rounds_is_rejected(self):
        capture, rounds = reader._capture, []

        def changing(directory, limits, names=None):
            self.assertEqual(names, ('Account.csv',))
            if rounds:
                self.fixture.rows['Account.csv'][0]['总资产'] = '999'
                self.fixture.write_one('Account.csv', csv_bytes('Account.csv', self.fixture.rows['Account.csv']))
            rounds.append(True)
            return capture(directory, limits, names)

        with mock.patch.object(reader, '_capture', side_effect=changing):
            self.assert_code('EXPORT_INDEPENDENT_READS_DIFFER', self.fixture.read_account)
        self.assertEqual(len(rounds), 2)

    def test_account_only_uses_the_same_strict_csv_encoding_and_decimal_parser(self):
        self.assert_code('EXPORT_ENCODING_CONTRACT_MISMATCH', lambda: self.fixture.read_account(encoding='utf-8'))
        self.fixture.rows['Account.csv'][0]['总资产'] = '1,000'
        self.fixture.write()
        observation = self.fixture.read_account()
        self.assertEqual(observation.row['总资产'], '1,000')
        self.assert_code('EXPORT_DECIMAL_INVALID', lambda: observation.decimal('总资产'))
        path = self.fixture.directory / 'Account.csv'
        self.fixture.write_one('Account.csv', path.read_bytes()[:-1])
        self.assert_code('EXPORT_CSV_TRUNCATED', self.fixture.read_account)


if __name__ == '__main__':
    unittest.main()
