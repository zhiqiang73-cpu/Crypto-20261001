"""CoinGlass 解析器夹具测试 — 不打真实网络."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.coinglass import (
    CoinGlassCollector,
    build_coinglass_snapshot,
    is_black_swan_liquidation,
    parse_liquidation_heatmap,
    parse_liquidation_history,
    parse_long_short_ratio,
    parse_oi_history,
)
from models.snapshots import CoinGlassSnapshot


class TestOIHistoryParse(unittest.TestCase):
    def test_list_format_with_timestamps(self):
        now = 1_700_000_000_000
        payload = {
            "code": "0",
            "data": [
                {"t": now - 24 * 3600 * 1000, "oi": 10_000_000_000},
                {"t": now - 5 * 60 * 1000, "oi": 10_500_000_000},
                {"t": now, "oi": 11_200_000_000},
            ],
        }
        oi, ch5, ch24 = parse_oi_history(payload)
        self.assertAlmostEqual(oi, 11_200_000_000)
        self.assertIsNotNone(ch5)
        self.assertIsNotNone(ch24)
        # 24h: (11.2 - 10) / 10 = 12%
        self.assertAlmostEqual(ch24, 0.12, delta=0.01)
        # 5m: (11.2 - 10.5) / 10.5 ≈ 6.67%
        self.assertAlmostEqual(ch5, 0.0667, delta=0.01)

    def test_time_list_format(self):
        payload = {
            "data": {
                "time_list": [1000, 2000, 3000],
                "open_interest_list": [100, 110, 120],
            }
        }
        oi, ch5, ch24 = parse_oi_history(payload)
        self.assertEqual(oi, 120)
        self.assertIsNotNone(ch24)


class TestHeatmapParse(unittest.TestCase):
    def test_above_magnet(self):
        # mark=100, 上方 1~3% = 101~103 有大量清算, 下方很少
        payload = {
            "data": {
                "y_axis": [97.0, 98.0, 102.0, 103.0],
                "liquidation_leverage_data": [
                    [0, 0, 100],   # 下方 3%
                    [0, 1, 50],    # 下方 2%
                    [0, 2, 5000],  # 上方 2%
                    [0, 3, 4000],  # 上方 3%
                ],
            }
        }
        above, below, magnet = parse_liquidation_heatmap(payload, mark_price=100.0)
        self.assertIsNotNone(magnet)
        self.assertGreater(magnet, 0.5)
        self.assertGreater(above, below)

    def test_below_magnet(self):
        payload = {
            "data": {
                "y_axis": [97.0, 98.0, 102.0, 103.0],
                "liquidation_leverage_data": [
                    [0, 0, 8000],
                    [0, 1, 7000],
                    [0, 2, 100],
                    [0, 3, 50],
                ],
            }
        }
        above, below, magnet = parse_liquidation_heatmap(payload, mark_price=100.0)
        self.assertLess(magnet, -0.5)


class TestLiquidationHistory(unittest.TestCase):
    def test_list_last_bar(self):
        payload = {
            "data": [
                {"long_liquidation_usd": 1e6, "short_liquidation_usd": 2e6},
                {"long_liquidation_usd": 60e6, "short_liquidation_usd": 5e6},
            ]
        }
        long_v, short_v, total = parse_liquidation_history(payload)
        self.assertAlmostEqual(long_v, 60e6)
        self.assertAlmostEqual(short_v, 5e6)
        self.assertAlmostEqual(total, 65e6)

    def test_dict_format(self):
        payload = {
            "data": {
                "long_liquidation_usd": 10e6,
                "short_liquidation_usd": 20e6,
            }
        }
        long_v, short_v, total = parse_liquidation_history(payload)
        self.assertAlmostEqual(total, 30e6)


class TestLongShortRatio(unittest.TestCase):
    def test_ratio_field(self):
        payload = {"data": [{"long_short_ratio": 1.25}]}
        self.assertAlmostEqual(parse_long_short_ratio(payload), 1.25)

    def test_account_fields(self):
        payload = {"data": {"longAccount": 0.6, "shortAccount": 0.4}}
        self.assertAlmostEqual(parse_long_short_ratio(payload), 1.5)


class TestBuildSnapshot(unittest.TestCase):
    def test_assemble(self):
        now = 1_700_000_000_000
        oi_payload = {
            "data": [
                {"t": now - 86400000, "oi": 10e9},
                {"t": now, "oi": 11.2e9},
            ]
        }
        heatmap = {
            "data": {
                "y_axis": [66000, 68000],
                "liquidation_leverage_data": [[0, 1, 9000], [0, 0, 100]],
            }
        }
        liq = {
            "data": [
                {"long_liquidation_usd": 55e6, "short_liquidation_usd": 2e6}
            ]
        }
        lsr = {"data": [{"long_short_ratio": 0.8}]}
        snap = build_coinglass_snapshot(
            oi_payload=oi_payload,
            heatmap_payload=heatmap,
            liq_payload=liq,
            lsr_payload=lsr,
            mark_price=67000.0,
        )
        self.assertTrue(snap.available)
        self.assertIsNotNone(snap.open_interest_usd)
        self.assertIsNotNone(snap.heatmap_magnet)
        self.assertAlmostEqual(snap.liq_long_5m_usd, 55e6)
        self.assertAlmostEqual(snap.long_short_ratio, 0.8)


class TestCollectorNoKey(unittest.TestCase):
    def test_no_key_returns_empty(self):
        import asyncio

        col = CoinGlassCollector(api_key="")
        self.assertFalse(col.has_api_key)

        async def _run():
            snap = await col.fetch_once(mark_price=67000)
            self.assertFalse(snap.available)
            self.assertIsNone(snap.open_interest_usd)

        asyncio.run(_run())

    def test_black_swan_helper(self):
        snap = CoinGlassSnapshot(liq_total_5m_usd=250_000_000)
        self.assertTrue(is_black_swan_liquidation(snap))
        snap2 = CoinGlassSnapshot(liq_total_5m_usd=50_000_000)
        self.assertFalse(is_black_swan_liquidation(snap2))


if __name__ == "__main__":
    unittest.main()
