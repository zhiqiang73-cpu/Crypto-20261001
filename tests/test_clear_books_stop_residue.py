"""仓位平掉后，上一笔的止损记录必须一起清掉。

2026-10-04 实测 Bug
-------------------
`clear_symbol_books` 旧实现只把 `entry` 置空，`exchange_stop.algo_id`
会永久留着上一笔的死单号。后果：

  1. 表象：`clear_ledger_if_stop_fired` 的「形态 B」每 tick 都判定
     「状态残留」并打印，永不停止（实测刷屏）。
  2. 实质：`manage_exchange_stop` 读 `book["exchange_stop"]` 作为 prev_id，
     非空时走「收紧」分支（先立后破、去撤一张已不存在的单）而不是「建立」
     分支。下一笔新仓的止损走进异常路径 —— 若旧单撤销失败被当成硬失败，
     新仓会被判为未受保护并禁止开新仓，等于止损失效。

本文件锁住这条：**仓位没了，止损记录就不能留。**
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow.strategy_books import (  # noqa: E402
    clear_symbol_books,
    specs_for_symbol,
)


def _book(st, symbol):
    """取该标的第一个策略账本（不 import deploy，避免拉入网络依赖）。"""
    spec = specs_for_symbol(symbol)[0]
    return st["strategies"][spec.id]


def _state_with_stale_stop(symbol="BTCUSDT"):
    st = {"strategies": {}}
    for spec in specs_for_symbol(symbol):
        st["strategies"][spec.id] = {
            "entry": {"ms": 1791100000000, "side": 1, "px": 85000.0},
            "exchange_stop": {"algo_id": "1000000228436951",
                              "trigger": 85152.8, "armed": True},
            "break_even_armed_ms": 1791104400000,
        }
    return st


class TestClearBooksRemovesStopRecord(unittest.TestCase):

    def test_exchange_stop_is_cleared(self):
        st = _state_with_stale_stop()
        clear_symbol_books(st, "BTCUSDT")
        for spec in specs_for_symbol("BTCUSDT"):
            book = st["strategies"][spec.id]
            self.assertIsNone(book["entry"], "账本必须清零")
            self.assertNotIn(
                "exchange_stop", book,
                "上一笔的止损单号必须一起清掉 —— 否则下一笔新仓会拿死单号"
                "走「收紧」分支，止损失效")

    def test_no_stale_stop_reaches_next_position(self):
        """模拟：清账本 → 下一笔开仓 → 必须走「建立」而不是「收紧」。"""
        st = _state_with_stale_stop()
        clear_symbol_books(st, "BTCUSDT")
        # 新仓位写入
        for spec in specs_for_symbol("BTCUSDT"):
            st["strategies"][spec.id]["entry"] = {
                "ms": 1791200000000, "side": 1, "px": 86000.0}
        book = _book(st, "BTCUSDT")
        prev_id = str((book.get("exchange_stop") or {}).get("algo_id") or "")
        self.assertEqual(
            prev_id, "",
            "新仓位的 prev_id 必须为空，才会走「建立」分支去挂新的止损单")

    def test_clearing_twice_is_safe(self):
        st = _state_with_stale_stop()
        clear_symbol_books(st, "BTCUSDT")
        clear_symbol_books(st, "BTCUSDT")   # 幂等
        book = _book(st, "BTCUSDT")
        self.assertNotIn("exchange_stop", book)

    def test_other_symbol_untouched(self):
        st = _state_with_stale_stop("BTCUSDT")
        for spec in specs_for_symbol("ETHUSDT"):
            st["strategies"][spec.id] = {
                "entry": {"ms": 1, "side": 1, "px": 2700.0},
                "exchange_stop": {"algo_id": "999", "trigger": 2692.0},
            }
        clear_symbol_books(st, "BTCUSDT")
        eth = _book(st, "ETHUSDT")
        self.assertEqual(
            str((eth.get("exchange_stop") or {}).get("algo_id") or ""), "999",
            "清 BTC 不能影响 ETH 的止损记录")

    def test_stop_defer_logged_also_cleared(self):
        """stop_defer_logged 是「本次仓位已记录过延迟日志」的标记，同样属于
        上一笔仓位，必须一起清，否则下一笔的延迟日志不会打印。"""
        st = _state_with_stale_stop()
        for spec in specs_for_symbol("BTCUSDT"):
            st["strategies"][spec.id]["stop_defer_logged"] = True
        clear_symbol_books(st, "BTCUSDT")
        for spec in specs_for_symbol("BTCUSDT"):
            self.assertNotIn("stop_defer_logged", st["strategies"][spec.id])




class TestMarkFlatClearsBookStop(unittest.TestCase):
    """「形态 B」路径：账本已空、只剩保护状态残留。

    这条路径**不走** clear_symbol_books，只走 _mark_flat。若 _mark_flat 不清
    exchange_stop，clear_ledger_if_stop_fired 的 ours 集合永远非空，会每
    tick 重复打印「已空仓但状态残留」——2026-10-04 实测最后 200 行里 186 行
    是这一句。
    """

    def setUp(self):
        from shadow.deploy import _mark_flat
        self._mark_flat = _mark_flat

    def test_mark_flat_clears_strategy_book_stop(self):
        st = _state_with_stale_stop()
        for spec in specs_for_symbol("BTCUSDT"):
            st["strategies"][spec.id]["entry"] = None   # 账本已空 = 形态 B
        self._mark_flat(st, "BTCUSDT", 0.0, announce=False)
        for spec in specs_for_symbol("BTCUSDT"):
            book = st["strategies"][spec.id]
            self.assertNotIn(
                "exchange_stop", book,
                "_mark_flat 必须清掉策略账本里的止损记录，否则 ours 永不为空、"
                "每 tick 重复打印「状态残留」")

    def test_mark_flat_also_sets_protection_flat(self):
        st = _state_with_stale_stop()
        st.setdefault("protection", {})["BTCUSDT"] = {
            "state": "PROTECTED", "algo_id": "1000000228436951",
            "trigger": 85152.8, "tp_orders": [{"algo_id": "ord-1"}],
            "best_price": 86000.0}
        self._mark_flat(st, "BTCUSDT", 85152.8, announce=False)
        rec = st["protection"]["BTCUSDT"]
        self.assertEqual(rec["state"], "FLAT")
        self.assertEqual(rec["algo_id"], "")
        self.assertEqual(rec["tp_orders"], [])
        self.assertEqual(rec["best_price"], 0.0)

    def test_other_symbol_stop_untouched(self):
        st = _state_with_stale_stop()
        for spec in specs_for_symbol("ETHUSDT"):
            st["strategies"][spec.id] = {
                "entry": {"ms": 1, "side": 1, "px": 2700.0},
                "exchange_stop": {"algo_id": "eth-alive", "trigger": 2692.0},
            }
        self._mark_flat(st, "BTCUSDT", 0.0, announce=False)
        eth = _book(st, "ETHUSDT")
        self.assertEqual(
            str((eth.get("exchange_stop") or {}).get("algo_id") or ""),
            "eth-alive", "清理 BTC 不能碰 ETH 仍在生效的止损记录")


if __name__ == "__main__":
    unittest.main()
