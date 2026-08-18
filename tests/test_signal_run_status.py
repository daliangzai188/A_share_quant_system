from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts import run_strategy_e_signal as e_signal
from scripts import run_strategy_l_signal as l_signal
from scripts.trading_daemon import _strategy_signal_run_readiness
from src.rolling_signal_store import (
    ERROR,
    NO_CANDIDATE,
    NO_SIGNAL_OCCUPIED,
    SIGNAL_READY,
    load_recent_signal_runs,
    save_recent_signal_run,
    save_recent_signal,
    signal_run_by_signal_date,
)


class SignalRunStoreTests(unittest.TestCase):
    def test_same_day_rerun_replaces_error_and_keeps_recent_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runs.json"
            save_recent_signal_run(
                path,
                {"signal_date": "20260801", "status": ERROR, "reason": "首次失败"},
                strategy_leg="E",
                max_trade_days=2,
            )
            save_recent_signal_run(
                path,
                {"signal_date": "20260801", "status": NO_CANDIDATE, "reason": "重跑正常"},
                strategy_leg="E",
                max_trade_days=2,
            )
            save_recent_signal_run(
                path,
                {"signal_date": "20260802", "status": NO_SIGNAL_OCCUPIED, "reason": "已有持仓"},
                strategy_leg="E",
                max_trade_days=2,
            )
            save_recent_signal_run(
                path,
                {"signal_date": "20260803", "status": SIGNAL_READY, "reason": "已生成"},
                strategy_leg="E",
                max_trade_days=2,
            )

            runs = load_recent_signal_runs(path)
            self.assertEqual([run["signal_date"] for run in runs], ["20260802", "20260803"])
            self.assertEqual(signal_run_by_signal_date(path, "20260803")["status"], SIGNAL_READY)

    def test_unknown_status_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "未知信号运行状态"):
                save_recent_signal_run(
                    Path(temp_dir) / "runs.json",
                    {"signal_date": "20260803", "status": "UNKNOWN"},
                    strategy_leg="L",
                )


class SignalRunReadinessTests(unittest.TestCase):
    def assess(self, root: Path, strategy_leg: str, signal_date: str) -> dict:
        return _strategy_signal_run_readiness(
            strategy_leg=strategy_leg,
            run_status_path=root / "runs.json",
            signal_path=root / "signals.json",
            signal_date=signal_date,
        )

    def test_normal_no_candidate_is_information_not_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_recent_signal_run(
                root / "runs.json",
                {"signal_date": "20260803", "status": NO_CANDIDATE, "reason": "过滤后为0"},
                strategy_leg="L",
            )

            result = self.assess(root, "L", "20260803")

            self.assertTrue(result["ok"])
            self.assertEqual(result["icon"], "ℹ️")
            self.assertEqual(result["status"], NO_CANDIDATE)

    def test_missing_run_and_error_are_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = self.assess(root, "E", "20260803")
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["status"], "NOT_RUN")

            save_recent_signal_run(
                root / "runs.json",
                {"signal_date": "20260803", "status": ERROR, "reason": "数据失败"},
                strategy_leg="E",
            )
            failed = self.assess(root, "E", "20260803")
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["status"], ERROR)

    def test_signal_ready_requires_matching_formal_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_recent_signal_run(
                root / "runs.json",
                {"signal_date": "20260803", "status": SIGNAL_READY, "reason": "已生成"},
                strategy_leg="E",
            )
            inconsistent = self.assess(root, "E", "20260803")
            self.assertFalse(inconsistent["ok"])
            self.assertEqual(inconsistent["status"], ERROR)

            save_recent_signal(
                root / "signals.json",
                {"signal_date": "20260803", "ts_code": "000001.SZ"},
                strategy_leg="E",
            )
            ready = self.assess(root, "E", "20260803")
            self.assertTrue(ready["ok"])
            self.assertEqual(ready["icon"], "✅")


class SignalGeneratorStatusTests(unittest.TestCase):
    def test_e_occupied_and_empty_shadow_candidates_are_explained(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "e_runs.json"
            with (
                patch.object(e_signal, "RUN_STATUS_PATH", status_path),
                patch.object(e_signal, "migrate_existing_signals"),
                patch.object(e_signal, "load_e_candidates", return_value=pd.DataFrame()),
                patch.object(e_signal, "save_candidates"),
                patch.object(
                    e_signal,
                    "load_open_positions",
                    return_value=[
                        {
                            "strategy_leg": "L",
                            "ts_code": "300996.SZ",
                            "planned_exit_date": "20260804",
                            "status": "open",
                        }
                    ],
                ),
            ):
                e_signal.run_signal_generation("20260803", dry_run=False)

            run = signal_run_by_signal_date(status_path, "20260803")
            self.assertEqual(run["status"], NO_SIGNAL_OCCUPIED)
            self.assertIn("300996.SZ", run["reason"])
            self.assertEqual(run["candidate_count"], 0)
            self.assertIn("即使账户空仓也不会触发", run["reason"])

    def test_e_occupied_records_available_shadow_candidate_without_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "e_runs.json"
            candidates = pd.DataFrame(
                [{"ts_code": "000001.SZ", "name": "平安银行", "circ_mv": 100.0}]
            )
            with (
                patch.object(e_signal, "RUN_STATUS_PATH", status_path),
                patch.object(e_signal, "migrate_existing_signals"),
                patch.object(e_signal, "load_e_candidates", return_value=candidates),
                patch.object(e_signal, "save_candidates"),
                patch.object(e_signal, "save_signal") as save_signal_mock,
                patch.object(
                    e_signal,
                    "load_open_positions",
                    return_value=[
                        {
                            "strategy_leg": "L",
                            "ts_code": "300996.SZ",
                            "planned_exit_date": "20260804",
                            "status": "open",
                        }
                    ],
                ),
            ):
                e_signal.run_signal_generation("20260803", dry_run=False)

            run = signal_run_by_signal_date(status_path, "20260803")
            self.assertEqual(run["status"], NO_SIGNAL_OCCUPIED)
            self.assertEqual(run["candidate_count"], 1)
            self.assertEqual(run["candidate_ts_code"], "000001.SZ")
            self.assertEqual(run["candidate_name"], "平安银行")
            self.assertIn("但因当前持仓阻断", run["reason"])
            save_signal_mock.assert_not_called()

    def test_e_candidate_failure_writes_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "e_runs.json"
            with (
                patch.object(e_signal, "RUN_STATUS_PATH", status_path),
                patch.object(e_signal, "migrate_existing_signals"),
                patch.object(e_signal, "load_open_positions", return_value=[]),
                patch.object(e_signal, "has_ac_planned_order", return_value=False),
                patch.object(
                    e_signal,
                    "load_d_intraday_status",
                    return_value={"has_filled": False, "has_failed": False, "summary": ""},
                ),
                patch.object(e_signal, "compute_segment_retreat_states", return_value={}),
                patch.object(e_signal, "load_e_candidates", side_effect=ValueError("关键字段缺失")),
            ):
                e_signal.run_signal_generation("20260803", dry_run=False)

            run = signal_run_by_signal_date(status_path, "20260803")
            self.assertEqual(run["status"], ERROR)
            self.assertIn("关键字段缺失", run["reason"])

    def test_l_empty_candidates_writes_normal_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "l_runs.json"
            with (
                patch.object(l_signal, "RUN_STATUS_PATH", status_path),
                patch.object(l_signal, "migrate_existing_signals"),
                patch.object(l_signal, "load_json_config", return_value={}),
                patch.object(
                    l_signal,
                    "load_l_candidates",
                    return_value=(pd.DataFrame(), ["L2过滤后候选=0"]),
                ),
                patch.object(l_signal, "save_outputs"),
            ):
                l_signal.run_signal_generation("20260803", dry_run=False)

            run = signal_run_by_signal_date(status_path, "20260803")
            self.assertEqual(run["status"], NO_CANDIDATE)
            self.assertEqual(run["candidate_count"], 0)


if __name__ == "__main__":
    unittest.main()
