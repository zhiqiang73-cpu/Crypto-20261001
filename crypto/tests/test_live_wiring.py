"""实盘接线回归（2026-10-05）：
* live + 双重确认 → 客户端默认用主网凭据/地址；
* live 未确认 → 仍然拒绝（闸门不放松）；
* 默认（无 live）行为与旧版完全一致（测试网）；
* 行情端点跟随运行模式（live→主网，其余→测试网）。
"""
from __future__ import annotations
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


ENV_KEYS = (
    "TRADING_MODE", "CONFIRM_MAINNET",
    "BINANCE_MAINNET_API_KEY", "BINANCE_MAINNET_API_SECRET",
    "BINANCE_MAINNET_BASE_URL",
    "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET",
    "BINANCE_TESTNET_BASE_URL",
)


class LiveWiringTests(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.copy()
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp = TemporaryDirectory()
        patcher = mock.patch(
            "config.secrets.SECRETS_FILE",
            Path(self._tmp.name) / "secrets.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    # ---- 客户端默认凭据/地址 -------------------------------------------
    def test_live_client_uses_mainnet_credentials(self):
        os.environ["TRADING_MODE"] = "live"
        os.environ["CONFIRM_MAINNET"] = "YES_I_UNDERSTAND"
        os.environ["BINANCE_MAINNET_API_KEY"] = "mk"
        os.environ["BINANCE_MAINNET_API_SECRET"] = "ms"
        from trading.binance_client import BinanceTestnetClient
        client = BinanceTestnetClient()
        self.assertEqual(client.base_url, "https://fapi.binance.com")
        self.assertEqual(client.api_key, "mk")
        self.assertEqual(client.api_secret, "ms")
        self.assertTrue(client.configured)

    def test_live_without_confirm_is_blocked_at_construction(self):
        os.environ["TRADING_MODE"] = "live"  # 不给第二道确认
        os.environ["BINANCE_MAINNET_API_KEY"] = "mk"
        os.environ["BINANCE_MAINNET_API_SECRET"] = "ms"
        from trading.binance_client import BinanceTestnetClient
        with self.assertRaises(RuntimeError):
            BinanceTestnetClient()

    def test_default_behavior_stays_testnet(self):
        os.environ["BINANCE_TESTNET_API_KEY"] = "tk"
        os.environ["BINANCE_TESTNET_API_SECRET"] = "ts"
        from trading.binance_client import BinanceTestnetClient
        client = BinanceTestnetClient()
        self.assertEqual(client.base_url, "https://testnet.binancefuture.com")
        self.assertEqual(client.api_key, "tk")

    def test_explicit_params_still_win(self):
        os.environ["TRADING_MODE"] = "live"
        os.environ["CONFIRM_MAINNET"] = "YES_I_UNDERSTAND"
        from trading.binance_client import BinanceTestnetClient
        client = BinanceTestnetClient(
            api_key="x", api_secret="y",
            base_url="https://testnet.binancefuture.com",
        )
        self.assertEqual(client.base_url, "https://testnet.binancefuture.com")

    # ---- 行情端点跟随模式 ----------------------------------------------
    def test_market_endpoints_follow_live_account(self):
        os.environ["TRADING_MODE"] = "live"
        os.environ["CONFIRM_MAINNET"] = "YES_I_UNDERSTAND"
        os.environ["BINANCE_MAINNET_BASE_URL"] = "https://fapi.binance.com"
        from config.market_endpoints import (MARKET_MAINNET,
                                             resolve_for_account)
        ep = resolve_for_account()
        self.assertEqual(ep.market, MARKET_MAINNET)
        self.assertEqual(ep.account_base_url, "https://fapi.binance.com")
        self.assertEqual(ep.source, "account_base_url")

    def test_market_endpoints_default_testnet(self):
        from config.market_endpoints import (MARKET_TESTNET,
                                             resolve_for_account)
        ep = resolve_for_account()
        self.assertEqual(ep.market, MARKET_TESTNET)
        self.assertTrue(ep.account_base_url)


if __name__ == "__main__":
    unittest.main()
