from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.qmt_single_owner import (
    assert_standalone_qmt_allowed,
    running_daemon_pid,
)


class QmtSingleOwnerTests(unittest.TestCase):
    def test_stale_daemon_pid_does_not_block_standalone_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".daemon_pid").write_text("998877", encoding="utf-8")
            with patch("src.qmt_single_owner._pid_alive", return_value=False):
                self.assertIsNone(running_daemon_pid(root))
                assert_standalone_qmt_allowed(root, caller="test")

    def test_live_daemon_blocks_second_qmt_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".daemon_pid").write_text("778899", encoding="utf-8")
            with patch("src.qmt_single_owner._pid_alive", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "不得并发建立第二条连接"):
                    assert_standalone_qmt_allowed(
                        root,
                        caller="test",
                        current_pid=123456,
                    )

    def test_daemon_process_itself_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".daemon_pid").write_text(str(os.getpid()), encoding="utf-8")
            with patch("src.qmt_single_owner._pid_alive", return_value=True):
                assert_standalone_qmt_allowed(root, caller="daemon")

    def test_all_standalone_trading_connections_have_owner_gate(self) -> None:
        root = Path(__file__).absolute().parents[1]
        guarded_files = {
            "src/live_order_gateway.py": "QMTBrokerAdapter.from_config",
            "scripts/qmt_connection_probe.py": "QMTBrokerAdapter.from_config",
            "scripts/probe_qmt_connection.py": "XtQuantTrader(",
            "scripts/monitor_strategy_d_intraday.py": "QMTBrokerAdapter.from_config",
        }
        for relative, constructor in guarded_files.items():
            source = (root / relative).read_text(encoding="utf-8")
            self.assertIn(constructor, source, relative)
            self.assertIn("assert_standalone_qmt_allowed", source, relative)


if __name__ == "__main__":
    unittest.main()
