import copy
import unittest
from src.account_privacy import mask_account_id, public_account_data


class AccountPrivacyTests(unittest.TestCase):
    def test_display_removes_name_and_id_without_changing_execution_identity(self):
        original = {'account_id': '1234567890', 'm_strAccountName': '测试姓名',
                    'raw': {'memo': '测试姓名 1234567890'},
                    'positions': [{'account_id': '1234567890', 'name': '证券名称', 'ts_code': '000001.SZ'}]}
        before = copy.deepcopy(original)
        public = public_account_data(original)
        self.assertEqual(public['account_id'], '***90')
        self.assertEqual(public['m_strAccountName'], '***')
        self.assertNotIn('测试姓名', str(public))
        self.assertNotIn('1234567890', str(public))
        self.assertEqual(public['positions'][0]['name'], '证券名称')
        self.assertEqual(original, before)

    def test_short_empty_and_already_masked_ids(self):
        self.assertEqual(mask_account_id(None), '***')
        self.assertEqual(mask_account_id('7'), '***7')
        self.assertEqual(mask_account_id('***90'), '***90')
