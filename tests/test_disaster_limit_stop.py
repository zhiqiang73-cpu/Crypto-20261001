"""灾难止损执行测试。所有客户端调用均为内存桩，不连接交易所。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from shadow.deploy import DISASTER_ATR, disaster_limit_stop


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    def __init__(self, *, mark=100.0, remaining=0.0):
        self.mark = mark
        self.remaining = remaining
        self.calls = []

    async def mark_price(self, symbol=None):
        self.calls.append("mark_price")
        return self.mark

    async def place_limit_chase(self, **kwargs):
        self.calls.append(("place_limit_chase", kwargs))
        return SimpleNamespace(
            order_id="", client_order_id="", ok=True, cum_filled_qty=0.1,
            avg_price=self.mark, raw={}, error="",
        )

    async def get_position(self, symbol=None):
        self.calls.append("get_position")
        return SimpleNamespace(quantity=self.remaining)


class TestDisasterLimitStop(unittest.TestCase):
    def test_observation_mode_never_reads_mark_or_places_order(self):
        client = FakeClient(mark=50)
        out = run(disaster_limit_stop(
            client, {}, ex_side=0.1, entry_px=100, atr_1h=10, execute=False
        ))
        self.assertIsNone(out)
        self.assertEqual(client.calls, [])

    def test_untriggered_loss_does_not_submit_order(self):
        client = FakeClient(mark=75)  # 亏25，小于3×ATR=30
        out = run(disaster_limit_stop(
            client, {}, ex_side=0.1, entry_px=100, atr_1h=10, execute=True
        ))
        self.assertIsNone(out)
        self.assertEqual(client.calls, ["mark_price"])

    def test_long_trigger_uses_immediate_reduce_only_limit_cross(self):
        client = FakeClient(mark=69, remaining=0.0)  # 亏31，触发 3×ATR
        state = {"entry": {"side": 1}}
        out = run(disaster_limit_stop(
            client, state, ex_side=0.1, entry_px=100, atr_1h=10, execute=True
        ))
        self.assertEqual(out["action"], "灾难止损平仓")
        self.assertTrue(out["flat"])
        self.assertIsNone(state["entry"])
        call = next(x for x in client.calls if isinstance(x, tuple))[1]
        self.assertEqual(call["side"], "SHORT")
        self.assertTrue(call["reduce_only"])
        self.assertTrue(call["force_cross"])
        self.assertEqual(call["tag"], "kdj")

    def test_short_trigger_closes_with_long_limit(self):
        client = FakeClient(mark=131, remaining=0.02)
        out = run(disaster_limit_stop(
            client, {}, ex_side=-0.1, entry_px=100, atr_1h=10, execute=True
        ))
        self.assertEqual(out["action"], "灾难止损部分平仓")
        self.assertFalse(out["flat"])
        call = next(x for x in client.calls if isinstance(x, tuple))[1]
        self.assertEqual(call["side"], "LONG")
        self.assertTrue(call["reduce_only"])
        self.assertTrue(call["force_cross"])

    def test_invalid_atr_or_entry_is_safe_noop(self):
        for entry, atr in ((0, 10), (100, 0), (100, -1), (100, float("nan"))):
            with self.subTest(entry=entry, atr=atr):
                client = FakeClient(mark=1)
                self.assertIsNone(run(disaster_limit_stop(
                    client, {}, ex_side=0.1, entry_px=entry, atr_1h=atr,
                    execute=True,
                )))
                self.assertEqual(client.calls, [])

    def test_exact_threshold_triggers(self):
        client = FakeClient(mark=100 - DISASTER_ATR * 10, remaining=0)
        out = run(disaster_limit_stop(
            client, {}, ex_side=0.1, entry_px=100, atr_1h=10, execute=True
        ))
        self.assertIsNotNone(out)


if __name__ == "__main__":
    unittest.main()
