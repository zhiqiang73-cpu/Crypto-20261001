"""组合仓位不变量测试 — 假交易所, 无网络副作用."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading.fake_exchange import FakeBinanceClient
from trading.position_manager import PositionManager


def _run(coro):
    return asyncio.run(coro)


class TestPortfolioNetting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "positions.json"
        self.client = FakeBinanceClient(equity=5000.0, mark=100.0)
        self.mgr = PositionManager(self.client, persist_path=self.path)
        self.mgr._ready = True  # skip exchange bootstrap mode flips

    def tearDown(self):
        self.tmp.cleanup()

    async def _exch_signed(self) -> float:
        p = await self.client.get_position()
        if p.side == "FLAT":
            return 0.0
        return p.quantity if p.side == "LONG" else -p.quantity

    def _assert_conserved(self):
        local = self.mgr._local_net()
        exch = _run(self._exch_signed())
        self.assertAlmostEqual(local, exch, places=5, msg=f"local={local} exch={exch}")

    def test_short_long_hedge_attribution(self):
        """短多 0.010 + 长空 0.004 → 净多 0.006; 策略数量按意图记账."""
        acts = _run(self.mgr.on_signal(
            "short_term", "STANDARD_LONG", 100.0, atr=2.0, cs=25.0
        ))
        self.assertTrue(acts)
        # 强制指定数量场景: 直接写账本再 sync 测对冲 bug
        from trading.models import HorizonPosition
        self.mgr.positions["short_term"] = HorizonPosition(
            horizon="short_term", side="LONG", quantity=0.010,
            entry_price=100.0, original_quantity=0.010, peak_price=100.0,
        )
        _run(self.mgr._sync_exchange_to_net(100.0))
        self.mgr.positions["long_term"] = HorizonPosition(
            horizon="long_term", side="SHORT", quantity=0.004,
            entry_price=100.0, original_quantity=0.004, peak_price=100.0,
        )
        _run(self.mgr._sync_exchange_to_net(100.0))

        # 策略账本保持意图数量
        self.assertAlmostEqual(self.mgr.positions["short_term"].quantity, 0.010, places=6)
        self.assertAlmostEqual(self.mgr.positions["long_term"].quantity, 0.004, places=6)
        self.assertEqual(self.mgr.positions["long_term"].side, "SHORT")
        # 净仓守恒
        self._assert_conserved()
        self.assertAlmostEqual(self.mgr._local_net(), 0.006, places=6)

    def test_reverse_hedge(self):
        """短空 + 长多."""
        from trading.models import HorizonPosition
        self.mgr.positions["short_term"] = HorizonPosition(
            horizon="short_term", side="SHORT", quantity=0.008,
            entry_price=100.0, original_quantity=0.008, peak_price=100.0,
        )
        _run(self.mgr._sync_exchange_to_net(100.0))
        self.mgr.positions["long_term"] = HorizonPosition(
            horizon="long_term", side="LONG", quantity=0.005,
            entry_price=100.0, original_quantity=0.005, peak_price=100.0,
        )
        _run(self.mgr._sync_exchange_to_net(100.0))
        self._assert_conserved()
        self.assertAlmostEqual(self.mgr._local_net(), -0.003, places=6)

    def test_same_direction(self):
        from trading.models import HorizonPosition
        self.mgr.positions["short_term"] = HorizonPosition(
            horizon="short_term", side="LONG", quantity=0.003,
            entry_price=100.0, original_quantity=0.003, peak_price=100.0,
        )
        _run(self.mgr._sync_exchange_to_net(100.0))
        self.mgr.positions["long_term"] = HorizonPosition(
            horizon="long_term", side="LONG", quantity=0.002,
            entry_price=100.0, original_quantity=0.002, peak_price=100.0,
        )
        _run(self.mgr._sync_exchange_to_net(100.0))
        self._assert_conserved()
        self.assertAlmostEqual(self.mgr._local_net(), 0.005, places=6)

    def test_partial_reduce_uses_reduce_only(self):
        from trading.models import HorizonPosition
        self.mgr.positions["short_term"] = HorizonPosition(
            horizon="short_term", side="LONG", quantity=0.010,
            entry_price=100.0, original_quantity=0.010, peak_price=100.0,
            remaining_pct=1.0,
        )
        _run(self.mgr._sync_exchange_to_net(100.0))
        before_orders = len(self.client.market_orders)
        act = _run(self.mgr.partial_close("short_term", 0.5, "tp1", 101.0))
        self.assertIsNotNone(act)
        self.assertAlmostEqual(self.mgr.positions["short_term"].quantity, 0.005, places=6)
        self._assert_conserved()
        # 最后一笔应为 reduce_only
        last = self.client.market_orders[-1]
        self.assertTrue(last["reduce_only"])

    def test_reverse_requires_close_first(self):
        acts = _run(self.mgr.on_signal(
            "short_term", "STANDARD_LONG", 100.0, atr=2.0, cs=25.0
        ))
        self.assertTrue(any(a.action == "open" for a in acts))
        acts2 = _run(self.mgr.on_signal(
            "short_term", "STANDARD_SHORT", 100.0, atr=2.0, cs=-25.0
        ))
        kinds = [a.action for a in acts2]
        self.assertIn("reverse_close", kinds)
        self.assertIn("reverse_open", kinds)
        self._assert_conserved()

    def test_response_lost_then_query(self):
        self.client._drop_response = True
        # market_open 会返回失败但交易所已成交 — sync 路径
        from trading.models import HorizonPosition
        self.mgr.positions["short_term"] = HorizonPosition(
            horizon="short_term", side="LONG", quantity=0.002,
            entry_price=100.0, original_quantity=0.002, peak_price=100.0,
        )
        order = _run(self.mgr._sync_exchange_to_net(100.0))
        # drop 后本地以为失败, 但交易所有仓
        self.assertFalse(order.ok)
        # 查询确认
        mo = _run(self.client.query_order(client_order_id=order.client_order_id))
        self.assertGreater(mo.filled_qty, 0)

    def test_persist_and_restore(self):
        from trading.models import HorizonPosition
        self.mgr.positions["short_term"] = HorizonPosition(
            horizon="short_term", side="LONG", quantity=0.007,
            entry_price=100.0, original_quantity=0.007, peak_price=100.0,
            entry_atr=2.0, entry_cs=22.0,
        )
        _run(self.mgr._sync_exchange_to_net(100.0))
        self.mgr._persist()

        mgr2 = PositionManager(self.client, persist_path=self.path)
        self.assertIsNotNone(mgr2.positions["short_term"])
        self.assertAlmostEqual(mgr2.positions["short_term"].quantity, 0.007, places=6)
        self.assertAlmostEqual(mgr2.positions["short_term"].entry_atr, 2.0, places=6)

    def test_reconcile_orphan(self):
        # 交易所人工仓, 本地空
        _run(self.client.market_open("LONG", 0.003))
        self.mgr.positions = {"short_term": None, "long_term": None}
        _run(self.mgr.reconcile_on_startup())
        self.assertTrue(self.mgr.reconciliation_needed)
        self.assertFalse(self.mgr._allow_new_entries)
        self.assertAlmostEqual(abs(self.mgr.orphan_exchange_qty), 0.003, places=6)

    def test_neutral_does_not_force_close(self):
        from trading.models import HorizonPosition
        self.mgr.positions["short_term"] = HorizonPosition(
            horizon="short_term", side="LONG", quantity=0.002,
            entry_price=100.0, original_quantity=0.002, peak_price=100.0,
        )
        _run(self.mgr._sync_exchange_to_net(100.0))
        acts = _run(self.mgr.on_signal("short_term", "NEUTRAL", 100.0, atr=2.0))
        self.assertEqual(acts, [])
        self.assertFalse(self.mgr.positions["short_term"].is_flat())

    def test_no_atr_rejects_open(self):
        acts = _run(self.mgr.on_signal(
            "short_term", "STANDARD_LONG", 100.0, atr=None, cs=30.0
        ))
        self.assertEqual(acts, [])

    def test_concurrent_signals_serialized(self):
        async def _both():
            t1 = asyncio.create_task(self.mgr.on_signal(
                "short_term", "STANDARD_LONG", 100.0, atr=2.0, cs=25.0
            ))
            t2 = asyncio.create_task(self.mgr.on_signal(
                "long_term", "STANDARD_SHORT", 100.0, atr=4.0, cs=-25.0
            ))
            await asyncio.gather(t1, t2)
            return self.mgr._local_net(), await self._exch_signed()

        local, exch = _run(_both())
        self.assertAlmostEqual(local, exch, places=5)


if __name__ == "__main__":
    unittest.main()
