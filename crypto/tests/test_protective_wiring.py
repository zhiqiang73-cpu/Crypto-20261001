"""保护单接入执行器的回归测试 —— 真的调用接入后的代码路径。

为什么需要这个文件
------------------
上一版把 `_protective_attempt_ok` 定义写到了文件末尾（1879 行），调用点在
1055 行。结果：

    * `import shadow.deploy` 完全正常 —— Python 不在导入时求值函数体
    * 765 项全量测试全部通过 —— 它们只测模块，不测 deploy.py 的接入路径
    * 直到运行器重启，启动对账才炸出 NameError，保护单一张都没建起来

所以这里的测试**必须真的调用** `manage_exchange_stop` / `reconcile_protection`
等接入函数，而不是只 import 或只测底层模块。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow.deploy import (  # noqa: E402
    EXCHANGE_STOPS_ENABLED,
    PROTECTIVE_RETRY_INTERVAL_MS,
    _PROTECTIVE_ATTEMPT,
    _protective_attempt_ok,
    clear_ledger_if_stop_fired,
    manage_exchange_stop,
    protection_blocked,
    protection_state,
)
from shadow.strategy_books import specs_for_symbol  # noqa: E402
from trading.models import ManagedOrder, OrderState  # noqa: E402

MS = 1791100000000


def _run(coro):
    return asyncio.run(coro)


class FakeExchange:
    """只实现保护单路径需要的接口。"""

    def __init__(self, *, tick=0.10):
        self.tick = tick
        self.algo: dict = {}
        self._next = 1
        self.place_calls = 0
        self.query_fail = None
        # True = 下单"成功"但 openAlgoOrders 里看不见（模拟字段解析不匹配 /
        # 可见性延迟）。这才是真正危险的失控场景：每 tick 都以为缺保护单，
        # 每 tick 都下一张，在交易所堆出一串重复保护单。
        self.hide_placed = False

    async def price_tick(self, symbol=None):
        return self.tick

    async def place_stop_market(self, side, quantity, stop_price, symbol=None, *,
                                client_order_id=None, close_position=False):
        self.place_calls += 1
        aid = str(self._next)
        self._next += 1
        o = {"algoId": aid, "clientAlgoId": client_order_id or "",
             "symbol": symbol, "side": "SELL" if side == "LONG" else "BUY",
             "triggerPrice": f"{stop_price:.2f}", "algoStatus": "NEW",
             "closePosition": "true" if close_position else "false",
             "quantity": f"{quantity:.6f}"}
        if not self.hide_placed:
            self.algo[aid] = o
        return ManagedOrder(client_order_id=client_order_id or "",
                            state=OrderState.ACKNOWLEDGED, symbol=symbol,
                            is_stop=True, is_algo=True, algo_id=aid, raw=dict(o))

    async def get_open_algo_orders(self, symbol=None):
        if self.query_fail is not None:
            raise self.query_fail
        return [o for o in self.algo.values() if o.get("symbol") == symbol]

    async def cancel_algo_order(self, *, client_algo_id=None, algo_id=None,
                                symbol=None):
        self.algo.pop(algo_id, None)
        return ManagedOrder(client_order_id=client_algo_id or "",
                            state=OrderState.CANCELED, is_algo=True, is_stop=True)

    def seed(self, *, symbol="BTCUSDT", side="SELL", trig=84800.0) -> dict:
        aid = str(self._next)
        self._next += 1
        o = {"algoId": aid, "clientAlgoId": f"seed{aid}", "symbol": symbol,
             "side": side, "triggerPrice": f"{trig:.2f}", "algoStatus": "NEW",
             "closePosition": "true", "quantity": "0.0"}
        self.algo[aid] = o
        return o


def make_state(symbol="BTCUSDT", *, armed=False):
    st = {"strategies": {}}
    for spec in specs_for_symbol(symbol):
        book = {"entry": {"ms": MS, "side": 1, "px": 85000.0}}
        if armed:
            book["break_even_armed_ms"] = MS
        st["strategies"][spec.id] = book
    return st


class TestModuleLevelNames(unittest.TestCase):
    """直接调用模块级函数 —— 这就是 NameError 会露出来的地方。"""

    def test_retry_helper_is_callable(self):
        self.assertIsInstance(_protective_attempt_ok("BTCUSDT"), bool)

    def test_backoff_constants_present(self):
        self.assertIsInstance(EXCHANGE_STOPS_ENABLED, bool)
        self.assertGreater(PROTECTIVE_RETRY_INTERVAL_MS, 0)
        self.assertIsInstance(_PROTECTIVE_ATTEMPT, dict)


class TestManageExchangeStop(unittest.TestCase):
    def setUp(self):
        _PROTECTIVE_ATTEMPT.clear()

    def test_places_initial_stop_when_missing(self):
        ex = FakeExchange()
        st = make_state()
        _run(manage_exchange_stop(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                  entry_px=85042.07, atr_1h=126.18,
                                  execute=True))
        self.assertEqual(ex.place_calls, 1, "缺保护单时必须下一张")
        self.assertEqual(protection_state(st, "BTCUSDT")["state"], "PROTECTED")
        self.assertEqual(len(ex.algo), 1)

    def test_stop_is_below_average_for_long(self):
        """多仓的初始止损必须在均价之下，且距离等于 1.5×ATR。"""
        ex = FakeExchange()
        st = make_state(armed=False)
        _run(manage_exchange_stop(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                  entry_px=85042.07, atr_1h=126.18,
                                  execute=True))
        trig = float(list(ex.algo.values())[0]["triggerPrice"])
        self.assertAlmostEqual(trig, 85042.07 - 1.5 * 126.18, delta=0.11)

    def test_breakeven_stop_is_above_average_for_long(self):
        """保本档触发价必须高于均价（扣掉全部成本后不亏）。"""
        ex = FakeExchange()
        st = make_state(armed=True)
        _run(manage_exchange_stop(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                  entry_px=85042.07, atr_1h=126.18,
                                  execute=True))
        trig = float(list(ex.algo.values())[0]["triggerPrice"])
        self.assertGreater(trig, 85042.07)

    def test_idempotent_when_stop_already_covers(self):
        ex = FakeExchange()
        st = make_state(armed=False)
        target = 85042.07 - 1.5 * 126.18
        ex.seed(trig=target)
        _run(manage_exchange_stop(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                  entry_px=85042.07, atr_1h=126.18,
                                  execute=True))
        self.assertEqual(ex.place_calls, 0, "已有合格保护单不得重复下单")
        self.assertEqual(protection_state(st, "BTCUSDT")["state"], "PROTECTED")

    def test_observe_mode_places_nothing(self):
        ex = FakeExchange()
        st = make_state()
        _run(manage_exchange_stop(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                  entry_px=85042.07, atr_1h=126.18,
                                  execute=False))
        self.assertEqual(ex.place_calls, 0, "观察模式绝不产生委托")

    def test_flat_position_places_nothing(self):
        ex = FakeExchange()
        st = make_state()
        _run(manage_exchange_stop(ex, st, symbol="BTCUSDT", ex_side=0.0,
                                  entry_px=0.0, atr_1h=126.18, execute=True))
        self.assertEqual(ex.place_calls, 0)

    def test_query_failure_marks_unknown_and_places_nothing(self):
        """查询失败不得盲目下单，也不得谎报已保护。"""
        ex = FakeExchange()
        ex.query_fail = TimeoutError("429 Too Many Requests")
        st = make_state()
        _run(manage_exchange_stop(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                  entry_px=85042.07, atr_1h=126.18,
                                  execute=True))
        self.assertEqual(ex.place_calls, 0)
        self.assertEqual(protection_state(st, "BTCUSDT")["state"], "UNKNOWN")
        self.assertIsNotNone(protection_blocked(st, "BTCUSDT"),
                             "未确认时必须禁止开新仓")


class TestRetryBackoff(unittest.TestCase):
    def setUp(self):
        _PROTECTIVE_ATTEMPT.clear()

    def test_backoff_blocks_repeat_placement_when_verify_fails(self):
        """真正危险的场景：下单"成功"但验证看不见。

        不做退避的话，主循环每 15 秒就会以为「缺保护单」再下一张，在交易所
        堆出一串重复保护单 —— 触发时重复平仓。退避期内必须只下第一张。
        """
        ex = FakeExchange()
        ex.hide_placed = True          # 下了但查不到
        st = make_state()
        for _ in range(6):             # 模拟 6 个 tick
            _run(manage_exchange_stop(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                      entry_px=85042.07, atr_1h=126.18,
                                      execute=True))
        self.assertEqual(ex.place_calls, 1,
                         f"退避期内应只下 1 张，实际下了 {ex.place_calls} 张")
        self.assertIn("BTCUSDT", _PROTECTIVE_ATTEMPT)
        self.assertEqual(protection_state(st, "BTCUSDT")["state"], "UNKNOWN",
                         "验证看不见时必须报未确认，不得谎报已保护")
        self.assertIsNotNone(protection_blocked(st, "BTCUSDT"))

    def test_query_failure_places_nothing_at_all(self):
        """查询失败时连下单分支都不该进入 —— 不盲目下单。"""
        ex = FakeExchange()
        ex.query_fail = TimeoutError("429")
        st = make_state()
        for _ in range(3):
            _run(manage_exchange_stop(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                      entry_px=85042.07, atr_1h=126.18,
                                      execute=True))
        self.assertEqual(ex.place_calls, 0)
        self.assertEqual(protection_state(st, "BTCUSDT")["state"], "UNKNOWN")

    def test_backoff_expires(self):
        ex = FakeExchange()
        st = make_state()
        _PROTECTIVE_ATTEMPT["BTCUSDT"] = 0.0     # 很久以前
        _run(manage_exchange_stop(ex, st, symbol="BTCUSDT", ex_side=0.296,
                                  entry_px=85042.07, atr_1h=126.18,
                                  execute=True))
        self.assertEqual(ex.place_calls, 1)


class TestProtectionGate(unittest.TestCase):
    def test_blocked_when_unprotected(self):
        st = {"protection": {"BTCUSDT": {"state": "UNPROTECTED",
                                         "note": "下单被拒"}}}
        self.assertIsNotNone(protection_blocked(st, "BTCUSDT"))

    def test_allowed_when_protected(self):
        st = {"protection": {"BTCUSDT": {"state": "PROTECTED", "note": ""}}}
        self.assertIsNone(protection_blocked(st, "BTCUSDT"))

    def test_allowed_when_no_record(self):
        self.assertIsNone(protection_blocked({}, "BTCUSDT"))


class TestLedgerClearOnStopFill(unittest.TestCase):
    def test_clears_when_recorded_stop_is_gone(self):
        """保护单已从交易所消失 + 交易所空仓 ⇒ 判定保护单成交，清账本。"""
        ex = FakeExchange()
        st = make_state()
        book = next(iter(st["strategies"].values()))
        book["exchange_stop"] = {"algo_id": "999", "trigger": 84852.8}
        # 交易所里已经没有 999 这张单了
        fired = _run(clear_ledger_if_stop_fired(ex, st, symbol="BTCUSDT",
                                                ex_side=0.0))
        self.assertTrue(fired)
        self.assertIsNone(book.get("entry"), "账本必须清零，不得补回")

    def test_does_not_clear_when_stop_still_alive(self):
        ex = FakeExchange()
        st = make_state()
        book = next(iter(st["strategies"].values()))
        o = ex.seed(trig=84852.8)
        book["exchange_stop"] = {"algo_id": o["algoId"], "trigger": 84852.8}
        fired = _run(clear_ledger_if_stop_fired(ex, st, symbol="BTCUSDT",
                                                ex_side=0.0))
        self.assertFalse(fired, "保护单还在，仓位变空是别的原因，不得擅自清账本")
        self.assertIsNotNone(book.get("entry"))

    def test_does_not_clear_when_position_still_open(self):
        ex = FakeExchange()
        st = make_state()
        book = next(iter(st["strategies"].values()))
        book["exchange_stop"] = {"algo_id": "999", "trigger": 84852.8}
        fired = _run(clear_ledger_if_stop_fired(ex, st, symbol="BTCUSDT",
                                                ex_side=0.296))
        self.assertFalse(fired)

    def test_does_not_clear_when_query_fails(self):
        """查不到就下一轮再判，不误清账本。"""
        ex = FakeExchange()
        ex.query_fail = TimeoutError("429")
        st = make_state()
        book = next(iter(st["strategies"].values()))
        book["exchange_stop"] = {"algo_id": "999", "trigger": 84852.8}
        fired = _run(clear_ledger_if_stop_fired(ex, st, symbol="BTCUSDT",
                                                ex_side=0.0))
        self.assertFalse(fired)
        self.assertIsNotNone(book.get("entry"))


if __name__ == "__main__":
    unittest.main()
