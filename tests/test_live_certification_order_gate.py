from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
        },
        "portfolio_certification": {"require_live_certification": True},
        "logging": {"log_dir": "logs", "log_file": "test.log", "level": "INFO"},
    }


class LiveCertificationOrderGateTests(unittest.TestCase):
    def gateway(self, root: Path) -> LiveOrderGateway:
        path = root / "config.json"
        path.write_text(json.dumps(config()), encoding="utf-8")
        return LiveOrderGateway(path)

    def test_invalid_certification_blocks_buy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self.gateway(Path(temporary))
            with patch(
                "src.live_order_gateway.validate_live_certification",
                return_value=SimpleNamespace(ok=False, reason="hash mismatch"),
            ):
                with self.assertRaisesRegex(RuntimeError, "拒绝新增BUY"):
                    gateway.assert_real_order_allowed("CONFIRM", side="BUY")

    def test_invalid_certification_never_blocks_sell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self.gateway(Path(temporary))
            with patch(
                "src.live_order_gateway.validate_live_certification",
                return_value=SimpleNamespace(ok=False, reason="hash mismatch"),
            ) as validator:
                gateway.assert_real_order_allowed("CONFIRM", side="SELL")
                validator.assert_not_called()

    def test_side_must_be_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self.gateway(Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "明确声明"):
                gateway.assert_real_order_allowed("CONFIRM", side="")


if __name__ == "__main__":
    unittest.main()
