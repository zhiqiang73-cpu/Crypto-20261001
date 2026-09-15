"""Predict.fun / CVD / Fear&Greed / FRED / DefiLlama / renorm / agreement 单测."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.binance_ws import CVDTracker
from collectors.defi_llama import parse_usdt_net_change
from collectors.fear_greed import parse_fear_greed
from collectors.fred import cpi_yoy_from_levels, parse_fred_csv
from collectors.free_derivatives import ForceOrderBuffer
from collectors.predict_fun import (
    extract_up_prob,
    infer_window,
    is_btc_market,
    select_best_by_window,
)
from engine.scorer import FactorScoringEngine
from mappers.news_mapper import NewsFactorMapper
from mappers.prediction_mapper import PredictionFactorMapper
from models.signals import DimensionScores, StrategyHorizon
from models.snapshots import (
    FearGreedSnapshot,
    NewsSnapshot,
    PolymarketSnapshot,
    PredictFunSnapshot,
    PredictionSnapshot,
)
from utils.scoring import agreement_boost, compute_cs_with_boost, renormalized_weighted_sum


class TestPredictFunParsers(unittest.TestCase):
    def test_extract_up_prob_mid(self):
        m = {
            "outcomes": [
                {"name": "Up", "bestBid": 0.55, "bestAsk": 0.57},
                {"name": "Down", "bestBid": 0.43, "bestAsk": 0.45},
            ]
        }
        self.assertAlmostEqual(extract_up_prob(m), 0.56)

    def test_extract_up_prob_nested_price(self):
        m = {
            "outcomes": [
                {
                    "name": "Up",
                    "bestBid": {"price": 0.48, "size": 100},
                    "bestAsk": {"price": 0.52, "size": 80},
                }
            ]
        }
        self.assertAlmostEqual(extract_up_prob(m), 0.50)

    def test_infer_window(self):
        self.assertEqual(
            infer_window({"title": "BTC/USD Up or Down - 15 minutes"}),
            "15m",
        )
        self.assertEqual(
            infer_window({"categorySlug": "btc-usd-up-down-5-minutes"}),
            "5m",
        )
        self.assertEqual(
            infer_window({
                "title": "Bitcoin Up or Down - September 13, 11AM ET",
            }),
            "1h",
        )
        self.assertEqual(
            infer_window({
                "title": "Bitcoin Up or Down on September 13?",
                "categorySlug": "bitcoin-up-or-down-on-september-13-2026",
                "description": "Close price ... 12:00 in the ET timezone (noon) ...",
            }),
            "1d",
        )

    def test_select_best(self):
        markets = [
            {
                "id": 1,
                "title": "Bitcoin Up or Down - September 13, 11AM ET",
                "status": "PRICE_PROPOSED",
                "tradingStatus": "OPEN",
                "outcomes": [
                    {
                        "name": "Up",
                        "bestBid": {"price": 0.40, "size": 1},
                        "bestAsk": {"price": 0.44, "size": 1},
                    }
                ],
                "variantData": {"startPrice": "115000", "priceFeedProvider": "BINANCE"},
            },
            {
                "id": 2,
                "title": "Bitcoin Up or Down - September 13, 11AM ET",
                "status": "PRICE_PROPOSED",
                "tradingStatus": "OPEN",
                "outcomes": [
                    {
                        "name": "Up",
                        "bestBid": {"price": 0.98, "size": 1},
                        "bestAsk": {"price": 0.99, "size": 1},
                    }
                ],
            },
            {
                "id": 3,
                "title": "ETH Up or Down 15 minutes",
                "status": "OPEN",
                "outcomes": [{"name": "Up", "bestAsk": 0.70}],
            },
        ]
        by = select_best_by_window(markets)
        self.assertIn("1h", by)
        # 应选有信息量的 0.42, 而非近结算的 0.985
        self.assertAlmostEqual(by["1h"][1], 0.42)
        self.assertEqual(by["1h"][0]["id"], 1)
        self.assertTrue(is_btc_market(markets[0]))
        self.assertFalse(is_btc_market(markets[2]))


class TestCVD(unittest.TestCase):
    def test_buy_pressure(self):
        t = CVDTracker(window_sec=60)
        # buyer maker=False → taker buy → +
        t.on_trade(100.0, 10.0, is_buyer_maker=False, ts_ms=1_000)
        t.on_trade(100.0, 3.0, is_buyer_maker=True, ts_ms=2_000)
        self.assertAlmostEqual(t.cvd_usd(), 700.0)


class TestLiqSpeed(unittest.TestCase):
    def test_cleared_ratio(self):
        buf = ForceOrderBuffer(window_sec=300, peak_window_sec=1800)
        # 灌入大量多头强平
        for i in range(20):
            buf.add(100000, 1.0, "SELL", ts_ms=1_000_000 + i * 1000)
        long_c, short_c = buf.cleared_ratios()
        # 峰值刚形成, 清除率接近 0
        self.assertIsNotNone(long_c)
        self.assertLess(long_c, 0.2)


class TestFearGreedParse(unittest.TestCase):
    def test_parse(self):
        snap = parse_fear_greed(
            {"data": [{"value": "28", "value_classification": "Fear"}]}
        )
        self.assertIsNotNone(snap)
        self.assertEqual(snap.value, 28.0)


class TestFredParse(unittest.TestCase):
    def test_csv_and_yoy(self):
        csv_text = "DATE,CPIAUCSL\n"
        for i in range(14):
            csv_text += f"2024-{i+1:02d}-01,{100 + i}\n"
        # fix invalid months - use sequential months properly
        csv_text = "DATE,CPIAUCSL\n"
        levels = []
        for i in range(14):
            y = 2024 + (i // 12)
            m = (i % 12) + 1
            v = 100.0 + i
            csv_text += f"{y}-{m:02d}-01,{v}\n"
            levels.append((f"{y}-{m:02d}-01", v))
        rows = parse_fred_csv(csv_text)
        self.assertEqual(len(rows), 14)
        yoy, chg = cpi_yoy_from_levels(rows)
        self.assertIsNotNone(yoy)
        # (112-100)/100 = 0.12 for last that has +12
        self.assertAlmostEqual(yoy, 0.12, places=2)


class TestDefiLlamaParse(unittest.TestCase):
    def test_net_change(self):
        payload = {
            "circulating": {"peggedUSD": 120e9},
            "circulatingPrevDay": {"peggedUSD": 119e9},
            "tokensCirculating": [
                {"circulating": {"peggedUSD": 119e9}},
                {"circulating": {"peggedUSD": 120e9}},
            ],
        }
        net, mcap = parse_usdt_net_change(payload)
        self.assertAlmostEqual(net, 1e9)
        self.assertAlmostEqual(mcap, 120e9)


class TestRenormAndBoost(unittest.TestCase):
    def test_renorm(self):
        scores = {"a": 100.0, "b": 0.0, "c": 50.0}
        weights = {"a": 0.5, "b": 0.3, "c": 0.2}
        s = renormalized_weighted_sum(scores, weights, missing=["b"])
        # a=0.5/0.7*100 + c=0.2/0.7*50
        self.assertAlmostEqual(s, 0.5 / 0.7 * 100 + 0.2 / 0.7 * 50, places=4)

    def test_agreement_boost_all_same(self):
        faces = {"news": 50, "data": 40, "tech": 30, "prediction": 20}
        w = {"news": 0.15, "data": 0.45, "tech": 0.25, "prediction": 0.15}
        b = agreement_boost(faces, w)
        self.assertAlmostEqual(b, 1.3, places=4)

    def test_engine_boost(self):
        eng = FactorScoringEngine()
        r = eng.evaluate(
            StrategyHorizon.SHORT_TERM,
            DimensionScores(news=50, data=50, tech=50, prediction=50),
        )
        # linear CS=50; agreement_boost 默认关闭 → 50
        self.assertAlmostEqual(r.composite_score, 50.0, places=1)


class TestPredictionMapperV2(unittest.TestCase):
    def test_predict_fun_dominates(self):
        snap = PredictionSnapshot(
            predict_fun=PredictFunSnapshot(btc_up_prob=0.72, available=True),
            fear_greed=FearGreedSnapshot(value=30, available=True),
            polymarket=PolymarketSnapshot(btc_prob=0.4, available=True),
            max_pain_distance=-0.06,
            available=True,
        )
        r = PredictionFactorMapper().map(snap, StrategyHorizon.SHORT_TERM)
        self.assertIn("predict_fun_btc", r.sub_scores)
        self.assertGreater(r.sub_scores["predict_fun_btc"], 40)
        self.assertGreater(r.s_prediction, 20)

    def test_missing_renorm(self):
        snap = PredictionSnapshot(
            predict_fun=PredictFunSnapshot(btc_up_prob=0.7, available=True),
            available=True,
        )
        r = PredictionFactorMapper().map(snap, StrategyHorizon.SHORT_TERM)
        # 缺项被归一化, 不应被稀释到接近 0
        self.assertGreater(r.s_prediction, 40)
        self.assertIn("fear_greed", r.missing_fields)


class TestNewsMacro(unittest.TestCase):
    def test_macro_filled(self):
        snap = NewsSnapshot(
            etf_daily_net_usd=100_000_000,
            dxy_change_5d=0.0,
            regulation_score=0,
            months_since_halving=24,
            cpi_yoy_change=-0.01,
            yield_curve_10y2y=0.4,
            pmi=0.03,
            m2_yoy=0.04,
            usdt_net_mint_24h=200_000_000,
            available=True,
        )
        r = NewsFactorMapper().map(snap, StrategyHorizon.LONG_TERM)
        self.assertNotIn("macro_data", r.missing_fields)
        self.assertGreater(r.sub_scores["macro_data"], 0)

    def test_short_breaking_path(self):
        snap = NewsSnapshot(
            breaking_sentiment=-30,
            regulatory_event_score=-20,
            macro_surprise_score=10,
            black_swan_score=None,
            institutional_score=0,
            available=True,
        )
        r = NewsFactorMapper().map(snap, StrategyHorizon.SHORT_TERM)
        self.assertIn("breaking_crypto", r.sub_scores)
        self.assertLess(r.sub_scores["breaking_crypto"], 0)
        self.assertNotIn("etf_flows", r.sub_scores)



if __name__ == "__main__":
    unittest.main()
