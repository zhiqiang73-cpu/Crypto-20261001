"""限价单追价循环测试 — 验证「争取 maker 手续费 + 保证成交」的语义。

用户需求原文:
    "我要的是吃限价单的手续费, 这样可以低很多, 而且我又要可以成交。
     所以就建议挂在当时价格的上或下一点点, B. 挂单 + 追价循环的方式。"

因此本文件重点验证三件事:
  1. 价格阶梯确实从「被动档」开始, 且逐档朝市价推进, 最后一档穿越盘口
  2. 任何一档成交都能被正确识别 (含撤单/成交竞态)
  3. 追完所有档仍未成交时, 必须明确返回失败, 绝不静默挂单
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading.binance_client import (
    CHASE_INITIAL_OFFSET_BPS,
    CHASE_MAX_STEPS,
    _CHASE_FRACS,
    BinanceTestnetClient,
)
from trading.models import ManagedOrder, OrderResult, OrderState


def _run(coro):
    return asyncio.run(coro)


def _order_result(*, ok, filled=0.0, avg=0.0, qty=0.001, cid="c") -> OrderResult:
    state = OrderState.FILLED if ok else OrderState.CANCELED
    return OrderResult(
        ok=ok,
        symbol="BTCUSDT",
        side="BUY",
        quantity=filled,
        requested_qty=qty,
        submitted_qty=qty,
        cum_filled_qty=filled,
        avg_price=avg,
        status=state.value,
        client_order_id=cid,
        order_state=state.value,
        error="" if ok else "unfilled",
    )


def _managed(*, filled=0.0, state=OrderState.CANCELED) -> ManagedOrder:
    return ManagedOrder(
        client_order_id="c",
        state=state,
        symbol="BTCUSDT",
        side="BUY",
        cum_filled_qty=filled,
        filled_qty=filled,
        avg_price=0.0,
        raw={},
    )


class _ChaseStub(BinanceTestnetClient):
    """脚本化交易所: 指定第几档成交, 并记录每次挂单的价格。

    place_limit_order / query_order 被替换为受控实现, 以便精确断言
    追价循环自身的行为 (真实 place_limit_order 已有独立测试覆盖)。
    """

    def __init__(
        self,
        *,
        fill_at_step=None,
        requery_filled=False,
        mark=83000.0,
        tick="0.10",
        step="0.001",
    ) -> None:
        super().__init__(
            api_key="stub-key",
            api_secret="stub-secret",
            base_url="https://testnet.binancefuture.com",
        )
        self.fill_at_step = fill_at_step
        self.requery_filled = requery_filled
        self._mark = mark
        self._tick = float(tick)
        self._step = float(step)
        self.calls = []

    async def _lot_step(self, symbol=None) -> float:
        return self._step

    async def price_tick(self, symbol=None) -> float:
        return self._tick

    async def mark_price(self, symbol=None) -> float:
        return self._mark

    async def place_limit_order(self, side, quantity, price, symbol=None, **kw):
        idx = len(self.calls)
        self.calls.append(
            {
                "side": side,
                "qty": quantity,
                "price": price,
                "cid": kw.get("client_order_id", ""),
            }
        )
        if self.fill_at_step is not None and idx == self.fill_at_step:
            return _order_result(ok=True, filled=quantity, avg=price, qty=quantity)
        return _order_result(ok=False, filled=0.0, qty=quantity)

    async def query_order(self, *, client_order_id=None, order_id=None, symbol=None):
        # 模拟「撤单后复核」: 默认未成交; requery_filled 时模拟竞态成交
        if self.requery_filled:
            return _managed(filled=self._step, state=OrderState.FILLED)
        return _managed(filled=0.0, state=OrderState.CANCELED)


class TestChaseLadder(unittest.TestCase):
    """价格阶梯: 被动起挂 → 逐档推进 → 最后一档穿越盘口。"""

    def test_ladder_matches_expected_fractions(self):
        client = _ChaseStub(fill_at_step=None)
        _run(client.place_limit_chase("LONG", 0.001))
        self.assertEqual(len(client.calls), CHASE_MAX_STEPS)
        tick = client._tick
        offset = max(tick, client._mark * CHASE_INITIAL_OFFSET_BPS / 10000.0)
        for call, frac in zip(client.calls, _CHASE_FRACS):
            expected = round(client._mark + frac * offset, 1)
            self.assertAlmostEqual(
                call["price"], expected, places=4,
                msg=f"档位价格不符: got={call['price']} want={expected}",
            )

    def test_first_step_is_passive_below_market_for_long(self):
        """做多第一档必须挂在市价下方 —— 这样才可能吃到 maker。"""
        client = _ChaseStub()
        _run(client.place_limit_chase("LONG", 0.001))
        self.assertLess(client.calls[0]["price"], client._mark)

    def test_last_step_crosses_market_to_guarantee_fill(self):
        """最后一档必须穿越盘口, 否则无法保证成交。"""
        client = _ChaseStub()
        _run(client.place_limit_chase("LONG", 0.001))
        self.assertGreater(client.calls[-1]["price"], client._mark)

    def test_short_side_ladder_is_mirrored(self):
        """做空第一档挂在市价上方, 最后一档穿越到下方。"""
        client = _ChaseStub()
        _run(client.place_limit_chase("SHORT", 0.001))
        self.assertGreater(client.calls[0]["price"], client._mark)
        self.assertLess(client.calls[-1]["price"], client._mark)

    def test_offset_never_below_one_tick(self):
        """价格极小时 offset 取 tick, 避免挂价与市价重合。"""
        client = _ChaseStub(mark=1.0, tick="0.10")
        _run(client.place_limit_chase("LONG", 0.001))
        self.assertLessEqual(client.calls[0]["price"], 1.0 - 0.10 + 1e-9)

    def test_max_steps_is_configurable(self):
        client = _ChaseStub()
        _run(client.place_limit_chase("LONG", 0.001, max_steps=3))
        self.assertEqual(len(client.calls), 3)


class TestChaseFillDetection(unittest.TestCase):
    """成交识别: 任何一档成交都必须被正确判定, 含竞态。"""

    def test_fill_at_first_step_is_maker(self):
        client = _ChaseStub(fill_at_step=0)
        res = _run(client.place_limit_chase("LONG", 0.001))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        self.assertAlmostEqual(res.cum_filled_qty, 0.001)
        self.assertEqual(len(client.calls), 1, "第一档成交后不得继续追价")
        self.assertTrue(res.raw["chase"]["likely_maker"])

    def test_fill_at_middle_step_is_not_maker(self):
        client = _ChaseStub(fill_at_step=2)
        res = _run(client.place_limit_chase("LONG", 0.001))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        self.assertEqual(res.raw["chase"]["steps_used"], 3)
        self.assertFalse(res.raw["chase"]["likely_maker"])

    def test_cancel_race_filled_on_requery_is_success(self):
        """撤单与成交竞态: 撤单后复核发现已成交, 必须按成功返回。"""
        client = _ChaseStub(fill_at_step=None, requery_filled=True)
        res = _run(client.place_limit_chase("LONG", 0.001))
        self.assertTrue(res.ok, msg="竞态成交被误判为失败")
        self.assertAlmostEqual(res.cum_filled_qty, client._step)
        self.assertEqual(len(client.calls), 1)

    def test_exhausted_returns_explicit_failure(self):
        """追完所有档仍未成交: 必须明确失败, 绝不静默。"""
        client = _ChaseStub(fill_at_step=None)
        res = _run(client.place_limit_chase("LONG", 0.001))
        self.assertFalse(res.ok)
        self.assertIn("chase_exhausted", res.error)
        self.assertEqual(res.cum_filled_qty, 0.0)
        self.assertEqual(len(client.calls), CHASE_MAX_STEPS)

    def test_chase_meta_records_all_attempts(self):
        client = _ChaseStub(fill_at_step=3)
        res = _run(client.place_limit_chase("LONG", 0.001))
        attempts = res.raw["chase"]["attempts"]
        self.assertEqual(len(attempts), 4)
        self.assertEqual(attempts[-1]["step"], 3)
        self.assertEqual([a["filled"] for a in attempts[:3]], [0.0, 0.0, 0.0])


class TestChaseGuards(unittest.TestCase):
    """边界: 数量取整、市价缺失、做空方向。"""

    def test_quantity_rounded_down_to_step(self):
        client = _ChaseStub(fill_at_step=0)
        res = _run(client.place_limit_chase("LONG", 0.0019))
        self.assertTrue(res.ok)
        self.assertAlmostEqual(client.calls[0]["qty"], 0.001)

    def test_quantity_too_small_is_rejected(self):
        client = _ChaseStub(fill_at_step=0)
        res = _run(client.place_limit_chase("LONG", 0.0001))
        self.assertFalse(res.ok)
        self.assertIn("quantity too small", res.error)
        self.assertEqual(client.calls, [], "无效数量不得发出任何挂单")

    def test_mark_price_unavailable_returns_failure(self):
        client = _ChaseStub(mark=0.0)
        res = _run(client.place_limit_chase("LONG", 0.001))
        self.assertFalse(res.ok)
        self.assertIn("mark price unavailable", res.error)
        self.assertEqual(client.calls, [])

    def test_short_fill_at_first_step_is_maker(self):
        client = _ChaseStub(fill_at_step=0)
        res = _run(client.place_limit_chase("SHORT", 0.001))
        self.assertTrue(res.ok)
        self.assertTrue(res.raw["chase"]["likely_maker"])


if __name__ == "__main__":
    unittest.main()
