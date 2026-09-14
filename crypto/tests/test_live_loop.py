"""Live scoring loop unit tests."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models.signals import ActionDecision, StrategyHorizon
from runtime.live_loop import LiveScoringLoop


class TestPartialCS(unittest.TestCase):
    def setUp(self):
        self.loop = LiveScoringLoop(horizon=StrategyHorizon.SHORT_TERM)

    def test_renormalize_data_tech_only(self):
        # short-term V8: data 0.35, tech 0.25 → share 0.35/0.6 and 0.25/0.6
        partial, decision, missing, reasoning, _bk = self.loop.compute_partial_cs({
            "news": None,
            "data": 40.0,
            "tech": 20.0,
            "prediction": None,
        })
        expected = (0.35 / 0.60) * 40 + (0.25 / 0.60) * 20
        self.assertAlmostEqual(partial, round(expected, 2), delta=0.05)
        self.assertEqual(set(missing), {"news", "prediction"})
        self.assertIn("confidence-weighted", reasoning)
        # ~31.7 → WATCH_LONG
        self.assertEqual(decision, ActionDecision.WATCH_LONG)

    def test_does_not_fake_zero_news(self):
        # 若错误地把 news/pred 填 0: CS = 0.35*40 + 0.25*20 = 19
        # 正确重归一化 ≈ 31.67 — 两者必须不同
        partial, _, _, _, _ = self.loop.compute_partial_cs({
            "news": None, "data": 40.0, "tech": 20.0, "prediction": None,
        })
        fake = 0.20 * 0 + 0.35 * 40 + 0.25 * 20 + 0.20 * 0
        self.assertNotAlmostEqual(partial, fake, delta=1.0)

    def test_all_missing(self):
        partial, decision, missing, _, _ = self.loop.compute_partial_cs({
            "news": None, "data": None, "tech": None, "prediction": None,
        })
        self.assertEqual(partial, 0.0)
        self.assertEqual(decision, ActionDecision.NEUTRAL)
        self.assertEqual(len(missing), 4)


if __name__ == "__main__":
    unittest.main()
