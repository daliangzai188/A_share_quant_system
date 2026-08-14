from __future__ import annotations

import ast
import unittest
from pathlib import Path

from src.live_order_gateway import LiveOrderGateway


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
        gateway = object.__new__(LiveOrderGateway)
        with self.assertRaisesRegex(RuntimeError, "已退役"):
            gateway.submit("unused.csv", "unused", "unused")
        with self.assertRaisesRegex(RuntimeError, "已退役"):
            gateway.submit_small_cash_test("unused.csv", "unused")

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


if __name__ == "__main__":
    unittest.main()
