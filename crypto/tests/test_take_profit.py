"""分批止盈 + 剩余仓位移动止损的测试。

最重要的一组是 `TestStopTakeProfitDisambiguation`：
多仓的止损单与止盈单**都是 SELL 方向**，只按方向+触发价选单会把触发价更高
的止盈单当成「更紧的止损」。后果是止损永远建不起来，而系统显示「已保护」——
仓位实际裸奔。必须按 orderType 区分。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow.deploy import (  # noqa: E402
    TP_STAGES,
    TRAIL_ATR,
    _PROTECTIVE_ATTEMPT,
    _protective_target,
    manage_take_profit,
    protection_state,
    reconcile_tp_fills,
)
from shadow.strategy_books import specs_for_symbol  # noqa: E402
from trading.models import ManagedOrder, OrderState  # noqa: E402
from trading.protective_orders import (  # noqa: E402
    find_protective_stop,
    find_take_profit,
    is_stop_type,
    is_take_profit_type,
)

MS = 1791100000000
AVG = 85042.07
ATR = 126.62


def _run(coro):
    return asyncio.run(coro)


class FakeExchange:
    def __init__(self, *, tick=0.10, step=0.0001):
        self.tick = tick
        self.step = step
        self.algo: dict = {}
        self._next = 1
        self.place_calls = 0
        self.hide_placed = False

    async def price_tick(self, symbol=None):
        return self.tick

    async def _lot_step(self, symbol=None):
        return self.step

    def _price_precision(self, price, tick):
        return round(round(float(price) / tick) * tick, 10)

    async def place_stop_market(self, side, quantity, stop_price, symbol=None, *,
                                client_order_id=None, close_position=False,
                                order_type="STOP_MARKET"):
        self.place_calls += 1
        aid = str(self._next)
        self._next += 1
        o = {
            "algoId": aid, "clientAlgoId": client_order_id or "", "symbol": symbol,
            "side": "SELL" if side == "LONG" else "BUY",
            "orderType": order_type,
            "triggerPrice": f"{stop_price:.2f}", "algoStatus": "NEW",
            "closePosition": "true" if close_position else "false",
            "quantity": f"{quantity:.6f}",
        }
        if not self.hide_placed:
            self.algo[aid] = o
        return ManagedOrder(client_order_id=client_order_id or "",
                            state=OrderState.ACKNOWLEDGED, symbol=symbol,
                            is_stop=True, is_algo=True, algo_id=aid, raw=dict(o))

    async def get_open_algo_orders(self, symbol=None):
        return [o for o in self.algo.values() if o.get("symbol") == symbol]

    async def cancel_algo_order(self, *, client_algo_id=None, algo_id=None,
                                symbol=None):
        self.algo.pop(algo_id, None)
        return ManagedOrder(client_order_id=client_algo_id or "",
                            state=OrderState.CANCELED, is_algo=True, is_stop=True)

    def seed(self, *, symbol="BTCUSDT", side="SELL", trig, otype, qty="0.0"):
        aid = str(self._next)
        self._next += 1
        o = {"algoId": aid, "clientAlgoId": f"seed{aid}", "symbol": symbol,
             "side": side, "orderType": otype, "triggerPrice": f"{trig:.2f}",
             "algoStatus": "NEW", "closePosition": "false", "quantity": qty}
        self.algo[aid] = o
        return o


def make_state(symbol="BTCUSDT", *, armed=False, best=0.0):
    st = {"strategies": {}}
    for spec in specs_for_symbol(symbol):
        book = {"entry": {"ms": MS, "side": 1, "px": AVG}}
        if armed:
            book["break_even_armed_ms"] = MS
        st["strategies"][spec.id] = book
    if best:
        st["protection"] = {symbol: {"state": "PROTECTED", "best_price": best}}
    return st


class TestStopTakeProfitDisambiguation(unittest.TestCase):
    """多仓止损与止盈都是 SELL —— 只能靠 orderType 区分。"""

    def test_is_stop_type_excludes_take_profit(self):
        self.assertTrue(is_stop_type({"orderType": "STOP_MARKET"}))
        self.assertFalse(is_stop_type({"orderType": "TAKE_PROFIT_MARKET"}))
        self.assertTrue(is_take_profit_type({"orderType": "TAKE_PROFIT_MARKET"}))
        self.assertFalse(is_take_profit_type({"orderType": "STOP_MARKET"}))

    def test_missing_type_defaults_to_stop_for_compat(self):
        """老响应/既有测试不带 orderType，必须仍按止损处理。"""
        self.assertTrue(is_stop_type({"algoId": "1", "triggerPrice": "84800"}))
        self.assertFalse(is_take_profit_type({"algoId": "1"}))

    def test_find_protective_stop_ignores_higher_take_profit(self):
        """止盈触发价更高，绝不能被当成「更紧的止损」。"""
        orders = [
            {"algoId": "tp1", "side": "SELL", "orderType": "TAKE_PROFIT_MARKET",
             "triggerPrice": "85295.31", "algoStatus": "NEW"},
            {"algoId": "sl1", "side": "SELL", "orderType": "STOP_MARKET",
             "triggerPrice": "85152.80", "algoStatus": "NEW"},
        ]
        got = find_protective_stop(orders, side=1, required_trigger=0.0)
        self.assertIsNotNone(got)
        self.assertEqual(got["algoId"], "sl1", "必须选中真正的止损单")

    def test_only_take_profit_present_means_unprotected(self):
        """只有止盈单时，系统必须认为「没有保护」，而不是「已保护」。"""
        orders = [
            {"algoId": "tp1", "side": "SELL", "orderType": "TAKE_PROFIT_MARKET",
             "triggerPrice": "85295.31", "algoStatus": "NEW"},
        ]
        self.assertIsNone(find_protective_stop(orders, side=1, required_trigger=0.0))

    def test_find_take_profit_ignores_stop(self):
        orders = [
            {"algoId": "sl1", "side": "SELL", "orderType": "STOP_MARKET",
             "triggerPrice": "85152.80", "algoStatus": "NEW"},
            {"algoId": "tp1", "side": "SELL", "orderType": "TAKE_PROFIT_MARKET",
             "triggerPrice": "85295.31", "algoStatus": "NEW"},
        ]
        got = find_take_profit(orders, side=1, trigger=85295.31, tolerance=0.1)
        self.assertIsNotNone(got)
        self.assertEqual(got["algoId"], "tp1")


class TestManageTakeProfit(unittest.TestCase):
    def setUp(self):
        _PROTECTIVE_ATTEMPT.clear()

    def test_first_stage_placed_at_2atr_for_half(self):
        ex = FakeExchange()
        st = make_state()
        _run(manage_take_profit(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                entry_px=AVG, atr_1h=ATR, execute=True))
        self.assertEqual(ex.place_calls, 1)
        o = list(ex.algo.values())[0]
        self.assertEqual(o["orderType"], "TAKE_PROFIT_MARKET")
        self.assertAlmostEqual(float(o["triggerPrice"]), AVG + 2 * ATR, delta=0.11)
        self.assertAlmostEqual(float(o["quantity"]), 0.148, delta=1e-4)
        self.assertEqual(o["closePosition"], "false", "止盈是部分单，不能覆盖全仓")

    def test_idempotent(self):
        ex = FakeExchange()
        st = make_state()
        for _ in range(3):
            _run(manage_take_profit(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                    entry_px=AVG, atr_1h=ATR, execute=True))
        self.assertEqual(ex.place_calls, 1, "同一批止盈不得重复挂")

    def test_second_stage_after_first_filled(self):
        ex = FakeExchange()
        st = make_state()
        rec = st.setdefault("protection", {}).setdefault("BTCUSDT", {})
        rec["tp_filled"] = 1
        _run(manage_take_profit(ex, st, symbol="BTCUSDT", ex_side=0.148,
                                entry_px=AVG, atr_1h=ATR, execute=True))
        o = list(ex.algo.values())[0]
        self.assertAlmostEqual(float(o["triggerPrice"]), AVG + 3 * ATR, delta=0.11)

    def test_no_more_stages_after_all_filled(self):
        ex = FakeExchange()
        st = make_state()
        rec = st.setdefault("protection", {}).setdefault("BTCUSDT", {})
        rec["tp_filled"] = len(TP_STAGES)
        _run(manage_take_profit(ex, st, symbol="BTCUSDT", ex_side=0.074,
                                entry_px=AVG, atr_1h=ATR, execute=True))
        self.assertEqual(ex.place_calls, 0, "所有批次已触发，不应再挂")

    def test_observe_mode_places_nothing(self):
        ex = FakeExchange()
        st = make_state()
        _run(manage_take_profit(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                entry_px=AVG, atr_1h=ATR, execute=False))
        self.assertEqual(ex.place_calls, 0)


class TestReconcileTpFills(unittest.TestCase):
    def test_aligns_books_when_tp_order_gone(self):
        """止盈成交后账本必须对齐 —— 否则净仓同步会把刚止盈的部分买回来。"""
        ex = FakeExchange()
        st = make_state()
        rec = st.setdefault("protection", {}).setdefault("BTCUSDT", {})
        rec["tp_orders"] = [{"algo_id": "999", "trigger": 85295.31,
                             "qty": 0.148, "stage": 0, "filled": False}]
        # 交易所里已经没有 999 了，且净仓只剩一半
        fired = _run(reconcile_tp_fills(ex, st, symbol="BTCUSDT", ex_side=0.148))
        self.assertTrue(fired)
        self.assertTrue(rec["tp_orders"][0]["filled"])
        self.assertEqual(rec["tp_filled"], 1)

    def test_no_action_when_tp_still_alive(self):
        ex = FakeExchange()
        st = make_state()
        o = ex.seed(trig=85295.31, otype="TAKE_PROFIT_MARKET", qty="0.148")
        rec = st.setdefault("protection", {}).setdefault("BTCUSDT", {})
        rec["tp_orders"] = [{"algo_id": o["algoId"], "trigger": 85295.31,
                             "qty": 0.148, "stage": 0, "filled": False}]
        fired = _run(reconcile_tp_fills(ex, st, symbol="BTCUSDT", ex_side=0.296))
        self.assertFalse(fired)
        self.assertFalse(rec["tp_orders"][0]["filled"])

    def test_query_failure_leaves_books_alone(self):
        ex = FakeExchange()
        st = make_state()
        rec = st.setdefault("protection", {}).setdefault("BTCUSDT", {})
        rec["tp_orders"] = [{"algo_id": "999", "trigger": 1.0, "qty": 1.0,
                             "stage": 0, "filled": False}]
        ex.get_open_algo_orders = lambda symbol=None: (_ for _ in ()).throw(
            TimeoutError("429"))
        fired = _run(reconcile_tp_fills(ex, st, symbol="BTCUSDT", ex_side=0.148))
        self.assertFalse(fired)
        self.assertFalse(rec["tp_orders"][0]["filled"])


class TestTrailingStop(unittest.TestCase):
    def test_trail_tightens_when_price_advances(self):
        """价格走高后，止损应随最有利价上移，而不是停在保本价。"""
        ex = FakeExchange()
        st = make_state(armed=True, best=AVG + 400.0)   # 曾到过 +400
        target, armed, _tick = _run(_protective_target(
            ex, st, symbol="BTCUSDT", ex_side=0.296, entry_px=AVG, atr_1h=ATR))
        self.assertTrue(armed)
        self.assertAlmostEqual(target, AVG + 400.0 - TRAIL_ATR * ATR, delta=0.11)
        self.assertGreater(target, AVG, "追踪止损必须仍在均价之上")

    def test_trail_never_loosens_below_breakeven(self):
        """最有利价不高时，追踪价低于保本价 —— 必须取保本价（更紧的一侧）。"""
        ex = FakeExchange()
        st = make_state(armed=True, best=AVG + 100.0)
        target, _armed, _tick = _run(_protective_target(
            ex, st, symbol="BTCUSDT", ex_side=0.296, entry_px=AVG, atr_1h=ATR))
        be = AVG + 110.73
        self.assertAlmostEqual(target, be, delta=0.5,
                               msg="追踪价更松时必须退回保本价，不能放宽")

    def test_trail_only_up(self):
        """同样的最有利价，重复计算必须给出同一个目标价（幂等）。"""
        ex = FakeExchange()
        st = make_state(armed=True, best=AVG + 400.0)
        a = _run(_protective_target(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                    entry_px=AVG, atr_1h=ATR))[0]
        b = _run(_protective_target(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                    entry_px=AVG, atr_1h=ATR))[0]
        self.assertEqual(a, b)

    def test_not_armed_uses_initial_stop(self):
        ex = FakeExchange()
        st = make_state(armed=False, best=AVG + 400.0)
        target, armed, _tick = _run(_protective_target(
            ex, st, symbol="BTCUSDT", ex_side=0.296, entry_px=AVG, atr_1h=ATR))
        self.assertFalse(armed)
        self.assertAlmostEqual(target, AVG - 1.5 * ATR, delta=0.11)


if __name__ == "__main__":
    unittest.main()
