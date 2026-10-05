"""主网凭据只读验收模块的安全边界测试。

这些测试不联网；守住两个不可退让的约束：
1. 签名查询串中绝不能泄露 API Secret；
2. 模块只声明 Binance Futures 的两个 GET 端点。
"""
from __future__ import annotations

import hashlib
import hmac
import unittest
from urllib.parse import parse_qs

from trading.mainnet_readonly import (
    MAINNET_USDM_BASE,
    READ_ONLY_PATHS,
    MainnetReadOnlyError,
    signed_account_query,
)


class MainnetReadonlyTests(unittest.TestCase):
    def test_declares_only_get_safe_paths(self):
        self.assertEqual(
            READ_ONLY_PATHS,
            frozenset({"/fapi/v1/time", "/fapi/v2/account"}),
        )
        self.assertEqual(MAINNET_USDM_BASE, "https://fapi.binance.com")

    def test_signed_query_has_expected_hmac_without_secret_leak(self):
        secret = "unit-test-secret"
        query, signature = signed_account_query(secret, 1_234_567_890, 5_000)
        self.assertEqual(parse_qs(query), {
            "recvWindow": ["5000"],
            "timestamp": ["1234567890"],
        })
        expected = hmac.new(
            secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(signature, expected)
        self.assertNotIn(secret, query)
        self.assertNotIn(secret, signature)

    def test_rejects_blank_and_whitespace_credentials(self):
        with self.assertRaises(MainnetReadOnlyError):
            signed_account_query("", 1)
        with self.assertRaises(MainnetReadOnlyError):
            signed_account_query("has a space", 1)


if __name__ == "__main__":
    unittest.main()
