import unittest
from okx.Account import AccountAPI

class AccountTest(unittest.TestCase):
    def setUp(self):
        api_key = '3d8cd153-3eef-4f3c-81e8-122ede8d1979'
        api_secret_key = '2123E88E3F544DAB2EC3B75779781801'
        passphrase = 'Srjsrj526526...'
        self.AccountAPI = AccountAPI(api_key, api_secret_key, passphrase, flag='0')# 0: 实盘，1： 模拟盘


    def test_account_risk(self):
        result = self.AccountAPI.get_account_bills()
        print('账户账单信息:', result)
        # 断言返回结果不为空
        self.assertIsNotNone(result)

if __name__ == '__main__':
    unittest.main()
