"""人工干预检测的时间窗回归测试。

核心回归：**在非扫描 tick 手工平仓，系统绝不能补回。**

原实现每 15 秒推进一次净仓基线，却每 60 秒才扫一次订单，导致扫描执行时
查询起点是「上一个 tick」而不是「上一次扫描」——中间 45 秒的窗口永远扫不到。
下面的测试用同一串事件序列同时跑「新逻辑」与「旧逻辑」的等价实现，证明
旧逻辑漏检、新逻辑检出。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow.external_guard import (
    SCAN_INTERVAL_MS,
    ExternalWatch,
    fill_blocked,
    note_net,
    scan_external,
    should_scan,
)

MIN_QTY = 1e-9
TICK_MS = 15_000          # 主循环约 15 秒一个 tick
SCAN_MS = 60_000          # 旧实现 60 秒才扫一次


def _run(coro):
    return asyncio.run(coro)


# --- 可替换的判定函数（避开 deploy.py 的重依赖）---------------------------
def _fills_fn(orders, *, since_ms: int):
    """只把 clientOrderId 以 manual 开头的成交当作外部（人工）单。"""
    return [o for o in orders
            if int(o.get("time") or 0) >= since_ms
            and str(o.get("clientOrderId") or "").startswith("manual")]


def _classify(before: float, after: float, *, min_qty: float):
    if abs(after - before) <= min_qty:
        return None
    return "add" if abs(after) > abs(before) else "reduce"


class FakeOrders:
    """假交易所委托查询。"""

    def __init__(self) -> None:
        self.orders: list = []
        self.fail: Exception | None = None
        self.queries: list = []
        # 「当前时刻」。查询只能看到此刻**已经发生**的成交 —— 不设上界的话
        # 假交易所会把未来的订单也返回，旧逻辑会被测成「检出」，掩盖真实缺陷。
        self.now: int = 10 ** 18

    def add(self, *, t: int, cid: str, qty: float, price: float,
            side: str = "SELL") -> None:
        self.orders.append({
            "time": t, "clientOrderId": cid, "executedQty": qty,
            "avgPrice": price, "side": side,
        })

    async def all_orders(self, symbol, *, start_time=0, limit=500):
        self.queries.append(start_time)
        if self.fail is not None:
            raise self.fail
        return [o for o in self.orders
                if start_time <= int(o["time"]) <= self.now]


async def _drive_new_logic(ex: FakeOrders, events: list):
    """按事件序列驱动新逻辑。events: [(now, ex_now)]。返回检测结果。"""
    watch = ExternalWatch()
    hits = []
    for now, ex_now in events:
        ex.now = now
        note_net(watch, ex_now=ex_now, now=now, min_qty=MIN_QTY)
        got = await scan_external(ex, "BTCUSDT", ex_now=ex_now, now=now,
                                  watch=watch, min_qty=MIN_QTY,
                                  classify=_classify, fills_fn=_fills_fn)
        if got:
            hits.append((now, got))
    return watch, hits


def _drive_old_logic(ex: FakeOrders, events: list):
    """旧实现语义的等价复刻 —— 每个 tick 推进基线，60 秒才扫。"""
    last_check_ms = 0
    last_net = None
    last_scan_ms = 0
    hits = []
    for now, ex_now in events:
        ex.now = now
        prev_ms, prev_net = last_check_ms, last_net
        last_check_ms = now
        last_net = ex_now
        if prev_ms <= 0 or prev_net is None:
            continue
        if last_scan_ms > 0 and now - last_scan_ms < SCAN_MS:
            continue
        last_scan_ms = now
        # ⚠ 查询起点是 prev_ms（上个 tick），不是 last_scan_ms（上次扫描）
        # 上界用 now：此刻还没发生的成交不能出现在查询结果里。
        orders = [o for o in ex.orders
                  if prev_ms <= int(o["time"]) <= now]
        found = _fills_fn(orders, since_ms=prev_ms)
        if found:
            hits.append((now, prev_net, ex_now))
    return hits


class TestNonScanTickManualClose(unittest.TestCase):
    """需求原文：在非扫描 tick 手工平仓，系统绝不能补回。"""

    def _scenario(self):
        """T=0 建基线；T=45s 用户手工平仓；之后每 15 秒一个 tick。"""
        ex = FakeOrders()
        # 手工平仓发生在 T=45s —— 注意它落在「距上次扫描 45 秒」的位置，
        # 也就是旧实现扫描时查询窗口（只回看 15 秒）之外。
        ex.add(t=45_000, cid="manual_web_1", qty=0.296, price=84_900.0)
        events = [
            (0, 0.296),          # 第一次见到：只记基线
            (15_000, 0.296),
            (30_000, 0.296),
            (45_000, 0.296),     # 平仓前最后一刻仍是满仓
            (60_000, 0.0),       # ← 手工平仓已生效，tick 看到净仓变化
            (75_000, 0.0),
            (90_000, 0.0),
        ]
        return ex, events

    def test_new_logic_detects_it(self):
        ex, events = self._scenario()
        watch, hits = _run(_drive_new_logic(ex, events))
        self.assertEqual(len(hits), 1, f"必须检出人工平仓，实际 {hits}")
        now, got = hits[0]
        self.assertEqual(got["kind"], "reduce")
        self.assertAlmostEqual(got["ex_after"], 0.0)
        self.assertIn("manual", got["detail"])

    def test_old_logic_misses_it(self):
        """证明修复的必要性：旧逻辑在同一串事件下漏检。"""
        ex, events = self._scenario()
        hits = _drive_old_logic(ex, events)
        self.assertEqual(hits, [], "旧逻辑应当漏检（这正是被修复的缺陷）")

    def test_scan_window_covers_full_interval(self):
        """扫描的查询起点必须是「距上次扫描」，不是「距上个 tick」。"""
        ex, events = self._scenario()
        _run(_drive_new_logic(ex, events))
        self.assertTrue(ex.queries, "应当发生过查询")
        # 检测到变化那一次扫描，起点必须早于手工平仓时刻 45_000
        detecting = [q for q in ex.queries if q <= 45_000]
        self.assertTrue(detecting,
                        f"查询起点没有覆盖到 45s 的手工单: {ex.queries}")


class TestFastGate(unittest.TestCase):
    def test_change_opens_gate_immediately(self):
        w = ExternalWatch(last_scan_ms=0, scan_net=0.296)
        why = note_net(w, ex_now=0.0, now=60_000, min_qty=MIN_QTY)
        self.assertIsNotNone(why)
        self.assertTrue(w.pending)
        self.assertIsNotNone(fill_blocked(w), "变化未定性前必须拦住自动补仓")

    def test_no_change_keeps_gate_open(self):
        w = ExternalWatch(last_scan_ms=0, scan_net=0.296)
        self.assertIsNone(note_net(w, ex_now=0.296, now=60_000,
                                   min_qty=MIN_QTY))
        self.assertIsNone(fill_blocked(w))

    def test_gate_clears_after_scan(self):
        ex = FakeOrders()
        ex.add(t=45_000, cid="manual_web_1", qty=0.296, price=84_900.0)
        ex.now = 60_000
        w = ExternalWatch(last_scan_ms=0, scan_net=0.296)
        note_net(w, ex_now=0.0, now=60_000, min_qty=MIN_QTY)
        self.assertTrue(w.pending)
        _run(scan_external(ex, "BTCUSDT", ex_now=0.0, now=60_000, watch=w,
                           min_qty=MIN_QTY, classify=_classify,
                           fills_fn=_fills_fn))
        self.assertFalse(w.pending, "定性之后必须解除闸门")
        self.assertIsNone(fill_blocked(w))

    def test_gate_stays_closed_when_scan_fails(self):
        """429 限流时不能「看起来正常」却把闸门打开。"""
        ex = FakeOrders()
        ex.fail = TimeoutError("429 Too Many Requests")
        ex.now = 60_000
        w = ExternalWatch(last_scan_ms=0, scan_net=0.296)
        note_net(w, ex_now=0.0, now=60_000, min_qty=MIN_QTY)
        _run(scan_external(ex, "BTCUSDT", ex_now=0.0, now=60_000, watch=w,
                           min_qty=MIN_QTY, classify=_classify,
                           fills_fn=_fills_fn))
        self.assertTrue(w.pending, "扫描失败不得解除闸门")
        self.assertIsNotNone(fill_blocked(w))
        self.assertEqual(w.misses, 1)

    def test_failed_scan_does_not_advance_baseline(self):
        """一次 429 不能把窗口吃掉 —— 基线不推进，下次重扫同一段。"""
        ex = FakeOrders()
        ex.fail = TimeoutError("429")
        w = ExternalWatch(last_scan_ms=1_000, scan_net=0.296)
        _run(scan_external(ex, "BTCUSDT", ex_now=0.0, now=60_000, watch=w,
                           min_qty=MIN_QTY, classify=_classify,
                           fills_fn=_fills_fn))
        self.assertEqual(w.last_scan_ms, 1_000, "失败时基线不得推进")
        self.assertAlmostEqual(w.scan_net, 0.296)

    def test_pending_triggers_immediate_scan(self):
        """闸门一开就立刻扫，不等 60 秒周期。"""
        w = ExternalWatch(last_scan_ms=1_000, scan_net=0.296)
        self.assertFalse(should_scan(w, now=2_000), "没变化时不该提前扫")
        note_net(w, ex_now=0.0, now=2_000, min_qty=MIN_QTY)
        self.assertTrue(should_scan(w, now=2_000), "有变化必须立刻扫")

    def test_periodic_scan_still_happens(self):
        w = ExternalWatch(last_scan_ms=1_000, scan_net=0.296)
        self.assertTrue(should_scan(w, now=1_000 + SCAN_INTERVAL_MS))


class TestBaselineDiscipline(unittest.TestCase):
    def test_first_sight_only_records(self):
        """重启后不能把「重启前就有的仓位」误判成人工干预。"""
        ex = FakeOrders()
        w = ExternalWatch()
        got = _run(scan_external(ex, "BTCUSDT", ex_now=0.296, now=1_000,
                                 watch=w, min_qty=MIN_QTY,
                                 classify=_classify, fills_fn=_fills_fn))
        self.assertIsNone(got)
        self.assertEqual(ex.queries, [], "首次见到不该发查询")
        self.assertAlmostEqual(w.scan_net, 0.296)

    def test_tick_does_not_advance_scan_baseline(self):
        """核心不变量：每个 tick 只判定，不推进扫描基线。"""
        w = ExternalWatch(last_scan_ms=1_000, scan_net=0.296)
        for now in range(2_000, 60_000, TICK_MS):
            note_net(w, ex_now=0.296, now=now, min_qty=MIN_QTY)
        self.assertEqual(w.last_scan_ms, 1_000,
                         "扫描基线只能由扫描推进")

    def test_own_orders_are_not_external(self):
        """本运行器自己的成交不算人工干预。"""
        ex = FakeOrders()
        ex.add(t=45_000, cid="kdjx1791105492193", qty=0.098, price=85_098.0)
        ex.now = 60_000
        w = ExternalWatch(last_scan_ms=0, scan_net=0.198)
        got = _run(scan_external(ex, "BTCUSDT", ex_now=0.296, now=60_000,
                                 watch=w, min_qty=MIN_QTY,
                                 classify=_classify, fills_fn=_fills_fn))
        self.assertIsNone(got, "自家委托不得被当成人工干预")


if __name__ == "__main__":
    unittest.main()
