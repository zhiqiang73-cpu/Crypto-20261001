"""四面齐全时走完整 CS; 缺面回退 partial."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.scorer import FactorScoringEngine
from models.signals import DimensionScores, StrategyHorizon
from runtime.live_loop import LiveScoringLoop
from runtime.panel_server import snapshot_to_panel
from runtime.live_loop import LiveScoreSnapshot
from models.signals import ActionDecision


class TestFullCS(unittest.TestCase):
    def test_engine_four_faces(self):
        eng = FactorScoringEngine()
        ev = eng.evaluate(
            StrategyHorizon.SHORT_TERM,
            DimensionScores(news=10, data=50, tech=40, prediction=20),
        )
        # 线性 37; agreement_boost 默认关闭
        from utils.scoring import compute_cs_with_boost
        from config.weights import DIMENSION_WEIGHTS
        expected, _ = compute_cs_with_boost(
            {"news": 10, "data": 50, "tech": 40, "prediction": 20},
            DIMENSION_WEIGHTS["short_term"],
        )
        self.assertAlmostEqual(ev.composite_score, round(expected, 2), delta=0.1)
        self.assertFalse(ev.safety_valve_triggered)

    def test_partial_when_news_missing(self):
        loop = LiveScoringLoop()
        partial, decision, missing, _, bk, safety = loop.compute_partial_cs({
            "news": None, "data": 50.0, "tech": 40.0, "prediction": 20.0,
        })
        self.assertIn("news", missing)
        self.assertNotIn("data", missing)
        # 重归一 V8: w_sum = 0.35+0.25+0.20=0.80; boost off
        base = (0.35 / 0.80) * 50 + (0.25 / 0.80) * 40 + (0.20 / 0.80) * 20
        expected = base
        self.assertAlmostEqual(partial, round(expected, 2), delta=0.05)
        self.assertTrue(bk)
        self.assertFalse(safety)


class TestPanelPayload(unittest.TestCase):
    def test_snapshot_to_panel_shape(self):
        snap = LiveScoreSnapshot(
            timestamp_ms=1,
            mark_price=90000.0,
            s_data=20.0,
            s_tech=10.0,
            s_news=5.0,
            s_prediction=8.0,
            partial_cs=15.0,
            composite_score=15.0,
            decision=ActionDecision.WATCH_LONG,
            is_full_cs=True,
            weighted_breakdown={
                "news": 0.75, "data": 9.0, "tech": 2.5, "prediction": 1.2
            },
            staleness_sec={"binance": 1.0},
        )
        payload = snapshot_to_panel(snap, StrategyHorizon.SHORT_TERM)
        self.assertTrue(payload["live"])
        self.assertEqual(len(payload["faces"]), 4)
        self.assertEqual(len(payload["contrib"]), 4)
        self.assertIn("staleness", payload)
        self.assertIn("watch", payload)
        self.assertIn("liq", payload["watch"])


if __name__ == "__main__":
    unittest.main()
