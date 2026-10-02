"""部分成交后必须撤掉剩余挂单 —— 全部离线，不连接交易所。

存在理由（2026-10-02 21:46 真实事故）：
    追价下单目标 5.070 ETH，实际只成交 0.130 后函数就返回了，**没有撤单**，
    未成交的 4.940 继续以 GTC post-only 挂在盘口。运行器以为这张单已经结束，
    下一步净仓同步又下了一张 4.940 的补差单。两张单在同一秒全部成交：

        21:46:30  BUY 4.940 @2762.27  FILLED
        21:46:30  BUY 5.070 @2760.67  FILLED   ← 原单剩余部分

    持仓从目标 5.070 变成 10.010（正好翻倍），一分钟后被迫反向卖出 4.939
    纠正，白付一次买卖价差与手续费。

根因在 `place_limit_order` 的收尾：
    filled = float(last.cum_filled_qty or last.filled_qty or 0)
    if filled > 0:
        return last.to_order_result()      # ← 部分成交直接返回，跳过撤单
"""
from __future__ import annotations

import asyncio
import unittest

from trading.binance_client import BinanceTestnetClient
from trading.models import OrderState


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


CID = "e1511790948734506382"

NEW_RAW = {
    "orderId": 16812864781,
    "clientOrderId": CID,
    "symbol": "ETHUSDT",
    "side": "BUY",
    "type": "LIMIT",
    "status": "NEW",
    "origQty": "5.070",
    "executedQty": "0",
    "avgPrice": "0.00000",
    "price": "2760.00",
    "timeInForce": "GTX",
    "updateTime": 1790948734506,
}

PARTIAL_RAW = {**NEW_RAW, "status": "PARTIALLY_FILLED",
               "executedQty": "0.130", "avgPrice": "2760.67"}

FILLED_RAW = {**NEW_RAW, "status": "FILLED",
              "executedQty": "5.070", "avgPrice": "2760.67"}

CANCELED_RAW = {**PARTIAL_RAW, "status": "CANCELED"}


class _Harness:
    """最小桩：提交返回 NEW，轮询返回指定状态，撤单只记录调用。"""

    def __init__(self, poll_raw, cancel_raw=CANCELED_RAW):
        self.client = BinanceTestnetClient()
        self.poll_raw = poll_raw
        self.cancel_raw = cancel_raw
        self.cancel_calls = 0

        async def fake_request(method, path, params=None, signed=False):
            return dict(NEW_RAW)

        async def fake_position_mode():
            return False

        async def fake_lot_step(symbol):
            return 0.001

        async def fake_query(**kwargs):
            return self.client._raw_to_managed(
                dict(self.poll_raw), client_order_id=CID
            )

        async def fake_cancel(**kwargs):
            self.cancel_calls += 1
            return self.client._raw_to_managed(
                dict(self.cancel_raw), client_order_id=CID
            )

        self.client._request = fake_request          # type: ignore[assignment]
        self.client.get_position_mode = fake_position_mode  # type: ignore[assignment]
        self.client._lot_step = fake_lot_step        # type: ignore[assignment]
        self.client.query_order = fake_query         # type: ignore[assignment]
        self.client.cancel_order = fake_cancel       # type: ignore[assignment]

    def place(self, **overrides):
        kwargs = dict(
            side="LONG",
            quantity=5.07,
            price=2760.0,
            symbol="ETHUSDT",
            client_order_id=CID,
            fill_timeout_sec=0.05,
            poll_interval_sec=0.01,
            cancel_if_unfilled=True,
            post_only=True,
        )
        kwargs.update(overrides)
        return _run(self.client.place_limit_order(**kwargs))


class PartialFillCancelTest(unittest.TestCase):
    def test_partial_fill_still_cancels_remainder(self):
        """核心回归：部分成交后必须撤单，否则剩余量会二次成交。"""
        h = _Harness(PARTIAL_RAW)
        res = h.place()
        self.assertEqual(
            h.cancel_calls, 1,
            "部分成交后没有撤单 —— 未成交的剩余量会留在盘口，"
            "与下一步的补差单同时成交，仓位翻倍",
        )
        self.assertAlmostEqual(float(res.cum_filled_qty or 0), 0.13, places=6)
        self.assertTrue(res.ok, "部分成交应视为已确认的部分成交, 不能报失败")

    def test_fully_filled_does_not_cancel(self):
        """完全成交时不该再去撤单。"""
        h = _Harness(FILLED_RAW)
        res = h.place()
        self.assertEqual(h.cancel_calls, 0)
        self.assertEqual(res.order_state, OrderState.FILLED.value)
        self.assertAlmostEqual(float(res.cum_filled_qty or 0), 5.07, places=6)

    def test_zero_fill_still_cancels(self):
        """完全未成交时照旧撤单（原有行为不能退化）。"""
        h = _Harness(NEW_RAW, cancel_raw={**NEW_RAW, "status": "CANCELED"})
        res = h.place()
        self.assertEqual(h.cancel_calls, 1)
        self.assertAlmostEqual(float(res.cum_filled_qty or 0), 0.0, places=6)

    def test_cancel_receipt_without_qty_keeps_polled_fill(self):
        """撤单回执没带成交量时，用轮询到的成交量兜底，不能记成 0。"""
        h = _Harness(PARTIAL_RAW, cancel_raw={**NEW_RAW, "status": "CANCELED"})
        res = h.place()
        self.assertEqual(h.cancel_calls, 1)
        self.assertAlmostEqual(
            float(res.cum_filled_qty or 0), 0.13, places=6,
            msg="撤单回执缺少成交量时把已成交的 0.13 记成了 0",
        )


if __name__ == "__main__":
    unittest.main()
