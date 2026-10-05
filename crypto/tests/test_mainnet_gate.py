"""主网双确认闸门的回归测试。

守住的语义（2026-10-05 改动）：
  1. **默认行为完全不变** —— 没有确认串时，主网 URL 与 LIVE 模式都必须被拒绝。
     这是最重要的一组：把「无条件禁止」改成「双确认」时，绝不能顺手把默认打开。
  2. 只有 `TRADING_MODE=live` **且** `CONFIRM_MAINNET=YES_I_UNDERSTAND` 才放行。
  3. 确认串必须逐字匹配（防手滑、防大小写差异被当成确认）。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from trading.runtime_mode import (
    CONFIRM_ENV,
    MAINNET_CONFIRM_TOKEN,
    TradingMode,
    current_mode,
    is_mainnet_url,
    mainnet_confirmed,
    startup_status,
    validate_exchange_target,
)

MAINNET_REST = "https://fapi.binance.com"
TESTNET_REST = "https://testnet.binancefuture.com"


def _env(**kw):
    """构造干净的环境变量（清掉两个相关键后再按需设置）。"""
    env = {k: v for k, v in os.environ.items() if k not in ("TRADING_MODE", CONFIRM_ENV)}
    env.update({k: v for k, v in kw.items() if v is not None})
    return mock.patch.dict(os.environ, env, clear=True)


class TestDefaultStillBlocks(unittest.TestCase):
    """默认配置必须继续阻断主网 —— 防回归的核心。"""

    def test_mainnet_url_blocked_with_no_env(self):
        with _env():
            with self.assertRaises(RuntimeError) as ctx:
                validate_exchange_target(MAINNET_REST)
            self.assertIn("主网 URL", str(ctx.exception))

    def test_live_mode_blocked_with_no_confirm(self):
        with _env(TRADING_MODE="live"):
            with self.assertRaises(RuntimeError) as ctx:
                validate_exchange_target(TESTNET_REST)
            self.assertIn("第二道确认", str(ctx.exception))

    def test_mainnet_url_blocked_in_testnet_mode(self):
        with _env(TRADING_MODE="testnet"):
            with self.assertRaises(RuntimeError):
                validate_exchange_target(MAINNET_REST)

    def test_testnet_url_passes(self):
        with _env(TRADING_MODE="testnet"):
            validate_exchange_target(TESTNET_REST)  # 不应抛错

    def test_startup_status_reports_not_allowed(self):
        with _env(TRADING_MODE="testnet"):
            st = startup_status()
            self.assertFalse(st["live_allowed"])
            self.assertFalse(st["mainnet_confirmed"])
            self.assertFalse(st["entries_default"])


class TestConfirmTokenMustMatchExactly(unittest.TestCase):
    """确认串必须逐字匹配。"""

    def test_wrong_token_does_not_confirm(self):
        for bad in ("yes_i_understand", "YES", "true", "1", "YES_I_UNDERSTAND_NOW", ""):
            with self.subTest(bad=bad), _env(CONFIRM_MAINNET=bad):
                self.assertFalse(mainnet_confirmed(), f"{bad!r} 不该被当成确认")

    def test_exact_token_confirms(self):
        with _env(CONFIRM_MAINNET=MAINNET_CONFIRM_TOKEN):
            self.assertTrue(mainnet_confirmed())

    def test_surrounding_whitespace_is_tolerated(self):
        """环境变量常带尾部换行/空格，strip 后匹配即算确认（有意设计）。"""
        for ok in (f" {MAINNET_CONFIRM_TOKEN} ", f"{MAINNET_CONFIRM_TOKEN}\n"):
            with self.subTest(ok=ok), _env(CONFIRM_MAINNET=ok):
                self.assertTrue(mainnet_confirmed(), f"{ok!r} 应被接受")

    def test_live_plus_wrong_token_still_blocked(self):
        with _env(TRADING_MODE="live", CONFIRM_MAINNET="yes"):
            with self.assertRaises(RuntimeError):
                validate_exchange_target(MAINNET_REST)


class TestDoubleConfirmAllowsMainnet(unittest.TestCase):
    """两道齐备才放行。"""

    def test_live_plus_token_allows_mainnet_url(self):
        with _env(TRADING_MODE="live", CONFIRM_MAINNET=MAINNET_CONFIRM_TOKEN):
            validate_exchange_target(MAINNET_REST)  # 不应抛错

    def test_token_alone_without_live_still_allows_url_but_not_live_semantics(self):
        """确认串给了、但模式不是 live：URL 放行，startup_status 仍报未启用主网。"""
        with _env(TRADING_MODE="testnet", CONFIRM_MAINNET=MAINNET_CONFIRM_TOKEN):
            validate_exchange_target(MAINNET_REST)
            self.assertFalse(startup_status()["live_allowed"])

    def test_startup_status_reports_allowed(self):
        with _env(TRADING_MODE="live", CONFIRM_MAINNET=MAINNET_CONFIRM_TOKEN):
            st = startup_status()
            self.assertTrue(st["live_allowed"])
            self.assertTrue(st["mainnet_confirmed"])
            self.assertEqual(st["mode"], "live")
            self.assertIn("已启用", st["reason"])


class TestHelpers(unittest.TestCase):
    def test_is_mainnet_url(self):
        self.assertTrue(is_mainnet_url(MAINNET_REST))
        self.assertTrue(is_mainnet_url("https://api.binance.com"))
        self.assertFalse(is_mainnet_url(TESTNET_REST))
        self.assertFalse(is_mainnet_url(""))

    def test_current_mode_defaults_to_paper(self):
        with _env():
            self.assertIs(current_mode(), TradingMode.PAPER)

    def test_unknown_mode_falls_back_to_paper(self):
        with _env(TRADING_MODE="whatever"):
            self.assertIs(current_mode(), TradingMode.PAPER)


if __name__ == "__main__":
    unittest.main()
