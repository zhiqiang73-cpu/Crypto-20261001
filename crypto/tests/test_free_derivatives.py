"""免费衍生品解析器夹具测试."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.free_derivatives import (
    ForceOrderBuffer,
    heatmap_from_liquidations,
    parse_binance_lsr,
    parse_binance_oi_hist,
    parse_bybit_oi_btc,
    parse_force_order_event,
    parse_okx_oi_usd,
)


class TestOIParse(unittest.TestCase):
    def test_binance_oi_hist(self):
        now = 1_700_000_000_000
        rows = [
            {"timestamp": now - 86400000, "sumOpenInterestValue": "10000000000"},
            {"timestamp": now - 300000, "sumOpenInterestValue": "10500000000"},
            {"timestamp": now, "sumOpenInterestValue": "11200000000"},
        ]
        oi, ch5, ch24 = parse_binance_oi_hist(rows)
        self.assertAlmostEqual(oi, 11.2e9)
        self.assertAlmostEqual(ch24, 0.12, delta=0.01)
        self.assertAlmostEqual(ch5, 0.0667, delta=0.01)

    def test_bybit_okx(self):
        bybit = {"result": {"list": [{"openInterest": "50000"}]}}
        okx = {"data": [{"oiUsd": "2000000000"}]}
        self.assertEqual(parse_bybit_oi_btc(bybit), 50000.0)
        self.assertEqual(parse_okx_oi_usd(okx), 2e9)


class TestLSR(unittest.TestCase):
    def test_lsr(self):
        rows = [{"longShortRatio": "1.67", "longAccount": "0.62", "shortAccount": "0.38"}]
        self.assertAlmostEqual(parse_binance_lsr(rows), 1.67)


class TestForceOrder(unittest.TestCase):
    def test_parse_and_buffer(self):
        data = {
            "e": "forceOrder",
            "E": 1_700_000_000_000,
            "o": {"s": "BTCUSDT", "S": "SELL", "p": "67000", "q": "1.5", "T": 1_700_000_000_000},
        }
        parsed = parse_force_order_event(data)
        self.assertIsNotNone(parsed)
        buf = ForceOrderBuffer(300)
        buf.add(parsed["price"], parsed["qty"], parsed["side"], parsed["ts_ms"])
        long_u, short_u, total = buf.totals()
        self.assertAlmostEqual(long_u, 67000 * 1.5)
        self.assertEqual(short_u, 0.0)
        self.assertGreater(total, 0)

    def test_heatmap_magnet(self):
        # 下方多头爆仓密集 → 负磁铁
        events = [
            (98000.0, 1e6, True),
            (97500.0, 2e6, True),
            (102000.0, 1e5, False),
        ]
        above, below, magnet = heatmap_from_liquidations(events, mark_price=100000.0)
        self.assertIsNotNone(magnet)
        self.assertLess(magnet, 0)


if __name__ == "__main__":
    unittest.main()
