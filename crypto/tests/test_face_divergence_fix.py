"""四面偏差修复单测: Polymarket 相对映射 / 置信度 / 黑天鹅 / 阻尼."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.cryptopanic import aggregate_posts
from collectors.polymarket import select_btc_price_market
from utils.scoring import (
    apply_consistency_damping,
    available_weight_ratio,
    confidence_weighted_cs,
    estimate_neutral_prob,
    map_polymarket_relative,
    shrink_by_confidence,
)


class TestPolymarketRelative(unittest.TestCase):
    def test_near_atm_preferred_over_far(self):
        """现价 $77620 时优先选 $78k (近 ATM) 而非 $80k."""
        markets = [
            {
                "slug": "bitcoin-above-80k",
                "question": "Will Bitcoin be above $80,000 on September 16?",
                "active": True,
                "closed": False,
                "volume": 1000,
                "outcomePrices": ["0.14", "0.86"],
                "outcomes": ["Yes", "No"],
            },
            {
                "slug": "bitcoin-above-78k",
                "question": "Will Bitcoin be above $78,000 on September 16?",
                "active": True,
                "closed": False,
                "volume": 500,
                "outcomePrices": ["0.41", "0.59"],
                "outcomes": ["Yes", "No"],
            },
        ]
        picked = select_btc_price_market(markets, mark_price=77620)
        self.assertIsNotNone(picked)
        self.assertIn("78k", picked["slug"])

    def test_relative_mapping_not_extreme(self):
        """3% 上方阈值 + 14% 概率 ≈ 公平中性, 不应出 -80."""
        # old absolute: 0.14 → ~-80
        score = map_polymarket_relative(0.14, threshold=80000, mark_price=77620)
        self.assertGreater(score, -40)
        self.assertLess(score, 20)

    def test_relative_above_fair_is_bullish(self):
        # fair for ~0.5% above ≈ 0.40; actual 0.60 → bullish
        score = map_polymarket_relative(0.60, threshold=78000, mark_price=77620)
        self.assertGreater(score, 10)

    def test_neutral_prob_increases_below_mark(self):
        self.assertGreater(estimate_neutral_prob(-0.01), 0.5)
        self.assertLess(estimate_neutral_prob(0.03), 0.25)


class TestConfidence(unittest.TestCase):
    def test_available_ratio(self):
        w = {"a": 0.45, "b": 0.20, "c": 0.35}
        self.assertAlmostEqual(available_weight_ratio(w, ["a"]), 0.55)
        self.assertAlmostEqual(available_weight_ratio(w, []), 1.0)

    def test_shrink(self):
        # 0.55 → √0.55 ≈ 0.742
        self.assertAlmostEqual(shrink_by_confidence(-41.5, 0.55), -41.5 * (0.55 ** 0.5), places=2)

    def test_confidence_weighted_cs(self):
        faces = {"news": 14.0, "data": -5.0, "tech": 2.0, "prediction": -41.0}
        w = {"news": 0.15, "data": 0.45, "tech": 0.25, "prediction": 0.15}
        conf = {"news": 1.0, "data": 0.6, "tech": 0.9, "prediction": 0.55}
        cs_conf, _ = confidence_weighted_cs(faces, w, conf)
        cs_plain, _ = confidence_weighted_cs(faces, w, None)
        # 低置信的 prediction 拉偏应被削弱 → |cs_conf| < |cs_plain| 在 prediction 主导时
        # 至少不应比 plain 更偏空
        self.assertGreater(cs_conf, cs_plain - 1e-6)


class TestBlackSwanV2(unittest.TestCase):
    def test_single_extreme_not_enough(self):
        now = datetime.now(timezone.utc)
        posts = [
            {
                "title": "Exchange hack drains funds",
                "published_at": (now - timedelta(minutes=10)).isoformat(),
                "source": {"domain": "coindesk.com"},
                "votes": {"important": 12, "negative": 8, "positive": 0},
            }
        ]
        agg = aggregate_posts(posts, now=now, min_extreme_posts=3)
        self.assertIsNone(agg["black_swan_score"])
        self.assertNotEqual(agg["event_severity"], "extreme")

    def test_three_extreme_activates(self):
        now = datetime.now(timezone.utc)
        posts = [
            {
                "title": f"Exchange hack drains vault {i}",
                "published_at": (now - timedelta(minutes=10 + i)).isoformat(),
                "source": {"domain": "coindesk.com"},
                "votes": {"important": 12, "negative": 8, "positive": 0},
            }
            for i in range(3)
        ]
        agg = aggregate_posts(posts, now=now, min_extreme_posts=3)
        self.assertIsNotNone(agg["black_swan_score"])
        self.assertEqual(agg["event_severity"], "extreme")
        self.assertLess(agg["black_swan_score"], 0)

    def test_ema_smoothes(self):
        now = datetime.now(timezone.utc)
        posts = [
            {
                "title": f"Major exchange hack exploit drain {i}",
                "published_at": (now - timedelta(minutes=5 + i)).isoformat(),
                "source": {"domain": "reuters.com"},
                "votes": {"important": 15, "negative": 10, "positive": 0},
            }
            for i in range(3)
        ]
        raw = aggregate_posts(posts, now=now, min_extreme_posts=3)
        smoothed = aggregate_posts(
            posts, now=now, prev_black_swan=0.0, black_swan_ema_alpha=0.35,
            min_extreme_posts=3,
        )
        self.assertIsNotNone(raw["black_swan_score"])
        self.assertIsNotNone(smoothed["black_swan_score"])
        # EMA 拉向 0 → |smoothed| < |raw|
        self.assertLess(abs(smoothed["black_swan_score"]), abs(raw["black_swan_score"]))


class TestConsistencyDamping(unittest.TestCase):
    def test_outlier_pulled(self):
        faces = {"news": 14.0, "data": -5.0, "tech": 2.0, "prediction": -41.0}
        conf = {"news": 0.8, "data": 0.6, "tech": 0.9, "prediction": 0.4}
        out = apply_consistency_damping(faces, conf, spread_trigger=40.0, outlier_gap=20.0)
        # prediction 离群应被拉向中位数
        self.assertGreater(out["prediction"], faces["prediction"])
        self.assertLess(abs(out["prediction"]), abs(faces["prediction"]))

    def test_no_damp_when_tight(self):
        faces = {"news": 5.0, "data": -3.0, "tech": 2.0, "prediction": -4.0}
        out = apply_consistency_damping(faces, spread_trigger=50.0)
        self.assertEqual(out, faces)


if __name__ == "__main__":
    unittest.main()
