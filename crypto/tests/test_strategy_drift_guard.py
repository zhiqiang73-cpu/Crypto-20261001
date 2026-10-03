"""KDJ 运行事实防漂移哨兵：只读，不触发运行器或交易。"""
from __future__ import annotations

import unittest
from pathlib import Path

from scripts.strategy_drift_audit import build_audit

ROOT = Path(__file__).resolve().parents[1]


class TestStrategyDriftGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = build_audit()

    def test_only_btc_eth_15m_are_runtime_active(self):
        runtime = self.report["checks"]["runtime"]
        self.assertEqual(runtime["runtime_specs"], ["kdj15", "eth15"])
        self.assertEqual(runtime["heartbeat_strategies"], ["kdj15", "eth15"])
        self.assertEqual(runtime["state_strategy_books"], ["eth15", "kdj15"])
        self.assertTrue(runtime["five_minute_not_in_runtime_specs"])

    def test_five_minute_cards_remain_research_only(self):
        runtime = self.report["checks"]["runtime"]
        self.assertEqual(runtime["card_count"], 4)
        self.assertEqual(runtime["disabled_5m_runtime_keys"], ["eth5", "kdj5"])
        self.assertTrue(runtime["five_minute_definitions_retained"])

    def test_current_signal_gate_is_macd_without_k_extreme(self):
        signal = self.report["checks"]["signal"]
        self.assertEqual(signal["kdj"], [9, 3, 3])
        self.assertEqual(signal["macd"], [12, 26, 9])
        self.assertTrue(signal["all_runtime_require_macd"])
        self.assertEqual(signal["runtime_max_layers"], {"kdj15": 3, "eth15": 3})
        self.assertTrue(signal["all_runtime_pyramiding_enabled"])
        self.assertTrue(signal["enabled_cards_pyramid_match_runtime"])
        self.assertTrue(signal["all_cards_macd_gate"])
        self.assertTrue(signal["no_enabled_card_k_extreme"])

    def test_risk_and_market_facts_are_consistent(self):
        risk = self.report["checks"]["risk"]
        market = self.report["checks"]["market"]
        self.assertTrue(risk["cards_uniform_risk"])
        self.assertTrue(risk["cards_uniform_leverage"])
        self.assertEqual(risk["risk_r"], 0.03)
        self.assertEqual(risk["leverage"], 10)
        self.assertTrue(market["heartbeat_is_testnet"])
        self.assertTrue(market["state_is_testnet"])
        self.assertTrue(market["symbols_are_btc_eth"])

    def test_contract_has_schema_and_implementation_fingerprint(self):
        contract = self.report["contract"]
        self.assertEqual(contract["schema_version"], "strategy_governance.v1")
        self.assertEqual(contract["contract_version"], "2026-10-03.1")
        self.assertEqual(len(contract["schema_sha256"]), 64)
        self.assertEqual(len(contract["implementation_fingerprint"]), 64)

    def test_frontend_does_not_define_retired_k_extreme_or_fixed_date(self):
        frontend = "\n".join(
            (ROOT / "frontend" / name).read_text(encoding="utf-8")
            for name in ("app.js", "index.html")
        )
        self.assertNotIn("K<30", frontend)
        self.assertNotIn("K>70", frontend)
        self.assertNotIn("HISTORY_DAY", frontend)


if __name__ == "__main__":
    unittest.main()
