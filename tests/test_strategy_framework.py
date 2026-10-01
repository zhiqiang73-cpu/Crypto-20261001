import tempfile
import unittest
from pathlib import Path

from config.strategy_registry import upsert_strategy, validate_strategy
from trading.runtime_mode import is_mainnet_url


class TestStrategyFramework(unittest.TestCase):
    def setUp(self):
        self.spec = {
            "strategy_id": "btc-mean-reversion",
            "name": "BTC 均值回归",
            "kind": "mean_reversion",
            "timeframe": "15m",
            "side": "both",
            "entry": {"indicator": "bbands", "zscore": -2.0},
            "exit": {"take_profit_r": 1.5, "stop_loss_r": 1.0},
            "risk": {"max_risk_pct": 0.005, "leverage": 2},
            "source_note": "Claude 已验证结论",
        }

    def test_validate_and_persist_declarative_strategy(self):
        with tempfile.TemporaryDirectory() as td:
            row = upsert_strategy(self.spec, Path(td) / "registry.json")
            self.assertEqual(row["status"], "paper_only")
            self.assertFalse(row["enabled"])

    def test_reject_executable_payload(self):
        bad = dict(self.spec, python="place_order()")
        with self.assertRaises(ValueError):
            validate_strategy(bad)

    def test_mainnet_is_detectable(self):
        self.assertTrue(is_mainnet_url("https://fapi.binance.com"))
        self.assertFalse(is_mainnet_url("https://testnet.binancefuture.com"))


if __name__ == "__main__":
    unittest.main()
