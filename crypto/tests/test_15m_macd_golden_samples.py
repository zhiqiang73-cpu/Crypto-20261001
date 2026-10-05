"""黄金样本：15m MACD 方向闸门的最小可复现规则矩阵。"""
import unittest

from shadow.signals import crossing, macd_gate


class Test15mMacdGoldenSamples(unittest.TestCase):
    def test_green_positive_hist_only_allows_long(self):
        gold, dead = crossing(49.0, 50.0, 51.0, 50.0)
        self.assertTrue(gold)
        self.assertFalse(dead)
        self.assertEqual(macd_gate(gold, dead, 0.25), (True, False, ""))

    def test_red_negative_hist_only_allows_short(self):
        gold, dead = crossing(51.0, 50.0, 49.0, 50.0)
        self.assertFalse(gold)
        self.assertTrue(dead)
        self.assertEqual(macd_gate(gold, dead, -0.25), (False, True, ""))

    def test_divergence_is_a_no_action_not_an_exit_or_reverse(self):
        gold, dead = crossing(49.0, 50.0, 51.0, 50.0)
        self.assertEqual(macd_gate(gold, dead, -0.25)[0:2], (False, False))
        gold, dead = crossing(51.0, 50.0, 49.0, 50.0)
        self.assertEqual(macd_gate(gold, dead, 0.25)[0:2], (False, False))

    def test_zero_histogram_is_not_a_direction(self):
        gold, dead = crossing(49.0, 50.0, 51.0, 50.0)
        long_, short_, note = macd_gate(gold, dead, 0.0)
        self.assertFalse(long_)
        self.assertFalse(short_)
        self.assertIn("丢弃", note)


if __name__ == "__main__":
    unittest.main()
