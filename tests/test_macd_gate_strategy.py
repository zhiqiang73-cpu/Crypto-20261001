"""2026-10-02/03 用户规则：KDJ 交叉 + MACD 能量柱方向闸门。

规则原文（用户口头确认）：
    MACD 方向按红绿柱（能量柱）的正负为准；做双向；
    背离类信号直接丢弃不操作。
    2026-10-02：15m 两条先切换；2026-10-03：5m 两条改为与 15m 完全相同。

本文件守住四件事：
    1. MACD(12,26,9) 的计算口径与 TradingView / 币安一致；
    2. 闸门判定矩阵（同向放行 / 背离丢弃 / 柱为 0 丢弃）；
    3. 回测引擎产出的每一个信号都真的与能量柱同向；
    4. 四条规格与四张策略卡都已切到新规则，不再有任何 K 极值过滤。
"""
from __future__ import annotations

import json
import pathlib
import unittest

import numpy as np

from shadow.engine import MACD_GATE_ENABLED, run_shadow
from shadow.indicators import macd
from shadow.signals import macd_gate, macd_side
from shadow.strategy_books import (SPEC_5M, SPEC_15M, SPEC_ETH_5M,
                                   SPEC_ETH_15M)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _ema_ref(values, n):
    alpha = 2.0 / (n + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(alpha * float(v) + (1.0 - alpha) * out[-1])
    return np.array(out)


class TestMacdMath(unittest.TestCase):
    def test_matches_reference_ema(self):
        rng = np.random.default_rng(7)
        close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, 400))
        dif, dea, hist = macd(close, 12, 26, 9)
        exp_dif = _ema_ref(close, 12) - _ema_ref(close, 26)
        exp_dea = _ema_ref(exp_dif, 9)
        np.testing.assert_allclose(dif, exp_dif, rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(dea, exp_dea, rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(hist, dif - dea, rtol=1e-12, atol=1e-12)

    def test_constant_price_gives_zero_hist(self):
        _dif, _dea, hist = macd(np.full(50, 1234.0))
        np.testing.assert_allclose(hist, 0.0, atol=1e-9)

    def test_rising_price_gives_positive_hist(self):
        _dif, _dea, hist = macd(np.linspace(100.0, 200.0, 200))
        self.assertGreater(hist[-1], 0.0)

    def test_falling_price_gives_negative_hist(self):
        _dif, _dea, hist = macd(np.linspace(200.0, 100.0, 200))
        self.assertLess(hist[-1], 0.0)

    def test_empty_input_is_safe(self):
        dif, dea, hist = macd(np.array([]))
        self.assertEqual(len(dif), 0)
        self.assertEqual(len(dea), 0)
        self.assertEqual(len(hist), 0)


class TestMacdSide(unittest.TestCase):
    def test_red_green_and_flat(self):
        self.assertEqual(macd_side(1.5), 1)
        self.assertEqual(macd_side(-0.0001), -1)
        self.assertEqual(macd_side(0.0), 0)
        self.assertEqual(macd_side(float("nan")), 0)
        self.assertEqual(macd_side(None), 0)


class TestMacdGate(unittest.TestCase):
    def test_gold_with_red_hist_passes(self):
        self.assertEqual(macd_gate(True, False, 3.0), (True, False, ""))

    def test_dead_with_green_hist_passes(self):
        self.assertEqual(macd_gate(False, True, -3.0), (False, True, ""))

    def test_gold_with_green_hist_is_discarded(self):
        sig_long, sig_short, note = macd_gate(True, False, -3.0)
        self.assertFalse(sig_long)
        self.assertFalse(sig_short)
        self.assertIn("背离", note)

    def test_dead_with_red_hist_is_discarded(self):
        sig_long, sig_short, note = macd_gate(False, True, 3.0)
        self.assertFalse(sig_long)
        self.assertFalse(sig_short)
        self.assertIn("背离", note)

    def test_flat_hist_is_discarded(self):
        sig_long, sig_short, note = macd_gate(True, False, 0.0)
        self.assertFalse(sig_long)
        self.assertFalse(sig_short)
        self.assertIn("0", note)

    def test_no_cross_never_reports_a_note(self):
        self.assertEqual(macd_gate(False, False, 5.0), (False, False, ""))

    def test_disabled_gate_passes_everything(self):
        self.assertEqual(macd_gate(True, False, -3.0, enabled=False),
                         (True, False, ""))


class TestEngineRespectsGate(unittest.TestCase):
    """回测引擎里每一个信号都必须与当时的能量柱同向。"""

    def _synthetic(self, n=400, seed=11):
        rng = np.random.default_rng(seed)
        step = 15 * 60 * 1000
        ts = np.arange(n, dtype=np.int64) * step + 1_800_000_000_000
        close = 86_000.0 + np.cumsum(rng.normal(0.0, 55.0, n))
        high = close + np.abs(rng.normal(0.0, 20.0, n))
        low = close - np.abs(rng.normal(0.0, 20.0, n))
        op = np.concatenate(([close[0]], close[:-1]))
        bars15 = {"ts": ts, "open": op, "high": high, "low": low,
                  "close": close, "volume": np.ones(n)}

        m = n // 4 + 20
        ts1h = np.arange(m, dtype=np.int64) * 60 * 60 * 1000 + 1_800_000_000_000
        close1h = 86_000.0 + np.cumsum(rng.normal(0.0, 120.0, m))
        bars1h = {"ts": ts1h, "open": close1h, "high": close1h + 40.0,
                  "low": close1h - 40.0, "close": close1h,
                  "volume": np.ones(m)}
        return bars15, bars1h

    def test_every_signal_matches_hist_sign(self):
        self.assertTrue(MACD_GATE_ENABLED)
        bars15, bars1h = self._synthetic()
        res = run_shadow(bars15, bars1h)
        self.assertTrue(res.bars)
        n_long = n_short = n_loose = 0
        for b in res.bars:
            if b.sig_long:
                n_long += 1
                self.assertGreater(b.macd_hist, 0.0)
            if b.sig_short:
                n_short += 1
                self.assertLess(b.macd_hist, 0.0)
            n_loose += int(b.loose_long) + int(b.loose_short)
            # 触发信号必然是裸交叉的子集。
            if b.sig_long:
                self.assertTrue(b.gold_cross)
            if b.sig_short:
                self.assertTrue(b.dead_cross)
        # 闸门必须真的挡掉一部分裸交叉，否则这个测试没有意义。
        self.assertGreater(n_loose, n_long + n_short)

    def test_gate_can_be_disabled_for_comparison(self):
        import shadow.engine as engine
        bars15, bars1h = self._synthetic()
        gated = run_shadow(bars15, bars1h)
        old = engine.MACD_GATE_ENABLED
        engine.MACD_GATE_ENABLED = False
        try:
            raw = run_shadow(bars15, bars1h)
        finally:
            engine.MACD_GATE_ENABLED = old
        gated_sig = sum(int(b.sig_long) + int(b.sig_short) for b in gated.bars)
        raw_sig = sum(int(b.sig_long) + int(b.sig_short) for b in raw.bars)
        self.assertGreater(raw_sig, gated_sig)


class TestSpecsAndCards(unittest.TestCase):
    def test_all_four_specs_use_the_gate(self):
        for spec in (SPEC_15M, SPEC_5M, SPEC_ETH_15M, SPEC_ETH_5M):
            self.assertTrue(spec.require_macd, spec.id)

    def test_all_specs_directional_without_k_thresholds(self):
        for spec in (SPEC_15M, SPEC_5M, SPEC_ETH_15M, SPEC_ETH_5M):
            self.assertIn(spec.interval, ("15m", "5m"), spec.id)
            self.assertIsNone(spec.k_long_max, spec.id)
            self.assertIsNone(spec.k_short_min, spec.id)
            self.assertIn("MACD", spec.signal_rule, spec.id)
            self.assertIn("背离", spec.signal_rule, spec.id)

    def test_all_cards_describe_the_gate(self):
        for name, timeframe in (
                ("deployed_kdj_extreme_v1.json", "15m"),
                ("deployed_kdj_eth_extreme_v1.json", "15m"),
                ("deployed_kdj_5m_extreme_v1.json", "5m"),
                ("deployed_kdj_eth_5m_extreme_v1.json", "5m")):
            cfg = json.loads((ROOT / "config/strategies" / name)
                             .read_text(encoding="utf-8"))
            self.assertEqual(cfg["timeframe"], timeframe, name)
            self.assertEqual(cfg["side"], "both", name)
            self.assertIn("MACD", cfg["indicators"], name)
            self.assertIn("12,26,9", cfg["indicators"]["MACD"], name)
            self.assertIn("背离", cfg["entry"]["divergence"], name)
            self.assertIn("MACD", cfg["entry"]["long"], name)
            self.assertIn("正（绿柱）", cfg["entry"]["long"], name)
            self.assertIn("MACD", cfg["entry"]["short"], name)
            self.assertIn("负（红柱）", cfg["entry"]["short"], name)
            self.assertIn("金叉遇红柱 / 死叉遇绿柱", cfg["entry"]["divergence"], name)

    def test_5m_cards_have_no_k_threshold_text(self):
        for name, key in (("deployed_kdj_5m_extreme_v1.json", "kdj5"),
                          ("deployed_kdj_eth_5m_extreme_v1.json", "eth5")):
            cfg = json.loads((ROOT / "config/strategies" / name)
                             .read_text(encoding="utf-8"))
            self.assertEqual(cfg["runtime_key"], key)
            self.assertNotIn("K<30", cfg["entry"]["long"])
            self.assertNotIn("K>70", cfg["entry"]["short"])


if __name__ == "__main__":
    unittest.main()
