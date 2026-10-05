"""2026-10-03：权益曲线必须等于「初始权益 + 逐笔净盈亏之和」。

背景：开仓时权益已经扣过一次入场手续费，平仓时若再加 `毛 − 入场费 − 出场费`，
入场费就被扣了两次。症状是「逐笔净盈亏为正、权益却下降」，并且最大回撤被系统性
放大（从而更早触发 10% 熔断）。本测试把这个恒等式钉住。

允许的唯一偏差：回放结束时若仍有持仓未平，其入场费已扣、但还没有 Trade 记录，
所以差额至多等于「一笔」入场费；绝不允许随笔数累积。
"""
from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from shadow.engine import SPEC_5M, ShadowConfig, run_shadow_spec


def _synthetic_5m(n: int = 6000, start_px: float = 30000.0) -> dict:
    """波动足够多的合成 5m 行情，用来产出大量交叉与成交。"""
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


def _synthetic_1h(n: int = 600, start_px: float = 30000.0) -> dict:
    t = np.arange(n, dtype=np.float64)
    px = start_px + 700.0 * np.sin(t / 9.0) + 2.0 * t
    ts = np.arange(n, dtype=np.int64) * 60 * 60 * 1000
    return {"ts": ts, "open": px, "high": px + 120.0, "low": px - 120.0,
            "close": px, "volume": np.ones(n)}


class TestEquityEqualsSumOfNet(unittest.TestCase):
    def setUp(self):
        self.b5 = _synthetic_5m()
        self.b1h = _synthetic_1h()
        self.equity0 = 1000.0

    def _run(self, mode: str):
        spec = replace(SPEC_5M, stop_atr_mult=1.5, halt_on_drawdown=False)
        res = run_shadow_spec(self.b5, self.b1h, spec,
                              ShadowConfig(equity0=self.equity0))
        trades = res.trades_a if mode == "A" else res.trades_b
        equity = res.final_equity_a if mode == "A" else res.final_equity_b
        return res, trades, equity

    def test_no_double_counted_entry_fee(self):
        for mode in ("A", "B"):
            with self.subTest(mode=mode):
                res, trades, equity = self._run(mode)
                self.assertGreater(len(trades), 20)          # 样本要有意义
                drift = equity - (self.equity0 + sum(t.net for t in trades))
                max_entry_fee = max(t.entry_fee for t in trades)
                # 未平仓时只允许差「一笔」入场费；有 bug 时差额会累积到几十倍
                self.assertLessEqual(abs(drift), max_entry_fee * 1.5,
                                     f"权益与逐笔净盈亏之和不符：drift={drift:.4f}")

    def test_flat_at_end_means_exact_identity(self):
        res, trades, equity = self._run("A")
        if res.bars and res.bars[-1].pos_side == 0:
            self.assertAlmostEqual(
                equity, self.equity0 + sum(t.net for t in trades), places=6)

    def test_positive_net_cannot_lose_equity(self):
        """逐笔净盈亏为正时，权益绝不可能低于初始权益（旧 bug 的典型症状）。"""
        res, trades, equity = self._run("A")
        if sum(t.net for t in trades) > 0:
            self.assertGreaterEqual(equity, self.equity0 - 1e-9)


if __name__ == "__main__":
    unittest.main()
