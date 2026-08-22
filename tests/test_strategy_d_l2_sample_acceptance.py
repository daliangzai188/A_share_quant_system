from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from src.strategy_d_l2_sample_acceptance import validate_sample_package


def write_valid_sample(root: Path, *, trade_date: str = "20240701", exchange: str = "SSE") -> dict[str, object]:
    sample_dir = root / trade_date / exchange
    sample_dir.mkdir(parents=True)
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[exchange]
    codes = [f"000001.{suffix}", f"000002.{suffix}"]
    orders = pd.DataFrame(
        [
            [trade_date, exchange, codes[0], "09:29:59.001", "1", "1", "B1", "ADD", "BUY", "10", "1000"],
            [trade_date, exchange, codes[0], "09:30:00.001", "1", "2", "S1", "ADD", "SELL", "10", "500"],
            [trade_date, exchange, codes[0], "14:00:00.001", "1", "3", "B1", "CANCEL", "BUY", "10", "100"],
        ],
        columns=[
            "trade_date", "exchange", "ts_code", "event_time", "channel_no",
            "sequence", "order_id", "action", "side", "price", "volume",
        ],
    )
    transactions = pd.DataFrame(
        [[trade_date, exchange, codes[0], "14:00:01.001", "1", "1", "10", "100", "B1", "S1"]],
        columns=[
            "trade_date", "exchange", "ts_code", "event_time", "channel_no",
            "sequence", "price", "volume", "bid_order_id", "ask_order_id",
        ],
    )
    snapshots = pd.DataFrame(
        [
            [trade_date, exchange, code, time, scan_id, "10", "10", "1000", "2", "[600, 400]"]
            for scan_id, time in [("OPEN", "09:30:00.000"), ("CANCEL", "14:55:00.000")]
            for code in codes
        ],
        columns=[
            "trade_date", "exchange", "ts_code", "event_time", "scan_id",
            "last_price", "bid_price_1", "bid_volume_1", "bid_order_count_1", "bid_queue_1",
        ],
    )
    orders.to_csv(sample_dir / "orders.csv", index=False)
    transactions.to_csv(sample_dir / "transactions.csv", index=False)
    snapshots.to_csv(sample_dir / "snapshots.csv", index=False)
    return {
        "trade_date": trade_date,
        "exchange": exchange,
        "orders_file": f"{trade_date}/{exchange}/orders.csv",
        "transactions_file": f"{trade_date}/{exchange}/transactions.csv",
        "snapshots_file": f"{trade_date}/{exchange}/snapshots.csv",
        "full_market": True,
        "sequence_complete": True,
        "sequence_gap_detection": True,
        "order_trade_linkage": True,
        "raw_unadjusted_price": True,
        "volume_unit": "SHARE",
        "coverage_start_hhmm": 930,
        "coverage_end_hhmm": 1455,
        "expected_security_count": 2,
    }


def write_manifest(root: Path, samples: list[dict[str, object]]) -> Path:
    path = root / "manifest.json"
    path.write_text(
        json.dumps({"schema_version": 1, "provider": "TEST", "samples": samples}),
        encoding="utf-8",
    )
    return path


class StrategyDL2SampleAcceptanceTest(unittest.TestCase):
    def test_complete_content_sample_passes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entry = write_valid_sample(root)
            write_manifest(root, [entry])

            report = validate_sample_package(
                sample_root=root,
                required_dates=["20240701"],
                required_exchanges=["SSE"],
            )

            self.assertTrue(report["passed"])
            self.assertEqual(report["status"], "PREPAYMENT_SAMPLE_GATE_PASSED")
            self.assertEqual(report["passed_sample_count"], 1)
            self.assertEqual(
                report["samples"][0]["metrics"]["actual_security_count"], 2
            )
            self.assertTrue(report["samples"][0]["files"]["orders"]["sha256"])

    def test_duplicate_channel_sequence_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entry = write_valid_sample(root)
            orders_path = root / str(entry["orders_file"])
            orders = pd.read_csv(orders_path, dtype=str)
            orders.loc[1, "sequence"] = orders.loc[0, "sequence"]
            orders.to_csv(orders_path, index=False)
            write_manifest(root, [entry])

            report = validate_sample_package(
                sample_root=root,
                required_dates=["20240701"],
                required_exchanges=["SSE"],
            )

            self.assertFalse(report["passed"])
            self.assertTrue(
                any(
                    "重复(channel_no, sequence)" in error
                    for error in report["samples"][0]["errors"]
                )
            )

    def test_unknown_trade_order_reference_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entry = write_valid_sample(root)
            transactions_path = root / str(entry["transactions_file"])
            transactions = pd.read_csv(transactions_path, dtype=str)
            transactions.loc[0, "bid_order_id"] = "UNKNOWN"
            transactions.to_csv(transactions_path, index=False)
            write_manifest(root, [entry])

            report = validate_sample_package(
                sample_root=root,
                required_dates=["20240701"],
                required_exchanges=["SSE"],
            )

            self.assertFalse(report["passed"])
            self.assertTrue(
                any(
                    "逐笔成交引用未知委托" in error
                    for error in report["samples"][0]["errors"]
                )
            )

    def test_missing_market_samples_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entry = write_valid_sample(root)
            write_manifest(root, [entry])

            report = validate_sample_package(sample_root=root)

            self.assertFalse(report["passed"])
            self.assertEqual(report["passed_sample_count"], 1)
            self.assertEqual(report["missing_sample_count"], 8)
            self.assertIn("20240701|BSE", report["missing_samples"])


if __name__ == "__main__":
    unittest.main()
