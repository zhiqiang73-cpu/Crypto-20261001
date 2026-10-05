"""预测面 / 消息面 mapper 单测."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mappers.news_mapper import NewsFactorMapper, map_halving
from mappers.prediction_mapper import PredictionFactorMapper
from models.signals import StrategyHorizon
from models.snapshots import (
    NewsSnapshot,
    PolymarketSnapshot,
    FearGreedSnapshot,
    PredictFunSnapshot,
    PredictionSnapshot,
)


class TestPredictionMapper(unittest.TestCase):
    def test_bullish_prob(self):
        snap = PredictionSnapshot(
            predict_fun=PredictFunSnapshot(btc_up_prob=0.9, available=True),
            fear_greed=FearGreedSnapshot(value=25, available=True),
            polymarket=PolymarketSnapshot(
                btc_prob=0.9,
                btc_prob_change_1h=0.35,
                fed_cut_prob=0.9,
                fed_hike_prob=0.05,
                available=True,
            ),
            max_pain_distance=-0.08,
            available=True,
        )
        r = PredictionFactorMapper().map(snap, StrategyHorizon.SHORT_TERM)
        self.assertGreater(r.s_prediction, 40)
        # 月度 Polymarket 与 1h 策略期限不匹配，必须排除并明确标记。
        self.assertIn("polymarket_prob", r.missing_fields)

    def test_missing_all(self):
        r = PredictionFactorMapper().map(
            PredictionSnapshot(), StrategyHorizon.SHORT_TERM
        )
        self.assertEqual(r.s_prediction, 0.0)
        self.assertIn("polymarket_prob", r.missing_fields)


class TestNewsMapper(unittest.TestCase):
    def test_short_breaking(self):
        snap = NewsSnapshot(
            breaking_sentiment=40,
            regulatory_event_score=10,
            macro_surprise_score=0,
            black_swan_score=None,
            institutional_score=-2000,  # 流出交易所 → 看多
            event_severity="important",
            available=True,
        )
        r = NewsFactorMapper().map(snap, StrategyHorizon.SHORT_TERM)
        self.assertGreater(r.s_news, 10)
        self.assertIn("breaking_crypto", r.sub_scores)
        self.assertGreater(r.sub_scores["breaking_crypto"], 30)
        self.assertGreater(r.sub_scores["whale_institutional"], 50)

    def test_long_etf_halving(self):
        snap = NewsSnapshot(
            etf_daily_net_usd=250_000_000,
            dxy_change_5d=-0.02,
            months_since_halving=8,
            months_to_halving=20,
            regulation_score=0,
            institutional_score=-2000,
            usdt_net_mint_24h=300_000_000,
            m2_yoy=0.06,
            pmi=0.025,
            cpi_yoy_change=-0.003,
            available=True,
        )
        r = NewsFactorMapper().map(snap, StrategyHorizon.LONG_TERM)
        self.assertGreater(r.s_news, 10)
        self.assertGreater(r.sub_scores["etf_flows"], 50)
        self.assertGreater(r.sub_scores["halving"], 50)

    def test_halving_window(self):
        self.assertEqual(map_halving(8, 20), 90.0)
        self.assertEqual(map_halving(4, 20), 50.0)
        self.assertEqual(map_halving(24, 20), 0.0)


if __name__ == "__main__":
    unittest.main()
