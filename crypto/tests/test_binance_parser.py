"""Binance 解析器 / 本地订单簿 / 大单过滤 — 纯夹具测试, 不连真实 WS."""

import sys
import os
import unittest
from collections import deque

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.binance_ws import (
    BlockTradeTracker,
    LocalOrderBook,
    BinanceFuturesCollector,
    compute_spread_metrics,
    parse_agg_trade_event,
    parse_mark_price_event,
)
from config.mapping import BLOCK_TRADE_NOTIONAL_USD
from models.snapshots import BlockTradeBias
from utils.scoring import annualize_funding_rate


class TestMarkPriceParse(unittest.TestCase):
    def test_parse_funding_annualized(self):
        # 8h 费率使得年化约 -32%
        # annual = period * 3 * 365 = -0.32 → period = -0.32 / 1095 ≈ -0.0002922
        period = -0.32 / (3 * 365)
        data = {
            "e": "markPriceUpdate",
            "E": 1_700_000_000_000,
            "s": "BTCUSDT",
            "p": "67000.10",
            "i": "66990.00",
            "r": str(period),
            "T": 1_700_000_000_000 + 8 * 3_600_000,
        }
        parsed = parse_mark_price_event(data)
        self.assertAlmostEqual(parsed["mark_price"], 67000.10, places=2)
        self.assertAlmostEqual(parsed["funding_rate_annualized"], -0.32, delta=0.001)
        self.assertEqual(parsed["funding_period_hours"], 8.0)


class TestAggTradeFilter(unittest.TestCase):
    def test_small_trade_filtered(self):
        data = {
            "e": "aggTrade",
            "p": "67000",
            "q": "0.1",  # notional = 6700 < 500k
            "m": False,
            "T": 1_700_000_000_000,
        }
        self.assertIsNone(parse_agg_trade_event(data))

    def test_block_trade_kept(self):
        qty = (BLOCK_TRADE_NOTIONAL_USD + 1000) / 67000
        data = {
            "e": "aggTrade",
            "p": "67000",
            "q": str(qty),
            "m": False,  # 主动买
            "T": 1_700_000_000_000,
        }
        parsed = parse_agg_trade_event(data)
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["is_buy"])
        self.assertGreaterEqual(parsed["notional"], BLOCK_TRADE_NOTIONAL_USD)


class TestLocalOrderBook(unittest.TestCase):
    def _make_snapshot(self, mid=67000.0):
        # 构造覆盖 ±2% 的 bids/asks
        bids = []
        asks = []
        for i in range(50):
            bp = mid - (i + 1) * 30  # 约覆盖 1500 = 2.2%
            ap = mid + (i + 1) * 30
            bids.append([str(bp), "1.0"])
            asks.append([str(ap), "1.0"])
        return {
            "lastUpdateId": 1000,
            "bids": bids,
            "asks": asks,
        }

    def test_bid_ask_ratio_within_2pct(self):
        book = LocalOrderBook()
        book.apply_snapshot(self._make_snapshot())
        # 手动标记 synced (跳过 WS sync 流程)
        book.synced = True
        book._buffering = False
        ratio = book.bid_ask_ratio()
        self.assertIsNotNone(ratio)
        # 对称簿 → 比率接近 1.0
        self.assertAlmostEqual(ratio, 1.0, delta=0.15)

    def test_imbalanced_book(self):
        book = LocalOrderBook()
        mid = 67000.0
        bids = [[str(mid - (i + 1) * 20), "5.0"] for i in range(80)]
        asks = [[str(mid + (i + 1) * 20), "1.0"] for i in range(80)]
        book.apply_snapshot({
            "lastUpdateId": 100,
            "bids": bids,
            "asks": asks,
        })
        book.synced = True
        ratio = book.bid_ask_ratio()
        self.assertIsNotNone(ratio)
        self.assertGreater(ratio, 2.0)

    def test_pu_gap_breaks_sync(self):
        book = LocalOrderBook()
        book.apply_snapshot({
            "lastUpdateId": 100,
            "bids": [["67000", "1"]],
            "asks": [["67001", "1"]],
        })
        # 第一个事件满足 U<=100<=u
        ev1 = {"U": 99, "u": 105, "pu": 98, "b": [["67000", "2"]], "a": []}
        book.buffer_event(ev1)
        self.assertTrue(book.try_sync_from_buffer())
        self.assertTrue(book.synced)
        # 断裂: pu != last_update_id
        ev2 = {"U": 200, "u": 210, "pu": 150, "b": [], "a": [["67001", "0"]]}
        ok = book.on_depth_event(ev2)
        self.assertFalse(ok)
        self.assertFalse(book.synced)


class TestBlockTradeTracker(unittest.TestCase):
    def test_accumulation_bias(self):
        tracker = BlockTradeTracker(notional_threshold=500_000, window_sec=120)
        base_ts = 1_700_000_000_000
        price = 67000.0
        qty = 10.0  # notional = 670k
        # 连续大买, 价格不跌
        for i in range(5):
            tracker.on_agg_trade(price, qty, is_buyer_maker=False, ts_ms=base_ts + i * 1000)
        bias, buy_n, sell_n, count = tracker.summary(current_price=price)
        self.assertEqual(bias, BlockTradeBias.ACCUMULATION)
        self.assertGreater(buy_n, sell_n)
        self.assertEqual(count, 5)

    def test_distribution_bias(self):
        tracker = BlockTradeTracker(notional_threshold=500_000, window_sec=120)
        base_ts = 1_700_000_000_000
        price = 67000.0
        qty = 10.0
        for i in range(5):
            tracker.on_agg_trade(price, qty, is_buyer_maker=True, ts_ms=base_ts + i * 1000)
        bias, buy_n, sell_n, count = tracker.summary(current_price=price * 0.995)
        self.assertEqual(bias, BlockTradeBias.DISTRIBUTION)


class TestSpreadMetrics(unittest.TestCase):
    def test_spread_vs_mean(self):
        hist = deque(maxlen=60)
        for _ in range(10):
            compute_spread_metrics(100.0, 100.1, hist)
        spread, vs = compute_spread_metrics(100.0, 100.5, hist)
        self.assertAlmostEqual(spread, 0.5, places=5)
        self.assertGreater(vs, 3.0)


class TestCollectorHandleMessage(unittest.TestCase):
    def test_combined_stream_mark_price(self):
        col = BinanceFuturesCollector()
        period = -0.32 / 1095
        col.handle_message({
            "stream": "btcusdt@markPrice@1s",
            "data": {
                "e": "markPriceUpdate",
                "E": 1_700_000_000_000,
                "s": "BTCUSDT",
                "p": "68000",
                "r": str(period),
                "T": 1_700_028_800_000,
            },
        })
        snap = col.get_snapshot()
        self.assertAlmostEqual(snap.mark_price, 68000.0)
        self.assertAlmostEqual(snap.funding_rate_annualized, -0.32, delta=0.01)


if __name__ == "__main__":
    unittest.main()
