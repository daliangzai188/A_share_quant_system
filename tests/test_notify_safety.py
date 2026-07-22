from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.notify import notify


class NotificationSafetyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
