from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

try:
    import dotenv  # noqa: F401
except ModuleNotFoundError:  # CI最小环境不加载QMT，只为导入网关提供无副作用替身。
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False
    sys.modules["dotenv"] = dotenv_stub

from src.live_order_gateway import LiveOrderGateway


def config() -> dict:
    return {
        "trade_mode": "live",
        "broker_adapter_enabled": True,
        "qmt_enabled": True,
        "broker": {"adapter": "qmt", "enabled": True},
        "live_trade": {
            "enabled": True,
            "real_order_enabled": True,
            "real_order_confirm_text": "CONFIRM",
            "allow_buy": True,
            "allow_sell": True,
        },
        "portfolio_certification": {"initial_equity": 500000},
        "logging": {"log_dir": "logs", "log_file": "test.log", "level": "INFO"},
    }


class LiveOrderGatewayTests(unittest.TestCase):
    def gateway(self, root: Path) -> LiveOrderGateway:
        path = root / "config.json"
        path.write_text(json.dumps(config()), encoding="utf-8")
        return LiveOrderGateway(path)

    def test_buy_uses_execution_gates_without_research_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self.gateway(Path(temporary))
            gateway.assert_real_order_allowed("CONFIRM", side="BUY")

    def test_sell_uses_execution_gates_without_research_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self.gateway(Path(temporary))
            gateway.assert_real_order_allowed("CONFIRM", side="SELL")

    def test_side_must_be_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self.gateway(Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "明确声明"):
                gateway.assert_real_order_allowed("CONFIRM", side="")

    def test_buy_switch_is_enforced_at_lowest_order_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = config()
            payload["live_trade"]["allow_buy"] = False
            path = root / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            gateway = LiveOrderGateway(path)
            with self.assertRaisesRegex(RuntimeError, "allow_buy=false"):
                gateway.assert_real_order_allowed("CONFIRM", side="BUY")
            gateway.assert_real_order_allowed("CONFIRM", side="SELL")

    def test_sell_switch_does_not_reopen_buy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = config()
            payload["live_trade"]["allow_sell"] = False
            path = root / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            gateway = LiveOrderGateway(path)
            gateway.assert_real_order_allowed("CONFIRM", side="BUY")
            with self.assertRaisesRegex(RuntimeError, "allow_sell=false"):
                gateway.assert_real_order_allowed("CONFIRM", side="SELL")


if __name__ == "__main__":
    unittest.main()
