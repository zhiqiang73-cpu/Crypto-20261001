"""Deribit Max Pain / DVOL 解析单测."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.deribit import compute_max_pain, median_mark_iv, parse_dvol, parse_instrument_name


class TestDeribitParsers(unittest.TestCase):
    def test_parse_name(self):
        self.assertEqual(
            parse_instrument_name("BTC-27JUN25-100000-C"),
            ("27JUN25", 100000.0, "C"),
        )
        self.assertIsNone(parse_instrument_name("BTC-PERPETUAL"))

    def test_max_pain_simple(self):
        # put OI 集中在 90k, call 在 110k → max pain 应靠近中间
        rows = [
            {"instrument_name": "BTC-27JUN25-90000-P", "open_interest": 100},
            {"instrument_name": "BTC-27JUN25-100000-P", "open_interest": 50},
            {"instrument_name": "BTC-27JUN25-100000-C", "open_interest": 50},
            {"instrument_name": "BTC-27JUN25-110000-C", "open_interest": 100},
        ]
        result = compute_max_pain(rows)
        self.assertIsNotNone(result)
        strike, expiry = result
        self.assertEqual(expiry, "27JUN25")
        self.assertIn(strike, (90000.0, 100000.0, 110000.0))

    def test_max_pain_empty(self):
        self.assertIsNone(compute_max_pain([]))

    def test_parse_dvol_percent(self):
        self.assertAlmostEqual(
            parse_dvol({"result": {"volatility": 55.2}}), 0.552, places=3
        )

    def test_parse_dvol_series(self):
        payload = {"result": {"data": [[1, 50, 60, 40, 52.0]]}}
        self.assertAlmostEqual(parse_dvol(payload), 0.52, places=3)

    def test_median_mark_iv(self):
        rows = [{"mark_iv": 30}, {"mark_iv": 40}, {"mark_iv": 50}]
        self.assertAlmostEqual(median_mark_iv(rows), 0.40, places=3)


if __name__ == "__main__":
    unittest.main()
