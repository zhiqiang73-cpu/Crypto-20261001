"""交易所保护单生命周期测试 —— 全部用假交易所，不发任何真实委托。

覆盖需求文档点名的失败路径：
    下单被拒 / 下单超时但交易所已接收 / 下单超时且未落地 /
    新单成功而撤旧单失败 / 重复保护单 / 查询失败 / 空仓残留保护单 /
    进程被杀后交易所侧保护仍然有效。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading.models import ManagedOrder, OrderState
from trading.protective_orders import (
    ProtectionState,
    algo_alive,
    cancel_protective_orders,
    covers_full_position,
    fetch_open_algo_orders,
    find_protective_stop,
    new_client_algo_id,
    normalize_algo_orders,
    place_protective_stop,
    reconcile_protective,
    tighten_protective_stop,
)


def _run(coro):
    return asyncio.run(coro)


class FakeExchange:
    """最小可用的假交易所 —— 只管 Algo 条件单。

    刻意把「HTTP 成功」与「交易所有这张单」分开：可以构造「下单抛异常但单子
    其实已经落地」这种真实世界里最常见的超时场景。
    """

    def __init__(self) -> None:
        self.algo: dict = {}
        self._next = 1
        self.place_fail = None        # None | "REJECT" | "TIMEOUT_LANDED" | "TIMEOUT_LOST"
        self.query_fail = None        # None | Exception
        self.cancel_fail = None       # None | "UNKNOWN" | "RAISE"
        self.calls: list = []

    # -- 内部 ------------------------------------------------------------
    def _make(self, side, qty, trig, cid, close_position,
              symbol="BTCUSDT") -> dict:
        # 交易所侧存的是**买卖方向**：多仓的止损是 SELL，空仓的是 BUY。
        # 客户端入参是持仓方向（LONG/SHORT），这里按交易所语义换算 ——
        # 不换算的话假交易所与真实接口的字段语义不一致，测试会给出假通过。
        ex_side = {"LONG": "SELL", "SHORT": "BUY"}.get(side, side)
        aid = str(self._next)
        self._next += 1
        return {
            "algoId": aid, "clientAlgoId": cid, "symbol": symbol,
            "side": ex_side, "type": "STOP_MARKET", "algoType": "CONDITIONAL",
            "triggerPrice": f"{trig:.2f}",
            "quantity": f"{qty:.6f}",
            "closePosition": "true" if close_position else "false",
            "algoStatus": "NEW",
        }

    def seed(self, *, side="SELL", trig=84000.0, qty=0.3, cid="seed1",
             close_position=True, status="NEW", symbol="BTCUSDT") -> dict:
        o = self._make(side, qty, trig, cid, close_position, symbol)
        o["algoStatus"] = status
        self.algo[o["algoId"]] = o
        return o

    # -- 假客户端接口 -----------------------------------------------------
    async def place_stop_market(self, side, quantity, stop_price, symbol=None, *,
                                client_order_id=None, close_position=False):
        self.calls.append(("place", side, float(stop_price), client_order_id,
                           close_position))
        if self.place_fail == "REJECT":
            return ManagedOrder(
                client_order_id=client_order_id or "", state=OrderState.REJECTED,
                error="-2021 Order would immediately trigger", symbol=symbol,
                is_stop=True, is_algo=True,
            )
        if self.place_fail == "TIMEOUT_LANDED":
            o = self._make(side, quantity, stop_price, client_order_id,
                           close_position, symbol)
            self.algo[o["algoId"]] = o          # 交易所已接单
            raise TimeoutError("simulated read timeout")
        if self.place_fail == "TIMEOUT_LOST":
            raise TimeoutError("simulated read timeout")
        o = self._make(side, quantity, stop_price, client_order_id,
                       close_position, symbol)
        self.algo[o["algoId"]] = o
        return ManagedOrder(
            client_order_id=client_order_id or "", state=OrderState.ACKNOWLEDGED,
            symbol=symbol, is_stop=True, is_algo=True, algo_id=o["algoId"],
            raw=dict(o),
        )

    async def get_open_algo_orders(self, symbol=None):
        self.calls.append(("query", symbol))
        if self.query_fail is not None:
            raise self.query_fail
        # 真实接口按 symbol 过滤。假交易所也必须过滤，否则一个标的的保护单
        # 会被算到另一个标的上 —— 跨标的串扰正是这类系统最容易漏的坑。
        if symbol:
            return [o for o in self.algo.values() if o.get("symbol") == symbol]
        return list(self.algo.values())

    async def cancel_algo_order(self, *, client_algo_id=None, algo_id=None,
                                symbol=None):
        self.calls.append(("cancel", algo_id, client_algo_id))
        if self.cancel_fail == "RAISE":
            raise TimeoutError("simulated cancel timeout")
        if self.cancel_fail == "UNKNOWN":
            return ManagedOrder(client_order_id=client_algo_id or "",
                                state=OrderState.UNKNOWN, error="query timeout",
                                is_algo=True, is_stop=True)
        if algo_id and algo_id in self.algo:
            del self.algo[algo_id]
        elif client_algo_id:
            for k, v in list(self.algo.items()):
                if v.get("clientAlgoId") == client_algo_id:
                    del self.algo[k]
        return ManagedOrder(client_order_id=client_algo_id or "",
                            state=OrderState.CANCELED, is_algo=True, is_stop=True)


class TestNormalize(unittest.TestCase):
    def test_bare_list(self):
        self.assertEqual(len(normalize_algo_orders([{"algoId": "1"}])), 1)

    def test_wrapped_orders_key(self):
        self.assertEqual(
            len(normalize_algo_orders({"orders": [{"algoId": "1"}]})), 1)

    def test_wrapped_data_key(self):
        self.assertEqual(
            len(normalize_algo_orders({"data": [{"algoId": "1"}]})), 1)

    def test_single_object(self):
        self.assertEqual(
            len(normalize_algo_orders({"algoId": "7", "triggerPrice": "1"})), 1)

    def test_garbage_is_empty(self):
        self.assertEqual(normalize_algo_orders(None), [])
        self.assertEqual(normalize_algo_orders("boom"), [])

    def test_alive_status(self):
        self.assertTrue(algo_alive({"algoStatus": "NEW"}))
        self.assertFalse(algo_alive({"algoStatus": "CANCELED"}))
        self.assertFalse(algo_alive({"algoStatus": "EXPIRED"}))


class TestFindStop(unittest.TestCase):
    def test_long_position_needs_sell_stop(self):
        orders = [{"side": "SELL", "triggerPrice": "84000", "algoStatus": "NEW"},
                  {"side": "BUY", "triggerPrice": "86000", "algoStatus": "NEW"}]
        got = find_protective_stop(orders, side=1)
        self.assertEqual(got["side"], "SELL")

    def test_short_position_needs_buy_stop(self):
        orders = [{"side": "SELL", "triggerPrice": "84000", "algoStatus": "NEW"},
                  {"side": "BUY", "triggerPrice": "86000", "algoStatus": "NEW"}]
        got = find_protective_stop(orders, side=-1)
        self.assertEqual(got["side"], "BUY")

    def test_required_trigger_filters_loose_stops(self):
        """多仓已有 84000 的止损，要求 ≥84500 时必须判定为不合格。"""
        orders = [{"side": "SELL", "triggerPrice": "84000",
                   "algoStatus": "NEW"}]
        self.assertIsNone(find_protective_stop(orders, side=1,
                                               required_trigger=84500.0))

    def test_picks_tightest(self):
        orders = [{"side": "SELL", "triggerPrice": "84000", "algoStatus": "NEW"},
                  {"side": "SELL", "triggerPrice": "84500", "algoStatus": "NEW"}]
        got = find_protective_stop(orders, side=1)
        self.assertAlmostEqual(float(got["triggerPrice"]), 84500.0)

    def test_canceled_stop_does_not_count(self):
        orders = [{"side": "SELL", "triggerPrice": "84000",
                   "algoStatus": "CANCELED"}]
        self.assertIsNone(find_protective_stop(orders, side=1))

    def test_close_position_covers_full(self):
        self.assertTrue(covers_full_position(
            {"closePosition": "true", "quantity": "0"}, quantity=999.0))

    def test_explicit_quantity_must_cover(self):
        self.assertTrue(covers_full_position({"quantity": "0.30"}, quantity=0.296))
        self.assertFalse(covers_full_position({"quantity": "0.10"}, quantity=0.296))


class TestPlaceStop(unittest.TestCase):
    def test_placed_and_verified(self):
        ex = FakeExchange()
        res = _run(place_protective_stop(ex, symbol="BTCUSDT", side=1,
                                         trigger_price=84000.0))
        self.assertTrue(res.protects)
        self.assertTrue(res.verified)
        self.assertEqual(res.state, ProtectionState.PROTECTED)
        self.assertEqual(len(ex.algo), 1, "交易所应恰好多出一张保护单")

    def test_reject_is_unprotected(self):
        ex = FakeExchange()
        ex.place_fail = "REJECT"
        res = _run(place_protective_stop(ex, symbol="BTCUSDT", side=1,
                                         trigger_price=84000.0))
        self.assertFalse(res.protects)
        self.assertEqual(res.state, ProtectionState.UNPROTECTED)
        self.assertEqual(len(ex.algo), 0)

    def test_timeout_but_accepted_is_protected(self):
        """超时但交易所已接单 —— 回查后必须判定为已受保护。"""
        ex = FakeExchange()
        ex.place_fail = "TIMEOUT_LANDED"
        res = _run(place_protective_stop(ex, symbol="BTCUSDT", side=1,
                                         trigger_price=84000.0))
        self.assertTrue(res.protects, f"应回查确认已落地: {res.error}")
        self.assertEqual(len(ex.algo), 1)

    def test_timeout_and_lost_is_unknown_not_protected(self):
        """超时且确实没落地 —— 必须是 UNKNOWN，绝不能谎报已保护。"""
        ex = FakeExchange()
        ex.place_fail = "TIMEOUT_LOST"
        res = _run(place_protective_stop(ex, symbol="BTCUSDT", side=1,
                                         trigger_price=84000.0))
        self.assertFalse(res.protects)
        self.assertEqual(res.state, ProtectionState.UNKNOWN)
        self.assertEqual(len(ex.algo), 0)

    def test_query_failure_is_not_protected(self):
        """查询失败 ≠ 没有保护，但也 ≠ 有保护。必须按未确认处理。"""
        ex = FakeExchange()
        ex.query_fail = TimeoutError("429 rate limited")
        res = _run(place_protective_stop(ex, symbol="BTCUSDT", side=1,
                                         trigger_price=84000.0))
        self.assertFalse(res.protects)
        self.assertEqual(res.state, ProtectionState.UNKNOWN)
        self.assertIn("429", res.error)

    def test_client_algo_id_is_deterministic_when_given(self):
        ex = FakeExchange()
        res = _run(place_protective_stop(ex, symbol="BTCUSDT", side=1,
                                         trigger_price=84000.0,
                                         client_algo_id="mycid1"))
        self.assertEqual(res.client_algo_id, "mycid1")
        self.assertEqual(list(ex.algo.values())[0]["clientAlgoId"], "mycid1")

    def test_invalid_trigger_rejected_locally(self):
        ex = FakeExchange()
        res = _run(place_protective_stop(ex, symbol="BTCUSDT", side=1,
                                         trigger_price=0.0))
        self.assertFalse(res.protects)
        self.assertEqual(ex.calls, [], "非法触发价不得发出任何委托")


class TestTightenStop(unittest.TestCase):
    def test_places_new_then_cancels_old(self):
        ex = FakeExchange()
        old = ex.seed(trig=84000.0, cid="old1")
        res = _run(tighten_protective_stop(
            ex, symbol="BTCUSDT", side=1, new_trigger=84800.0,
            old_algo_id=old["algoId"]))
        self.assertTrue(res.protects)
        self.assertTrue(res.old_cancel_ok)
        self.assertNotIn(old["algoId"], ex.algo, "旧保护单必须被撤掉")
        self.assertEqual(len(ex.algo), 1, "最终应只剩一张保护单")
        # 顺序：先 place 后 cancel
        kinds = [c[0] for c in ex.calls]
        self.assertLess(kinds.index("place"), kinds.index("cancel"),
                        "必须先把新单立起来，再撤旧单")

    def test_keeps_old_when_new_unconfirmed(self):
        """新单没确认时绝不能撤旧单 —— 否则仓位会裸奔。"""
        ex = FakeExchange()
        old = ex.seed(trig=84000.0, cid="old1")
        ex.place_fail = "TIMEOUT_LOST"
        res = _run(tighten_protective_stop(
            ex, symbol="BTCUSDT", side=1, new_trigger=84800.0,
            old_algo_id=old["algoId"]))
        self.assertIn(old["algoId"], ex.algo, "旧保护单必须还在")
        self.assertFalse(any(c[0] == "cancel" for c in ex.calls),
                         "不得发出任何撤单")
        # 2026-10-05 改：**新单没挂上 ≠ 仓位没保护** —— 旧单仍在交易所生效。
        # 原先这里断言 protects=False，把「旧单还活着」的仓位标成未保护，
        # 系统随即永久禁止该标的开新仓（实测刷屏 1224 条）。
        # 现在如实返回 PROTECTED 并用旧单的触发价，同时把失败原因留在 error。
        self.assertTrue(res.protects, "旧保护单还活着，仓位就是受保护的")
        self.assertEqual(res.algo_id, old["algoId"],
                         "必须如实回报旧单的 algoId")
        self.assertAlmostEqual(res.trigger_price, 84000.0, places=2,
                               msg="触发价必须用旧单的，不能报新单的目标价")
        self.assertIn("未确认", res.error, "收紧失败的原因仍须暴露")

    def test_reports_when_old_cancel_fails(self):
        """新单已生效、旧单撤不掉 —— 仍算已保护，但必须暴露问题。"""
        ex = FakeExchange()
        old = ex.seed(trig=84000.0, cid="old1")
        ex.cancel_fail = "UNKNOWN"
        res = _run(tighten_protective_stop(
            ex, symbol="BTCUSDT", side=1, new_trigger=84800.0,
            old_algo_id=old["algoId"]))
        self.assertTrue(res.protects, "新保护已生效，不能判为未保护")
        self.assertIs(res.old_cancel_ok, False)
        self.assertIn("旧保护单撤销未确认", res.error)

    def test_idempotent_when_already_tight_enough(self):
        """已经有一张同等或更紧的保护单时，不得重复下单。"""
        ex = FakeExchange()
        ex.seed(trig=84800.0, cid="already")
        res = _run(tighten_protective_stop(
            ex, symbol="BTCUSDT", side=1, new_trigger=84800.0))
        self.assertTrue(res.protects)
        self.assertFalse(any(c[0] == "place" for c in ex.calls),
                         "已满足目标就不该再下单")
        self.assertEqual(len(ex.algo), 1)

    def test_short_only_moves_down(self):
        ex = FakeExchange()
        old = ex.seed(side="BUY", trig=86000.0, cid="olds")
        res = _run(tighten_protective_stop(
            ex, symbol="BTCUSDT", side=-1, new_trigger=85500.0,
            old_algo_id=old["algoId"]))
        self.assertTrue(res.protects)
        self.assertAlmostEqual(res.trigger_price, 85500.0, places=2)


class TestCancelAll(unittest.TestCase):
    def test_cancels_via_algo_api(self):
        """清理保护单必须走 Algo 接口，不能只看普通 openOrders。"""
        ex = FakeExchange()
        ex.seed(cid="a", trig=84000.0)
        ex.seed(cid="b", trig=84500.0)
        n, left = _run(cancel_protective_orders(ex, "BTCUSDT"))
        self.assertEqual(n, 2)
        self.assertEqual(left, 0)
        self.assertTrue(all(c[0] != "open_orders" for c in ex.calls))

    def test_raises_when_query_fails(self):
        """查询失败时不得假装「已经撤干净了」。"""
        ex = FakeExchange()
        ex.query_fail = TimeoutError("429")
        with self.assertRaises(RuntimeError):
            _run(cancel_protective_orders(ex, "BTCUSDT"))


class TestReconcile(unittest.TestCase):
    def test_flat_with_residual_stops_clears_them(self):
        """空仓时必须清理残留保护单 —— 上一笔的不能留到下一笔。"""
        ex = FakeExchange()
        ex.seed(cid="residual", trig=84000.0)
        res = _run(reconcile_protective(ex, symbol="BTCUSDT", side=0,
                                        quantity=0.0, required_trigger=0.0))
        self.assertEqual(res.duplicates_canceled, 1)
        self.assertEqual(len(ex.algo), 0)
        self.assertTrue(res.can_trade)

    def test_position_without_stop_is_unprotected(self):
        ex = FakeExchange()
        res = _run(reconcile_protective(ex, symbol="BTCUSDT", side=1,
                                        quantity=0.296,
                                        required_trigger=84800.0))
        self.assertEqual(res.state, ProtectionState.UNPROTECTED)
        self.assertFalse(res.can_trade, "没有保护单时不允许恢复开仓")

    def test_position_with_good_stop_can_trade(self):
        ex = FakeExchange()
        ex.seed(trig=84850.0, cid="good")
        res = _run(reconcile_protective(ex, symbol="BTCUSDT", side=1,
                                        quantity=0.296,
                                        required_trigger=84800.0))
        self.assertEqual(res.state, ProtectionState.PROTECTED)
        self.assertTrue(res.can_trade)

    def test_loose_stop_is_not_enough(self):
        ex = FakeExchange()
        ex.seed(trig=80000.0, cid="loose")
        res = _run(reconcile_protective(ex, symbol="BTCUSDT", side=1,
                                        quantity=0.296,
                                        required_trigger=84800.0))
        self.assertEqual(res.state, ProtectionState.UNPROTECTED)

    def test_duplicates_are_cleaned(self):
        """同方向多张保护单触发时会重复平仓，必须只留最紧的一张。"""
        ex = FakeExchange()
        ex.seed(trig=84500.0, cid="tighter")
        ex.seed(trig=84000.0, cid="looser")
        res = _run(reconcile_protective(ex, symbol="BTCUSDT", side=1,
                                        quantity=0.296,
                                        required_trigger=84000.0))
        self.assertEqual(res.state, ProtectionState.PROTECTED)
        self.assertEqual(res.duplicates_canceled, 1)
        self.assertEqual(len(ex.algo), 1)

    def test_query_failure_is_unknown_and_blocks(self):
        """429 限流时不能显示「正常运行」却跳过保护。"""
        ex = FakeExchange()
        ex.query_fail = TimeoutError("429 Too Many Requests")
        res = _run(reconcile_protective(ex, symbol="BTCUSDT", side=1,
                                        quantity=0.296,
                                        required_trigger=84800.0))
        self.assertEqual(res.state, ProtectionState.UNKNOWN)
        self.assertFalse(res.can_trade)
        self.assertIn("429", res.note)


class TestSurvivesProcessDeath(unittest.TestCase):
    """需求原文：断网或杀掉策略进程的模拟测试中，交易所侧保护单状态仍应保持有效。

    做法：下好保护单后，**完全丢弃本地客户端对象**，用一个全新的假交易所
    句柄（模拟重启后的新进程）去对账 —— 只要保护单还在交易所，就必须判定
    为已受保护。
    """

    def test_stop_survives_and_is_rediscovered(self):
        ex = FakeExchange()
        placed = _run(place_protective_stop(ex, symbol="BTCUSDT", side=1,
                                            trigger_price=84000.0))
        self.assertTrue(placed.protects)

        # 模拟进程被杀：本地什么都不留，只剩交易所上的单子
        del placed

        res = _run(reconcile_protective(ex, symbol="BTCUSDT", side=1,
                                        quantity=0.296,
                                        required_trigger=84000.0))
        self.assertEqual(res.state, ProtectionState.PROTECTED)
        self.assertTrue(res.can_trade)
        self.assertIsNotNone(res.stop)

    def test_missing_stop_after_restart_blocks_trading(self):
        """重启后保护单没了（比如被手工撤掉）→ 必须禁止恢复开仓。"""
        ex = FakeExchange()
        res = _run(reconcile_protective(ex, symbol="BTCUSDT", side=1,
                                        quantity=0.296,
                                        required_trigger=84000.0))
        self.assertFalse(res.can_trade)


class TestBothSymbols(unittest.TestCase):
    def test_btc_and_eth_are_independent(self):
        ex = FakeExchange()
        ex.seed(side="SELL", trig=84800.0, cid="btc", symbol="BTCUSDT")
        # ETH 完全没有保护单
        btc = _run(reconcile_protective(ex, symbol="BTCUSDT", side=1,
                                        quantity=0.296,
                                        required_trigger=84800.0))
        eth = _run(reconcile_protective(ex, symbol="ETHUSDT", side=1,
                                        quantity=3.707,
                                        required_trigger=2680.0))
        self.assertEqual(btc.state, ProtectionState.PROTECTED)
        self.assertEqual(eth.state, ProtectionState.UNPROTECTED,
                         "ETH 缺保护单不能被 BTC 的单子掩盖")


class TestClientAlgoId(unittest.TestCase):
    def test_id_is_unique_per_call(self):
        a = new_client_algo_id("BTCUSDT")
        b = new_client_algo_id("BTCUSDT")
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), 36, "clientAlgoId 不得超过 36 字符")

    def test_id_carries_symbol_hint(self):
        self.assertIn("btcusd", new_client_algo_id("BTCUSDT"))


if __name__ == "__main__":
    unittest.main()
