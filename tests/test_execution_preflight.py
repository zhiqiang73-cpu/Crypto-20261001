"""Testnet 执行账户预检测试。

预检的目的不是下单，而是防止策略在错误账户状态（双向/全仓/非10倍、遗留挂单）
里启动。所有测试都使用内存桩，不连接交易所、不提交委托。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from shadow.deploy import LEVERAGE, prepare_testnet_execution


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    def __init__(self, *, orders=None, quantity=0.0, hedge=False,
                 isolated=True, leverage=LEVERAGE, apply_changes=True):
        self.orders = list(orders or [])
        self.quantity = quantity
        self.hedge = hedge
        self.isolated = isolated
        self.leverage = leverage
        self.apply_changes = apply_changes
        self.calls = []

    async def get_open_orders(self, symbol=None):
        self.calls.append("get_open_orders")
        return self.orders

    async def get_position(self, symbol=None):
        self.calls.append("get_position")
        return SimpleNamespace(quantity=self.quantity)

    async def get_position_mode(self):
        self.calls.append("get_position_mode")
        return self.hedge

    async def get_position_settings(self, symbol=None):
        self.calls.append("get_position_settings")
        return {
            "symbol": symbol or "BTCUSDT",
            "isolated": self.isolated,
            "margin_type": "ISOLATED" if self.isolated else "CROSSED",
            "leverage": self.leverage,
        }

    async def set_one_way_mode(self):
        self.calls.append("set_one_way_mode")
        if self.apply_changes:
            self.hedge = False

    async def set_margin_type_isolated(self, symbol=None):
        self.calls.append("set_margin_type_isolated")
        if self.apply_changes:
            self.isolated = True

    async def set_leverage(self, leverage, symbol=None):
        self.calls.append(("set_leverage", leverage))
        if self.apply_changes:
            self.leverage = leverage


class TestExecutionPreflight(unittest.TestCase):
    def test_compliant_flat_account_has_no_writes(self):
        client = FakeClient()
        out = run(prepare_testnet_execution(client))
        self.assertEqual(out["position_mode"], "one_way")
        self.assertEqual(out["margin_type"], "ISOLATED")
        self.assertEqual(out["leverage"], LEVERAGE)
        self.assertEqual(out["changes"], [])
        self.assertNotIn("set_one_way_mode", client.calls)
        self.assertNotIn("set_margin_type_isolated", client.calls)
        self.assertFalse(any(isinstance(x, tuple) for x in client.calls))

    def test_flat_mismatch_is_reconfigured_and_verified(self):
        client = FakeClient(hedge=True, isolated=False, leverage=1)
        out = run(prepare_testnet_execution(client))
        self.assertIn("单向持仓", out["changes"])
        self.assertTrue(any("ISOLATED" in item for item in out["changes"]))
        self.assertTrue(any("10x" in item for item in out["changes"]))
        self.assertIn("set_one_way_mode", client.calls)
        self.assertIn("set_margin_type_isolated", client.calls)
        self.assertIn(("set_leverage", LEVERAGE), client.calls)
        self.assertEqual(out["leverage"], LEVERAGE)

    def test_open_orders_refuse_without_cancelling_them(self):
        client = FakeClient(orders=[{"orderId": 1}])
        with self.assertRaisesRegex(RuntimeError, "未完成委托"):
            run(prepare_testnet_execution(client))
        self.assertNotIn("set_one_way_mode", client.calls)
        self.assertNotIn("set_margin_type_isolated", client.calls)
        self.assertFalse(any(isinstance(x, tuple) for x in client.calls))

    def test_existing_position_with_mismatch_refuses_without_modifying(self):
        client = FakeClient(quantity=0.01, isolated=False, leverage=1)
        with self.assertRaisesRegex(RuntimeError, "现有仓位"):
            run(prepare_testnet_execution(client))
        self.assertNotIn("set_margin_type_isolated", client.calls)
        self.assertFalse(any(isinstance(x, tuple) for x in client.calls))

    def test_existing_position_already_compliant_is_allowed(self):
        client = FakeClient(quantity=0.01, isolated=True, leverage=LEVERAGE)
        out = run(prepare_testnet_execution(client))
        self.assertEqual(out["changes"], [])

    def test_failed_isolated_change_is_rejected(self):
        client = FakeClient(isolated=False, apply_changes=False)
        with self.assertRaisesRegex(RuntimeError, "逐仓"):
            run(prepare_testnet_execution(client))

    def test_failed_leverage_change_is_rejected(self):
        client = FakeClient(leverage=1, apply_changes=False)
        with self.assertRaisesRegex(RuntimeError, "10x"):
            run(prepare_testnet_execution(client))

    def test_failed_position_mode_change_is_rejected(self):
        client = FakeClient(hedge=True, apply_changes=False)
        with self.assertRaisesRegex(RuntimeError, "单向"):
            run(prepare_testnet_execution(client))


if __name__ == "__main__":
    unittest.main()
