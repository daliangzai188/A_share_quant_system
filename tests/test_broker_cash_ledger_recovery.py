import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import recover_broker_cash_ledger as recovery


FIELDS = ["资金账号", "发生日期", "发生时间", "流水序号", "业务名称", "发生金额", "剩余金额", "币种"]


def row(kind="银行转存", amount="100.00", balance="100.00", serial="1", date="20260608", **overrides):
    record = dict(zip(FIELDS, ["TEST_ACCOUNT", date, "160000", serial, kind, amount, balance, "人民币"]))
    record.update(overrides)
    return record


class CashLedgerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "original.txt"
        self.output = self.root / "restored"

    def save(self, rows, fields=FIELDS, encoding="gb18030"):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
        self.source.write_bytes(stream.getvalue().encode(encoding))
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def restore(self, **overrides):
        kwargs = dict(expected_sha256=self.digest, opening_cash="0", query_start="20260601", query_end="20261011")
        kwargs.update(overrides)
        return recovery.restore(self.source, self.output, **kwargs)

    def test_exact_cash_reconciliation_does_not_double_count_frozen_or_interest(self):
        self.save([
            row(), row("转账取预冻结", "-10", "100", "2"),
            row("转账冻结取消", "10", "100", "3"),
            row("转存管转出", "-20", "100", "4"),
            row("转存管转入", "20", "100", "5"),
            row("利息结算", "0.03", "100", "6"),
            row("利息归本", "0.03", "100.03", "7"),
            row("证券买入", "-50.01", "50.02", "8"),
            row("证券卖出", "55.01", "105.03", "9"),
            row("市值申购中签扣款", "-2", "103.03", "10"),
            row("市值申购中签扣款回冲", "2", "105.03", "11"),
            row("新股申购确认缴款", "-2", "103.03", "12"),
            row("银行转取", "-3", "100.03", "13"),
        ])
        original = self.source.read_bytes()
        summary = self.restore()
        self.assertEqual(summary["matched_balance_changes"], 13)
        self.assertEqual(summary["ending_cash"], "100.03")
        self.assertEqual(summary["cash_changes_by_category"]["security_settlement"], "5.00")
        self.assertEqual(summary["cash_changes_by_category"]["no_cash_change"], "0.00")
        self.assertFalse(summary["strategy_equity_certified"])
        self.assertFalse(summary["history_coverage_certified"])
        ledger = self.output / "recovered_broker_cash_ledger.json"
        payload = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(payload["daily_last_recorded_cash"], {"20260608": "100.03"})
        self.assertNotIn("TEST_ACCOUNT", ledger.read_text(encoding="utf-8"))
        csv_path = self.output / "recovered_broker_cash_flow.csv"
        with csv_path.open(encoding="utf-8-sig") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 13)
        self.assertEqual(self.source.read_bytes(), original)
        if os.name != "nt":
            self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
            self.assertEqual(csv_path.stat().st_mode & 0o777, 0o600)

    def test_nonzero_opening_cash_is_explicit_and_reconciled(self):
        self.save([row(amount="10", balance="60")])
        self.assertEqual(self.restore(opening_cash="50")["ending_cash"], "60")
        with self.assertRaisesRegex(ValueError, "余额变化"):
            self.restore()

    def test_tampered_source_leaves_existing_outputs_intact(self):
        self.save([row()])
        self.restore()
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        self.source.write_bytes(self.source.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.restore()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_missing_or_invalid_hash_is_rejected(self):
        self.save([row()])
        for value in ("", "not_a_hash", "0" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.restore(expected_sha256=value)
        self.assertFalse(self.output.exists())

    def test_unknown_business_is_rejected(self):
        self.save([row(kind="未审核业务")])
        with self.assertRaisesRegex(ValueError, "未审核"):
            self.restore()
        self.assertFalse(self.output.exists())

    def test_duplicate_and_mixed_or_empty_account_are_rejected(self):
        for rows in ([row(), row()], [row(), row(serial="2", **{"资金账号": "OTHER"})], [row(**{"资金账号": ""})]):
            with self.subTest(rows=rows):
                self.save(rows)
                with self.assertRaises(ValueError):
                    self.restore()
        self.assertFalse(self.output.exists())

    def test_empty_missing_and_malformed_columns_are_rejected(self):
        self.save([])
        with self.assertRaises(ValueError):
            self.restore()
        self.save([{"发生日期": "20260608"}], fields=["发生日期"])
        with self.assertRaises(ValueError):
            self.restore()
        self.save([row()])
        self.source.write_bytes(self.source.read_bytes() + b"short\trow\n")
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            self.restore()
        self.assertFalse(self.output.exists())

    def test_invalid_out_of_range_and_decreasing_dates_are_rejected(self):
        for rows in ([row(date="20260230")], [row(date="20260101")], [row(date="2026/06/08")],
                     [row(date="20260609"), row(date="20260608", serial="2")]):
            with self.subTest(rows=rows):
                self.save(rows)
                with self.assertRaises(ValueError):
                    self.restore()
        self.assertFalse(self.output.exists())

    def test_query_period_cannot_be_reversed(self):
        self.save([row()])
        with self.assertRaises(ValueError):
            self.restore(query_start="20261011", query_end="20260601")

    def test_nonfinite_invalid_or_subcent_money_is_rejected(self):
        for value in ("NaN", "Infinity", "not_money", "0.001"):
            for field in ("发生金额", "剩余金额"):
                with self.subTest(value=value, field=field):
                    self.save([row(**{field: value})])
                    with self.assertRaises(ValueError):
                        self.restore()
        self.assertFalse(self.output.exists())

    def test_currency_cannot_be_mixed(self):
        self.save([row(**{"币种": "美元"})])
        with self.assertRaisesRegex(ValueError, "人民币"):
            self.restore()

    def test_output_cannot_write_inside_sync_project_or_replace_source(self):
        self.save([row()])
        with self.assertRaisesRegex(ValueError, "同步目录"):
            recovery.restore(self.source, recovery.PROJECT_ROOT / "data/state", expected_sha256=self.digest,
                             opening_cash="0", query_start="20260601", query_end="20261011")
        self.source = self.root / "recovered_broker_cash_ledger.json"
        self.save([row()])
        with self.assertRaisesRegex(ValueError, "覆盖"):
            recovery.restore(self.source, self.root, expected_sha256=self.digest,
                             opening_cash="0", query_start="20260601", query_end="20261011")

    def test_failed_atomic_replace_cleans_temporary_file(self):
        self.save([row()])
        with patch.object(Path, "replace", side_effect=OSError("write blocked")):
            with self.assertRaises(OSError):
                self.restore()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cli_accepts_explicit_parameters_and_utf8_export(self):
        self.save([row()], encoding="utf-8-sig")
        result = subprocess.run([
            sys.executable, str(Path(recovery.__file__)), "--source", str(self.source),
            "--output", str(self.output), "--expected-sha256", self.digest,
            "--opening-cash", "0", "--query-start", "20260601", "--query-end", "20261011",
            "--encoding", "utf-8-sig",
        ], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["original_row_count"], 1)


if __name__ == "__main__":
    unittest.main()
