import sys, os, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from models.signals import StrategyHorizon, ActionDecision, DimensionScores
from engine.scorer import FactorScoringEngine

class TestV7BidirectionalScoring(unittest.TestCase):
    def setUp(self):
        self.e = FactorScoringEngine()

    def test_case1_squeeze_long(self):
        # 费率极负+清算磁铁上方+CVD底背离+Pin Bar支撑
        r = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(news=+8, data=+54, tech=+62, prediction=+20))
        self.assertGreater(r.composite_score, 0)
        self.assertEqual(r.direction, "LONG")
        self.assertIn(r.decision, [ActionDecision.STANDARD_LONG, ActionDecision.STRONG_LONG])
        self.assertFalse(r.safety_valve_triggered)

    def test_case2_cascade_short(self):
        # 费率极正+多头清算厚+CVD顶背离+看跌吞没
        r = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(news=-10, data=-68, tech=-58, prediction=-25))
        self.assertLess(r.composite_score, 0)
        self.assertEqual(r.direction, "SHORT")
        self.assertIn(r.decision, [ActionDecision.STANDARD_SHORT, ActionDecision.STRONG_SHORT])

    def test_case3_cpi_bullish(self):
        r = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(news=+85, data=+42, tech=+55, prediction=+65))
        self.assertGreater(r.composite_score, 35)
        # 四面同向 → agreement boost 可能升到 STRONG_LONG
        self.assertIn(
            r.decision,
            [ActionDecision.STANDARD_LONG, ActionDecision.STRONG_LONG],
        )

    def test_case4_blackswan_phase1(self):
        r = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(news=-90, data=-50, tech=-70, prediction=-60))
        self.assertLess(r.composite_score, -40)
        self.assertEqual(r.direction, "SHORT")

    def test_case5_gunpowder_neutral(self):
        # 所有面中性 -> CS接近0
        r = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(news=+10, data=0, tech=0, prediction=+10))
        self.assertGreater(r.composite_score, -10)
        self.assertLess(r.composite_score, +10)
        self.assertEqual(r.decision, ActionDecision.NEUTRAL)

    def test_safety_valve_downgrades(self):
        # CS 正(做多), 但消息面 -40 (明确看空), 差距 > 50
        r = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(news=-40, data=+80, tech=+70, prediction=+50))
        # 线性 CS≈55, agreement boost 后可能更高; 安全阀仍触发并降一档
        self.assertTrue(r.safety_valve_triggered)
        self.assertIn(
            r.decision,
            [ActionDecision.WATCH_LONG, ActionDecision.STANDARD_LONG],
        )

    def test_asymmetric_thresholds(self):
        # CS = +40 -> STANDARD_LONG (threshold +35)
        r1 = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(news=+30, data=+45, tech=+40, prediction=+35))
        # CS = -40 -> STANDARD_SHORT (threshold -40)
        r2 = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(news=-30, data=-45, tech=-40, prediction=-35))
        self.assertEqual(r1.decision, ActionDecision.STANDARD_LONG)
        self.assertEqual(r2.decision, ActionDecision.STANDARD_SHORT)
        # 验证不对称: |CS|相同但决策等级不同
        # +40 >= +35 -> standard_long; -40 == -40 -> standard_short (刚好踩线)
        self.assertAlmostEqual(abs(r1.composite_score), abs(r2.composite_score), delta=1)

    def test_direction_property(self):
        r_pos = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(+50, +50, +50, +50))
        r_neg = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(-50, -50, -50, -50))
        r_zero = self.e.evaluate(StrategyHorizon.SHORT_TERM,
            DimensionScores(0, 0, 0, 0))
        self.assertEqual(r_pos.direction, "LONG")
        self.assertEqual(r_neg.direction, "SHORT")
        self.assertEqual(r_zero.direction, "NEUTRAL")

if __name__ == "__main__":
    unittest.main()
