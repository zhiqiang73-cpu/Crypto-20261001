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
        partial_fills=None,
        cross_partial=None,
        book_seq=None,
        min_notional=0.0,
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
        # {档位: 占请求量的比例} —— 模拟交易所只吃到一部分 (2026-10-04 P0 回归)。
        self.partial_fills = dict(partial_fills or {})
        self.cross_partial = cross_partial
        # 逐次变化的盘口, 用来验证跨档成交的加权均价。
        self.book_seq = list(book_seq or [])
        self._book_i = 0
        self._min_notional = float(min_notional)
        self._bid = float(bid)
        self._ask = float(ask)
        self._tick = float(tick)
        self._step = float(step)
        self.calls = []

    # --- 交易所元数据 -------------------------------------------------
    async def _lot_step(self, symbol=None) -> float:
        return self._step

    async def min_notional(self, symbol=None) -> float:
        return self._min_notional

    async def price_tick(self, symbol=None) -> float:
        return self._tick

    async def book_ticker(self, symbol=None):
        if self.book_seq:
            b = self.book_seq[min(self._book_i, len(self.book_seq) - 1)]
            self._book_i += 1
            return {"bid": b[0], "ask": b[1]}
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
                "time_in_force": kw.get("time_in_force", "GTC"),
            }
        )
        if idx in self.reject_steps:
            return _order_result(
                ok=False, filled=0.0, qty=quantity,
                error="POST_ONLY_REJECT: -5022 could not be executed as maker",
            )
        post_only = bool(kw.get("post_only"))
        if not post_only and self.cross_partial is not None:
            return _order_result(ok=True, filled=quantity * self.cross_partial,
                                 avg=price, qty=quantity)
        if idx in self.partial_fills:
            return _order_result(ok=True, filled=quantity * self.partial_fills[idx],
                                 avg=price, qty=quantity)
        if self.fill_crossing and not post_only:
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

    def test_partial_fill_does_not_end_the_chase(self):
        """2026-10-04 P0 回归：部分成交后必须继续追剩余量。

        真实事故：目标 0.2010 BTC 只成交 0.0048（2.4%），被动尝试仅 1 次就
        收工，剩下 97.6% 既没继续挂、也没走兜底 —— 仓位凭空消失。
        这里断言：第一档只吃到 40% 时，必须再挂一档把剩余 60% 追回来。
        """
        c = _PassiveStub(partial_fills={0: 0.4}, fill_at_step=1)
        res = _run(c.place_limit_chase("LONG", 0.010))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        self.assertEqual(len(c.calls), 2, "部分成交后必须继续挂下一档")
        self.assertAlmostEqual(c.calls[1]["qty"], 0.006, places=6)
        self.assertAlmostEqual(res.cum_filled_qty, 0.010, places=6)
        self.assertEqual(res.raw["chase"]["maker_filled"], 0.010)
        self.assertEqual(res.raw["chase"]["taker_filled"], 0.0)

    def test_partial_fills_accumulate_across_three_attempts(self):
        """连续三档各吃一部分，总量必须逐档累加而不是只记最后一次。"""
        # 比例刻意取能落在最小变动单位 (0.001) 网格上的值：剩余量每次都会按
        # lot_step 向下取整，取整损失是正确行为，不该被算成 bug。
        c = _PassiveStub(partial_fills={0: 0.4, 1: 0.5, 2: 1.0})
        res = _run(c.place_limit_chase("LONG", 0.010))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        self.assertEqual(len(c.calls), 3)
        self.assertAlmostEqual(res.cum_filled_qty, 0.010, places=6)
        for got, want in zip([a["filled"] for a in res.raw["chase"]["attempts"]],
                             [0.004, 0.003, 0.003]):
            self.assertAlmostEqual(got, want, places=6)
        for call, want in zip(c.calls, [0.010, 0.006, 0.003]):
            self.assertAlmostEqual(call["qty"], want, places=6)

    def test_avg_price_is_weighted_across_partial_fills(self):
        """跨档成交的均价必须按成交额加权，不能取最后一档的价。"""
        c = _PassiveStub(
            partial_fills={0: 0.5}, fill_at_step=1,
            book_seq=[(83000.00, 83000.50), (84000.00, 84000.50)],
        )
        res = _run(c.place_limit_chase("LONG", 0.010))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        self.assertAlmostEqual(c.calls[0]["price"], 83000.00, places=4)
        self.assertAlmostEqual(c.calls[1]["price"], 84000.00, places=4)
        self.assertAlmostEqual(res.avg_price, 83500.00, places=4)

    def test_partial_then_cross_completes_and_flags_both_sides(self):
        """被动吃到一半 + 兜底补齐：总量是全部，且 maker/taker 分开记账。"""
        c = _PassiveStub(partial_fills={0: 0.5}, fill_crossing=True)
        res = _run(c.place_limit_chase("LONG", 0.010,
                                       window_sec=_TINY_WINDOW))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        self.assertAlmostEqual(res.cum_filled_qty, 0.010, places=6)
        meta = res.raw["chase"]
        self.assertAlmostEqual(meta["maker_filled"], 0.005, places=6)
        self.assertAlmostEqual(meta["taker_filled"], 0.005, places=6)
        # 兜底只对剩余量下单，不得重复整笔。
        self.assertAlmostEqual(c.calls[-1]["qty"], 0.005, places=6)

    def test_partial_then_exhausted_still_reports_the_partial_fill(self):
        """兜底也没成交时，已拿到的被动成交量不得被丢掉。"""
        c = _PassiveStub(partial_fills={0: 0.5}, fill_crossing=False)
        res = _run(c.place_limit_chase("LONG", 0.010,
                                       window_sec=_TINY_WINDOW))
        self.assertTrue(res.ok, "部分成交不应被判为失败")
        self.assertAlmostEqual(res.cum_filled_qty, 0.005, places=6)
        self.assertIn("partial_then_exhausted", res.error)

    def test_remainder_below_min_notional_stops_cleanly(self):
        """剩余量小于最小名义金额时不再空转，按已成交量收工。"""
        c = _PassiveStub(partial_fills={0: 0.9999}, min_notional=100.0)
        res = _run(c.place_limit_chase("LONG", 0.010))
        self.assertTrue(res.ok, msg=f"error={res.error}")
        self.assertEqual(len(c.calls), 1, "剩余量已低于最小名义金额，不应再挂")
        self.assertAlmostEqual(res.cum_filled_qty, 0.009999, places=6)

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
        # 2026-10-03: 失败路径也必须写全 chase meta 键, 否则日志把
        # 「被动N次」读成「被动0次」, 真实失败原因 (如保证金不足) 无从判断。
        meta = res.raw["chase"]
        for key in ("attempts", "steps_used", "final_step", "likely_maker",
                    "passive_attempts"):
            self.assertIn(key, meta)

    def test_fallback_cannot_leave_a_resting_gtc_order(self):
        c = _PassiveStub(fill_crossing=False)
        _run(c.place_limit_chase("LONG", 0.001, force_cross=True))
        self.assertEqual(c.calls[-1]["time_in_force"], "IOC")

    def test_unknown_fallback_is_not_reported_canceled(self):
        class Unknown(_PassiveStub):
            async def place_limit_order(self, *args, **kwargs):
                return OrderResult(ok=False, client_order_id="pending-ioc",
                                   order_state=OrderState.UNKNOWN.value,
                                   status=OrderState.UNKNOWN.value,
                                   error="query timeout")
        res = _run(Unknown().place_limit_chase("LONG", 0.001, force_cross=True))
        self.assertEqual(res.order_state, OrderState.UNKNOWN.value)
        self.assertEqual(res.client_order_id, "pending-ioc")

    def test_ioc_partial_fill_survives_remainder_expiry(self):
        class Partial(_PassiveStub):
            async def place_limit_order(self, *args, **kwargs):
                return OrderResult(ok=False, cum_filled_qty=0.001,
                                   quantity=0.001, avg_price=83000.5,
                                   order_state=OrderState.CANCELED.value)
        res = _run(Partial().place_limit_chase("LONG", 0.002, force_cross=True))
        self.assertTrue(res.ok)
        self.assertEqual(res.cum_filled_qty, 0.001)

    def test_reprice_attempts_are_capped(self):
        """窗口内重挂次数有上限, 不得空转。"""
        c = _PassiveStub(fill_at_step=None)
        _run(c.place_limit_chase("LONG", 0.001, window_sec=600))
        passive = [x for x in c.calls if x["post_only"]]
        self.assertLessEqual(len(passive), PASSIVE_MAX_REPRICE)


class _RestingStub(_PassiveStub):
    """让 place_limit_order 真的跑 price_watch 轮询, 以验证驻留行为。

    _PassiveStub 直接替换了 place_limit_order, 因此跑不到新的「盘口不动就
    不撤单」逻辑。本 stub 复刻它的轮询语义: 每轮问一次 price_watch, 返回真值
    就收工(代表撤单), 否则一直等到本档耗尽。
    """

    def __init__(self, *, book_moves_after=None, **kw):
        super().__init__(**kw)
        self.book_moves_after = book_moves_after
        self.book_reads = 0
        self.polls = 0

    async def book_ticker(self, symbol=None):
        self.book_reads += 1
        if (self.book_moves_after is not None
                and self.book_reads > self.book_moves_after):
            return {"bid": 83100.0, "ask": 83100.5}
        return {"bid": 83000.0, "ask": 83000.5}

    async def place_limit_order(self, side, qty, px, symbol=None, *,
                                post_only=False, price_watch=None,
                                fill_timeout_sec=6.0, **kw):
        self.calls.append({"qty": qty, "price": px, "post_only": post_only,
                           "fill_timeout_sec": fill_timeout_sec})
        if not post_only:
            return _order_result(ok=False, filled=0.0, qty=qty, cid="cross")
        for _ in range(80):
            self.polls += 1
            await asyncio.sleep(0.004)      # 让 deadline 真实推进
            if price_watch is not None and await price_watch():
                break
        return _order_result(ok=False, filled=0.0, qty=qty, cid="passive")


class TestQueuePositionPreserved(unittest.TestCase):
    """2026-10-04 改造: 盘口不变时不撤单, 保住排队位置。

    交易所按「价格优先、时间优先」撮合, 撤单重挂等于排回队尾。
    实测 17:00 那单 180 秒里撤挂 28 次, maker 成交率只剩 6%。
    """

    def test_stable_book_does_not_requote(self):
        c = _RestingStub(fill_at_step=None)
        _run(c.place_limit_chase("LONG", 0.001, window_sec=0.1))
        passive = [x for x in c.calls if x["post_only"]]
        self.assertEqual(len(passive), 1,
                         "盘口没动就不该撤单重挂, 否则每次都在重排队尾")

    def test_moving_book_still_requotes(self):
        c = _RestingStub(fill_at_step=None, book_moves_after=3)
        _run(c.place_limit_chase("LONG", 0.001, window_sec=0.1))
        passive = [x for x in c.calls if x["post_only"]]
        self.assertGreater(len(passive), 1, "盘口动了必须跟盘口重挂")
        self.assertNotEqual(passive[0]["price"], passive[-1]["price"],
                            "重挂价格应跟随盘口")

    def test_resting_order_gets_full_window(self):
        """盘口不动时, 挂单的等待时间应覆盖整个剩余窗口, 而不是只有几秒。"""
        c = _RestingStub(fill_at_step=None)
        _run(c.place_limit_chase("LONG", 0.001, window_sec=120))
        first = [x for x in c.calls if x["post_only"]][0]
        self.assertGreater(first["fill_timeout_sec"], 60,
                           "盘口不动就该挂到窗口结束, 不能几秒一撤")

    def test_safety_checkpoint_runs_while_resting(self):
        """驻留期间主循环被占住, 安全检查点必须仍在每个轮询周期运行。"""
        seen = {"n": 0}

        async def on_step(bid, ask):
            seen["n"] += 1
            return seen["n"] >= 3

        c = _RestingStub(fill_at_step=None)
        res = _run(c.place_limit_chase("LONG", 0.001, window_sec=0.5,
                                       on_step=on_step))
        self.assertFalse(res.ok)
        self.assertIn("chase_aborted", res.error)
        self.assertGreaterEqual(seen["n"], 3)

    def test_abort_keeps_already_filled_maker_qty(self):
        """中止追价不得抹掉已经拿到的被动成交。"""
        class PartialThenAbort(_RestingStub):
            async def place_limit_order(self, side, qty, px, symbol=None, *,
                                        post_only=False, price_watch=None, **kw):
                self.calls.append({"qty": qty, "price": px,
                                   "post_only": post_only,
                                   "fill_timeout_sec": kw.get(
                                       "fill_timeout_sec", 0.0)})
                if not post_only:
                    return _order_result(ok=False, filled=0.0, qty=qty, cid="x")
                return _order_result(ok=True, filled=qty, avg=px,
                                     qty=qty, cid="p")

        c = PartialThenAbort(fill_at_step=None)
        res = _run(c.place_limit_chase("LONG", 0.001, window_sec=0.1))
        self.assertTrue(res.ok)
        self.assertAlmostEqual(res.cum_filled_qty, 0.001, places=6)


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
