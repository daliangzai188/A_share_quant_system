from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import pandas as pd

from scripts.reconcile_strategy_d_forward_evidence import reconcile


class StrategyDForwardEvidenceTests(unittest.TestCase):
    def test_reconcile_uses_intent_ledger_and_fill_sample_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            signal_dir = root / "signals"
            signal_dir.mkdir()
            signal_path = signal_dir / "intraday_signals_20260914.csv"
            pd.DataFrame(
                [
                    {
                        "signal_type": "BUY",
                        "ts_code": "688001.SH",
                        "first_time_bucket": "open_limit",
                        "fill_matched_source": "fallback_due_to_low_sample",
                        "order_id": "ORDER-1",
                        "order_status": "PENDING_OR_PARTIAL",
                        "filled_qty": 0,
                    }
                ]
            ).to_csv(signal_path, index=False)

            database = root / "events.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE trade_intents (
                    broker_order_id TEXT, business_date TEXT, strategy_leg TEXT,
                    status TEXT, filled_qty INTEGER, filled_amount REAL,
                    avg_fill_price REAL, error_message TEXT, updated_at TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO trade_intents VALUES
                ('ORDER-1', '20260914', 'D', 'CANCELLED', 0, 0, 0, '',
                 '2026-09-14T14:55:01+08:00')
                """
            )
            connection.commit()
            connection.close()

            fallback = root / "fill_rate_fallback.csv"
            pd.DataFrame(
                [
                    {
                        "market_segment": "star",
                        "limit_times_bucket": "1",
                        "board_type": "multi_open",
                        "first_time_bucket": "open_limit",
                        "sample_count": 48,
                        "is_sample_enough": True,
                    }
                ]
            ).to_csv(fallback, index=False)
            config = root / "config.json"
            config.write_text(
                json.dumps({"fill_model": {"min_group_samples": 30}}),
                encoding="utf-8",
            )

            result = reconcile(
                start_date="20260903",
                signal_dir=signal_dir,
                intent_db=database,
                fill_fallback_path=fallback,
                config_path=config,
            )

            self.assertEqual(result["order_status_rows_reconciled"], 1)
            self.assertEqual(result["sample_evidence_rows_reconciled"], 1)
            row = pd.read_csv(signal_path).iloc[0]
            self.assertEqual(row["order_status"], "CANCELLED_NO_FILL")
            self.assertEqual(int(row["fill_sample_count"]), 48)
            self.assertEqual(int(row["fill_min_group_samples"]), 30)
            self.assertTrue(bool(row["fill_sample_enough_reconciled"]))


if __name__ == "__main__":
    unittest.main()
