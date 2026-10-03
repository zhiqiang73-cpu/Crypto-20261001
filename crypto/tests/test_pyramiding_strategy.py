"""同向分层趋势跟随：只测虚拟账本，不下单、不访问交易所。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import shadow.deploy as deploy
from shadow.strategy_books import (
    SPEC_15M,
    SPEC_5M,
    apply_virtual_signal,
    book_signed_qty,
    empty_book,
    entry_layers,
    layer_count,
    position_sources,
    desired_net,
)


class TestVirtualPyramiding(unittest.TestCase):
    """用户确认的六个状态：首层、同向加层、达上限、背离不动、反向清层、风控阻加。"""

    def _apply(self, book, *, long=False, short=False, px=100.0, ms=1,
               block=False, max_layers=3):
        return apply_virtual_signal(
            book,
            sig_long=long,
            sig_short=short,
            qty=0.1,
            px=px,
            atr=10.0,
            ms=ms,
            block=block,
            min_qty=0.001,
            max_layers=max_layers,
            meta={"reason": "测试有效信号", "symbol": "BTCUSDT"},
        )

    def test_same_direction_long_adds_to_three_layers_then_stops(self):
        book = empty_book()
        self.assertEqual(self._apply(book, long=True, px=100.0, ms=1)[0], "开多")
        action, note, changed = self._apply(book, long=True, px=105.0, ms=2)
        self.assertEqual(action, "加多")
        self.assertTrue(changed)
        self.assertIn("第2/3层", note)
        self.assertEqual(self._apply(book, long=True, px=110.0, ms=3)[0], "加多")

        entry = book["entry"]
        self.assertEqual(layer_count(entry), 3)
        self.assertEqual(len(entry_layers(entry)), 3)
        self.assertAlmostEqual(entry["qty"], 0.3)
        self.assertAlmostEqual(entry["px"], 105.0)
        self.assertAlmostEqual(book_signed_qty(book), 0.3)
        self.assertAlmostEqual(desired_net({"strategies": {"kdj15": book}}), 0.3)

        action, note, changed = self._apply(book, long=True, px=115.0, ms=4)
        self.assertIsNone(action)
        self.assertFalse(changed)
        self.assertIn("最大分层 3层", note)
        self.assertEqual(layer_count(book["entry"]), 3)
        self.assertAlmostEqual(book["entry"]["qty"], 0.3)

    def test_same_direction_short_adds(self):
        book = empty_book()
        self.assertEqual(self._apply(book, short=True, px=100.0, ms=1)[0], "开空")
        self.assertEqual(self._apply(book, short=True, px=95.0, ms=2)[0], "加空")
        self.assertEqual(layer_count(book["entry"]), 2)
        self.assertAlmostEqual(book["entry"]["qty"], 0.2)
        self.assertAlmostEqual(book["entry"]["px"], 97.5)
        self.assertAlmostEqual(book_signed_qty(book), -0.2)

    def test_valid_reverse_clears_all_accumulated_layers_then_restarts_at_one(self):
        book = empty_book()
        for i, px in enumerate((100.0, 105.0, 110.0), start=1):
            self._apply(book, long=True, px=px, ms=i)
        old_layers = entry_layers(book["entry"])
        self.assertEqual(len(old_layers), 3)

        action, note, changed = self._apply(book, short=True, px=90.0, ms=4)
        self.assertEqual(action, "清仓3层并开空")
        self.assertTrue(changed)
        self.assertIn("反手第1/3层", note)
        self.assertEqual(book["entry"]["side"], -1)
        self.assertEqual(layer_count(book["entry"]), 1)
        self.assertEqual(len(entry_layers(book["entry"])), 1)
        self.assertAlmostEqual(book["entry"]["qty"], 0.1)
        self.assertAlmostEqual(book["entry"]["px"], 90.0)
        self.assertAlmostEqual(book_signed_qty(book), -0.1)
        self.assertNotIn(100.0, [layer["px"] for layer in entry_layers(book["entry"])])

    def test_direction_conflict_is_no_signal_and_preserves_layers(self):
        book = empty_book()
        self._apply(book, long=True, px=100.0, ms=1)
        self._apply(book, long=True, px=105.0, ms=2)
        before = json.dumps(book["entry"], sort_keys=True)

        # 金叉遇MACD负柱 / 死叉遇MACD正柱在调用方归为 sig_long=sig_short=False。
        action, note, changed = self._apply(book, ms=3)
        self.assertIsNone(action)
        self.assertEqual(note, "观察")
        self.assertFalse(changed)
        self.assertEqual(json.dumps(book["entry"], sort_keys=True), before)

    def test_risk_gate_blocks_same_direction_add_but_valid_reverse_still_flattens(self):
        book = empty_book()
        self._apply(book, long=True, px=100.0, ms=1)
        action, note, changed = self._apply(book, long=True, px=105.0, ms=2, block=True)
        self.assertIsNone(action)
        self.assertFalse(changed)
        self.assertIn("不加仓", note)
        self.assertEqual(layer_count(book["entry"]), 1)

        action, note, changed = self._apply(book, short=True, px=95.0, ms=3, block=True)
        self.assertEqual(action, "清仓1层")
        self.assertTrue(changed)
        self.assertIn("阻止反手", note)
        self.assertIsNone(book["entry"])

    def test_legacy_single_entry_upgrades_without_losing_its_first_layer(self):
        book = {
            "entry": {
                "side": 1, "qty": 0.1, "px": 100.0, "atr": 10.0,
                "ms": 1, "reason": "旧账本入场", "symbol": "BTCUSDT",
            }
        }
        action, _note, changed = self._apply(book, long=True, px=110.0, ms=2)
        self.assertEqual(action, "加多")
        self.assertTrue(changed)
        self.assertEqual(layer_count(book["entry"]), 2)
        self.assertEqual(entry_layers(book["entry"])[0]["reason"], "旧账本入场")
        self.assertAlmostEqual(book["entry"]["qty"], 0.2)
        self.assertAlmostEqual(book["entry"]["px"], 105.0)

    def test_position_sources_exposes_layer_count_and_limit(self):
        book = empty_book()
        self._apply(book, long=True, px=100.0, ms=1)
        self._apply(book, long=True, px=110.0, ms=2)
        st = {"strategies": {"kdj15": book, "eth15": empty_book()}}
        sources = position_sources(st)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["layer_count"], 2)
        self.assertEqual(sources[0]["max_layers"], 3)

    def test_disabled_five_minute_spec_keeps_legacy_single_layer_behavior(self):
        book = empty_book()
        self._apply(book, long=True, px=100.0, ms=1, max_layers=SPEC_5M.max_layers)
        action, note, changed = self._apply(
            book, long=True, px=105.0, ms=2, max_layers=SPEC_5M.max_layers,
        )
        self.assertIsNone(action)
        self.assertFalse(changed)
        self.assertIn("最大分层 1层", note)
        self.assertEqual(layer_count(book["entry"]), 1)


class TestProcessStrategyPyramiding(unittest.TestCase):
    """确保运行器把 StrategySpec.max_layers 真正传入虚拟账本。"""

    def _bars(self):
        n = 8
        ts = np.arange(n, dtype=np.int64) * SPEC_15M.interval_ms + 1_800_000_000_000
        px = np.full(n, 86_000.0)
        return {
            "ts": ts, "open": px, "high": px + 10, "low": px - 10,
            "close": px, "volume": np.ones(n),
        }, np.full(n, 400.0)

    def _armed_long_book(self, bars):
        book = empty_book()
        book["armed"] = True
        book["last_ts"] = int(bars["ts"][-2])
        book["entry"] = {
            "side": 1, "qty": 0.1, "px": 86_000.0, "atr": 400.0,
            "ms": int(bars["ts"][-2]), "reason": "首层",
        }
        return book

    def test_valid_same_direction_signal_adds_a_second_layer(self):
        tmp = tempfile.TemporaryDirectory()
        old_reading, old_log = deploy.READING, deploy.TRADE_LOG
        deploy.READING = os.path.join(tmp.name, "reading.json")
        deploy.TRADE_LOG = os.path.join(tmp.name, "trades.csv")
        bars, atr = self._bars()
        n = len(bars["ts"])
        # 前根K<=D、当根K>D：金叉；MACD柱为正，因此是有效同向信号。
        k, d = np.full(n, 40.0), np.full(n, 45.0)
        k[-1], d[-1] = 50.0, 45.0
        try:
            with patch("shadow.deploy.kdj", return_value=(k, d, k)), \
                    patch("shadow.deploy.macd", return_value=(np.zeros(n), np.zeros(n), np.ones(n))):
                changed = deploy.process_strategy(
                    SPEC_15M, self._armed_long_book(bars), bars, atr,
                    equity=5_000.0, block=False,
                    now=int(bars["ts"][-1] + SPEC_15M.interval_ms), execute=False,
                )
            self.assertTrue(changed)
            # 从落盘日志核对动作，避免只测函数内部状态。
            with open(deploy.TRADE_LOG, encoding="utf-8") as fh:
                rows = fh.read().splitlines()
            self.assertIn("BTC 15m加多", rows[-1])
        finally:
            deploy.READING, deploy.TRADE_LOG = old_reading, old_log
            tmp.cleanup()

    def test_macd_direction_conflict_neither_adds_nor_closes_existing_long(self):
        tmp = tempfile.TemporaryDirectory()
        old_reading, old_log = deploy.READING, deploy.TRADE_LOG
        deploy.READING = os.path.join(tmp.name, "reading.json")
        deploy.TRADE_LOG = os.path.join(tmp.name, "trades.csv")
        bars, atr = self._bars()
        n = len(bars["ts"])
        # 死叉但MACD柱为正：背离，必须不平仓、不反手、不加仓。
        k, d = np.full(n, 70.0), np.full(n, 68.0)
        k[-1], d[-1] = 60.0, 62.0
        book = self._armed_long_book(bars)
        before = json.dumps(book["entry"], sort_keys=True)
        try:
            with patch("shadow.deploy.kdj", return_value=(k, d, k)), \
                    patch("shadow.deploy.macd", return_value=(np.zeros(n), np.zeros(n), np.ones(n))):
                changed = deploy.process_strategy(
                    SPEC_15M, book, bars, atr, equity=5_000.0, block=False,
                    now=int(bars["ts"][-1] + SPEC_15M.interval_ms), execute=False,
                )
            self.assertFalse(changed)
            self.assertEqual(json.dumps(book["entry"], sort_keys=True), before)
        finally:
            deploy.READING, deploy.TRADE_LOG = old_reading, old_log
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
