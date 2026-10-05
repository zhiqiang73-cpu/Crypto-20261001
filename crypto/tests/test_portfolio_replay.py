"""仅离线虚拟事件：不导入运行器、不联网、不触碰 runtime。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.replay_portfolio_events import replay_file
from shadow.engine import IntervalSpec, OpenPosition, _stop_hit
from shadow.portfolio_replay import (Poll, PortfolioReplay, ReplayConfig, Signal,
                                     ohlc_path_polls)

BTC, ETH = "BTCUSDT", "ETHUSDT"


def poll(ms, btc=100., eth=100., *, btc_atr=10., eth_atr=10., fills=None,
         prices=None, **kwargs):
    return Poll(ms=ms, mark={BTC: btc, ETH: eth},
                current_atr_1h={BTC: btc_atr, ETH: eth_atr},
                trade_px=prices if prices is not None else {BTC: btc, ETH: eth},
                fill_fraction=fills or {}, **kwargs)


def sig(ms, leg="kdj15", side=1, qty=1., atr=10., hist=None):
    return Signal(ms, leg, side, qty, atr, hist)


class PortfolioReplayTest(unittest.TestCase):
    def cfg(self, **kw):
        return ReplayConfig(initial_equity=1000., fee_per_side=0., slippage_bps=0., **kw)

    def test_default_is_two_active_15m_not_disabled_5m(self):
        self.assertEqual(ReplayConfig().enabled_legs, ("kdj15", "eth15"))
        with self.assertRaises(ValueError):
            PortfolioReplay(self.cfg()).run([sig(0, "kdj5")], [poll(0)])

    def test_four_leg_netting_and_symbol_isolation(self):
        c = self.cfg(enabled_legs=("kdj15", "kdj5", "eth15", "eth5"))
        signals = [sig(0, "kdj15", 1, 1), sig(0, "kdj5", -1, .5),
                   sig(0, "eth15", -1, 2), sig(0, "eth5", 1, .4)]
        r = PortfolioReplay(c).run(signals, [poll(0, fills={BTC: 1, ETH: 1})])
        self.assertAlmostEqual(r.positions[BTC].qty, .5)
        self.assertAlmostEqual(r.positions[ETH].qty, -1.6)
        self.assertAlmostEqual(r.pending_targets[BTC], .5)
        self.assertAlmostEqual(r.pending_targets[ETH], -1.6)
        self.assertIsNone(r.legs["kdj5"].entry_px)  # 逆向虚拟腿没有独立真实入场价
        self.assertAlmostEqual(r.legs["kdj15"].entry_px, 100)

    def test_equity_fees_funding_slippage_and_short_accounting(self):
        c = ReplayConfig(initial_equity=1000., fee_per_side=.001, slippage_bps=10.)
        signals = [sig(0, "kdj15", 1, 1), sig(0, "eth15", -1, 1)]
        r = PortfolioReplay(c).run(signals, [
            poll(0, fills={BTC: 1, ETH: 1}),
            poll(15_000, btc=110, eth=90, funding_cash={BTC: -.5, ETH: .2}),
            poll(30_000, btc=110, eth=90),
        ])
        self.assertAlmostEqual(r.positions[BTC].entry_px, 100.1)
        self.assertAlmostEqual(r.positions[ETH].entry_px, 99.9)
        self.assertAlmostEqual(r.fees, .2, places=3)
        self.assertAlmostEqual(r.funding, -.3)
        self.assertAlmostEqual(r.slippage_cost, .2)
        self.assertAlmostEqual(r.equity, 1000 - .2 - .3 + 9.9 + 9.9, places=4)
        self.assertEqual(len(r.equity_curve), 3)

    def test_funding_rate_signed_and_cash_cannot_be_double_counted(self):
        c = self.cfg()
        r = PortfolioReplay(c).run([sig(0, qty=2)], [
            poll(0, fills={BTC: 1}),
            poll(15_000, funding_rate={BTC: .01}),
        ])
        self.assertAlmostEqual(r.funding, -2.)
        with self.assertRaises(ValueError):
            PortfolioReplay(c).run([], [poll(0, funding_rate={BTC: .01},
                                            funding_cash={BTC: -2.})])

    def test_no_fill_means_no_exposure_no_pnl_even_when_virtual_target_exists(self):
        r = PortfolioReplay(self.cfg()).run([sig(0)], [poll(0), poll(15_000, btc=65)])
        self.assertEqual(r.positions[BTC].qty, 0)
        self.assertEqual(r.equity, 1000)
        self.assertEqual(r.legs["kdj15"].entry_px, None)
        self.assertGreaterEqual(len([e for e in r.events if e["kind"] == "unconfirmed_or_unfilled"]), 1)

    def test_current_atr_mark_poll_vs_entry_atr_ohlc_old_engine(self):
        old = OpenPosition(1, 0, 100., 1., 0., 0., 0., 10., 0., 0., 0.)
        self.assertEqual(_stop_hit(old, 105, 65,
                         IntervalSpec("15m", 900_000, disaster_atr_mult=3)),
                         (70., "灾难止损"))
        # 相同一根 trade-OHLC 的 low=65，不代表采样 mark 命中过 70。
        replay = PortfolioReplay(self.cfg()).run([sig(0)], [
            poll(0, fills={BTC: 1}), poll(15_000, btc=90, btc_atr=10.),
        ])
        self.assertEqual(replay.positions[BTC].qty, 1.)
        self.assertNotIn("disaster_priority", [e["kind"] for e in replay.events])
        # mark=84 / 当前 ATR=5 已达净仓 3×5；用入场 ATR=10 则不会触发。
        fixed_atr = PortfolioReplay(self.cfg()).run([sig(0)], [
            poll(0, fills={BTC: 1}), poll(15_000, btc=84, btc_atr=10., fills={BTC: 1})])
        rolling_atr = PortfolioReplay(self.cfg()).run([sig(0)], [
            poll(0, fills={BTC: 1}), poll(15_000, btc=84, btc_atr=5., fills={BTC: 1})])
        self.assertEqual(fixed_atr.positions[BTC].qty, 1.)
        self.assertEqual(rolling_atr.positions[BTC].qty, 0.)

    def test_disaster_partial_fill_latched_even_if_mark_recovers(self):
        c = self.cfg()
        r = PortfolioReplay(c).run([sig(0, qty=1)], [
            poll(0, fills={BTC: 1}),
            poll(15_000, btc=84, btc_atr=5., fills={BTC: .5}),
            poll(30_000, btc=90, btc_atr=10., fills={BTC: 1}),
        ])
        partial = [e for e in r.events if e["kind"] == "partial_residual"]
        self.assertEqual(len(partial), 1)
        self.assertAlmostEqual(partial[0]["net"], .5)
        self.assertAlmostEqual(r.positions[BTC].qty, 0.)
        self.assertIsNone(r.legs["kdj15"])
        self.assertEqual(len([e for e in r.events if e["kind"] == "disaster_priority"]), 2)

    def test_weighted_exchange_entry_not_first_virtual_entry(self):
        c = self.cfg(enabled_legs=("kdj15", "kdj5"))
        r = PortfolioReplay(c).run([sig(0), sig(15_000, "kdj5")], [
            poll(0, fills={BTC: 1}),
            poll(15_000, btc=110, fills={BTC: 1}),
            poll(30_000, btc=90, btc_atr=5, fills={BTC: 1}),
        ])
        fill = [e for e in r.events if e["kind"] == "fill"]
        self.assertAlmostEqual(fill[1]["entry"], 105.)
        self.assertEqual(fill[-1]["net"], 0.)
        self.assertIn("disaster_priority", [e["kind"] for e in r.events])

    def test_disaster_preempts_candidate_normal_stop_on_gap(self):
        c = self.cfg(normal_stop_atr=1.5)
        r = PortfolioReplay(c).run([sig(0)], [
            poll(0, fills={BTC: 1}),
            poll(15_000, btc=84, btc_atr=5, fills={BTC: 1}),
        ])
        kinds = [e["kind"] for e in r.events]
        self.assertIn("disaster_priority", kinds)
        self.assertNotIn("normal_stop", kinds)

    def test_signal_delay_is_next_eligible_15_second_poll(self):
        c = self.cfg(signal_delay_ms=17_000)
        r = PortfolioReplay(c).run([sig(0)], [
            poll(0, fills={BTC: 1}), poll(15_000, btc=101, fills={BTC: 1}),
            poll(30_000, btc=105, fills={BTC: 1}),
        ])
        self.assertEqual([p["btc_net"] for p in r.equity_curve], [0, 0, 1])
        self.assertEqual(r.positions[BTC].entry_px, 105)
        self.assertEqual(next(e["delay_ms"] for e in r.events
                              if e["kind"] == "signal_target"), 30_000)

    def test_multiple_missed_bars_only_latest_signal_applies(self):
        r = PortfolioReplay(self.cfg()).run([
            sig(0, side=1), sig(900_000, side=-1),
        ], [poll(1_800_000, fills={BTC: 1})])
        self.assertEqual(r.missed_signals, 1)
        self.assertEqual(r.positions[BTC].qty, -1)

    def test_macd_opposition_is_discarded_before_any_target_change(self):
        r = PortfolioReplay(self.cfg()).run([sig(0, hist=-.1)],
                                              [poll(0, fills={BTC: 1})])
        self.assertEqual(r.positions[BTC].qty, 0.)
        self.assertIn("macd_divergence_discarded", [e["kind"] for e in r.events])

    def test_partial_signal_fill_and_rounded_residual(self):
        r = PortfolioReplay(self.cfg()).run([sig(0, qty=.005)], [
            poll(0, fills={BTC: .4}),       # .002
            poll(15_000, fills={BTC: .5}),  # .001
            poll(30_000, fills={BTC: 1}),   # 剩余 .002
        ])
        self.assertEqual([round(p["btc_net"], 3) for p in r.equity_curve],
                         [.002, .003, .005])
        self.assertEqual(len([e for e in r.events if e["kind"] == "partial_residual"]), 2)

    def test_below_step_target_is_logged_not_invented_as_fill(self):
        r = PortfolioReplay(self.cfg()).run([sig(0, qty=.0005)],
                                             [poll(0, fills={BTC: 1})])
        self.assertEqual(r.positions[BTC].qty, 0.)
        self.assertIn("below_step_residual", [e["kind"] for e in r.events])

    def test_missing_atr_prevents_residual_expansion_without_hiding_gap(self):
        c = self.cfg()
        stale = Poll(15_000, {BTC: 99., ETH: 100.}, {ETH: 10.},
                     trade_px={BTC: 99., ETH: 100.}, fill_fraction={BTC: 1.})
        r = PortfolioReplay(c).run([sig(0, qty=1)], [
            poll(0, fills={BTC: .5}), stale,
        ])
        self.assertEqual(r.positions[BTC].qty, .5)
        self.assertIn("missing_current_atr_risk_gap", [e["kind"] for e in r.events])

    def test_account_level_drawdown_shared_and_exits_not_blocked(self):
        c = self.cfg()
        r = PortfolioReplay(c).run([sig(0, qty=5), sig(0, "eth15", 1, 5),
                                    sig(15_000, side=0, qty=0),
                                    sig(15_000, "eth15", -1, 5)], [
            poll(0, fills={BTC: 1, ETH: 1}),
            poll(15_000, btc=90, eth=90, btc_atr=100, eth_atr=100,
                 fills={BTC: 1, ETH: 1}),
        ])
        self.assertTrue(r.halted)
        self.assertTrue(r.daily_blocked)
        self.assertEqual(r.positions[BTC].qty, 0.)  # 仍准正常平仓
        self.assertEqual(r.positions[ETH].qty, 0.)  # 对侧信号只平，不反手
        self.assertIn("signal_entry_blocked", [e["kind"] for e in r.events])

    def test_daily_resets_utc_but_total_drawdown_latches(self):
        c = self.cfg()
        r = PortfolioReplay(c).run([sig(0), sig(86_400_000, "eth15")], [
            poll(0, fills={BTC: 1}),
            poll(15_000, btc=69, btc_atr=100),
            poll(86_400_000, btc=69, btc_atr=100, fills={ETH: 1}),
        ])
        self.assertEqual(r.positions[ETH].qty, 1.)
        self.assertFalse(r.daily_blocked)
        self.assertFalse(r.halted)

    def test_live_daily_gate_rechecks_after_rebound_vs_candidate_latch(self):
        signals = [sig(0), sig(30_000, "eth15")]
        polls = [poll(0, fills={BTC: 1}),
                 poll(15_000, btc=69, btc_atr=100),
                 poll(30_000, btc=100, btc_atr=100, fills={ETH: 1})]
        live_like = PortfolioReplay(self.cfg()).run(signals, polls)
        latched = PortfolioReplay(self.cfg(daily_latch=True)).run(signals, polls)
        self.assertEqual(live_like.positions[ETH].qty, 1.)
        self.assertEqual(latched.positions[ETH].qty, 0.)
        self.assertFalse(live_like.daily_blocked)
        self.assertTrue(latched.daily_blocked)

    def test_gross_risk_budget_ignores_virtual_hedge_offset(self):
        c = self.cfg(enabled_legs=("kdj15", "kdj5", "eth15"),
                     normal_stop_atr=1.5, portfolio_risk_limit=.02)
        r = PortfolioReplay(c).run([sig(0, "kdj15", 1), sig(0, "kdj5", -1)],
                                   [poll(0, fills={BTC: 1})])
        self.assertEqual(r.positions[BTC].qty, 1.)
        self.assertIsNone(r.legs["kdj5"])
        self.assertIn("gross_risk_budget", [e.get("reason") for e in r.events])

    def test_stop_priority_before_signal_and_same_poll_no_reverse(self):
        c = self.cfg(normal_stop_atr=1.5)
        r = PortfolioReplay(c).run([sig(0), sig(15_000, side=-1)], [
            poll(0, fills={BTC: 1}), poll(15_000, btc=84, btc_atr=10., fills={BTC: 1}),
        ])
        self.assertEqual(r.positions[BTC].qty, 0)
        self.assertIn("normal_stop", [e["kind"] for e in r.events])
        self.assertIn("signal_suppressed_by_stop", [e["kind"] for e in r.events])

    def test_normal_stop_cannot_reexpand_stale_hedge_next_poll(self):
        c = self.cfg(enabled_legs=("kdj15", "kdj5"), normal_stop_atr=1.5)
        r = PortfolioReplay(c).run([sig(0, "kdj15", 1, 1),
                                    sig(0, "kdj5", -1, .5)], [
            poll(0, fills={BTC: 1}),
            poll(15_000, btc=84, fills={BTC: 1}),
            poll(30_000, btc=100, fills={BTC: 1}),
        ])
        self.assertEqual([round(p["btc_net"], 3) for p in r.equity_curve], [.5, 0., 0.])
        self.assertEqual(r.pending_targets[BTC], 0.)
        self.assertIsNotNone(r.legs["kdj5"])  # 只冻结旧虚拟腿扩仓，不伪造其成交

    def test_ambiguous_partial_fill_stop_flattens_unattributed_net(self):
        c = self.cfg(enabled_legs=("kdj15", "kdj5"), normal_stop_atr=1.5)
        r = PortfolioReplay(c).run([sig(0, "kdj15", 1, 1, atr=10),
                                    sig(0, "kdj5", 1, 1, atr=20)], [
            poll(0, fills={BTC: .25}),  # 2 个虚拟腿只确认 .5 净仓
            poll(15_000, btc=84, fills={BTC: 1}),
        ])
        self.assertEqual(r.positions[BTC].qty, 0.)
        self.assertIn("ambiguous_stop_allocation", [e["kind"] for e in r.events])
        self.assertIsNotNone(r.legs["kdj5"])

    def test_ohlc_same_bar_paths_change_break_even_vs_normal_stop(self):
        c = self.cfg(normal_stop_atr=1.5, break_even_trigger_atr=1.5)
        histories = {}
        for path in ("high_first", "low_first"):
            polls = [poll(0, fills={BTC: 1})] + ohlc_path_polls(
                start_ms=15_000, bar_ms=300_000,
                ohlc={BTC: (100., 120., 80., 100.), ETH: (100., 100., 100., 100.)},
                current_atr_1h={BTC: 10., ETH: 10.}, path=path,
                fill_fraction={BTC: 1},
            )
            r = PortfolioReplay(c).run([sig(0)], polls)
            histories[path] = [e["kind"] for e in r.events]
        self.assertIn("break_even_armed", histories["high_first"])
        self.assertIn("break_even", histories["high_first"])
        self.assertIn("normal_stop", histories["low_first"])
        self.assertNotIn("break_even_armed", histories["low_first"])

    def test_price_break_even_is_not_fee_inclusive_and_buffer_is_explicit(self):
        sequence = [poll(0, fills={BTC: 1}),
                    poll(15_000, btc=116., fills={BTC: 1}),
                    poll(30_000, btc=100.5, fills={BTC: 1})]
        price_be = PortfolioReplay(self.cfg(break_even_trigger_atr=1.5)).run(
            [sig(0)], sequence)
        buffered = PortfolioReplay(self.cfg(break_even_trigger_atr=1.5,
                                            break_even_buffer_bps=100)).run(
            [sig(0)], sequence)
        self.assertEqual(price_be.positions[BTC].qty, 1.)
        self.assertEqual(buffered.positions[BTC].qty, 0.)
        be = next(e for e in buffered.events if e["kind"] == "break_even")
        self.assertEqual(be["protected_px"], 101.)

    def test_confirmed_fill_uses_exact_commission_deduplicates(self):
        r = PortfolioReplay(self.cfg()).run([sig(0)], [
            poll(0),
            poll(15_000, confirmed_fill={BTC: .4}, fill_id={BTC: "test-1"},
                 commission_cash={BTC: .05}),
            poll(30_000, confirmed_fill={BTC: .4}, fill_id={BTC: "test-1"},
                 commission_cash={BTC: .05}),
            poll(45_000, confirmed_fill={BTC: .6}, fill_id={BTC: "test-2"},
                 commission_cash={BTC: .06}),
        ])
        self.assertEqual(r.positions[BTC].qty, 1.)
        self.assertAlmostEqual(r.fees, .11)
        self.assertIn("duplicate_fill_ignored", [e["kind"] for e in r.events])
        with self.assertRaises(ValueError):
            PortfolioReplay(self.cfg()).run([sig(0)], [
                poll(0), poll(15_000, confirmed_fill={BTC: 2},
                              fill_id={BTC: "bad"}, commission_cash={BTC: .1})])

    def test_realized_equity_identity_after_close_with_cost_and_funding(self):
        c = ReplayConfig(initial_equity=1000., fee_per_side=.001, slippage_bps=0.)
        r = PortfolioReplay(c).run([sig(0), sig(0, "eth15", -1),
                                    sig(15_000, side=0, qty=0),
                                    sig(15_000, "eth15", 0, 0)], [
            poll(0, fills={BTC: 1, ETH: 1}),
            poll(15_000, btc=110, eth=90, fills={BTC: 1, ETH: 1},
                 funding_cash={BTC: -1.}),
        ])
        self.assertEqual(r.positions[BTC].qty, 0.)
        self.assertEqual(r.positions[ETH].qty, 0.)
        self.assertAlmostEqual(r.realized_pnl, 20.)
        self.assertAlmostEqual(r.fees, .4)
        self.assertAlmostEqual(r.equity, 1000 + r.realized_pnl - r.fees + r.funding)

    def test_input_validation_gap_fail_closed(self):
        with self.assertRaises(ValueError):
            PortfolioReplay(self.cfg()).run([], [Poll(0, {BTC: 100}, {BTC: 10})])
        with self.assertRaises(ValueError):
            PortfolioReplay(self.cfg()).run([], [poll(0), poll(1_000)])
        with self.assertRaises(ValueError):
            PortfolioReplay(self.cfg()).run([sig(0, atr=-1)], [poll(0)])
        with self.assertRaises(ValueError):
            ohlc_path_polls(start_ms=0, bar_ms=300_000, path="high_first",
                            ohlc={BTC: (100, 90, 80, 100), ETH: (100, 110, 90, 100)},
                            current_atr_1h={BTC: 10, ETH: 10})

    def test_json_entry_is_offline_and_refuses_runtime_output(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, output = base / "fixture.json", base / "result.json"
            source.write_text(json.dumps({
                "config": {"initial_equity": 1000, "fee_per_side": 0,
                           "slippage_bps": 0},
                "signals": [{"close_ms": 0, "leg_id": "kdj15", "side": 1,
                             "qty": 1, "atr_1h": 10}],
                "polls": [{"ms": 0, "mark": {BTC: 100, ETH: 100},
                           "current_atr_1h": {BTC: 10, ETH: 10},
                           "trade_px": {BTC: 100}, "fill_fraction": {BTC: 1}}],
            }), encoding="utf-8")
            out = replay_file(source, output)
            self.assertTrue(output.is_file())
            self.assertEqual(out["result"]["positions"][BTC]["qty"], 1)
            self.assertEqual(out["scope"], "offline_hypothetical_not_trading_or_verified_pnl")
            with self.assertRaises(ValueError):
                replay_file(source, base / "runtime" / "forbidden.json")
            self.assertFalse((base / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
