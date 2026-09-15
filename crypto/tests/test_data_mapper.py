"""DataFactorMapper 单元测试 — 对照手册 V7 第五节锚点."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models.signals import StrategyHorizon
from models.snapshots import (
    BinanceMicroSnapshot,
    BlockTradeBias,
    CoinGlassSnapshot,
    DataSnapshot,
    SessionZone,
)
from mappers.data_mapper import DataFactorMapper
from utils.scoring import interpolate_anchors, annualize_funding_rate
from config.mapping import FUNDING_RATE_ANCHORS, BID_ASK_RATIO_ANCHORS


class TestFundingMapping(unittest.TestCase):
    def setUp(self):
        self.m = DataFactorMapper()

    def test_extreme_negative_funding(self):
        # 年化 -32% → 接近 +90 (空头极拥挤)
        snap = DataSnapshot(
            binance=BinanceMicroSnapshot(funding_rate_annualized=-0.32),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertAlmostEqual(r.indicator_scores["funding_rate"], 90.0, delta=1.0)

    def test_extreme_positive_funding(self):
        # 年化 +54% → 接近 -90
        snap = DataSnapshot(
            binance=BinanceMicroSnapshot(funding_rate_annualized=0.54),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertAlmostEqual(r.indicator_scores["funding_rate"], -90.0, delta=1.0)

    def test_annualize_8h_period(self):
        # 8h 费率 -0.0292% ≈ -0.000292 → 年化 ≈ -0.32
        period = -0.000292237
        annual = annualize_funding_rate(period, 8.0)
        self.assertAlmostEqual(annual, -0.32, delta=0.01)


class TestOrderbookMapping(unittest.TestCase):
    def setUp(self):
        self.m = DataFactorMapper()

    def test_bid_ask_1_8_interpolated(self):
        # 1.8 落在 1.5→+20 与 2.0→+60 之间 → 44
        expected = interpolate_anchors(1.8, BID_ASK_RATIO_ANCHORS)
        self.assertGreater(expected, 20.0)
        self.assertLess(expected, 60.0)
        self.assertAlmostEqual(expected, 44.0, delta=0.5)

        snap = DataSnapshot(
            binance=BinanceMicroSnapshot(bid_ask_ratio_2pct=1.8),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertAlmostEqual(
            r.indicator_scores["orderbook_depth"], expected, delta=0.5
        )


class TestOIMapping(unittest.TestCase):
    def setUp(self):
        self.m = DataFactorMapper()

    def test_steady_oi_growth_normal_funding_is_zero(self):
        # OI 24h +12%、费率正常 → OI 分 = 0
        snap = DataSnapshot(
            binance=BinanceMicroSnapshot(
                funding_rate_annualized=0.05,  # 5% 年化, 正常
                bid_ask_ratio_2pct=1.8,
            ),
            coinglass=CoinGlassSnapshot(
                oi_change_24h_pct=0.12,
                oi_change_5m_pct=0.01,
                available=True,
            ),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        # 0.01 落在 0→0 与 0.02→-10 之间 → -5
        self.assertAlmostEqual(r.indicator_scores["open_interest"], -5.0, delta=0.1)

    def test_oi_flush_with_negative_funding(self):
        # 5min OI 骤降 4% + 极端负费率 → +60
        snap = DataSnapshot(
            binance=BinanceMicroSnapshot(funding_rate_annualized=-0.32),
            coinglass=CoinGlassSnapshot(
                oi_change_5m_pct=-0.04,
                available=True,
            ),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        # -0.04 落在 -0.08→80 与 -0.03→60 之间 → 64; 骤降 × 负费率方向 → +64
        self.assertAlmostEqual(r.indicator_scores["open_interest"], 64.0, delta=0.1)


class TestMissingFields(unittest.TestCase):
    def setUp(self):
        self.m = DataFactorMapper()

    def test_missing_coinglass_scores_zero(self):
        snap = DataSnapshot(
            binance=BinanceMicroSnapshot(
                funding_rate_annualized=-0.32,
                bid_ask_ratio_2pct=2.0,
            ),
            coinglass=CoinGlassSnapshot(available=False),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertEqual(r.indicator_scores["liquidation_heatmap"], 0.0)
        self.assertEqual(r.indicator_scores["open_interest"], 0.0)
        self.assertEqual(r.indicator_scores["liquidations_realtime"], 0.0)
        self.assertIn("liquidation_heatmap", r.missing_fields)
        # S_data 仍可计算 (不完全为 0, 因为有 funding + orderbook)
        self.assertNotEqual(r.s_data, 0.0)
        self.assertGreater(r.s_data, 0.0)

    def test_empty_snapshot_neutral(self):
        r = self.m.map(DataSnapshot(), StrategyHorizon.SHORT_TERM)
        self.assertEqual(r.s_data, 0.0)


class TestHeatmapAndLiq(unittest.TestCase):
    def setUp(self):
        self.m = DataFactorMapper()

    def test_heatmap_above_magnet(self):
        snap = DataSnapshot(
            coinglass=CoinGlassSnapshot(heatmap_magnet=1.0, available=True),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertAlmostEqual(
            r.indicator_scores["liquidation_heatmap"], 70.0, delta=0.1
        )

    def test_long_liquidation_bounce(self):
        snap = DataSnapshot(
            coinglass=CoinGlassSnapshot(
                liq_long_5m_usd=80_000_000,
                liq_short_5m_usd=5_000_000,
                liq_total_5m_usd=85_000_000,
                available=True,
            ),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        # 80M 落在 50M→70 与 150M→90 之间 → 76
        self.assertAlmostEqual(
            r.indicator_scores["liquidations_realtime"], 76.0, delta=0.1
        )
        self.assertFalse(r.black_swan_liq)

    def test_black_swan_flag(self):
        snap = DataSnapshot(
            coinglass=CoinGlassSnapshot(
                liq_long_5m_usd=150_000_000,
                liq_short_5m_usd=100_000_000,
                liq_total_5m_usd=250_000_000,
                available=True,
            ),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertTrue(r.black_swan_liq)


class TestBlockTradesAndSession(unittest.TestCase):
    def setUp(self):
        self.m = DataFactorMapper()

    def test_accumulation_score(self):
        snap = DataSnapshot(
            binance=BinanceMicroSnapshot(
                block_trade_bias=BlockTradeBias.ACCUMULATION
            ),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertAlmostEqual(r.indicator_scores["block_trades"], 70.0, delta=0.1)

    def test_asia_session_discounts_directional(self):
        snap_us = DataSnapshot(
            binance=BinanceMicroSnapshot(funding_rate_annualized=-0.32),
            session=SessionZone.US,
        )
        snap_asia = DataSnapshot(
            binance=BinanceMicroSnapshot(funding_rate_annualized=-0.32),
            session=SessionZone.ASIA,
        )
        r_us = self.m.map(snap_us, StrategyHorizon.SHORT_TERM)
        r_asia = self.m.map(snap_asia, StrategyHorizon.SHORT_TERM)
        self.assertAlmostEqual(r_us.indicator_scores["funding_rate"], 90.0, delta=1)
        self.assertAlmostEqual(
            r_asia.indicator_scores["funding_rate"], 90.0 * 0.7, delta=1
        )
        self.assertEqual(r_asia.session_multiplier, 0.7)

    def test_spread_danger_flag(self):
        snap = DataSnapshot(
            binance=BinanceMicroSnapshot(spread_vs_mean=6.0),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertEqual(r.indicator_scores["spread"], 0.0)
        self.assertTrue(r.spread_danger)


class TestLayerAggregation(unittest.TestCase):
    def setUp(self):
        self.m = DataFactorMapper()

    def test_s_data_in_range(self):
        snap = DataSnapshot(
            binance=BinanceMicroSnapshot(
                funding_rate_annualized=-0.32,
                bid_ask_ratio_2pct=2.0,
                block_trade_bias=BlockTradeBias.ACCUMULATION,
            ),
            coinglass=CoinGlassSnapshot(
                heatmap_magnet=1.0,
                oi_change_5m_pct=-0.04,
                liq_long_5m_usd=60_000_000,
                liq_short_5m_usd=1_000_000,
                liq_total_5m_usd=61_000_000,
                long_short_ratio=0.4,
                available=True,
            ),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertGreaterEqual(r.s_data, -100)
        self.assertLessEqual(r.s_data, 100)
        self.assertGreater(r.s_data, 20)  # 多头共振应明显为正
        self.assertIn("derivatives", r.layer_scores)
        self.assertIn("microstructure", r.layer_scores)


class TestDeribitAndOnchain(unittest.TestCase):
    def setUp(self):
        self.m = DataFactorMapper()

    def test_max_pain_and_iv(self):
        from models.snapshots import DeribitSnapshot
        snap = DataSnapshot(
            deribit=DeribitSnapshot(
                max_pain_distance=-0.08,
                iv=0.35,
                available=True,
            ),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertAlmostEqual(r.indicator_scores["option_max_pain"], 40.0, delta=1)
        self.assertAlmostEqual(r.indicator_scores["implied_volatility"], 60.0, delta=1)
        self.assertNotIn("option_max_pain", r.missing_fields)

    def test_whale_and_hashrate(self):
        from models.snapshots import OnchainSnapshot
        snap = DataSnapshot(
            onchain=OnchainSnapshot(
                hashrate_ma30_change_pct=0.08,
                whale_net_flow_btc=-2000,
                whale_transfer_direction="from_exchange",
                available=True,
            ),
            session=SessionZone.US,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertAlmostEqual(r.indicator_scores["hashrate"], 30.0, delta=1)
        # -2000 BTC: 连续锚点 (-1000→70, -5000→90) → 75
        self.assertAlmostEqual(r.indicator_scores["whale_transfers"], 75.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
