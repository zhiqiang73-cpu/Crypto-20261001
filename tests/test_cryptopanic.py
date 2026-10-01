"""CryptoPanic 解析 / 时效衰减 / 源可信度单测."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.cryptopanic import (
    age_decay,
    aggregate_posts,
    parse_cryptopanic_payload,
    severity_multiplier,
    source_credibility,
    title_sentiment,
)


class TestCryptoPanicParsers(unittest.TestCase):
    def test_source_credibility_tiers(self):
        self.assertEqual(source_credibility("www.coindesk.com"), 1.0)
        self.assertEqual(source_credibility("cointelegraph.com"), 0.7)
        self.assertLess(source_credibility("random-blog.xyz"), 0.5)

    def test_age_decay(self):
        self.assertEqual(age_decay(0.5), 1.0)
        self.assertEqual(age_decay(2.0), 0.5)
        self.assertEqual(age_decay(8.0), 0.2)
        self.assertEqual(age_decay(24.0), 0.0)

    def test_severity(self):
        m, s = severity_multiplier("Bitcoin ETF approval expected", 0)
        self.assertEqual(s, "important")
        self.assertEqual(m, 1.5)
        m2, s2 = severity_multiplier("Exchange hack drains funds", 12)
        self.assertEqual(s2, "extreme")
        self.assertEqual(m2, 2.0)

    def test_title_sentiment(self):
        self.assertGreater(title_sentiment("SEC ETF approval clarity"), 0)
        self.assertLess(title_sentiment("Major exchange hack exploit"), 0)

    def test_aggregate_fresh_bullish(self):
        now = datetime.now(timezone.utc)
        posts = [
            {
                "title": "Bitcoin spot ETF approval sparks inflow",
                "published_at": (now - timedelta(minutes=20)).isoformat(),
                "source": {"domain": "coindesk.com"},
                "votes": {"positive": 10, "negative": 1, "important": 3},
            },
            {
                "title": "Bitcoin spot ETF approval sparks inflow",
                "published_at": (now - timedelta(minutes=25)).isoformat(),
                "source": {"domain": "reuters.com"},
                "votes": {"positive": 5, "negative": 0, "important": 2},
            },
        ]
        agg = aggregate_posts(posts, now=now)
        # ETF approval → regulatory 桶
        self.assertIsNotNone(agg["regulatory_event_score"])
        self.assertGreater(agg["regulatory_event_score"], 0)
        self.assertIn(agg["event_severity"], ("important", "extreme", "normal"))

    def test_stale_ignored(self):
        now = datetime.now(timezone.utc)
        posts = [
            {
                "title": "Exchange hack exploit drain",
                "published_at": (now - timedelta(hours=20)).isoformat(),
                "source": {"domain": "coindesk.com"},
                "votes": {"important": 20, "negative": 10, "positive": 0},
            }
        ]
        agg = aggregate_posts(posts, now=now)
        self.assertIsNone(agg["breaking_sentiment"])
        self.assertIsNone(agg["black_swan_score"])

    def test_parse_payload(self):
        payload = {"results": [{"title": "x"}]}
        self.assertEqual(len(parse_cryptopanic_payload(payload)), 1)


if __name__ == "__main__":
    unittest.main()
