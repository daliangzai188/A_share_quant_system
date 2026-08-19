from __future__ import annotations

from dataclasses import asdict
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from types import SimpleNamespace

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False
    sys.modules["dotenv"] = dotenv_stub

from scripts.monitor_strategy_d_intraday import StockState, StrategyDMonitor
from src.strategy_d_checkpoint import (
    D_CHECKPOINT_STATUS_READY,
    D_CHECKPOINT_STATUS_SCAN_IN_PROGRESS,
    inspect_strategy_d_checkpoint,
    invalidate_strategy_d_checkpoint,
    strategy_d_market_context_sha256,
    strategy_d_runtime_fingerprint,
    strategy_d_universe_sha256,
    write_strategy_d_checkpoint,
)


BEIJING = dt.timezone(dt.timedelta(hours=8))


class _Logger:
    def info(self, *args, **kwargs) -> None:
        return None

    def warning(self, *args, **kwargs) -> None:
        return None

    def error(self, *args, **kwargs) -> None:
        return None


class _QuoteBroker:
    def __init__(self, quotes: dict) -> None:
        self.quotes = quotes

    def get_full_tick(self, _codes: list[str]) -> dict:
        return dict(self.quotes)


class StrategyDCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "checkpoint.json"
        self.now = dt.datetime(2026, 8, 19, 14, 8, 45, tzinfo=BEIJING)
        self.universe = ["000001.SZ"]
        self.universe_hash = strategy_d_universe_sha256(self.universe)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _payload(self) -> dict:
        state = StockState(
            ts_code="000001.SZ",
            name="平安银行",
            market_segment="sz_main",
            upper_limit=12.34,
            was_sealed=True,
            ever_sealed=True,
            open_times_today=2,
            first_seal_hhmm=1030,
            last_seal_hhmm=1405,
        )
        return {
            "status": D_CHECKPOINT_STATUS_READY,
            "resume_allowed": True,
            "trade_date": "20260819",
            "recorded_at": self.now.isoformat(),
            "tracking_start_hhmm": 930,
            "original_session_start_hhmm": 920,
            "first_complete_scan_at": dt.datetime(
                2026, 8, 19, 9, 30, 4, tzinfo=BEIJING
            ).isoformat(),
            "last_complete_scan_at": self.now.isoformat(),
            "scan_round": 537,
            "last_scan_updated_count": 1,
            "universe_size": 1,
            "universe_sha256": self.universe_hash,
            "state_count": 1,
            "states": {"000001.SZ": asdict(state)},
            "path_integrity_failed": False,
            "path_integrity_reason": "",
            "machine_fingerprint": "machine",
            "runtime_fingerprint": "runtime",
            "signal_records": [],
            "strong_notified": False,
            "limit_price_fallback_logged": True,
            "order_placed": False,
            "order_locked_ts_code": "",
            "session_orders": {},
        }

    def _inspect(self, *, now: dt.datetime | None = None):
        return inspect_strategy_d_checkpoint(
            self.path,
            trade_date="20260819",
            now=now or self.now,
            max_age_seconds=75,
            expected_tracking_start_hhmm=930,
            expected_machine_fingerprint="machine",
            expected_runtime_fingerprint="runtime",
            expected_universe_sha256=self.universe_hash,
            expected_universe_size=1,
        )

    def test_valid_checkpoint_passes_and_restores_exact_path_state(self) -> None:
        write_strategy_d_checkpoint(self.path, self._payload())

        check = self._inspect()

        self.assertTrue(check.ok, check.reason)
        monitor = StrategyDMonitor(
            broker=object(),
            live_order=False,
            logger=_Logger(),
            signal_csv=Path(self.temp_dir.name) / "signals.csv",
            allowed_segments={"sz_main"},
            config={"strategy_d": {"checkpoint_max_age_sec": 75}},
        )
        monitor.checkpoint_path = self.path
        monitor.checkpoint_machine_fingerprint = "machine"
        monitor.checkpoint_runtime_fingerprint = "runtime"
        monitor.universe = list(self.universe)
        monitor.universe_sha256 = self.universe_hash

        with patch(
            "scripts.monitor_strategy_d_intraday.now_beijing",
            return_value=self.now,
        ), patch(
            "scripts.monitor_strategy_d_intraday.today_beijing",
            return_value=self.now.date(),
        ):
            restored, reason = monitor._restore_ready_checkpoint()

        self.assertTrue(restored, reason)
        self.assertEqual(monitor.scan_round, 537)
        self.assertEqual(monitor.states["000001.SZ"].open_times_today, 2)
        self.assertEqual(monitor.states["000001.SZ"].first_seal_hhmm, 1030)
        self.assertEqual(monitor.states["000001.SZ"].last_seal_hhmm, 1405)

    def test_scan_in_progress_marker_cannot_be_resumed(self) -> None:
        write_strategy_d_checkpoint(self.path, self._payload())
        invalidate_strategy_d_checkpoint(
            self.path,
            trade_date="20260819",
            status=D_CHECKPOINT_STATUS_SCAN_IN_PROGRESS,
            reason="下一轮扫描中",
            recorded_at=self.now,
            machine_fingerprint="machine",
            runtime_fingerprint="runtime",
        )

        check = self._inspect()

        self.assertFalse(check.ok)
        self.assertIn("SCAN_IN_PROGRESS", check.reason)

    def test_complete_scan_with_no_limit_up_path_states_is_resumable(self) -> None:
        payload = self._payload()
        payload["states"] = {}
        payload["state_count"] = 0
        write_strategy_d_checkpoint(self.path, payload)

        check = self._inspect()

        self.assertTrue(check.ok, check.reason)

    def test_stale_or_different_universe_checkpoint_is_rejected(self) -> None:
        write_strategy_d_checkpoint(self.path, self._payload())

        stale = self._inspect(now=self.now + dt.timedelta(seconds=76))
        changed_universe = inspect_strategy_d_checkpoint(
            self.path,
            trade_date="20260819",
            now=self.now,
            max_age_seconds=75,
            expected_tracking_start_hhmm=930,
            expected_machine_fingerprint="machine",
            expected_runtime_fingerprint="runtime",
            expected_universe_sha256=strategy_d_universe_sha256(
                ["000001.SZ", "600000.SH"]
            ),
            expected_universe_size=2,
        )

        self.assertFalse(stale.ok)
        self.assertIn("过期", stale.reason)
        self.assertFalse(changed_universe.ok)
        self.assertIn("宇宙", changed_universe.reason)

    def test_tampered_checkpoint_fails_checksum(self) -> None:
        write_strategy_d_checkpoint(self.path, self._payload())
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["states"]["000001.SZ"]["open_times_today"] = 3
        self.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        check = self._inspect()

        self.assertFalse(check.ok)
        self.assertIn("摘要", check.reason)

    def test_runtime_fingerprint_changes_with_d_configuration(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        base = {"strategy_d": {"checkpoint_max_age_sec": 75}, "fill_model": {}}
        changed = {
            "strategy_d": {
                "checkpoint_max_age_sec": 75,
                "min_open_times": 99,
            },
            "fill_model": {},
        }

        self.assertNotEqual(
            strategy_d_runtime_fingerprint(project_root, base),
            strategy_d_runtime_fingerprint(project_root, changed),
        )

    def test_market_context_fingerprint_binds_previous_limit_and_circ_mv(self) -> None:
        first = strategy_d_market_context_sha256(
            self.universe,
            set(),
            {"000001.SZ": "平安银行"},
            {"000001.SZ": 100000.0},
        )
        previous_limit_changed = strategy_d_market_context_sha256(
            self.universe,
            {"000001.SZ"},
            {"000001.SZ": "平安银行"},
            {"000001.SZ": 100000.0},
        )
        circ_mv_changed = strategy_d_market_context_sha256(
            self.universe,
            set(),
            {"000001.SZ": "平安银行"},
            {"000001.SZ": 100001.0},
        )

        self.assertNotEqual(first, previous_limit_changed)
        self.assertNotEqual(first, circ_mv_changed)

    def test_only_complete_full_market_round_produces_ready_checkpoint(self) -> None:
        quote = SimpleNamespace(
            upper_limit=11.0,
            pre_close=10.0,
            last_price=11.0,
            bid_volumes=[10000],
        )
        monitor = StrategyDMonitor(
            broker=_QuoteBroker({"000001.SZ": quote}),
            live_order=False,
            logger=_Logger(),
            signal_csv=Path(self.temp_dir.name) / "signals.csv",
            allowed_segments={"sz_main"},
            config={"strategy_d": {"checkpoint_max_age_sec": 75}},
        )
        monitor.checkpoint_path = self.path
        monitor.checkpoint_machine_fingerprint = "machine"
        monitor.checkpoint_runtime_fingerprint = "runtime"
        monitor.universe = list(self.universe)
        monitor.universe_sha256 = self.universe_hash
        monitor.name_map = {"000001.SZ": "平安银行"}
        monitor.circ_mv_map = {"000001.SZ": 100000.0}
        monitor.segment_stock_counts = {"sz_main": 1}
        monitor.original_session_start_hhmm = 920

        with patch(
            "scripts.monitor_strategy_d_intraday.now_beijing",
            return_value=self.now,
        ), patch(
            "scripts.monitor_strategy_d_intraday.today_beijing",
            return_value=self.now.date(),
        ), patch(
            "scripts.monitor_strategy_d_intraday.notify",
            return_value=True,
        ):
            monitor.poll_once()

        ready = self._inspect()
        self.assertTrue(ready.ok, ready.reason)
        self.assertEqual(ready.payload["last_scan_updated_count"], 1)
        self.assertEqual(
            ready.payload["states"]["000001.SZ"]["first_seal_hhmm"], 1408
        )

        monitor.broker = _QuoteBroker({})
        with patch(
            "scripts.monitor_strategy_d_intraday.now_beijing",
            return_value=self.now + dt.timedelta(seconds=30),
        ), patch(
            "scripts.monitor_strategy_d_intraday.today_beijing",
            return_value=self.now.date(),
        ), patch(
            "scripts.monitor_strategy_d_intraday.notify",
            return_value=True,
        ):
            monitor.poll_once()

        rejected = self._inspect(now=self.now + dt.timedelta(seconds=30))
        self.assertFalse(rejected.ok)
        self.assertTrue(monitor.path_integrity_failed)


if __name__ == "__main__":
    unittest.main()
