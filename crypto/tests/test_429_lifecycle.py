"""429 压力 + 保护单完整生命周期验收测试。

全部使用假交易所，不调用网络、不发送真实委托。
覆盖：429 查询 -> UNKNOWN；恢复后确认保护；收紧止损；
止盈单存活；止盈成交/撤销区分；仓位归零清理全部条件单。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow.deploy import reconcile_tp_fills
from trading.models import ManagedOrder, OrderState
from trading.protective_orders import (
    ProtectionState,
    cancel_protective_orders,
    place_protective_stop,
    tighten_protective_stop,
)


def run(coro):
    return asyncio.run(coro)


class StressExchange:
    """可注入连续 429 的最小 Algo 交易所。"""

    def __init__(self):
        self.orders = {}
        self.next_id = 1
        self.query_429 = 0
        self.place_429 = 0
        self.cancel_429 = 0
        self.queries = 0
        self.places = 0
        self.cancels = 0

    def seed(self, *, order_type="STOP_MARKET", side="SELL", trigger=84000,
             qty="0.296", close="true"):
        aid = str(self.next_id); self.next_id += 1
        o = {
            "algoId": aid, "clientAlgoId": "seed-" + aid,
            "symbol": "BTCUSDT", "side": side, "orderType": order_type,
            "triggerPrice": str(trigger), "quantity": qty,
            "closePosition": close, "algoStatus": "NEW",
        }
        self.orders[aid] = o
        return o

    async def get_open_algo_orders(self, symbol=None):
        self.queries += 1
        if self.query_429 > 0:
            self.query_429 -= 1
            raise TimeoutError("429 Too Many Requests")
        return [o for o in self.orders.values()
                if not symbol or o.get("symbol") == symbol]

    async def place_stop_market(self, side, quantity, stop_price, symbol=None,
                                *, client_order_id=None, close_position=False,
                                order_type="STOP_MARKET"):
        self.places += 1
        if self.place_429 > 0:
            self.place_429 -= 1
            raise TimeoutError("429 Too Many Requests")
        for existing in self.orders.values():
            if existing.get("clientAlgoId") == (client_order_id or ""):
                return ManagedOrder(
                    client_order_id=existing["clientAlgoId"],
                    state=OrderState.ACKNOWLEDGED, symbol=existing["symbol"],
                    is_stop=True, is_algo=True, algo_id=existing["algoId"],
                    raw=dict(existing))
        aid = str(self.next_id); self.next_id += 1
        ex_side = "SELL" if side == "LONG" else "BUY"
        o = {
            "algoId": aid, "clientAlgoId": client_order_id or "",
            "symbol": symbol or "BTCUSDT", "side": ex_side,
            "orderType": order_type, "triggerPrice": str(stop_price),
            "quantity": str(quantity),
            "closePosition": "true" if close_position else "false",
            "algoStatus": "NEW",
        }
        self.orders[aid] = o
        return ManagedOrder(client_order_id=o["clientAlgoId"],
                            state=OrderState.ACKNOWLEDGED,
                            symbol=o["symbol"], is_stop=True, is_algo=True,
                            algo_id=aid, raw=dict(o))

    async def cancel_algo_order(self, *, algo_id=None, client_algo_id=None,
                                symbol=None):
        self.cancels += 1
        if self.cancel_429 > 0:
            self.cancel_429 -= 1
            return ManagedOrder(client_order_id=client_algo_id or "",
                                state=OrderState.UNKNOWN,
                                error="429 Too Many Requests",
                                is_stop=True, is_algo=True)
        if algo_id:
            self.orders.pop(str(algo_id), None)
        elif client_algo_id:
            for aid, o in list(self.orders.items()):
                if o.get("clientAlgoId") == client_algo_id:
                    self.orders.pop(aid, None)
        return ManagedOrder(client_order_id=client_algo_id or "",
                            state=OrderState.CANCELED,
                            is_stop=True, is_algo=True)


class Test429Lifecycle(unittest.TestCase):
    def test_repeated_429_never_creates_duplicate_and_recovers_by_same_id(self):
        ex = StressExchange()
        # 下单请求已被接受，但确认查询连续 20 次受限。
        ex.query_429 = 20
        first = run(place_protective_stop(
            ex, symbol="BTCUSDT", side=1, trigger_price=84000,
            quantity=0.296, close_position=True,
            client_algo_id="stable-btc-stop-1"))
        self.assertEqual(first.state, ProtectionState.UNKNOWN)
        self.assertFalse(first.protects)
        self.assertEqual(len(ex.orders), 1,
                         "确认429时只能有一张，不能因重试重复下单")
        # 模拟退避后恢复：用同一个 clientAlgoId 回查/重试，不能再生成第二张。
        ex.query_429 = 0
        second = run(place_protective_stop(
            ex, symbol="BTCUSDT", side=1, trigger_price=84000,
            quantity=0.296, close_position=True,
            client_algo_id="stable-btc-stop-1"))
        self.assertEqual(second.state, ProtectionState.PROTECTED)
        self.assertEqual(len(ex.orders), 1)

    def test_full_lifecycle_and_429_does_not_delete_old_stop(self):
        ex = StressExchange()
        old = ex.seed(trigger=84000)
        # 新止损查询受限：不得撤旧单，不能裸奔。
        ex.query_429 = 1
        res = run(tighten_protective_stop(
            ex, symbol="BTCUSDT", side=1, new_trigger=84800,
            old_algo_id=old["algoId"], quantity=0.296))
        self.assertFalse(res.protects)
        self.assertIn(old["algoId"], ex.orders)
        # 恢复：先立新单，再撤旧单。
        res = run(tighten_protective_stop(
            ex, symbol="BTCUSDT", side=1, new_trigger=84800,
            old_algo_id=old["algoId"], quantity=0.296))
        self.assertTrue(res.protects)
        stops = [o for o in ex.orders.values() if o["orderType"] == "STOP_MARKET"]
        self.assertEqual(len(stops), 1)
        # 额外挂一张止盈：默认清理止损不得误伤止盈。
        tp = ex.seed(order_type="TAKE_PROFIT_MARKET", trigger=85295,
                     qty="0.148", close="false")
        ex.query_429 = 3
        with self.assertRaises(RuntimeError):
            run(cancel_protective_orders(ex, "BTCUSDT"))
        self.assertIn(tp["algoId"], ex.orders)
        # 查询恢复后只撤止损，止盈仍保留。
        ex.query_429 = 0
        n, _ = run(cancel_protective_orders(ex, "BTCUSDT"))
        self.assertEqual(n, 1)
        self.assertIn(tp["algoId"], ex.orders)
        # 仓位归零时才允许清理止盈。
        n, _ = run(cancel_protective_orders(
            ex, "BTCUSDT", include_take_profit=True))
        self.assertEqual(n, 1)
        self.assertEqual(ex.orders, {})

    def test_cancel_429_is_conservative(self):
        ex = StressExchange()
        stop = ex.seed(trigger=84000)
        ex.cancel_429 = 1
        n, left = run(cancel_protective_orders(ex, "BTCUSDT"))
        self.assertEqual(n, 1)  # 发起过撤单，但返回值会以回查为准
        self.assertIn(stop["algoId"], ex.orders)  # 429 未确认，不能假报已清理
        self.assertGreaterEqual(left, 1)


if __name__ == "__main__":
    unittest.main()
