import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.show_trading_status import local_runtime_status, safe_order, safe_trade


class TradingStatusTests(unittest.TestCase):
    def test_order_and_trade_exports_have_no_account_identity(self):
        order = safe_order({
            'm_strAccountID': '1234567890', 'm_strAccountName': '测试姓名',
            'stock_code': '600000.SH', 'name': '浦发银行', 'side': 'BUY',
            'order_volume': 100, 'traded_volume': 40, 'price': 10.1,
            'order_status': 55, 'order_id': 'O1',
        })
        trade = safe_trade({
            'm_strAccountID': '1234567890', 'm_strAccountName': '测试姓名',
            'stock_code': '600000.SH', 'name': '浦发银行', 'side': 'BUY',
            'traded_volume': 40, 'traded_price': 10.1, 'traded_id': 'T1',
        })
        public = str((order, trade))
        self.assertNotIn('1234567890', public)
        self.assertNotIn('测试姓名', public)
        self.assertIn('浦发银行', public)

    def test_stopped_daemon_makes_notifications_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('scripts.show_trading_status.PROJECT_ROOT', root), \
                 patch.dict('os.environ', {'BARK_URL': 'https://example.invalid/key'}, clear=False):
                status = local_runtime_status({'notify': {'enabled': True, 'channel': 'bark'}})
        self.assertFalse(status['daemon_running'])
        self.assertTrue(status['notification']['configured'])
        self.assertFalse(status['notification']['active_now'])


if __name__ == '__main__':
    unittest.main()
