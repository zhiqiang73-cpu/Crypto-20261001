"""B6 回归：保护单成交必须被认成「自己的动作」，不能被误判为人工干预。

2026-10-04 实测事故
------------------
BTC 保护单 20:47 触发平仓（触发价 85152.80，成交 85138.90）。运行器把它记成
`[BTCUSDT 人工减仓跟随]`，日志里 0 次「保护单已成交」，保护状态停留在
PROTECTED（126 秒未更新，而 ETH 6 秒前刚更新过），也没有任何告警。

根因：`clear_ledger_if_stop_fired` 排在 `apply_external` 之后，账本已被当作
人工减仓清空，其前置守卫 `if not book.get("entry"): return False` 直接返回
False —— 该判定**永远不可能生效**。

修复：把判定提到人工干预检测之前；成交后状态记 FLAT 并清掉已死单号；发告警。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow import alerts
from shadow import deploy


def run(coro):
    return asyncio.run(coro)


class FakeAlgoClient:
    """只实现本判定用到的接口。"""

    def __init__(self, open_orders=None):
        self.open_orders = list(open_orders or [])
        self.calls = 0

    async def get_open_algo_orders(self, symbol=None):
        self.calls += 1
        return [dict(o) for o in self.open_orders]


def _state_with_ledger(symbol="BTCUSDT"):
    st = deploy._default_state()
    st["strategies"] = {}
    st["protection"] = {}
    return st


class TestStopAttribution(unittest.TestCase):

    def setUp(self):
        self.tmpdir = os.path.join(
            os.path.dirname(__file__), "..", "..", ".b6-tmp")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.alert_log = os.path.join(self.tmpdir, "alerts.jsonl")
        if os.path.exists(self.alert_log):
            os.remove(self.alert_log)
        self._p = self._patch_alerts()
        alerts.reset_cooldown()

    def _patch_alerts(self):
        import unittest.mock as mock
        p = mock.patch.object(alerts, "ALERT_LOG", self.alert_log)
        p.start()
        return p

    def tearDown(self):
        self._p.stop()

    def _make_state(self, symbol="BTCUSDT", *, algo_id="1000000228436951",
                    tp_ids=("1000000228506927",)):
        """构造「账本记着仓位 + 有保护单记录」的状态。"""
        st = deploy._default_state()
        st["strategies"] = {}
        st["protection"] = {
            symbol: {
                "state": "PROTECTED",
                "algo_id": algo_id,
                "trigger": 85152.8,
                "tp_orders": [{"algo_id": t, "stage": 0, "filled": False}
                              for t in tp_ids],
                "tp_filled": 0,
                "best_price": 85252.44,
            }
        }
        # 账本：用 strategy_books 的真实结构，避免测试与实现脱节
        from shadow.strategy_books import SPEC_15M
        sid = "kdj15" if symbol == "BTCUSDT" else "eth15"
        st["strategies"][sid] = {
            "entry": {"side": 1, "qty": 0.296, "avg_price": 84852.8,
                      "layers": [{"side": 1, "px": 84852.8, "qty": 0.296,
                                  "layer": 1}]},
            "exchange_stop": {"algo_id": algo_id, "trigger": 85152.8,
                              "armed": True},
        }
        return st, sid

    # ------------------------------------------------------------------
    def test_stop_fired_is_detected_when_orders_gone(self):
        """核心回归：仓位归零 + 我们的单不在了 → 必须判为保护单成交。"""
        st, sid = self._make_state()
        client = FakeAlgoClient(open_orders=[])          # 单子都没了
        fired = run(deploy.clear_ledger_if_stop_fired(
            client, st, symbol="BTCUSDT", ex_side=0.0))
        self.assertTrue(fired, "必须识别为保护单成交")

    def test_ledger_cleared_after_detection(self):
        st, sid = self._make_state()
        client = FakeAlgoClient(open_orders=[])
        run(deploy.clear_ledger_if_stop_fired(
            client, st, symbol="BTCUSDT", ex_side=0.0))
        entry = (st["strategies"][sid].get("entry") or {})
        self.assertFalse(entry.get("qty"), "账本必须清零，绝不补回")

    def test_state_becomes_flat_not_protected(self):
        """修前状态停留在 PROTECTED —— 仓位都没了还声称受保护是假的。"""
        st, sid = self._make_state()
        client = FakeAlgoClient(open_orders=[])
        run(deploy.clear_ledger_if_stop_fired(
            client, st, symbol="BTCUSDT", ex_side=0.0))
        ps = deploy.protection_state(st, "BTCUSDT")
        self.assertEqual(ps.get("state"), "FLAT")
        self.assertEqual(ps.get("algo_id"), "", "必须清掉已死的单号")
        self.assertEqual(ps.get("tp_orders"), [], "必须清掉上一笔的止盈批次")
        self.assertEqual(ps.get("best_price"), 0.0)
        self.assertEqual(float(ps.get("fired_trigger") or 0), 85152.8)

    def test_flat_state_does_not_block_new_entries(self):
        st, sid = self._make_state()
        client = FakeAlgoClient(open_orders=[])
        run(deploy.clear_ledger_if_stop_fired(
            client, st, symbol="BTCUSDT", ex_side=0.0))
        self.assertIsNone(deploy.protection_blocked(st, "BTCUSDT"),
                          "FLAT 不得挡住下一笔开仓")

    def test_critical_alert_emitted(self):
        """止损成交是无人值守时必须知道的事件，此前只打一行 stdout。"""
        st, sid = self._make_state()
        client = FakeAlgoClient(open_orders=[])
        run(deploy.clear_ledger_if_stop_fired(
            client, st, symbol="BTCUSDT", ex_side=0.0))
        with open(self.alert_log, encoding="utf-8") as fh:
            import json
            rows = [json.loads(x) for x in fh if x.strip()]
        keys = [r["key"] for r in rows]
        self.assertIn("stop_fired_BTCUSDT", keys)
        self.assertEqual(rows[0]["level"], "CRITICAL")

    # ------------------------------------------------------------------
    def test_manual_close_is_not_claimed_as_ours(self):
        """我们的单还挂着 → 仓位是别的原因变空的 → 不擅自清账本。"""
        st, sid = self._make_state()
        client = FakeAlgoClient(open_orders=[
            {"algoId": "1000000228436951", "orderType": "STOP_MARKET",
             "side": "SELL", "triggerPrice": "85152.8", "symbol": "BTCUSDT"}])
        fired = run(deploy.clear_ledger_if_stop_fired(
            client, st, symbol="BTCUSDT", ex_side=0.0))
        self.assertFalse(fired, "我们的单还在，不得认成自己成交")
        self.assertTrue((st["strategies"][sid].get("entry") or {}).get("qty"))

    def test_with_position_still_open_returns_fast(self):
        """有仓时首行就返回 —— 不得为每轮多打一次 API。"""
        st, sid = self._make_state()
        client = FakeAlgoClient(open_orders=[])
        fired = run(deploy.clear_ledger_if_stop_fired(
            client, st, symbol="BTCUSDT", ex_side=0.296))
        self.assertFalse(fired)
        self.assertEqual(client.calls, 0, "有仓时不应发起查询")

    def test_no_ledger_entry_returns_fast(self):
        st = deploy._default_state()
        st["strategies"] = {}
        client = FakeAlgoClient(open_orders=[])
        fired = run(deploy.clear_ledger_if_stop_fired(
            client, st, symbol="BTCUSDT", ex_side=0.0))
        self.assertFalse(fired)
        self.assertEqual(client.calls, 0)

    def test_query_failure_does_not_clear(self):
        """查不到就宁可下一轮再判，也不误清账本。"""
        class Boom(FakeAlgoClient):
            async def get_open_algo_orders(self, symbol=None):
                raise TimeoutError("429")

        st, sid = self._make_state()
        fired = run(deploy.clear_ledger_if_stop_fired(
            Boom(), st, symbol="BTCUSDT", ex_side=0.0))
        self.assertFalse(fired)
        self.assertTrue((st["strategies"][sid].get("entry") or {}).get("qty"))


class TestCallOrdering(unittest.TestCase):
    """顺序即正确性：判定必须在人工干预检测之前。"""

    def test_stop_check_precedes_external_scan(self):
        import inspect
        src = inspect.getsource(deploy.process_symbol)
        stop_at = src.find("clear_ledger_if_stop_fired")
        ext_at = src.find("detect_external")
        self.assertGreater(stop_at, -1, "process_symbol 必须调用成交判定")
        self.assertGreater(ext_at, -1, "process_symbol 必须调用人工干预检测")
        self.assertLess(
            stop_at, ext_at,
            "保护单成交判定必须排在人工干预检测之前 —— 否则账本先被 "
            "apply_external 清空，该判定的前置守卫会永远返回 False（B6）")


if __name__ == "__main__":
    unittest.main()
