"""人工干预检测测试 —— 全部离线，不连接交易所、不提交任何委托。

存在理由：2026-10-02 19:47 用户在币安网页手动平仓，运行器 11 秒 / 35 秒后
把两笔原样开了回来。用户裁定：
    人工减仓/平仓 → 该标的虚拟账本清零，不补回，等下一根信号
    人工加仓     → 暂停该标的自动开仓并报警，等人工确认

这些测试锁住的就是上面两条语义，防止以后被"顺手优化"回去。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest

from shadow.deploy import detect_external
from shadow.engine import MIN_QTY
from shadow.external_watch import (KIND_INCREASE, KIND_REDUCE, apply_external,
                                   classify_external, clear_hold_on_new_signal,
                                   consume_resume_requests, external_fills,
                                   is_runner_order, request_resume, summarize,
                                   watch_for)


def _state():
    # kdj5/eth5 是 2026-10-03 停用后留在 state 里的遗留条目：
    # 不参与净仓、不被干预流程触碰，但按真实状态保留在夹具里。
    return {
        "strategies": {
            "kdj15": {"last_ts": 111, "entry": {"side": 1, "qty": 0.19}},
            "kdj5": {"last_ts": 222, "entry": {"side": -1, "qty": 0.191}},
            "eth15": {"last_ts": 333, "entry": {"side": -1, "qty": 4.722}},
            "eth5": {"last_ts": 444, "entry": {"side": 1, "qty": 2.356}},
        },
        "entry": {"side": 1, "qty": 0.19},
    }


class TestOrderAttribution(unittest.TestCase):
    def test_runner_prefixes_are_ours(self):
        for cid in ("kdj01790941634320870", "kd5017", "e1551", "e5abc"):
            self.assertTrue(is_runner_order({"clientOrderId": cid}), cid)

    def test_web_and_other_prefixes_are_foreign(self):
        for cid in ("web_XYJXd56iYx6TA5lG9oqa", "usr123", "smk1", ""):
            self.assertFalse(is_runner_order({"clientOrderId": cid}), cid)


class TestExternalFills(unittest.TestCase):
    def test_filters_by_time_prefix_and_fill(self):
        orders = [
            # 窗口内、非本运行器、已成交 → 命中
            {"clientOrderId": "web_abc", "updateTime": 2000, "executedQty": "0.001"},
            # 本运行器 → 排除
            {"clientOrderId": "kdj0179", "updateTime": 2100, "executedQty": "0.5"},
            # 窗口之前 → 排除
            {"clientOrderId": "web_old", "updateTime": 500, "executedQty": "1"},
            # 没有成交（挂单未成）→ 排除
            {"clientOrderId": "web_pending", "updateTime": 2200, "executedQty": "0"},
        ]
        hits = external_fills(orders, since_ms=1000)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["clientOrderId"], "web_abc")

    def test_empty_orders(self):
        self.assertEqual(external_fills([], since_ms=1), [])
        self.assertEqual(external_fills(None, since_ms=1), [])


class TestClassify(unittest.TestCase):
    def test_exposure_grows_is_increase(self):
        self.assertEqual(classify_external(-0.001, -0.5, min_qty=MIN_QTY), KIND_INCREASE)
        self.assertEqual(classify_external(0.0, 2.0, min_qty=MIN_QTY), KIND_INCREASE)

    def test_exposure_shrinks_is_reduce(self):
        self.assertEqual(classify_external(-2.366, 0.0, min_qty=MIN_QTY), KIND_REDUCE)
        self.assertEqual(classify_external(-2.366, -0.5, min_qty=MIN_QTY), KIND_REDUCE)

    def test_tiny_change_is_ignored(self):
        self.assertIsNone(classify_external(-0.001, -0.0014, min_qty=MIN_QTY))


class TestApplyExternal(unittest.TestCase):
    def test_reduce_clears_books_and_holds(self):
        st = _state()
        watch = apply_external(
            st, "BTCUSDT", KIND_REDUCE, ex_before=-0.001, ex_after=0.0, now=9000
        )
        self.assertTrue(watch["hold"])
        self.assertFalse(watch["paused"])
        self.assertIsNone(st["strategies"]["kdj15"]["entry"])
        self.assertIsNone(st["entry"])
        # 只清 entry，不动 last_ts —— 否则历史 K 线会被重放并立刻反手
        self.assertEqual(st["strategies"]["kdj15"]["last_ts"], 111)
        # 已停用的 5m 账本不参与净仓，人工干预也不去动它
        self.assertEqual(st["strategies"]["kdj5"]["entry"]["qty"], 0.191)
        self.assertEqual(st["strategies"]["kdj5"]["last_ts"], 222)
        # 另一个标的完全不受影响
        self.assertIsNotNone(st["strategies"]["eth15"]["entry"])

    def test_increase_pauses_and_keeps_books(self):
        st = _state()
        watch = apply_external(
            st, "BTCUSDT", KIND_INCREASE, ex_before=-0.001, ex_after=-1.5, now=9000
        )
        self.assertTrue(watch["paused"])
        self.assertFalse(watch["hold"])
        self.assertEqual(watch["paused_ms"], 9000)
        # 账本不能吞下人工加的仓
        self.assertIsNotNone(st["strategies"]["kdj15"]["entry"])
        self.assertEqual(st["strategies"]["kdj15"]["entry"]["qty"], 0.19)

    def test_hold_cleared_only_by_new_signal(self):
        st = _state()
        apply_external(st, "BTCUSDT", KIND_REDUCE, ex_before=-0.001, ex_after=0.0,
                       now=9000)
        self.assertTrue(clear_hold_on_new_signal(st, "BTCUSDT"))
        self.assertFalse(watch_for(st, "BTCUSDT")["hold"])
        # 第二次没有 hold 可解除
        self.assertFalse(clear_hold_on_new_signal(st, "BTCUSDT"))
        # 暂停状态不会被新信号解除
        apply_external(st, "BTCUSDT", KIND_INCREASE, ex_before=0.0, ex_after=-1.0,
                       now=9500)
        self.assertFalse(clear_hold_on_new_signal(st, "BTCUSDT"))
        self.assertTrue(watch_for(st, "BTCUSDT")["paused"])


class TestResumeRequests(unittest.TestCase):
    def test_resume_after_pause_is_accepted(self):
        st = _state()
        apply_external(st, "BTCUSDT", KIND_INCREASE, ex_before=-0.001,
                       ex_after=-1.5, now=9000)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "resume.json")
            request_resume("BTCUSDT", now=9100, path=path)
            self.assertEqual(consume_resume_requests(st, path=path), ["BTCUSDT"])
            self.assertFalse(watch_for(st, "BTCUSDT")["paused"])
            # 一次性消费
            self.assertFalse(os.path.exists(path))

    def test_stale_request_before_pause_is_ignored(self):
        st = _state()
        apply_external(st, "BTCUSDT", KIND_INCREASE, ex_before=-0.001,
                       ex_after=-1.5, now=9000)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "resume.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"requests": {"BTCUSDT": 8000}}, fh)
            self.assertEqual(consume_resume_requests(st, path=path), [])
            self.assertTrue(watch_for(st, "BTCUSDT")["paused"])

    def test_missing_file_is_noop(self):
        self.assertEqual(consume_resume_requests(_state(), path="/nonexistent/x.json"), [])


class TestSummarize(unittest.TestCase):
    def test_only_reports_symbols_with_intervention(self):
        st = _state()
        self.assertEqual(summarize(st), {})
        apply_external(st, "ETHUSDT", KIND_INCREASE, ex_before=-2.366,
                       ex_after=-4.0, now=9000, detail="ETH 人工加仓")
        out = summarize(st)
        self.assertEqual(list(out.keys()), ["ETHUSDT"])
        self.assertTrue(out["ETHUSDT"]["paused"])
        self.assertEqual(out["ETHUSDT"]["detail"], "ETH 人工加仓")


class _FakeClient:
    def __init__(self, orders):
        self._orders = orders
        self.calls = 0

    async def all_orders(self, symbol=None, *, limit=50, start_time=None):
        self.calls += 1
        return list(self._orders)


class TestDetectExternal(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_first_sighting_only_records_baseline(self):
        watch = {}
        client = _FakeClient([])
        hit = self._run(detect_external(client, "BTCUSDT", ex_now=-0.001,
                                        now=1000, watch=watch))
        self.assertIsNone(hit)
        self.assertEqual(watch["last_net"], -0.001)
        self.assertEqual(watch["last_check_ms"], 1000)
        self.assertEqual(client.calls, 0)   # 没有基线时不该去查委托

    def test_manual_close_is_detected_as_reduce(self):
        watch = {"last_check_ms": 1000, "last_net": -2.366}
        client = _FakeClient([
            {"clientOrderId": "web_abc", "updateTime": 1500,
             "executedQty": "2.367", "side": "BUY", "avgPrice": "2747.63"},
        ])
        hit = self._run(detect_external(client, "ETHUSDT", ex_now=0.0,
                                        now=2000, watch=watch))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["kind"], KIND_REDUCE)
        self.assertAlmostEqual(hit["ex_before"], -2.366)
        self.assertAlmostEqual(hit["ex_after"], 0.0)
        self.assertIn("web_abc", hit["detail"])
        # 基线推进，避免下一轮重复判定同一笔
        self.assertEqual(watch["last_net"], 0.0)
        self.assertEqual(watch["last_check_ms"], 2000)

    def test_manual_add_is_detected_as_increase(self):
        watch = {"last_check_ms": 1000, "last_net": -0.001}
        client = _FakeClient([
            {"clientOrderId": "web_add", "updateTime": 1500,
             "executedQty": "1.0", "side": "SELL", "avgPrice": "86000"},
        ])
        hit = self._run(detect_external(client, "BTCUSDT", ex_now=-1.001,
                                        now=2000, watch=watch))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["kind"], KIND_INCREASE)

    def test_our_own_order_is_not_an_intervention(self):
        watch = {"last_check_ms": 1000, "last_net": 0.0}
        client = _FakeClient([
            {"clientOrderId": "kdj0179", "updateTime": 1500,
             "executedQty": "0.19", "side": "BUY", "avgPrice": "86308"},
        ])
        hit = self._run(detect_external(client, "BTCUSDT", ex_now=0.19,
                                        now=2000, watch=watch))
        self.assertIsNone(hit)

    def test_unfilled_manual_order_is_not_an_intervention(self):
        watch = {"last_check_ms": 1000, "last_net": -0.001}
        client = _FakeClient([
            {"clientOrderId": "web_pending", "updateTime": 1500,
             "executedQty": "0", "side": "BUY", "avgPrice": "0"},
        ])
        hit = self._run(detect_external(client, "BTCUSDT", ex_now=-0.001,
                                        now=2000, watch=watch))
        self.assertIsNone(hit)


if __name__ == "__main__":
    unittest.main()
