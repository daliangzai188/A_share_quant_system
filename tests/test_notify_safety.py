from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from src import notify as notify_module
from src.notify import notify


class NotificationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        with notify_module._lock:
            notify_module._last_sent.clear()
            notify_module._inflight.clear()

    def test_environment_kill_switch_blocks_external_notification(self) -> None:
        with patch.dict(os.environ, {"A_SYSTEM_DISABLE_NOTIFICATIONS": "1"}, clear=False), patch(
            "src.notify.urllib.request.urlopen"
        ) as urlopen:
            sent = notify(
                "sell_fail",
                "测试冻结时钟告警",
                "这条消息不得进入正式Bark通知中心。",
                level="critical",
                call=True,
            )

        self.assertFalse(sent)
        urlopen.assert_not_called()

    def test_failed_network_delivery_does_not_consume_throttle_window(self) -> None:
        cfg = {
            "enabled": True,
            "channel": "bark",
            "throttle_sec": 300,
            "events": {},
        }
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with (
            patch.dict(os.environ, {"A_SYSTEM_DISABLE_NOTIFICATIONS": "0"}, clear=False),
            patch.object(notify_module, "_load_notify_config", return_value=cfg),
            patch.object(notify_module, "_bark_url", return_value="https://example.invalid/key"),
            patch.object(
                notify_module.urllib.request,
                "urlopen",
                side_effect=[OSError("offline"), response],
            ) as urlopen,
        ):
            first = notify("connection", "恢复", "body")
            second = notify("connection", "恢复", "body")
            third = notify("connection", "恢复", "body")

        self.assertFalse(first)
        self.assertTrue(second)
        self.assertFalse(third)
        self.assertEqual(urlopen.call_count, 2)

    def test_inflight_duplicate_is_suppressed_without_marking_success(self) -> None:
        key = "same"
        self.assertTrue(notify_module._begin_delivery(key, 300))
        self.assertFalse(notify_module._begin_delivery(key, 300))
        notify_module._finish_delivery(key, success=False)
        self.assertTrue(notify_module._begin_delivery(key, 300))
        notify_module._finish_delivery(key, success=True)
        self.assertFalse(notify_module._begin_delivery(key, 300))


if __name__ == "__main__":
    unittest.main()
