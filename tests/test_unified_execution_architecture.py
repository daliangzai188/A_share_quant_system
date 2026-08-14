from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class UnifiedExecutionArchitectureTests(unittest.TestCase):
    def test_every_live_order_request_has_strategy_and_idempotency_identity(self) -> None:
        for relative in (
            "scripts/trading_daemon.py",
            "scripts/monitor_strategy_d_intraday.py",
        ):
            path = PROJECT_ROOT / relative
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            missing: list[tuple[int, list[str]]] = []
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "OrderRequest"
                ):
                    continue
                keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
                absent = sorted({"strategy_leg", "source_key"} - keywords)
                if absent:
                    missing.append((node.lineno, absent))
            self.assertEqual(missing, [], f"{relative}存在绕过统一意图标识的委托")

    def test_legacy_gateway_order_submission_is_retired(self) -> None:
        source = (PROJECT_ROOT / "src/live_order_gateway.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        gateway = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "LiveOrderGateway"
        )
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in gateway.body
            if isinstance(node, ast.FunctionDef)
        }
        for name in ("submit", "submit_small_cash_test"):
            self.assertIn("raise RuntimeError", methods[name])
            self.assertIn("已退役", methods[name])

    def test_daemon_business_qmt_get_returns_only_execution_proxy(self) -> None:
        source = (PROJECT_ROOT / "scripts/trading_daemon.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("service.proxy", functions["_qmt_get"])
        self.assertNotIn("return _qmt_adapter", functions["_qmt_get"])
        self.assertIn("return _qmt_adapter", functions["_qmt_get_raw"])
        self.assertIn("timeout_callback=_on_qmt_execution_timeout", source)
        self.assertIn("_request_process_recovery(", functions["_on_qmt_execution_timeout"])
        self.assertIn("exit_code=EXIT_CODE_QMT_CHANNEL_POISONED", functions["_on_qmt_execution_timeout"])
        self.assertIn("os._exit(exit_code)", functions["_request_process_recovery"])

    def test_qmt_connect_and_account_verification_create_no_orphan_threads(self) -> None:
        source = (PROJECT_ROOT / "scripts/trading_daemon.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertNotIn("threading.Thread", functions["_qmt_connect_once"])
        self.assertNotIn("threading.Thread", functions["_qmt_query_account_positions"])
        self.assertIn('operation="connect_qmt"', functions["_qmt_get"])

    def test_live_d_uses_daemon_position_transaction_callback(self) -> None:
        daemon_source = (PROJECT_ROOT / "scripts/trading_daemon.py").read_text(encoding="utf-8")
        monitor_source = (
            PROJECT_ROOT / "scripts/monitor_strategy_d_intraday.py"
        ).read_text(encoding="utf-8")
        self.assertIn("position_recorder=_record_d_position", daemon_source)
        self.assertIn("if self.live_order and self.position_recorder is None", monitor_source)
        self.assertIn("self.position_recorder(payload)", monitor_source)

    def test_recovery_gate_precedes_every_trading_thread(self) -> None:
        source = (PROJECT_ROOT / "scripts/trading_daemon.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        main_source = ast.get_source_segment(source, main) or ""
        qmt_gate = main_source.index("wait_for_qmt_startup_gate()")
        recovery_gate = main_source.index("wait_for_trade_recovery_gate()")
        first_thread = main_source.index("threading.Thread(")
        self.assertLess(qmt_gate, recovery_gate)
        self.assertLess(recovery_gate, first_thread)

        recovery_source = source[
            source.index("def _recover_trade_execution_state_once"):
            source.index("def wait_for_trade_recovery_gate")
        ]
        for broker_truth in ("query_positions", "query_orders", "query_trades"):
            self.assertIn(broker_truth, recovery_source)

    def test_local_position_and_order_recovery_precedes_runtime_watchdogs(self) -> None:
        source = (PROJECT_ROOT / "scripts/trading_daemon.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        main_source = ast.get_source_segment(source, main) or ""
        first_runtime_watchdog = main_source.index('name="close-watchdog"')
        for required_recovery in (
            "_d_relay_pair_active_today()",
            "_pov_active_today()",
            "reconcile_d_orphan_fills()",
            "check_and_close_positions()",
        ):
            self.assertLess(
                main_source.index(required_recovery),
                first_runtime_watchdog,
                f"{required_recovery}必须在交易看门狗前完成",
            )


if __name__ == "__main__":
    unittest.main()
