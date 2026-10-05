"""正常止损（1.5×ATR）与保本止损（浮盈 +1.5×ATR 后移到入场价）测试。

用户 2026-10-03 确认、2026-10-04 要求落到实盘的口径：
  15m 正常止损 = 1.5 × ATR_1H；浮盈达 +1.5 × ATR_1H 后止损上移到入场价；
  灾难止损 3 × ATR 保留（由 disaster_limit_stop 负责，本文件不覆盖）。

所有客户端调用均为内存桩，不连接交易所、不下单。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from shadow.deploy import protective_stop
from shadow.engine import BREAK_EVEN_TRIGGER_ATR, NORMAL_STOP_ATR
from shadow.strategy_books import SPECS, empty_book

_ENTRY_MS = 1_790_000_000_000


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    def __init__(self, *, remaining=0.0):
        self.remaining = remaining
        self.calls = []

    async def place_limit_chase(self, **kwargs):
        self.calls.append(("chase", kwargs))
        return SimpleNamespace(order_id="o1", client_order_id="c1", ok=True,
                               cum_filled_qty=kwargs.get("quantity", 0.0),
                               avg_price=100.0, raw={}, error="")

    async def get_position(self, symbol=None):
        return SimpleNamespace(quantity=self.remaining, side="FLAT",
                               entry_price=0.0)


def state_with_position(*, side=1, qty=0.5, px=100.0, atr=10.0, ms=_ENTRY_MS):
    st = {"strategies": {spec.id: empty_book() for spec in SPECS}}
    book = st["strategies"][SPECS[0].id]
    book["entry"] = {"side": side, "qty": qty, "px": px, "atr": atr, "ms": ms}
    return st, book


def stop_price_for(side, px, atr):
    return (px - NORMAL_STOP_ATR * atr) if side > 0 else (px + NORMAL_STOP_ATR * atr)


class TestNormalStop(unittest.TestCase):
    def test_long_not_triggered_above_stop(self):
        client = FakeClient()
        st, _ = state_with_position(side=1)
        mark = stop_price_for(1, 100.0, 10.0) + 0.01
        self.assertIsNone(run(protective_stop(
            client, st, symbol=SPECS[0].symbol, ex_side=1.0, entry_px=100.0,
            atr_1h=10.0, mark=mark, execute=True)))
        self.assertEqual(client.calls, [])

    def test_long_triggers_at_1_5_atr_and_closes_reduce_only(self):
        client = FakeClient(remaining=0.0)
        st, book = state_with_position(side=1)
        mark = stop_price_for(1, 100.0, 10.0)      # 恰好在止损价
        out = run(protective_stop(
            client, st, symbol=SPECS[0].symbol, ex_side=1.0, entry_px=100.0,
            atr_1h=10.0, mark=mark, execute=True))
        self.assertIsNotNone(out)
        self.assertEqual(out["action"], "正常止损平仓")
        self.assertTrue(out["flat"])
        _, kw = client.calls[0]
        self.assertEqual(kw["side"], "SHORT")      # 多头用卖出平
        self.assertTrue(kw["reduce_only"])
        self.assertTrue(kw["force_cross"])         # 仍是限价穿盘口，不是 MARKET
        self.assertIsNone(book["entry"], "平掉后账本必须清空")

    def test_short_triggers_upward(self):
        client = FakeClient(remaining=0.0)
        st, _ = state_with_position(side=-1)
        mark = stop_price_for(-1, 100.0, 10.0)
        out = run(protective_stop(
            client, st, symbol=SPECS[0].symbol, ex_side=-1.0, entry_px=100.0,
            atr_1h=10.0, mark=mark, execute=True))
        self.assertIsNotNone(out)
        self.assertEqual(client.calls[0][1]["side"], "LONG")

    def test_observation_mode_never_places_an_order(self):
        client = FakeClient()
        st, _ = state_with_position(side=1)
        self.assertIsNone(run(protective_stop(
            client, st, symbol=SPECS[0].symbol, ex_side=1.0, entry_px=100.0,
            atr_1h=10.0, mark=1.0, execute=False)))
        self.assertEqual(client.calls, [])

    def test_flat_position_is_ignored(self):
        client = FakeClient()
        st, _ = state_with_position(side=1)
        self.assertIsNone(run(protective_stop(
            client, st, symbol=SPECS[0].symbol, ex_side=0.0, entry_px=100.0,
            atr_1h=10.0, mark=1.0, execute=True)))


class TestBreakEvenStop(unittest.TestCase):
    def test_arming_at_1_5_atr_then_stop_moves_to_entry(self):
        client = FakeClient(remaining=0.0)
        st, book = state_with_position(side=1)
        # 浮盈刚好达到阈值 → 激活，但此刻不触发（价格在入场价上方）
        mark = 100.0 + BREAK_EVEN_TRIGGER_ATR * 10.0
        self.assertIsNone(run(protective_stop(
            client, st, symbol=SPECS[0].symbol, ex_side=1.0, entry_px=100.0,
            atr_1h=10.0, mark=mark, execute=True)))
        self.assertEqual(book.get("break_even_armed_ms"), _ENTRY_MS)
        self.assertEqual(client.calls, [], "激活本身不下单")

        # 价格回落到入场价 → 保本止损触发
        out = run(protective_stop(
            client, st, symbol=SPECS[0].symbol, ex_side=1.0, entry_px=100.0,
            atr_1h=10.0, mark=100.0, execute=True))
        self.assertIsNotNone(out)
        self.assertEqual(out["action"], "保本止损平仓")

    def test_below_threshold_does_not_arm(self):
        client = FakeClient()
        st, book = state_with_position(side=1)
        mark = 100.0 + BREAK_EVEN_TRIGGER_ATR * 10.0 - 0.01
        run(protective_stop(client, st, symbol=SPECS[0].symbol, ex_side=1.0,
                            entry_px=100.0, atr_1h=10.0, mark=mark, execute=True))
        self.assertIsNone(book.get("break_even_armed_ms"))

    def test_arming_is_bound_to_the_entry_bar(self):
        """上一笔的保本标记不能带到新仓上。"""
        client = FakeClient()
        st, book = state_with_position(side=1, ms=_ENTRY_MS)
        book["break_even_armed_ms"] = _ENTRY_MS - 1        # 属于更早的一笔
        mark = 100.0                                    # 未达保本，也未到 1.5×ATR
        self.assertIsNone(run(protective_stop(
            client, st, symbol=SPECS[0].symbol, ex_side=1.0, entry_px=100.0,
            atr_1h=10.0, mark=mark, execute=True)))
        self.assertEqual(client.calls, [], "旧标记不得让新仓在入场价就止损")

    def test_short_side_break_even(self):
        client = FakeClient(remaining=0.0)
        st, book = state_with_position(side=-1)
        mark = 100.0 - BREAK_EVEN_TRIGGER_ATR * 10.0      # 空头浮盈 1.5×ATR
        self.assertIsNone(run(protective_stop(
            client, st, symbol=SPECS[0].symbol, ex_side=-1.0, entry_px=100.0,
            atr_1h=10.0, mark=mark, execute=True)))
        out = run(protective_stop(
            client, st, symbol=SPECS[0].symbol, ex_side=-1.0, entry_px=100.0,
            atr_1h=10.0, mark=100.0, execute=True))
        self.assertEqual(out["action"], "保本止损平仓")


if __name__ == "__main__":
    unittest.main()
