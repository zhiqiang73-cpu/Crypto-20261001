"""2026-10-03：回测引擎的 5m 回放能力 + 可配置止损。

覆盖四件事：
  1. ATR 对齐按「本次回放的周期」计算（5m 与 15m 边界不同）
  2. SPEC_5M 的入场规则与 15m 相同：交叉 + MACD 能量柱方向闸门
  3. 正常止损在指定距离触发，且先于灾难止损；记入 exit_reason
  4. 不设正常止损时，行为与历史一致（不会出现"止损"平仓）

另覆盖研究标定的两个关键防回归点：固定风险仓位不随回测权益复利，
以及 +1.5×ATR 保本触发从下一根 K 线才生效；止损同一根的信号不得反手。
"""
from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from shadow.engine import (SPEC_5M, SPEC_15M, OpenPosition, _arm_break_even,
                           _stop_hit, align_atr_1h, run_shadow, run_shadow_spec)
from shadow.engine import ShadowConfig


def _pos(side: int = 1, entry_px: float = 100.0, atr: float = 10.0) -> OpenPosition:
    return OpenPosition(
        side=side, entry_ms=0, entry_px=entry_px, qty=1.0, fee=0.0,
        k_at_entry=10.0, d_at_entry=10.0, atr_at_entry=atr,
        bandwidth_at_entry=0.0, multiple_at_entry=0.0, fee_points_at_entry=0.0,
    )


def _synthetic_5m(n: int = 900, start_px: float = 30000.0) -> dict:
    """合成一段带明显波动的 5m 行情，用于产出真实的 KDJ 交叉。"""
    t = np.arange(n, dtype=np.float64)
    px = (start_px + 900.0 * np.sin(t / 14.0) + 260.0 * np.sin(t / 4.3)
          + 1.5 * t)
    high = px + 18.0
    low = px - 18.0
    open_ = np.roll(px, 1)
    open_[0] = px[0]
    ts = np.arange(n, dtype=np.int64) * 5 * 60 * 1000
    return {"ts": ts, "open": open_, "high": high, "low": low,
            "close": px, "volume": np.ones(n)}


def _synthetic_1h(n: int = 120, start_px: float = 30000.0) -> dict:
    t = np.arange(n, dtype=np.float64)
    px = start_px + 700.0 * np.sin(t / 9.0) + 2.0 * t
    ts = np.arange(n, dtype=np.int64) * 60 * 60 * 1000
    return {"ts": ts, "open": px, "high": px + 120.0, "low": px - 120.0,
            "close": px, "volume": np.ones(n)}


class TestAtrAlignmentFollowsInterval(unittest.TestCase):
    def test_5m_and_15m_use_different_boundaries(self):
        h1_ms = np.array([0, 3_600_000], dtype=np.int64)
        atr = np.array([5.0, 7.0])

        bars5 = np.arange(0, 3_600_000 + 1, 300_000, dtype=np.int64)
        out5 = align_atr_1h(bars5, h1_ms, atr, 5 * 60 * 1000)
        first5 = int(np.argmax(~np.isnan(out5)))
        # 5m 第一根「收盘 ≥ 3600000」的是 open = 3300000
        self.assertEqual(int(bars5[first5]), 3_300_000)

        bars15 = np.arange(0, 3_600_000 + 1, 900_000, dtype=np.int64)
        out15 = align_atr_1h(bars15, h1_ms, atr, 15 * 60 * 1000)
        first15 = int(np.argmax(~np.isnan(out15)))
        # 15m 第一根是 open = 2700000
        self.assertEqual(int(bars15[first15]), 2_700_000)

    def test_default_parameter_keeps_15m_behaviour(self):
        h1_ms = np.array([0, 3_600_000], dtype=np.int64)
        atr = np.array([5.0, 7.0])
        bars15 = np.arange(0, 7_200_000 + 1, 900_000, dtype=np.int64)
        # 数组含 NaN（1H 尚未收盘），比较时必须 equal_nan
        self.assertTrue(np.allclose(
            align_atr_1h(bars15, h1_ms, atr),
            align_atr_1h(bars15, h1_ms, atr, 15 * 60 * 1000),
            equal_nan=True))


class TestStopHit(unittest.TestCase):
    def test_normal_stop_triggers_before_disaster(self):
        spec = replace(SPEC_5M, stop_atr_mult=1.5, disaster_atr_mult=3.0)
        # 多头入场 100，ATR=10 → 正常止损 85，灾难止损 70
        self.assertEqual(_stop_hit(_pos(1), 105.0, 80.0, spec), (85.0, "止损"))
        # 同一根同时穿过两条线：成交更近的正常止损
        self.assertEqual(_stop_hit(_pos(1), 105.0, 60.0, spec), (85.0, "止损"))
        self.assertIsNone(_stop_hit(_pos(1), 105.0, 86.0, spec))
        # 空头入场 100 → 正常止损 115，灾难止损 130
        self.assertEqual(_stop_hit(_pos(-1), 120.0, 80.0, spec), (115.0, "止损"))
        self.assertEqual(_stop_hit(_pos(-1), 140.0, 80.0, spec), (115.0, "止损"))
        self.assertIsNone(_stop_hit(_pos(-1), 114.0, 80.0, spec))

    def test_disaster_only_when_no_normal_stop(self):
        spec = replace(SPEC_5M, stop_atr_mult=None, disaster_atr_mult=3.0)
        self.assertEqual(_stop_hit(_pos(1), 105.0, 60.0, spec), (70.0, "灾难止损"))
        self.assertIsNone(_stop_hit(_pos(1), 105.0, 71.0, spec))

    def test_both_off_never_triggers(self):
        spec = replace(SPEC_5M, stop_atr_mult=None, disaster_atr_mult=0.0)
        self.assertIsNone(_stop_hit(_pos(1), 105.0, 1.0, spec))


class TestBreakEvenStop(unittest.TestCase):
    def test_profit_trigger_arms_break_even_for_later_bars(self):
        spec = replace(SPEC_5M, stop_atr_mult=1.5,
                       break_even_trigger_atr_mult=1.5, disaster_atr_mult=3.0)
        long_pos = _pos(1)
        # 开仓 100、ATR 10；本根高点达到 115 后，仅把下一根的止损移至 100。
        self.assertTrue(_arm_break_even(long_pos, 115.0, 101.0, spec))
        self.assertTrue(long_pos.break_even_armed)
        self.assertEqual(_stop_hit(long_pos, 110.0, 99.0, spec),
                         (100.0, "保本止损"))

        short_pos = _pos(-1)
        self.assertTrue(_arm_break_even(short_pos, 99.0, 85.0, spec))
        self.assertTrue(short_pos.break_even_armed)
        self.assertEqual(_stop_hit(short_pos, 101.0, 100.5, spec),
                         (100.0, "保本止损"))

    def test_break_even_can_be_disabled_without_changing_normal_stop(self):
        spec = replace(SPEC_5M, stop_atr_mult=1.5,
                       break_even_trigger_atr_mult=None, disaster_atr_mult=3.0)
        pos = _pos(1)
        self.assertFalse(_arm_break_even(pos, 120.0, 100.0, spec))
        self.assertFalse(pos.break_even_armed)
        self.assertEqual(_stop_hit(pos, 110.0, 84.0, spec), (85.0, "止损"))


class Test5mEntryRule(unittest.TestCase):
    def test_5m_signals_respect_macd_gate(self):
        b5 = _synthetic_5m()
        b1h = _synthetic_1h()
        res = run_shadow_spec(b5, b1h, SPEC_5M, ShadowConfig(equity0=1000.0))
        self.assertTrue(res.bars)

        n_long = n_short = n_loose = 0
        for b in res.bars:
            n_loose += int(b.loose_long) + int(b.loose_short)
            if b.sig_long:
                n_long += 1
                self.assertTrue(b.gold_cross)
                self.assertGreater(b.macd_hist, 0.0)     # 绿柱
            if b.sig_short:
                n_short += 1
                self.assertTrue(b.dead_cross)
                self.assertLess(b.macd_hist, 0.0)        # 红柱
        # 能量柱闸门必须真的挡掉一部分裸交叉，否则这个测试没有意义。
        self.assertGreater(n_long + n_short, 0)
        self.assertGreater(n_loose, n_long + n_short)

    def test_5m_spec_uses_macd_gate_like_15m(self):
        self.assertEqual(SPEC_5M.entry_mode, "macd_gate")
        self.assertEqual(SPEC_15M.entry_mode, "macd_gate")
        self.assertEqual(SPEC_5M.interval_ms, 5 * 60 * 1000)
        self.assertEqual(SPEC_15M.interval_ms, 15 * 60 * 1000)
        self.assertEqual(SPEC_5M.break_even_trigger_atr_mult, 1.5)
        self.assertEqual(SPEC_15M.break_even_trigger_atr_mult, 1.5)
        self.assertEqual(SPEC_15M.stop_atr_mult, 1.5)

    def test_disabling_the_gate_lets_more_signals_through(self):
        b5 = _synthetic_5m()
        b1h = _synthetic_1h()
        gated = run_shadow_spec(b5, b1h, SPEC_5M, ShadowConfig(equity0=1000.0))
        raw = run_shadow_spec(
            b5, b1h, replace(SPEC_5M, macd_gate_enabled=False),
            ShadowConfig(equity0=1000.0))
        n_gated = sum(int(b.sig_long) + int(b.sig_short) for b in gated.bars)
        n_raw = sum(int(b.sig_long) + int(b.sig_short) for b in raw.bars)
        self.assertGreater(n_gated, 0)
        self.assertGreater(n_raw, n_gated)

    def test_15m_wrapper_unchanged(self):
        b5 = _synthetic_5m()
        b1h = _synthetic_1h()
        # run_shadow 仍是 15m 语义：用同一份数据喂 15m 规格，两者应一致
        a = run_shadow(b5, b1h, ShadowConfig(equity0=1000.0))
        b = run_shadow_spec(b5, b1h, SPEC_15M, ShadowConfig(equity0=1000.0))
        self.assertEqual(len(a.bars), len(b.bars))
        self.assertEqual(a.final_equity_a, b.final_equity_a)
        self.assertEqual(len(a.trades_a), len(b.trades_a))


class TestStopChangesTrades(unittest.TestCase):
    def test_stop_can_only_cut_losses_not_create_them(self):
        b5 = _synthetic_5m()
        b1h = _synthetic_1h()
        none_ = run_shadow_spec(b5, b1h, replace(SPEC_5M, stop_atr_mult=None),
                                ShadowConfig(equity0=1000.0))
        tight = run_shadow_spec(b5, b1h, replace(SPEC_5M, stop_atr_mult=1.0),
                                ShadowConfig(equity0=1000.0))
        # 紧止损一定不会让最差单笔更差
        worst_none = min((t.net for t in none_.trades_a), default=0.0)
        worst_tight = min((t.net for t in tight.trades_a), default=0.0)
        self.assertGreaterEqual(worst_tight, worst_none)

    def test_no_stop_means_no_stop_exit_reason(self):
        b5 = _synthetic_5m()
        b1h = _synthetic_1h()
        res = run_shadow_spec(b5, b1h, replace(SPEC_5M, stop_atr_mult=None),
                              ShadowConfig(equity0=1000.0))
        reasons = {t.exit_reason for t in res.trades_a + res.trades_b}
        self.assertNotIn("止损", reasons)


class TestResearchSizingAndStopReentry(unittest.TestCase):
    def test_fixed_risk_position_scale_halves_each_trade_quantity(self):
        b5 = _synthetic_5m()
        b1h = _synthetic_1h()
        spec = replace(SPEC_5M, stop_atr_mult=1.5)
        full = run_shadow_spec(
            b5, b1h, spec,
            ShadowConfig(equity0=1000.0, fixed_risk_equity=1000.0,
                         position_scale=1.0, research_ignore_risk_gates=True,
                         record_bars=False),
        )
        half = run_shadow_spec(
            b5, b1h, spec,
            ShadowConfig(equity0=1000.0, fixed_risk_equity=1000.0,
                         position_scale=0.5, research_ignore_risk_gates=True,
                         record_bars=False),
        )
        self.assertEqual(len(full.trades_a), len(half.trades_a))
        self.assertGreater(len(full.trades_a), 3)
        for whole, reduced in zip(full.trades_a, half.trades_a):
            self.assertAlmostEqual(reduced.qty, whole.qty * 0.5, places=3)

    def test_stop_bar_signal_waits_for_a_future_complete_signal(self):
        """止损和反向信号同根出现时，不能在下一根直接反手。"""
        import shadow.engine as engine

        n = 240
        ts5 = np.arange(n, dtype=np.int64) * 5 * 60 * 1000
        close = np.full(n, 100.0)
        bars5 = {
            "ts": ts5, "open": close.copy(), "high": np.full(n, 101.0),
            "low": np.full(n, 99.0), "close": close, "volume": np.ones(n),
        }
        # 在第 191 根同时击中 1.5×ATR 止损；1H ATR 稳定在 10 左右。
        bars5["low"][191] = 80.0
        ts1h = np.arange(50, dtype=np.int64) * 60 * 60 * 1000
        bars1h = {
            "ts": ts1h, "open": np.full(50, 100.0), "high": np.full(50, 105.0),
            "low": np.full(50, 95.0), "close": np.full(50, 100.0),
            "volume": np.ones(50),
        }

        original_cross = engine.crossing
        original_gate = engine.macd_gate
        calls = {"i": -1}

        def scripted_cross(*_args, **_kwargs):
            calls["i"] += 1
            if calls["i"] == 190:
                return True, False       # 开多，下一根开盘成交
            if calls["i"] == 191:
                return False, True       # 与止损同根的反向信号
            return False, False

        # 本测试只验证止损与反手的时间关系；交叉走脚本, 闸门放行,
        # 不让平坦行情自己产生的 KDJ 交叉干扰。
        engine.crossing = scripted_cross
        engine.macd_gate = lambda sl, ss, *_a, **_k: (sl, ss, "")
        try:
            res = run_shadow_spec(
                bars5, bars1h,
                replace(SPEC_5M, stop_atr_mult=1.5, break_even_trigger_atr_mult=None),
                ShadowConfig(equity0=1000.0, fixed_risk_equity=1000.0,
                             research_ignore_risk_gates=True, record_bars=False),
            )
        finally:
            engine.crossing = original_cross
            engine.macd_gate = original_gate

        self.assertEqual(len(res.trades_a), 1)
        self.assertEqual(res.trades_a[0].exit_reason, "止损")
        self.assertTrue(any(skip["reason"] == "止损后等待下一完整信号"
                            for skip in res.skips))


if __name__ == "__main__":
    unittest.main()
