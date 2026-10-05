"""只读公开行情黄金样本：2026-08-03 至 08-08 BTC/ETH，15m/1h。

此样本只校验离线研究模型的规则/记账回归，不证明策略盈利，不接任何订单端点。
来源文件 SHA 固定在 fixture；不用网络、账号、原始大 CSV 或运行时目录。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import unittest
from dataclasses import replace

import numpy as np

from shadow.engine_15m_research import SPEC_15M, ShadowConfig, run_shadow_spec
from shadow.signals import crossing, macd_gate
from shadow.strategy_books import apply_virtual_signal

FIXTURE = pathlib.Path(__file__).parent / "fixtures/15m_btc_golden_202608.json"
FIXTURE_SHA = "3b97969a4eb76b9afaa92b2850ea6b85f3753cce40a39c90def1a89a1935aa42"
ETH_FIXTURE = pathlib.Path(__file__).parent / "fixtures/15m_eth_golden_202608.json"
ETH_SHA = "6ab1b06c7b002e0fbd634d327eba31533cacdec2bd8a8f55cdd34cf734deb282"


def bars(source=FIXTURE, expected_sha=FIXTURE_SHA):
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise AssertionError("黄金样本被改写")
    obj = json.loads(raw)
    result = {}
    for tf, rows in obj["bars"].items():
        a = np.asarray(rows, dtype=np.float64)
        result[tf] = dict(zip(
            ("ts", "open", "high", "low", "close", "volume"),
            [a[:, 0].astype(np.int64)] + [a[:, i] for i in range(1, 6)],
        ))
    return result


class Test15mGoldenSignals(unittest.TestCase):
    def test_cross_hist_direction_and_divergence_matrix(self):
        self.assertEqual(crossing(40, 40, 41, 40), (True, False))
        self.assertEqual(crossing(60, 60, 59, 60), (False, True))
        for long_, short_, hist, allowed in (
            (True, False, 0.01, (True, False)),
            (False, True, -0.01, (False, True)),
            (True, False, -0.01, (False, False)),
            (False, True, 0.01, (False, False)),
            (True, False, 0.0, (False, False)),
            (False, True, float("nan"), (False, False)),
        ):
            with self.subTest(long=long_, short=short_, hist=hist):
                self.assertEqual(macd_gate(long_, short_, hist)[:2], allowed)

    def test_divergent_cross_does_not_close_existing_book(self):
        book = {"entry": {"side": 1, "qty": 0.01, "px": 100.0}}
        long_, short_, _ = macd_gate(False, True, +0.25)  # 死叉遇绿柱
        action, _, changed = apply_virtual_signal(
            book, sig_long=long_, sig_short=short_, qty=.01,
            px=100.0, atr=10.0, ms=900_000, block=False, min_qty=.001)
        self.assertIsNone(action)
        self.assertFalse(changed)
        self.assertEqual(book["entry"]["side"], 1)

    def test_real_excerpt_signal_counts_no_first_bar_lookahead(self):
        b = bars()
        res = run_shadow_spec(b["15m"], b["1h"],
            replace(SPEC_15M, stop_atr_mult=None,
                    break_even_trigger_atr_mult=None),
            ShadowConfig(record_bars=True, research_ignore_risk_gates=True,
                         close_at_end=True))
        self.assertFalse(res.bars[0].gold_cross or res.bars[0].dead_cross)
        self.assertEqual((sum(x.gold_cross for x in res.bars),
                          sum(x.dead_cross for x in res.bars)), (44, 44))
        self.assertEqual((sum(x.sig_long for x in res.bars),
                          sum(x.sig_short for x in res.bars)), (15, 20))
        for x in res.bars:
            if x.sig_long:
                self.assertTrue(x.gold_cross)
                self.assertGreater(x.macd_hist, 0)
            if x.sig_short:
                self.assertTrue(x.dead_cross)
                self.assertLess(x.macd_hist, 0)

    def test_eth_excerpt_has_the_same_direction_gate(self):
        b = bars(ETH_FIXTURE, ETH_SHA)
        res = run_shadow_spec(b["15m"], b["1h"], SPEC_15M,
            ShadowConfig(record_bars=True, research_ignore_risk_gates=True,
                         close_at_end=True))
        self.assertEqual((sum(x.gold_cross for x in res.bars),
                          sum(x.dead_cross for x in res.bars)), (42, 42))
        self.assertEqual((sum(x.sig_long for x in res.bars),
                          sum(x.sig_short for x in res.bars)), (17, 17))
        self.assertEqual((len(res.trades_a), len(res.trades_b)), (15, 30))
        for x in res.bars:
            self.assertFalse(x.sig_long and x.macd_hist <= 0)
            self.assertFalse(x.sig_short and x.macd_hist >= 0)


class Test15mGoldenTrades(unittest.TestCase):
    def _run(self, stop, be, **overrides):
        b = bars()
        return run_shadow_spec(b["15m"], b["1h"],
            replace(SPEC_15M, stop_atr_mult=stop,
                    break_even_trigger_atr_mult=be),
            ShadowConfig(record_bars=False, close_at_end=True, **overrides))

    def test_policy_matrix_and_exact_fee_accounting(self):
        for stop, be, n_a, net_a, n_b, net_b in (
            (None, None, 17, -42.366363, 32, -69.272032),
            (1.5, None, 17, -38.286029, 32, -69.272032),
            (1.5, 1.5, 19, -88.583019, 32, -69.272032),
        ):
            with self.subTest(stop=stop, be=be):
                r = self._run(stop, be, research_ignore_risk_gates=True)
                self.assertEqual((len(r.trades_a), len(r.trades_b)), (n_a, n_b))
                self.assertAlmostEqual(sum(t.net for t in r.trades_a), net_a, places=5)
                self.assertAlmostEqual(sum(t.net for t in r.trades_b), net_b, places=5)
                for mode in ("a", "b"):
                    trades = getattr(r, "trades_" + mode)
                    equity = getattr(r, "final_equity_" + mode)
                    self.assertAlmostEqual(equity, 1000 + sum(t.net for t in trades), places=7)
                    self.assertTrue(all(abs(t.net - (t.gross - t.entry_fee - t.exit_fee)) < 1e-7
                                        for t in trades))
                    self.assertTrue(all(t.exit_ms > t.entry_ms for t in trades))

    def test_fee_and_slippage_stress_replays_path(self):
        baseline = self._run(1.5, 1.5, research_ignore_risk_gates=True)
        stress = self._run(1.5, 1.5, research_ignore_risk_gates=True,
                           fee_per_side=.001, slippage_bps=5.0)
        self.assertEqual(len(baseline.trades_b), 32)
        self.assertEqual(len(stress.trades_b), 32)
        self.assertEqual(len(stress.trades_a), 18)  # 费用可改变复利与换手路径
        self.assertLess(stress.final_equity_a, baseline.final_equity_a)
        self.assertLess(stress.final_equity_b, baseline.final_equity_b)
        self.assertAlmostEqual(sum(t.net for t in stress.trades_a), -183.908687, places=5)

    def test_permanent_halt_latches_a_and_b_separately(self):
        r = self._run(1.5, 1.5, research_ignore_risk_gates=False)
        by_mode = {mode: next(h["ts"] for h in r.halts
                              if h.get("mode", "A") == mode)
                   for mode in ("A", "B")}
        self.assertEqual((len(r.trades_a), len(r.trades_b)), (9, 22))
        for mode in ("A", "B"):
            self.assertTrue(all(t.entry_ms <= by_mode[mode]
                                for t in (r.trades_a if mode == "A" else r.trades_b)))

    def test_prewarm_has_no_trades_before_boundary(self):
        b = bars()
        start = int(b["15m"]["ts"][200])
        r = run_shadow_spec(b["15m"], b["1h"], SPEC_15M,
            ShadowConfig(trade_start_ms=start, research_ignore_risk_gates=True,
                         record_bars=False, close_at_end=True))
        self.assertTrue(all(t.entry_ms >= start for t in r.trades_a + r.trades_b))
        self.assertTrue(r.trades_a)


if __name__ == "__main__":
    unittest.main()
