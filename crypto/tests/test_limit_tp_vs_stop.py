"""限价止盈不阻塞止损 —— 2026-10-04 止盈腿改 LIMIT 后的安全验证。

背景
----
止盈腿从 `TAKE_PROFIT_MARKET`（Algo 条件单，openAlgoOrders）改为
`LIMIT + reduceOnly`（普通挂单，openOrders），目的是吃 maker 费率（2bp）
而不是 taker（4bp），并避免市价成交的滑点。

这个改动把止盈单搬到了**另一套接口**上，由此产生一类新的失效模式：
**把普通挂单误当成条件单，或反过来。** 最危险的一条是：
若 `find_protective_stop` 把限价止盈单当成止损单，止损就永远不会建立，
而系统会显示「已保护」—— 仓位实际裸奔。这比「没挂上」更糟。

本文件守的就是这条线，外加一条同等重要的：**止损腿必须仍是市价**。
省手续费不能顺手把止损也改成限价 —— 那等于用确定的尾部风险换确定的小钱。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow import deploy  # noqa: E402
from trading.binance_client import OrderResult  # noqa: E402
from trading.models import ManagedOrder, OrderState  # noqa: E402
from trading.protective_orders import (  # noqa: E402
    REGULAR_ORDER_PREFIX,
    cancel_protective_orders,
    covers_full_position,
    fetch_open_protective_orders,
    find_protective_stop,
    find_take_profit,
    is_regular_order,
    is_stop_type,
    is_take_profit_type,
    normalize_regular_order,
    regular_order_id,
)

MS = 1791100000000
AVG = 85042.07
ATR = 126.62


def _run(coro):
    return asyncio.run(coro)


class FakeEx:
    """同时支持 Algo 条件单与普通挂单的假交易所。"""

    def __init__(self):
        self.algo: dict = {}
        self.regular: dict = {}
        self._n = 1
        self.place_stop_calls = 0
        self.place_limit_calls = 0
        self.cancel_algo_calls = []
        self.cancel_order_calls = []

    def _id(self):
        v = str(self._n)
        self._n += 1
        return v

    async def price_tick(self, symbol=None):
        return 0.10

    async def _lot_step(self, symbol=None):
        return 0.0001

    def _price_precision(self, price, tick):
        return round(round(float(price) / tick) * tick, 10)

    async def get_position_mode(self):
        return False

    # --- 条件单（止损）---
    async def place_stop_market(self, side, quantity, stop_price, symbol=None, *,
                                client_order_id=None, close_position=False,
                                order_type="STOP_MARKET"):
        self.place_stop_calls += 1
        aid = self._id()
        o = {
            "algoId": aid, "clientAlgoId": client_order_id or "",
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "orderType": order_type, "triggerPrice": f"{stop_price:.2f}",
            "algoStatus": "NEW",
            "closePosition": "true" if close_position else "false",
            "quantity": f"{quantity:.6f}",
            # 2026-10-05 起止损改用显式数量 + reduceOnly；假交易所必须如实回显，
            # 否则测试无法守住「止损只能减仓」这条契约。
            "reduceOnly": "false" if close_position else "true",
        }
        self.algo[aid] = o
        return ManagedOrder(client_order_id=client_order_id or "",
                            state=OrderState.ACKNOWLEDGED, symbol=symbol,
                            is_stop=True, is_algo=True, algo_id=aid, raw=dict(o))

    async def get_open_algo_orders(self, symbol=None):
        return [o for o in self.algo.values() if o.get("symbol") == symbol]

    async def cancel_algo_order(self, *, client_algo_id=None, algo_id=None,
                                symbol=None):
        self.cancel_algo_calls.append(algo_id)
        self.algo.pop(algo_id, None)
        return ManagedOrder(client_order_id=client_algo_id or "",
                            state=OrderState.CANCELED, is_algo=True, is_stop=True)

    # --- 普通挂单（限价止盈）---
    async def place_resting_limit_order(self, side, quantity, price,
                                        symbol=None, *, reduce_only=False,
                                        client_order_id=None,
                                        time_in_force="GTC"):
        self.place_limit_calls += 1
        oid = self._id()
        o = {
            "orderId": oid, "clientOrderId": client_order_id or "",
            "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "type": "LIMIT", "price": f"{price:.2f}",
            "origQty": f"{quantity:.6f}", "status": "NEW",
            "reduceOnly": "true" if reduce_only else "false",
        }
        self.regular[oid] = o
        return OrderResult(
            ok=True, order_id=oid, symbol=symbol, side=o["side"],
            quantity=float(quantity), requested_qty=float(quantity),
            submitted_qty=float(quantity), cum_filled_qty=0.0, avg_price=0.0,
            status="NEW", client_order_id=client_order_id or "",
            order_state="ACKNOWLEDGED", raw=dict(o),
        )

    async def get_open_orders(self, symbol=None):
        return [o for o in self.regular.values() if o.get("symbol") == symbol]

    async def cancel_order(self, *, client_order_id=None, order_id=None,
                           symbol=None):
        self.cancel_order_calls.append(order_id)
        self.regular.pop(order_id, None)
        return ManagedOrder(client_order_id=client_order_id or "",
                            state=OrderState.CANCELED)

    def seed_stop(self, *, trig, symbol="BTCUSDT", side="SELL"):
        aid = self._id()
        o = {"algoId": aid, "clientAlgoId": f"prot{aid}", "symbol": symbol,
             "side": side, "orderType": "STOP_MARKET",
             "triggerPrice": f"{trig:.2f}", "algoStatus": "NEW",
             "closePosition": "true", "quantity": "0.296"}
        self.algo[aid] = o
        return o

    def seed_tp(self, *, trig, symbol="BTCUSDT", side="SELL", qty="0.148"):
        oid = self._id()
        o = {"orderId": oid, "clientOrderId": f"tp{oid}", "symbol": symbol,
             "side": side, "type": "LIMIT", "price": f"{trig:.2f}",
             "status": "NEW", "reduceOnly": "true", "origQty": qty}
        self.regular[oid] = o
        return o


# ==========================================================================
# 一、类型判定：绝不能让止盈单冒充止损单
# ==========================================================================
class TestTypeDisambiguation(unittest.TestCase):

    def test_regular_limit_is_take_profit_not_stop(self):
        o = normalize_regular_order(
            {"orderId": "77", "type": "LIMIT", "side": "SELL",
             "price": "85295.30", "origQty": "0.148", "status": "NEW"})
        self.assertTrue(is_take_profit_type(o), "限价止盈必须被认成止盈")
        self.assertFalse(is_stop_type(o), "限价止盈绝不能冒充止损")

    def test_find_protective_stop_ignores_resting_limit_tp(self):
        """核心安全断言：只有限价止盈单时，必须判定「没有止损」。"""
        tp = normalize_regular_order(
            {"orderId": "77", "type": "LIMIT", "side": "SELL",
             "price": "85295.30", "origQty": "0.148", "status": "NEW"})
        self.assertIsNone(
            find_protective_stop([tp], side=1, required_trigger=85152.80),
            "把限价止盈当成止损 = 仓位裸奔，而系统显示已保护")

    def test_find_take_profit_finds_resting_limit(self):
        tp = normalize_regular_order(
            {"orderId": "77", "type": "LIMIT", "side": "SELL",
             "price": "85295.30", "origQty": "0.148", "status": "NEW"})
        self.assertIsNotNone(find_take_profit([tp], side=1,
                                              trigger=85295.30, tolerance=1.0))

    def test_resting_limit_never_claims_close_position(self):
        """限价止盈绝不能带 closePosition=true。

        `closePosition=true` 是 covers_full_position 的**快捷通道**，一旦普通
        挂单带上它，任何「是否已覆盖全仓」的判定都会直接返回 True。币安不
        允许普通单带这个字段，所以归一化时显式写成 "false"，不留歧义。

        注：covers_full_position 目前在生产代码里**没有调用点**（只有测试在
        用），所以这里守的是「归一化不留危险快捷通道」，而不是某条现存路径。
        """
        tp = normalize_regular_order(
            {"orderId": "77", "type": "LIMIT", "side": "SELL",
             "price": "85295.30", "origQty": "0.296", "status": "NEW"})
        self.assertEqual(str(tp.get("closePosition")).lower(), "false")
        self.assertFalse(
            covers_full_position({"quantity": "0.10"}, quantity=0.296),
            "数量不足时不得算覆盖全仓")

    def test_namespaced_id_never_collides_with_algo_id(self):
        tp = normalize_regular_order(
            {"orderId": "123", "type": "LIMIT", "side": "SELL",
             "price": "1", "origQty": "1", "status": "NEW"})
        self.assertEqual(tp["algoId"], f"{REGULAR_ORDER_PREFIX}123")
        self.assertTrue(is_regular_order(tp))
        self.assertEqual(regular_order_id(tp), "123")
        self.assertNotEqual(tp["algoId"], "123", "必须与 Algo 单号区分开")


# ==========================================================================
# 二、限价止盈不得阻塞止损的建立与维护
# ==========================================================================
class TestTpDoesNotBlockStop(unittest.TestCase):

    def setUp(self):
        # _PROTECTIVE_ATTEMPT 是模块级节流表，跨用例共享。
        # 不清掉的话「建立止损」的第二次调用会被静默跳过，测试变成假绿。
        deploy._PROTECTIVE_ATTEMPT.clear()

    def test_stop_still_placed_while_limit_tp_rests(self):
        """盘口已有限价止盈 → 止损仍必须建立。"""
        ex = FakeEx()
        ex.seed_tp(trig=AVG + 2 * ATR)
        st = deploy._default_state()
        _run(deploy.manage_exchange_stop(
            ex, st, symbol="BTCUSDT", ex_side=0.296, entry_px=AVG,
            atr_1h=ATR, execute=True))
        self.assertEqual(ex.place_stop_calls, 1,
                         "有限价止盈在挂，绝不等于已受保护 —— 止损必须照建")
        self.assertEqual(len(ex.algo), 1, "止损单必须真的在交易所")
        st_ps = deploy.protection_state(st, "BTCUSDT")
        self.assertEqual(st_ps.get("state"), "PROTECTED")

    def test_stop_leg_is_still_market_order(self):
        """省手续费不能顺手把止损也改成限价。"""
        ex = FakeEx()
        ex.seed_tp(trig=AVG + 2 * ATR)
        st = deploy._default_state()
        _run(deploy.manage_exchange_stop(
            ex, st, symbol="BTCUSDT", ex_side=0.296, entry_px=AVG,
            atr_1h=ATR, execute=True))
        stop = list(ex.algo.values())[0]
        self.assertEqual(stop["orderType"], "STOP_MARKET",
                         "止损必须保持市价：确定性比 2bp 值钱")
        # 2026-10-05 起止损改用「显式数量 + reduceOnly」，不再用 closePosition：
        # closePosition=true 的条件单每标的每方向只允许一张，收紧时「先立后破」
        # 必然被 -4130 拒绝，导致保护单永久卡死、开仓被永久禁止。
        self.assertEqual(stop["closePosition"], "false",
                         "不得再用 closePosition=true，否则收紧会被 -4130 卡死")
        self.assertEqual(stop["reduceOnly"], "true",
                         "止损必须 reduceOnly，只能减仓不能反向开仓")
        self.assertAlmostEqual(float(stop["quantity"]), 0.296, places=6,
                               msg="止损数量必须等于当前持仓，加层后由系统重新覆盖")

    def test_merged_view_sees_both_legs(self):
        """统一视图必须同时看到止损与限价止盈 —— 只看一边必然漏。"""
        ex = FakeEx()
        ex.seed_stop(trig=AVG - 1.5 * ATR)
        ex.seed_tp(trig=AVG + 2 * ATR)
        orders = _run(fetch_open_protective_orders(ex, "BTCUSDT"))
        self.assertEqual(len(orders), 2, "两套接口都要查")
        kinds = sorted("tp" if is_take_profit_type(o) else "stop"
                       for o in orders)
        self.assertEqual(kinds, ["stop", "tp"])


# ==========================================================================
# 三、撤销必须分发到正确的端点
# ==========================================================================
class TestCancelDispatch(unittest.TestCase):

    def test_cancelling_stop_does_not_touch_limit_tp(self):
        """清理重复止损单时，绝不能误撤止盈单。"""
        ex = FakeEx()
        ex.seed_stop(trig=AVG - 1.5 * ATR)
        ex.seed_tp(trig=AVG + 2 * ATR)
        n, left = _run(cancel_protective_orders(ex, "BTCUSDT",
                                                include_take_profit=False))
        self.assertEqual(n, 1)
        self.assertEqual(ex.cancel_order_calls, [],
                         "限价止盈不该被这次清理碰到")
        self.assertEqual(len(ex.regular), 1, "止盈单必须还在")

    def test_cancelling_all_clears_both_endpoints(self):
        ex = FakeEx()
        ex.seed_stop(trig=AVG - 1.5 * ATR)
        ex.seed_tp(trig=AVG + 2 * ATR)
        n, left = _run(cancel_protective_orders(ex, "BTCUSDT",
                                                include_take_profit=True))
        self.assertEqual(n, 2, "两腿都要撤")
        self.assertEqual(left, 0)
        self.assertEqual(len(ex.algo), 0)
        self.assertEqual(len(ex.regular), 0)
        self.assertEqual(len(ex.cancel_order_calls), 1,
                         "限价止盈必须走普通挂单端点")

    def test_cancel_reports_remaining_across_both_endpoints(self):
        """剩余数必须跨两套接口统计，否则会「以为撤干净了」。"""
        ex = FakeEx()
        ex.seed_tp(trig=AVG + 2 * ATR)
        n, left = _run(cancel_protective_orders(ex, "BTCUSDT",
                                                include_take_profit=True))
        self.assertEqual(n, 1)
        self.assertEqual(left, 0, "撤完必须确认真的空了")

    def test_cancel_by_id_routes_regular_to_order_endpoint(self):
        ex = FakeEx()
        o = ex.seed_tp(trig=AVG + 2 * ATR)
        from trading.protective_orders import cancel_algo_by_id
        ok = _run(cancel_algo_by_id(
            ex, "BTCUSDT", f"{REGULAR_ORDER_PREFIX}{o['orderId']}"))
        self.assertTrue(ok, "撤销必须真的生效，而不是接口返回成功")
        self.assertEqual(ex.cancel_order_calls, [o["orderId"]])
        self.assertEqual(ex.cancel_algo_calls, [],
                         "限价止盈绝不能走 Algo 撤销端点")


# ==========================================================================
# 四、保护单成交判定必须看得见限价止盈（B6 的延伸）
# ==========================================================================
class TestStopFiredDetectionSeesLimitTp(unittest.TestCase):

    def _state(self):
        st = deploy._default_state()
        st["strategies"] = {}
        st["protection"] = {}
        return st

    def test_ledger_not_cleared_while_limit_tp_still_rests(self):
        """我们的止盈单还在挂 → 仓位变空不是我们造成的 → 不擅自清账本。

        限价止盈搬到 openOrders 后，若成交判定只查 openAlgoOrders，就会
        看不到它，从而误判「我们的单都没了」并把账本清掉。
        """
        ex = FakeEx()
        ex.seed_tp(trig=AVG + 2 * ATR)
        st = self._state()
        st["protection"]["BTCUSDT"] = {"state": "PROTECTED", "algo_id": "",
                                       "trigger": 0.0, "tp_orders": []}
        st["strategies"]["kdj15"] = {
            "entry": {"side": 1, "qty": 0.296, "avg_price": AVG},
            "exchange_stop": {"algo_id": "", "trigger": 0.0, "armed": True},
        }
        # 让状态里的止盈单号指向那张真实挂单
        oid = list(ex.regular.keys())[0]
        st["protection"]["BTCUSDT"]["tp_orders"] = [
            {"algo_id": f"{REGULAR_ORDER_PREFIX}{oid}", "stage": 0,
             "filled": False}]
        fired = _run(deploy.clear_ledger_if_stop_fired(
            ex, st, symbol="BTCUSDT", ex_side=0.0))
        self.assertFalse(fired, "止盈单还在挂，不得判为保护单成交")
        self.assertTrue(
            (st["strategies"]["kdj15"].get("entry") or {}).get("qty"),
            "账本必须保留")

    def test_ledger_cleared_when_no_orders_left(self):
        """两腿都没了 + 仓位归零 → 是我们的止损打掉的，清账本并告警。"""
        ex = FakeEx()
        st = self._state()
        st["protection"]["BTCUSDT"] = {"state": "PROTECTED", "algo_id": "999",
                                       "trigger": 85152.80, "tp_orders": []}
        st["strategies"]["kdj15"] = {
            "entry": {"side": 1, "qty": 0.296, "avg_price": AVG},
            "exchange_stop": {"algo_id": "999", "trigger": 85152.80,
                              "armed": True},
        }
        fired = _run(deploy.clear_ledger_if_stop_fired(
            ex, st, symbol="BTCUSDT", ex_side=0.0))
        self.assertTrue(fired)
        self.assertEqual(deploy.protection_state(st, "BTCUSDT").get("state"),
                         "FLAT")


if __name__ == "__main__":
    unittest.main()
