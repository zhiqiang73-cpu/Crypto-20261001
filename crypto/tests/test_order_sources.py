"""订单来源分类测试。

存在理由：2026-10-02 用户看到 10:22 出现 5 笔成交，以为策略发了 5 次信号。
根因是账户里「策略单」和「功能测试单」混在一起、没有可核验的来源标签。
这些测试钉死两件事：

1. 依据必须可核验（委托号台账 / clientOrderId 前缀 / 已记录的测试窗口）；
2. **判不出来时保持 visible（unknown），绝不把真实成交猜成测试单藏起来。**
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from review.order_sources import (HIDDEN_BY_DEFAULT, SOURCE_FUNCTION_TEST,
                                  SOURCE_LABELS, SOURCE_MANUAL_WEB,
                                  SOURCE_STRATEGY, SOURCE_UNKNOWN,
                                  SOURCE_USER_ACTION, annotate_orders,
                                  annotate_trades, classify, in_test_window,
                                  load_strategy_order_ids, summarize)

_BJ = timezone(timedelta(hours=8))


def _ms(text: str) -> int:
    """北京时间字符串 → 毫秒时间戳。"""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=_BJ).timestamp() * 1000)


# 已记录的测试窗口内 / 外（北京时间）
IN_WINDOW = _ms("2026-10-02 10:22")
BEFORE_WINDOW = _ms("2026-10-02 09:00")
AFTER_WINDOW = _ms("2026-10-02 11:30")
SMOKE_WINDOW = _ms("2026-10-01 16:13")


class TestClassify(unittest.TestCase):
    def test_strategy_by_ledger_order_id(self):
        row = {"orderId": 28613288405, "clientOrderId": "mkt1790879462739999",
               "time": IN_WINDOW}
        self.assertEqual(
            classify(row, strategy_order_ids=["28613288405"]), SOURCE_STRATEGY
        )

    def test_strategy_by_kdj_prefix(self):
        row = {"orderId": 1, "clientOrderId": "kdj0abc", "time": IN_WINDOW}
        self.assertEqual(classify(row), SOURCE_STRATEGY)

    def test_manual_web_by_prefix(self):
        row = {"orderId": 2, "clientOrderId": "web_abc123", "time": IN_WINDOW}
        self.assertEqual(classify(row), SOURCE_MANUAL_WEB)

    def test_smk_prefix_is_function_test(self):
        row = {"orderId": 3, "clientOrderId": "smk0abc", "time": AFTER_WINDOW}
        self.assertEqual(classify(row), SOURCE_FUNCTION_TEST)

    def test_smoke_prefix_is_function_test(self):
        row = {"orderId": 4, "clientOrderId": "smoke7181", "time": IN_WINDOW}
        self.assertEqual(classify(row), SOURCE_FUNCTION_TEST)

    def test_usr_prefix_is_user_action(self):
        row = {"orderId": 5, "clientOrderId": "usr0abc", "time": IN_WINDOW}
        self.assertEqual(classify(row), SOURCE_USER_ACTION)

    def test_own_prefix_inside_recorded_window_is_test(self):
        for cid in ("ps017909077271", "cx179090774419", "lmt179090774419",
                    "mkt179084239752"):
            with self.subTest(cid=cid):
                row = {"orderId": 6, "clientOrderId": cid, "time": IN_WINDOW}
                self.assertEqual(classify(row), SOURCE_FUNCTION_TEST)

    def test_smoke_window_also_detected(self):
        row = {"orderId": 7, "clientOrderId": "mkt179084239752", "time": SMOKE_WINDOW}
        self.assertEqual(classify(row), SOURCE_FUNCTION_TEST)

    def test_own_prefix_outside_window_is_unknown_not_hidden(self):
        """判不出来必须保持可见 —— 绝不能把真实成交猜成测试单。"""
        for stamp in (BEFORE_WINDOW, AFTER_WINDOW):
            with self.subTest(stamp=stamp):
                row = {"orderId": 8, "clientOrderId": "lmt179090999999", "time": stamp}
                self.assertEqual(classify(row), SOURCE_UNKNOWN)

    def test_foreign_prefix_is_unknown(self):
        row = {"orderId": 9, "clientOrderId": "someOtherBot", "time": IN_WINDOW}
        self.assertEqual(classify(row), SOURCE_UNKNOWN)

    def test_missing_client_id_and_time_is_unknown(self):
        self.assertEqual(classify({"orderId": 10}), SOURCE_UNKNOWN)

    def test_update_time_used_when_time_absent(self):
        row = {"orderId": 11, "clientOrderId": "ps0x", "updateTime": IN_WINDOW}
        self.assertEqual(classify(row), SOURCE_FUNCTION_TEST)


class TestWindows(unittest.TestCase):
    def test_boundaries_inclusive(self):
        self.assertTrue(in_test_window(_ms("2026-10-02 10:15")))
        self.assertTrue(in_test_window(_ms("2026-10-02 10:23")))
        self.assertFalse(in_test_window(_ms("2026-10-02 10:14")))
        self.assertFalse(in_test_window(_ms("2026-10-02 10:24")))

    def test_bad_timestamp_is_false(self):
        for bad in (None, 0, "", "abc"):
            with self.subTest(bad=bad):
                self.assertFalse(in_test_window(bad))


class TestAnnotate(unittest.TestCase):
    def setUp(self):
        self.orders = [
            {"orderId": 100, "clientOrderId": "web_a", "time": IN_WINDOW},
            {"orderId": 101, "clientOrderId": "smk0", "time": IN_WINDOW},
            {"orderId": 102, "clientOrderId": "mkt1", "time": IN_WINDOW},
        ]

    def test_orders_get_source_and_label(self):
        out = annotate_orders(self.orders)
        self.assertEqual([o["source"] for o in out],
                         [SOURCE_MANUAL_WEB, SOURCE_FUNCTION_TEST,
                          SOURCE_FUNCTION_TEST])
        for row in out:
            self.assertEqual(row["source_label"], SOURCE_LABELS[row["source"]])

    def test_annotate_does_not_mutate_input(self):
        annotate_orders(self.orders)
        self.assertNotIn("source", self.orders[0])

    def test_strategy_ledger_overrides_window_heuristic(self):
        """策略单即使落在测试窗口内也必须判为策略单。"""
        out = annotate_orders(self.orders, strategy_order_ids=["102"])
        self.assertEqual(out[2]["source"], SOURCE_STRATEGY)

    def test_trades_inherit_parent_source(self):
        orders = annotate_orders(self.orders)
        trades = [
            {"id": 1, "orderId": 100, "time": IN_WINDOW},
            {"id": 2, "orderId": 101, "time": IN_WINDOW},
        ]
        out = annotate_trades(trades, orders)
        self.assertEqual([t["source"] for t in out],
                         [SOURCE_MANUAL_WEB, SOURCE_FUNCTION_TEST])

    def test_trades_without_parent_fall_back_to_classify(self):
        trades = [{"id": 3, "orderId": 999, "time": IN_WINDOW,
                   "clientOrderId": "web_z"}]
        out = annotate_trades(trades, [])
        self.assertEqual(out[0]["source"], SOURCE_MANUAL_WEB)

    def test_summarize_counts(self):
        counts = summarize(annotate_orders(self.orders))
        self.assertEqual(counts[SOURCE_MANUAL_WEB], 1)
        self.assertEqual(counts[SOURCE_FUNCTION_TEST], 2)

    def test_only_function_test_hidden_by_default(self):
        self.assertEqual(tuple(HIDDEN_BY_DEFAULT), (SOURCE_FUNCTION_TEST,))

    def test_every_source_has_label(self):
        for key in (SOURCE_STRATEGY, SOURCE_MANUAL_WEB, SOURCE_FUNCTION_TEST,
                    SOURCE_USER_ACTION, SOURCE_UNKNOWN):
            self.assertIn(key, SOURCE_LABELS)


class TestStrategyLedger(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(load_strategy_order_ids("/nonexistent/ledger.jsonl"), {})

    def test_parses_records_and_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"order_id": "1", "action": "开空"}) + "\n")
                fh.write("not json\n")
                fh.write("\n")
                fh.write(json.dumps({"order_id": 2, "action": "平仓"}) + "\n")
                fh.write(json.dumps({"action": "no order id"}) + "\n")
            ids = load_strategy_order_ids(path)
            self.assertEqual(sorted(ids), ["1", "2"])
            self.assertEqual(ids["1"]["action"], "开空")

    def test_repository_ledger_marks_real_strategy_orders(self):
        """仓库内的台账必须把已确认的两笔策略单标为策略来源。"""
        ids = load_strategy_order_ids()
        if not ids:  # 全新环境没有运行时台账属正常，不得据此断言
            self.skipTest("运行时台账尚未生成")
        for oid in ids:
            row = {"orderId": oid, "clientOrderId": "mkt1", "time": IN_WINDOW}
            self.assertEqual(classify(row, strategy_order_ids=ids), SOURCE_STRATEGY)


if __name__ == "__main__":
    unittest.main()
