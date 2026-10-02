"""停机期间 K 线补记逻辑测试。

存在理由：2026-10-02 用户对照图表问「最近一笔应该是 09:45 的金叉做多」。
信号判定本身没错（当时规则仍要求 K<30，而该根 K=50.73），但旧实现只处理
「最新一根」已收盘 K 线，运行器停机期间收盘的 K 线**既不下单也不留痕**，
事后根本查不出「停了多久、跳过了哪几根、有没有漏掉信号」。
"""

from __future__ import annotations

import unittest

from shadow.deploy import MISSED_LOOKBACK_BARS, plan_pending

_BAR_MS = 15 * 60 * 1000
_BASE = 1_790_900_000_000


def _series(count: int):
    return [_BASE + i * _BAR_MS for i in range(count)]


class TestPlanPending(unittest.TestCase):
    def test_no_new_bar_returns_none(self):
        ts = _series(5)
        self.assertEqual(plan_pending(ts, ts[-1]), ([], None, 0))

    def test_last_ts_ahead_of_series_returns_none(self):
        ts = _series(3)
        self.assertEqual(plan_pending(ts, ts[-1] + _BAR_MS), ([], None, 0))

    def test_single_new_bar_is_never_treated_as_missed(self):
        ts = _series(4)
        missed, latest, dropped = plan_pending(ts, ts[-2])
        self.assertEqual(missed, [])
        self.assertEqual(latest, 3)
        self.assertEqual(dropped, 0)

    def test_intermediate_bars_are_reported_as_missed(self):
        ts = _series(6)
        missed, latest, dropped = plan_pending(ts, ts[1])
        self.assertEqual(missed, [2, 3, 4])
        self.assertEqual(latest, 5)
        self.assertEqual(dropped, 0)

    def test_empty_series_is_safe(self):
        self.assertEqual(plan_pending([], 0), ([], None, 0))

    def test_fresh_start_keeps_only_recent_lookback(self):
        """全新启动时不得把 400 根历史全部当成「错过」。"""
        ts = _series(400)
        missed, latest, dropped = plan_pending(ts, 0)
        self.assertEqual(latest, 399)
        self.assertEqual(len(missed), MISSED_LOOKBACK_BARS)
        self.assertEqual(dropped, 400 - 1 - MISSED_LOOKBACK_BARS)
        self.assertEqual(missed, list(range(400 - 1 - MISSED_LOOKBACK_BARS, 399)))

    def test_custom_lookback(self):
        ts = _series(10)
        missed, latest, dropped = plan_pending(ts, 0, lookback=3)
        self.assertEqual(missed, [6, 7, 8])
        self.assertEqual(latest, 9)
        self.assertEqual(dropped, 6)

    def test_lookback_zero_disables_truncation(self):
        ts = _series(10)
        missed, latest, dropped = plan_pending(ts, 0, lookback=0)
        self.assertEqual(len(missed), 9)
        self.assertEqual(dropped, 0)

    def test_missed_is_always_contiguous_and_before_latest(self):
        ts = _series(12)
        missed, latest, _ = plan_pending(ts, ts[4])
        self.assertEqual(missed, [5, 6, 7, 8, 9, 10])
        self.assertTrue(all(j < latest for j in missed))
        self.assertEqual(missed, sorted(missed))


if __name__ == "__main__":
    unittest.main()
