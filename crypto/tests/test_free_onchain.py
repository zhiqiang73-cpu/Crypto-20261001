"""免费链上 / 消息解析单测."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.free_news import (
    halving_months,
    parse_farside_html,
    parse_stooq_csv,
    parse_yahoo_chart,
    score_regulation_headlines,
)
from collectors.free_onchain import (
    classify_whale_flow,
    parse_defillama_usdt,
    parse_mempool_hashrate,
)


class TestOnchainParsers(unittest.TestCase):
    def test_hashrate_series(self):
        payload = {
            "hashrates": [
                {"timestamp": 1000, "avgHashrate": 100},
                {"timestamp": 1000 + 30 * 86400, "avgHashrate": 110},
            ]
        }
        ch = parse_mempool_hashrate(payload)
        self.assertAlmostEqual(ch, 0.10, places=4)

    def test_whale_classify(self):
        net, d = classify_whale_flow(2000, 0)
        self.assertEqual(d, "to_exchange")
        self.assertEqual(net, 2000)
        net, d = classify_whale_flow(0, 2000)
        self.assertEqual(d, "from_exchange")
        net, d = classify_whale_flow(100, 50)
        self.assertEqual(d, "none")

    def test_defillama_usdt(self):
        payload = {
            "tokens": [
                {"date": 1, "circulating": {"peggedUSD": 100e9}},
                {"date": 2, "circulating": {"peggedUSD": 100.3e9}},
            ]
        }
        mint, burn = parse_defillama_usdt(payload)
        self.assertAlmostEqual(mint, 0.3e9)
        self.assertEqual(burn, 0.0)


class TestNewsParsers(unittest.TestCase):
    def test_farside_table(self):
        html = """
        <html><body><table>
        <tr><th>Date</th><th>IBIT</th><th>Total</th></tr>
        <tr><td>11 Sep</td><td>10.5</td><td>120.0</td></tr>
        <tr><td>10 Sep</td><td>-5.0</td><td>-20.0</td></tr>
        </table></body></html>
        """
        parsed = parse_farside_html(html)
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed["daily_net_usd"], 120_000_000)

    def test_farside_garbage(self):
        self.assertIsNone(parse_farside_html("<html>no numbers</html>"))

    def test_stooq(self):
        csv = "Date,Open,High,Low,Close,Volume\n"
        for i in range(10):
            csv += f"2024-01-{i+1:02d},100,101,99,{100+i},1\n"
        ch = parse_stooq_csv(csv)
        self.assertIsNotNone(ch)
        self.assertGreater(ch, 0)

    def test_yahoo_fallback_shape(self):
        payload = {
            "chart": {
                "result": [{
                    "indicators": {
                        "quote": [{"close": [100, 101, 102, 103, 104, 105, 106]}]
                    }
                }]
            }
        }
        ch = parse_yahoo_chart(payload)
        self.assertIsNotNone(ch)

    def test_halving(self):
        since, until = halving_months()
        self.assertGreater(since, 0)
        self.assertGreater(until, 0)

    def test_regulation_keywords(self):
        score, conf = score_regulation_headlines([
            "SEC sue major exchange",
            "Another SEC charge filed",
        ])
        self.assertEqual(conf, "low")
        self.assertLess(score, 0)


if __name__ == "__main__":
    unittest.main()
