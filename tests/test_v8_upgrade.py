"""V8 ExitChecker / continuous mapper smoke tests."""

import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading.exit_checker import ExitChecker
from trading.models import HorizonPosition
from indicators.engine import ema_stack_score, _vwap_score, macd_hist_score
from utils.scoring import interpolate_anchors
from config.mapping import OI_CHANGE_5M_ANCHORS, MVRV_ANCHORS
from config.weights import DIMENSION_WEIGHTS
from config.review import LEVERAGE_LONG_TERM, EXIT_STRATEGY, SETTLE_CONFIG, RISK_PER_TRADE_PCT


class TestV8Weights(unittest.TestCase):
    def test_short_weights_sum(self):
        w = DIMENSION_WEIGHTS["short_term"]
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)
        self.assertEqual(w["news"], 0.20)
        self.assertEqual(w["data"], 0.35)
        self.assertEqual(w["prediction"], 0.20)

    def test_long_weights_sum(self):
        w = DIMENSION_WEIGHTS["long_term"]
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)
        self.assertEqual(w["tech"], 0.10)
        self.assertEqual(w["prediction"], 0.30)

    def test_leverage_and_settle(self):
        self.assertEqual(LEVERAGE_LONG_TERM, 3)
        self.assertEqual(SETTLE_CONFIG["long_term"]["window_hours"], 720.0)
        self.assertIn("tp1_pct", EXIT_STRATEGY["short_term"])
        self.assertIn("hard_sl_atr", EXIT_STRATEGY["short_term"])
        self.assertAlmostEqual(RISK_PER_TRADE_PCT["short_term"], 0.005)


class TestContinuousMapping(unittest.TestCase):
    def test_vwap_continuous(self):
        s = _vwap_score(101.0, 100.0, 1.0)
        self.assertAlmostEqual(s, 40.0, places=1)

    def test_ema_bonus(self):
        # bull stack + price above ema21
        s = ema_stack_score(110.0, 105.0, 100.0, 90.0, atr_v=2.0)
        self.assertGreater(s, 70.0)  # base 70 + bonus

    def test_macd_continuous(self):
        hist = [None, None, None, 1.0]
        s = macd_hist_score(hist, atr_v=2.0)
        self.assertAlmostEqual(s, 25.0, places=1)

    def test_oi_anchors(self):
        s = interpolate_anchors(-0.03, OI_CHANGE_5M_ANCHORS)
        self.assertAlmostEqual(s, 60.0, places=1)

    def test_mvrv_anchors(self):
        self.assertGreater(interpolate_anchors(0.9, MVRV_ANCHORS), 80)


class TestExitChecker(unittest.TestCase):
    def setUp(self):
        self.checker = ExitChecker()
        self.pos = HorizonPosition(
            horizon="short_term",
            side="LONG",
            entry_price=100.0,
            quantity=1.0,
            original_quantity=1.0,
            remaining_pct=1.0,
            peak_price=100.0,
            entry_cs=45.0,
            opened_at_ms=1_000_000,
            sl_price=99.0,
        )

    def test_hard_sl(self):
        acts = self.checker.check_exits(self.pos, 98.9, cs=40.0, now_ms=1_100_000)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0].reason, "hard_sl")

    def test_hard_sl_atr(self):
        # entry=100, atr=2, hard_sl_atr=1.0 → 止损距离 2 → 价格 98 触发
        self.pos.entry_atr = 2.0
        acts = self.checker.check_exits(
            self.pos, 97.9, cs=40.0, now_ms=1_100_000, atr=2.0
        )
        self.assertEqual(acts[0].reason, "hard_sl")

    def test_tp1(self):
        # V8.2: tp1_pct=0.012 → 需涨 1.2%
        acts = self.checker.check_exits(self.pos, 101.3, cs=40.0, now_ms=1_100_000)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0].reason, "tp1")
        self.assertAlmostEqual(acts[0].close_pct, 0.50)

    def test_spread_force(self):
        acts = self.checker.check_exits(
            self.pos, 100.5, cs=40.0, now_ms=1_100_000, spread_vs_mean=3.5
        )
        self.assertEqual(acts[0].reason, "spread_force")

    def test_cs_decay(self):
        self.pos.entry_cs = 50.0
        acts = self.checker.check_exits(self.pos, 100.2, cs=20.0, now_ms=1_100_000)
        self.assertEqual(acts[0].reason, "cs_decay")


if __name__ == "__main__":
    unittest.main()
