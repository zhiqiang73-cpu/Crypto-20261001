"""Polymarket 解析 / 市场匹配单测 — 不打真实网络."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.polymarket import (
    extract_btc_threshold,
    extract_yes_prob,
    infer_fed_cut_hike,
    select_btc_price_market,
    select_fed_market,
)


class TestPolymarketParsers(unittest.TestCase):
    def test_extract_yes_prob_list(self):
        m = {"outcomePrices": ["0.62", "0.38"], "outcomes": ["Yes", "No"]}
        self.assertAlmostEqual(extract_yes_prob(m), 0.62)

    def test_extract_yes_prob_json_string(self):
        m = {"outcomePrices": '["0.4","0.6"]', "outcomes": '["Yes","No"]'}
        self.assertAlmostEqual(extract_yes_prob(m), 0.4)

    def test_btc_threshold(self):
        self.assertEqual(extract_btc_threshold("Will Bitcoin reach $100,000?"), 100000.0)
        self.assertIsNone(extract_btc_threshold("Will BTC ETF be approved?"))

    def test_select_btc_closest_to_mark(self):
        markets = [
            {
                "slug": "btc-price-80000",
                "question": "Will Bitcoin be above $80,000?",
                "active": True,
                "closed": False,
                "volume": 100,
                "outcomePrices": ["0.9", "0.1"],
                "outcomes": ["Yes", "No"],
            },
            {
                "slug": "btc-price-100000",
                "question": "Will Bitcoin be above $100,000?",
                "active": True,
                "closed": False,
                "volume": 200,
                "outcomePrices": ["0.5", "0.5"],
                "outcomes": ["Yes", "No"],
            },
        ]
        picked = select_btc_price_market(markets, mark_price=95000)
        self.assertIsNotNone(picked)
        self.assertIn("100000", picked["slug"])

    def test_select_btc_no_match(self):
        markets = [
            {
                "slug": "fed-rate-cut",
                "question": "Will Fed cut rates?",
                "active": True,
                "closed": False,
            }
        ]
        self.assertIsNone(select_btc_price_market(markets, mark_price=90000))

    def test_select_btc_rejects_between(self):
        markets = [
            {
                "slug": "btc-between",
                "question": "Will the price of Bitcoin be between $76,000 and $78,000?",
                "active": True,
                "closed": False,
                "volume": 99999,
                "outcomePrices": ["0.96", "0.04"],
                "outcomes": ["Yes", "No"],
            },
            {
                "slug": "btc-above-80k",
                "question": "Will Bitcoin be above $80,000 on September 30?",
                "active": True,
                "closed": False,
                "volume": 100,
                "outcomePrices": ["0.55", "0.45"],
                "outcomes": ["Yes", "No"],
            },
        ]
        picked = select_btc_price_market(markets, mark_price=76700)
        self.assertIsNotNone(picked)
        self.assertIn("80k", picked["slug"])

    def test_select_fed_skips_near_settled(self):
        markets = [
            {
                "slug": "fed-cut-sept-dead",
                "question": "Fed rate cut by September 2026 meeting?",
                "active": True,
                "closed": False,
                "volume": 500000,
                "outcomePrices": ["0.004", "0.996"],
                "outcomes": ["Yes", "No"],
            },
            {
                "slug": "fed-cut-dec",
                "question": "Fed rate cut by December 2026 meeting?",
                "active": True,
                "closed": False,
                "volume": 100000,
                "outcomePrices": ["0.25", "0.75"],
                "outcomes": ["Yes", "No"],
            },
        ]
        m = select_fed_market(markets)
        self.assertEqual(m["slug"], "fed-cut-dec")
        cut, hike = infer_fed_cut_hike(m)
        self.assertAlmostEqual(cut, 0.25)
        self.assertIsNone(hike)

    def test_select_fed(self):
        markets = [
            {
                "slug": "fed-cut-march",
                "question": "Will the Fed cut rates in March?",
                "active": True,
                "closed": False,
                "volume": 500,
                "outcomePrices": ["0.7", "0.3"],
                "outcomes": ["Yes", "No"],
            }
        ]
        m = select_fed_market(markets)
        self.assertIsNotNone(m)
        cut, hike = infer_fed_cut_hike(m)
        self.assertAlmostEqual(cut, 0.7)
        self.assertIsNone(hike)


if __name__ == "__main__":
    unittest.main()
