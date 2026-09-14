"""技术面指标与 TechFactorMapper 夹具测试."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from indicators.ohlcv import Candle, ema, sma
from indicators.classic import rsi, atr, bollinger
from indicators.structure import StructureBias, detect_structure, structure_score
from indicators.profile_patterns import is_pin_bar, volume_score
from indicators.engine import extract_tech_features, ema_stack_score, macd_hist_score
from mappers.tech_mapper import TechFactorMapper
from models.signals import StrategyHorizon
from models.snapshots import TechSnapshot
from collectors.binance_klines import parse_klines_payload


def _candle(o, h, l, c, v=100.0, t=0):
    return Candle(open_time_ms=t, open=o, high=h, low=l, close=c, volume=v, close_time_ms=t)


def _uptrend_series(n=80, start=100.0):
    candles = []
    price = start
    for i in range(n):
        o = price
        c = price + 1.0 + (i % 3) * 0.2
        h = c + 0.5
        l = o - 0.3
        candles.append(_candle(o, h, l, c, 1000 + i * 10, t=i * 60_000))
        price = c
    return candles


def _downtrend_series(n=80, start=200.0):
    candles = []
    price = start
    for i in range(n):
        o = price
        c = price - 1.0 - (i % 3) * 0.2
        h = o + 0.3
        l = c - 0.5
        candles.append(_candle(o, h, l, c, 1000 + i * 10, t=i * 60_000))
        price = c
    return candles


class TestClassicIndicators(unittest.TestCase):
    def test_ema_rises_on_uptrend(self):
        vals = [float(i) for i in range(1, 60)]
        e = ema(vals, 10)
        self.assertIsNotNone(e[-1])
        self.assertGreater(e[-1], e[20])

    def test_rsi_bounds(self):
        vals = [100.0 + i * 0.5 for i in range(40)]
        r = rsi(vals, 14)
        self.assertIsNotNone(r[-1])
        self.assertGreaterEqual(r[-1], 0)
        self.assertLessEqual(r[-1], 100)

    def test_bollinger_bandwidth_positive(self):
        vals = [100.0 + ((-1) ** i) * (i % 5) for i in range(40)]
        _m, _u, _l, bw = bollinger(vals, 20)
        self.assertIsNotNone(bw[-1])
        self.assertGreater(bw[-1], 0)


class TestStructure(unittest.TestCase):
    def test_uptrend_structure_score(self):
        candles = _uptrend_series(100)
        score = structure_score(candles)
        # 强上行应偏多
        self.assertGreaterEqual(score, 0)

    def test_downtrend_structure_score(self):
        candles = _downtrend_series(100)
        score = structure_score(candles)
        self.assertLessEqual(score, 0)


class TestPatterns(unittest.TestCase):
    def test_bullish_pin_bar(self):
        c = _candle(100, 101, 90, 100.5)
        self.assertTrue(is_pin_bar(c, bullish=True))

    def test_volume_score_breakout(self):
        candles = _uptrend_series(30)
        # 最后一根巨量上涨
        last = candles[-1]
        candles[-1] = _candle(last.open, last.high + 1, last.low, last.close + 2, v=50_000, t=last.open_time_ms)
        score = volume_score(candles, ma_period=20)
        self.assertEqual(score, 60.0)


class TestKlinesParse(unittest.TestCase):
    def test_parse_rows(self):
        rows = [
            [1000, "100", "110", "90", "105", "12.5", 1999],
            [2000, "105", "120", "100", "115", "20", 2999],
        ]
        candles = parse_klines_payload(rows)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[1].close, 115.0)


class TestEmaStackAndMacd(unittest.TestCase):
    def test_bull_align(self):
        self.assertEqual(ema_stack_score(110, 108, 105, 100), 70.0)

    def test_bear_align(self):
        self.assertEqual(ema_stack_score(90, 92, 95, 100), -70.0)

    def test_macd_cross_up(self):
        hist = [None, -1.0, -0.5, 0.2, 0.5]
        self.assertEqual(macd_hist_score(hist), 40.0)


class TestTechMapper(unittest.TestCase):
    def setUp(self):
        self.m = TechFactorMapper()

    def test_unavailable(self):
        r = self.m.map(TechSnapshot(available=False))
        self.assertEqual(r.s_tech, 0.0)

    def test_bullish_bundle(self):
        snap = TechSnapshot(
            available=True,
            price=100,
            structure_score=80,
            ema_score=70,
            vwap_score=60,
            volume_score=60,
            pin_bar_score=70,
            rsi_divergence_score=70,
            adx=45,
            boll_squeeze=True,
            atr_pct=0.012,
        )
        r = self.m.map(snap, StrategyHorizon.SHORT_TERM)
        self.assertGreater(r.s_tech, 30)
        self.assertEqual(r.adx_multiplier, 1.2)
        self.assertEqual(r.boll_multiplier, 1.15)
        self.assertGreaterEqual(r.s_tech, -100)
        self.assertLessEqual(r.s_tech, 100)

    def test_extract_on_synthetic(self):
        candles = _uptrend_series(220)
        daily = _uptrend_series(10, start=80)
        snap = extract_tech_features(candles, daily)
        self.assertTrue(snap.available)
        self.assertIsNotNone(snap.ema21)
        r = self.m.map(snap)
        self.assertGreaterEqual(r.s_tech, -100)
        self.assertLessEqual(r.s_tech, 100)


if __name__ == "__main__":
    unittest.main()
