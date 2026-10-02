"""限价单被动挂单测试 — 验证「硬保证 maker + 兜底保证成交」的语义。

用户需求原文:
    "我要的是吃限价单的手续费, 这样可以低很多, 而且我又要可以成交。"
    "必须走限价单。"

设计 (2026-10-02 定稿):
    被动阶段一律 post-only (期货: type=LIMIT + timeInForce=GTX)。
    该 TIF 下交易所会**直接拒掉**任何会立即成交的单 (-5022),
    所以「只要成交, 手续费必定是 maker」不是约定, 是交易所规则。
    窗口耗尽后按 PASSIVE_ON_TIMEOUT 走兜底: 穿盘口限价单 (= taker)。

    ⚠ 挂价方向 (本文件的核心断言, 写反了手续费翻倍):
        做多 (BUY)  必须挂在最优买价之下  → 才可能躺进盘口当 maker
        做空 (SELL) 必须挂在最优卖价之上  → 同理
        买单挂在市价之上 = 立刻吃对手单 = taker
        卖单挂在市价之下 = 立刻吃对手单 = taker

因此本文件验证四件事:
  1. 挂价方向正确, 且绝不穿盘口 (被动阶段)
  2. 成交必被判为 maker; 撤单/成交竞态不得漏记
  3. post-only 被拒单时必须退档重挂
  4. 窗口耗尽后兜底穿盘口成交; 兜底也未成交必须明确失败, 绝不静默
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading.binance_client import (
    PASSIVE_CROSS_TICKS,
    PASSIVE_MAX_PAD,
    PASSIVE_MAX_REPRICE,
    BinanceTestnetClient,
)
from trading.models import ManagedOrder, OrderResult, OrderState


def _run(coro):
    return asyncio.run(coro)


# 极小但非零的被动窗口, 用来在毫秒内走到兜底分支。
# 注意: 不能用 0 —— place_limit_chase 里是 `window_sec or PASSIVE_WINDOW_SEC`,
# 0 是 falsy, 会被吃成默认的 180 秒 (见文件末尾的说明)。
_TINY_WINDOW = 1e-4


def _order_result(*, ok, filled=0.0, avg=0.0, qty=0.001, cid="c",
                  error="") -> OrderResult:
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
        error=error,
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


class _PassiveStub(BinanceTestnetClient):
    """脚本化交易所: 记录每次挂单的价格与参数, 可指定第几档成交 / 拒单。

    place_limit_order / query_order / book_ticker 被替换为受控实现,
    以便精确断言挂单循环自身的行为 (真实 place_limit_order 有独立测试覆盖)。
    """

    def __init__(
        self,
        *,
        fill_at_step=None,
        fill_crossing=False,
        reject_steps=(),
        requery_filled=False,
        bid=83000.00,
        ask=83000.50,
        tick="0.10",
        step="0.001",
    ) -> None:
        super().__init__(
            api_key="stub-key",
            api_secret="stub-secret",
            base_url="https://testnet.binancefuture.com",
        )
        self.fill_at_step = fill_at_step
        # 兜底那一单 (post_only=False) 是否成交。被动单永不成交时,
        # 用它可以精确验证「窗口耗尽 → 穿盘口 → 成交」这条路径。
        self.fill_crossing = fill_crossing
        self.reject_steps = set(reject_steps)
        self.requery_filled = requery_filled
        self._bid = float(bid)
        self._ask = float(ask)
        self._tick = float(tick)
        self._step = float(step)
        self.calls = []

    # --- 交易所元数据 -------------------------------------------------
    async def _lot_step(self, symbol=None) -> float:
        return self._step

    async def price_tick(self, symbol=None) -> float:
        return self._tick

    async def book_ticker(self, symbol=None):
        return {"bid": self._bid, "ask": self._ask}

    # --- 下单 ---------------------------------------------------------
    async def place_limit_order(self, side, quantity, price, symbol=None, **kw):
        idx = len(self.calls)
        self.calls.append(
            {
                "side": side,
                "qty": quantity,
                "price": price,
                "cid": kw.get("client_order_id", ""),
                "post_only": bool(kw.get("post_only")),
                "cancel_if_unfilled": bool(kw.get("cancel_if_unfilled")),
            }
        )
        if idx in self.reject_steps:
            return _order_result(
                ok=False, filled=0.0, qty=quantity,
                error="POST_ONLY_REJECT: -5022 could not be executed as maker",
            )
        if self.fill_crossing and not kw.get("post_only"):
            return _order_result(ok=True, filled=quantity, avg=price, qty=quantity)
        if self.fill_at_step is not None and idx == self.fill_at_step:
            return _order_result(ok=True, filled=quantity, avg=price, qty=quantity)
        return _order_result(ok=False, filled=0.0, qty=quantity)

    async def query_order(self, *, client_order_id=None, order_id=None, symbol=None):
        # 模拟「撤单后复核」: 默认未成交; requery_filled 时模拟竞态成交
        if self.requery_filled:
            return _managed(filled=self._step, state=OrderState.FILLED)
        return _managed(filled=0.0, state=OrderState.CANCELED)


class TestPassivePricing(unittest.TestCase):
    """挂价方向: 这是省手续费的全部要害。"""

    def test_long_rests_below_best_bid(self):
        """做多必须挂在最优买价之下, 否则立刻成交变 taker。"""
        c = _PassiveStub(fill_at_step=0)
        _run(c.place_limit_chase("LONG", 0.001))
        px = c.calls[0]["price"]
        self.assertLessEqual(px, c._bid, "做多挂价不得高于最优买价")
        self.assertLess(px, c._ask, "做多挂价必须严格低于最优卖价 (不穿盘口)")

    def test_short_rests_above_best_ask(self):
        """做空必须挂在最优卖价之上。"""
        c = _PassiveStub(fill_at_step=0)
        _run(c.place_limit_chase("SHORT", 0.001))
        px = c.calls[0]["price"]
        self.assertGreaterEqual(px, c._ask, "做空挂价不得低于最优卖价")
        self.assertGreater(px, c._bid, "做空挂价必须严格高于最优买价 (不穿盘口)")

    def test_never_crosses_the_book_on_any_passive_attempt(self):
        """整个被动阶段: 买单不碰卖价, 卖单不碰买价 —— 一次都不行。"""
        c = _PassiveStub(fill_at_step=None, reject_steps=range(6), )
        _run(c.place_limit_chase("LONG", 0.001, window_sec=0.35))
        self.assertTrue(c.calls, "至少应有一次挂单")
        for call in c.calls[:-1]:  # 末次可能是兜底穿盘口
            if call["post_only"]:
                self.assertLess(call["price"], c._ask)

    def test_passive_orders_are_post_only(self):
        """被动阶段必须带 post_only —— 这是 maker 的硬保证。"""
        c = _PassiveStub(fill_at_step=0)
        _run(c.place_limit_chase("LONG", 0.001))
        self.assertTrue(c.calls[0]["post_only"])

    def test_price_uses_book_not_mark(self):
        """挂价基于真实盘口: 盘口变了挂价必须跟着变。"""
        c = _PassiveStub(fill_at_step=0, bid=83000.00, ask=83000.50)
        _run(c.place_limit_chase("LONG", 0.001))
        first = c.calls[0]["price"]
        c2 = _PassiveStub(fill_at_step=0, bid=70000.00, ask=70000.50)
        _run(c2.place_limit_chase("LONG", 0.001))
        self.assertNotAlmostEqual(first, c2.calls[0]["price"], places=2)


class TestPostOnlyReject(unittest.TestCase):
    """post-only 被拒单 → 让开一档重挂。"""

    def test_reject_moves_price_away_from_book(self):
        """买单被拒后下一档必须更低; 卖单被拒后必须更高。"""
        c = _PassiveStub(fill_at_step=1, reject_steps=(0,))
        _run(c.place_limit_chase("LONG", 0.001))
        self.assertEqual(len(c.calls), 2)
        self.assertLess(c.calls[1]["price"], c.calls[0]["price"])

        c2 = _PassiveStub(fill_at_step=1, reject_steps=(0,))
        _run(c2.place_limit_chase("SHORT", 0.001))
        self.assertGreater(c2.calls[1]["price"], c2.calls[0]["price"])

    def test_reject_is_recorded_in_attempts(self):
        c = _PassiveStub(fill_at_step=1, reject_steps=(0,))
        res = _run(c.place_limit_chase("LONG", 0.001))
        attempts = res.raw["chase"]["attempts"]
        self.assertEqual(attempts[0].get("reject"), "post_only")
        self.assertEqual(attempts[0]["filled"], 0.0)

    def test_pad_is_capped(self):
        """连续被拒时退档幅度不得超过 PASSIVE_MAX_PAD。"""
        c = _PassiveStub(fill_at_step=None, reject_steps=range(60))
        _run(c.place_limit_chase("LONG", 0.001, window_sec=0.3))
        floor = c._bid - PASSIVE_MAX_PAD * c._tick
        for call in c.calls:
            if call["post_only"]:
                self.assertGreaterEqual(
                    call["price"], floor - 1e-9,
                    "退档超过了 PASSIVE_MAX_PAD 上限",
                )


class TestPassiveFill(unittest.TestCase):
    """成交识别: 被动成交必为 maker, 含撤单竞态。"""

    def test_fill_on_first_attempt_is_maker(self):
        c = _PassiveStub(fill_at_step=0)
        res = _run(c.place_limit_chase("LONG", 0.001))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        self.assertAlmostEqual(res.cum_filled_qty, 0.001)
        self.assertEqual(len(c.calls), 1, "第一档成交后不得继续重挂")
        self.assertTrue(res.raw["chase"]["likely_maker"])

    def test_fill_after_reject_is_still_maker(self):
        c = _PassiveStub(fill_at_step=1, reject_steps=(0,))
        res = _run(c.place_limit_chase("LONG", 0.001))
        self.assertTrue(res.ok)
        self.assertTrue(res.raw["chase"]["likely_maker"])
        self.assertEqual(res.raw["chase"]["steps_used"], 2)

    def test_cancel_race_filled_on_requery_is_success(self):
        """撤单与成交竞态: 撤单后复核发现已成交, 必须按成功返回。"""
        c = _PassiveStub(fill_at_step=None, requery_filled=True)
        res = _run(c.place_limit_chase("LONG", 0.001))
        self.assertTrue(res.ok, msg="竞态成交被误判为失败")
        self.assertAlmostEqual(res.cum_filled_qty, c._step)
        self.assertEqual(len(c.calls), 1)
        self.assertTrue(res.raw["chase"]["likely_maker"])

    def test_meta_records_attempts_and_final_step(self):
        c = _PassiveStub(fill_at_step=2, reject_steps=(0,))
        res = _run(c.place_limit_chase("LONG", 0.001))
        meta = res.raw["chase"]
        self.assertEqual(len(meta["attempts"]), 3)
        self.assertEqual(meta["final_step"], 2)
        self.assertEqual(meta["steps_used"], 3)
        self.assertEqual([a["filled"] for a in meta["attempts"][:2]], [0.0, 0.0])

    def test_short_fill_is_maker(self):
        c = _PassiveStub(fill_at_step=0)
        res = _run(c.place_limit_chase("SHORT", 0.001))
        self.assertTrue(res.ok)
        self.assertTrue(res.raw["chase"]["likely_maker"])


class TestTimeoutFallback(unittest.TestCase):
    """窗口耗尽 → 兜底穿盘口保证成交 (taker)。"""

    def test_window_exhausted_crosses_the_book(self):
        c = _PassiveStub(fill_crossing=True)  # 被动单不成交, 兜底成交
        res = _run(c.place_limit_chase("LONG", 0.001, window_sec=_TINY_WINDOW))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        last = c.calls[-1]
        self.assertGreater(last["price"], c._ask, "兜底必须穿盘口")
        self.assertFalse(last["post_only"], "兜底不得用 post-only, 否则必然被拒")

    def test_fallback_is_marked_taker(self):
        c = _PassiveStub(fill_crossing=True)
        res = _run(c.place_limit_chase("LONG", 0.001, window_sec=_TINY_WINDOW))
        self.assertFalse(res.raw["chase"]["likely_maker"])

    def test_fallback_slippage_is_bounded(self):
        """兜底滑点有上限: 超出盘口不超过 PASSIVE_CROSS_TICKS 个 tick。"""
        c = _PassiveStub(fill_crossing=True)
        _run(c.place_limit_chase("LONG", 0.001, window_sec=_TINY_WINDOW))
        self.assertAlmostEqual(
            c.calls[-1]["price"],
            round(c._ask + PASSIVE_CROSS_TICKS * c._tick, 1),
            places=4,
        )

    def test_short_fallback_crosses_downwards(self):
        c = _PassiveStub(fill_crossing=True)
        _run(c.place_limit_chase("SHORT", 0.001, window_sec=_TINY_WINDOW))
        self.assertLess(c.calls[-1]["price"], c._bid)

    def test_force_cross_skips_post_only_but_remains_limit(self):
        """灾难止损不等 180 秒，但仍用有滑点上限的 LIMIT，不用 MARKET。"""
        c = _PassiveStub(fill_crossing=True)
        res = _run(c.place_limit_chase("LONG", 0.001, force_cross=True))
        self.assertTrue(res.ok)
        self.assertEqual(len(c.calls), 1)
        self.assertFalse(c.calls[0]["post_only"])
        self.assertGreater(c.calls[0]["price"], c._ask)
        self.assertFalse(res.raw["chase"]["likely_maker"])

    def test_fallback_unfilled_returns_explicit_failure(self):
        """被动 + 兜底都没成交: 必须明确失败, 绝不静默挂单。"""
        c = _PassiveStub(fill_at_step=None, fill_crossing=False)
        res = _run(c.place_limit_chase("LONG", 0.001, window_sec=_TINY_WINDOW))
        self.assertFalse(res.ok)
        self.assertIn("passive_exhausted", res.error)
        self.assertEqual(res.cum_filled_qty, 0.0)

    def test_reprice_attempts_are_capped(self):
        """窗口内重挂次数有上限, 不得空转。"""
        c = _PassiveStub(fill_at_step=None)
        _run(c.place_limit_chase("LONG", 0.001, window_sec=600))
        passive = [x for x in c.calls if x["post_only"]]
        self.assertLessEqual(len(passive), PASSIVE_MAX_REPRICE)


class TestGuards(unittest.TestCase):
    """边界: 数量取整、盘口缺失。"""

    def test_quantity_rounded_down_to_step(self):
        c = _PassiveStub(fill_at_step=0)
        res = _run(c.place_limit_chase("LONG", 0.0019))
        self.assertTrue(res.ok)
        self.assertAlmostEqual(c.calls[0]["qty"], 0.001)

    def test_quantity_too_small_is_rejected(self):
        c = _PassiveStub(fill_at_step=0)
        res = _run(c.place_limit_chase("LONG", 0.0001))
        self.assertFalse(res.ok)
        self.assertIn("quantity too small", res.error)
        self.assertEqual(c.calls, [], "无效数量不得发出任何挂单")

    def test_book_unavailable_returns_failure(self):
        c = _PassiveStub(fill_at_step=0, bid=0.0, ask=0.0)
        res = _run(c.place_limit_chase("LONG", 0.001))
        self.assertFalse(res.ok)
        self.assertIn("book ticker unavailable", res.error)
        self.assertEqual(c.calls, [], "盘口缺失不得发出任何挂单")


if __name__ == "__main__":
    unittest.main()
