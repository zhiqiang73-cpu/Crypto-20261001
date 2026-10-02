"""双策略虚拟账本：15m 交叉+MACD 能量柱闸门，5m 交叉+K 极值，共用净仓。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import numpy as np

import shadow.deploy as deploy
from shadow.deploy import process_strategy, save_signal_reading
from shadow.signals import (BREAK_ATR_MULT, confirmed_signal, crossing,
                            entry_signal, price_breaks)
from unittest.mock import patch

from shadow.engine import ATR_MULT_K, RISK_R, floor_step
from shadow.strategy_books import (SPEC_5M, SPEC_15M, SPEC_ETH_5M,
                                   SPEC_ETH_15M, apply_virtual_signal,
                                   contra_5m_qty, desired_net, empty_book,
                                   migrate_state, position_sources,
                                   reduce_only_for_delta, runtime_view,
                                   signal_reason, specs_for_symbol,
                                   trend_side)


class TestEntrySignal(unittest.TestCase):
    def test_15m_cross_without_k_filter(self):
        self.assertEqual(entry_signal(81, 83, 86, 84), (True, False, True, False))
        self.assertEqual(entry_signal(18, 16, 12, 14), (False, True, False, True))

    def test_5m_gold_needs_k_below_30(self):
        self.assertEqual(
            entry_signal(20, 25, 28, 26, k_long_max=30, k_short_min=70),
            (True, False, True, False),
        )
        self.assertEqual(
            entry_signal(81, 83, 86, 84, k_long_max=30, k_short_min=70),
            (False, False, True, False),
        )

    def test_5m_dead_needs_k_above_70(self):
        self.assertEqual(
            entry_signal(80, 75, 72, 74, k_long_max=30, k_short_min=70),
            (False, True, False, True),
        )
        self.assertEqual(
            entry_signal(18, 16, 12, 14, k_long_max=30, k_short_min=70),
            (False, False, False, True),
        )

    def test_wrapper_matches_crossing_when_no_threshold(self):
        cases = ((50, 50, 51, 50), (50, 50, 49, 50), (73, 61, 75, 65))
        for args in cases:
            gold, dead = crossing(*args)
            sig_long, sig_short, g2, d2 = entry_signal(*args)
            self.assertEqual((g2, d2), (gold, dead))
            self.assertEqual((sig_long, sig_short), (gold, dead))


class TestVirtualBooks(unittest.TestCase):
    def test_migrate_copies_15m_and_cold_starts_5m(self):
        st = migrate_state({
            "last_ts": 123, "entry": {"side": -1, "qty": 0.003, "px": 1},
            "missed_bars": 2, "missed_signals": 1, "halted": False,
        })
        self.assertEqual(st["strategies"]["kdj15"]["last_ts"], 123)
        self.assertEqual(st["strategies"]["kdj15"]["entry"]["qty"], 0.003)
        self.assertTrue(st["strategies"]["kdj15"]["armed"])
        self.assertFalse(st["strategies"]["kdj5"]["armed"])
        self.assertIsNone(st["strategies"]["kdj5"]["entry"])
        self.assertEqual(st["strategies"]["kdj5"]["last_ts"], 0)
        self.assertFalse(st["strategies"]["eth15"]["armed"])
        self.assertFalse(st["strategies"]["eth5"]["armed"])
        self.assertIsNone(st["strategies"]["eth15"]["entry"])

    def test_desired_net_sums_signed_qty(self):
        st = {
            "strategies": {
                "kdj15": {"entry": {"side": 1, "qty": 0.003}},
                "kdj5": {"entry": {"side": -1, "qty": 0.001}},
                "eth15": {"entry": {"side": 1, "qty": 2.5}},
            }
        }
        self.assertAlmostEqual(desired_net(st), 0.002)
        self.assertAlmostEqual(desired_net(st, "ETHUSDT"), 2.5)

    def test_apply_virtual_reverses_without_exchange(self):
        book = empty_book()
        action, _, changed = apply_virtual_signal(
            book, sig_long=False, sig_short=True, qty=0.003,
            px=100, atr=10, ms=1, block=False, min_qty=0.001,
        )
        self.assertEqual(action, "开空")
        self.assertTrue(changed)
        action, note, changed = apply_virtual_signal(
            book, sig_long=True, sig_short=False, qty=0.003,
            px=101, atr=10, ms=2, block=False, min_qty=0.001,
        )
        self.assertEqual(action, "平仓并开多")
        self.assertTrue(changed)
        self.assertEqual(book["entry"]["side"], 1)

    def test_apply_virtual_keeps_open_reason(self):
        book = empty_book()
        apply_virtual_signal(
            book, sig_long=True, sig_short=False, qty=0.003,
            px=100, atr=10, ms=1_790_927_100_000, block=False, min_qty=0.001,
            meta={"reason": "金叉做多（K=34.8）", "interval": "15m", "k": 34.8},
        )
        self.assertEqual(book["entry"]["reason"], "金叉做多（K=34.8）")
        self.assertEqual(book["entry"]["interval"], "15m")

    def test_block_prevents_new_but_allows_flatten(self):
        book = empty_book()
        action, note, changed = apply_virtual_signal(
            book, sig_long=True, sig_short=False, qty=0.003,
            px=100, atr=10, ms=1, block=True, min_qty=0.001,
        )
        self.assertFalse(changed)
        self.assertIn("风控", note)
        book["entry"] = {"side": 1, "qty": 0.003, "px": 100}
        action, note, changed = apply_virtual_signal(
            book, sig_long=False, sig_short=True, qty=0.003,
            px=90, atr=10, ms=2, block=True, min_qty=0.001,
        )
        self.assertEqual(action, "平仓")
        self.assertTrue(changed)
        self.assertIsNone(book["entry"])

    def test_reduce_only_when_shrinking_or_flattening(self):
        self.assertTrue(reduce_only_for_delta(0.005, 0.003, 0.001))
        self.assertTrue(reduce_only_for_delta(0.003, 0.0, 0.001))
        self.assertFalse(reduce_only_for_delta(0.003, 0.005, 0.001))
        self.assertFalse(reduce_only_for_delta(0.003, -0.002, 0.001))
        self.assertFalse(reduce_only_for_delta(0.0, 0.003, 0.001))

    def test_runtime_view_is_per_strategy(self):
        st = migrate_state({
            "last_ts": 9, "entry": {"side": 1, "qty": 0.002},
            "halted": True,
        })
        st["strategies"]["kdj5"]["entry"] = {"side": -1, "qty": 0.001}
        st["strategies"]["kdj5"]["armed"] = True
        v15 = runtime_view(st, "deployed_kdj_extreme_v1")
        v5 = runtime_view(st, "deployed_kdj_5m_extreme_v1", runtime_key="kdj5")
        self.assertEqual(v15["runtime_key"], "kdj15")
        self.assertEqual(v15["entry"]["side"], 1)
        self.assertEqual(v5["entry"]["side"], -1)
        self.assertAlmostEqual(v15["desired_net"], 0.001)
        self.assertTrue(v15["halted"])

    def test_signal_reason_keeps_threshold_text(self):
        self.assertIn("金叉做多", signal_reason(SPEC_15M, gold=True, dead=False, k=34.8))
        self.assertNotIn("确认", signal_reason(SPEC_15M, gold=True, dead=False, k=34.8))
        self.assertIn("K<30", signal_reason(SPEC_5M, gold=True, dead=False, k=14.9))
        self.assertIn("K>70", signal_reason(SPEC_5M, gold=False, dead=True, k=90.7))
        self.assertIn("金叉做多", signal_reason(SPEC_ETH_15M, gold=True, dead=False, k=64.0))
        self.assertNotIn("K<30", signal_reason(SPEC_ETH_15M, gold=True, dead=False, k=64.0))
        self.assertIn("K<30", signal_reason(SPEC_ETH_5M, gold=True, dead=False, k=25.4))

    def test_position_sources_reads_reason_from_trade_log(self):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        tmp.write(
            "时间,动作,方向,数量,价格,净盈亏,K,D,ATR_1H,倍数,权益,说明\n"
            "2026-10-02 07:45,15m平仓并开多,多,0.1870,86034.00,,34.77,30.08,405.41,,5077.69,平仓; 反手 0.1870\n"
        )
        tmp.close()
        st = {
            "strategies": {
                "kdj15": {
                    "entry": {"side": 1, "qty": 0.187, "px": 86034.0,
                              "ms": 1_790_927_100_000},
                },
                "kdj5": {"entry": None},
            }
        }
        src = position_sources(st, trade_log_path=tmp.name)
        os.unlink(tmp.name)
        self.assertEqual(len(src), 1)
        self.assertEqual(src[0]["interval"], "15m")
        self.assertEqual(src[0]["symbol"], "BTCUSDT")
        self.assertIn("金叉做多", src[0]["reason"])
        self.assertAlmostEqual(src[0]["k"], 34.77)


class TestConfirmNextBar(unittest.TestCase):
    def test_dead_cross_waits_then_shorts_if_still_below(self):
        sig_long, sig_short, gold, dead, note = confirmed_signal(
            70, 68, 60.4, 61.2, 58.0, 60.0,
        )
        self.assertFalse(sig_long)
        self.assertTrue(sig_short)
        self.assertFalse(gold)
        self.assertFalse(dead)
        self.assertIn("已确认", note)

    def test_pierce_does_not_short(self):
        sig_long, sig_short, gold, dead, note = confirmed_signal(
            70, 68, 60.4, 61.2, 66.4, 63.0,
        )
        self.assertFalse(sig_long)
        self.assertFalse(sig_short)
        self.assertTrue(gold)
        self.assertIn("未站稳", note)

    def test_cross_bar_only_marks_pending(self):
        sig_long, sig_short, gold, dead, note = confirmed_signal(
            70, 68, 70, 68, 60.4, 61.2,
        )
        self.assertFalse(sig_long)
        self.assertFalse(sig_short)
        self.assertTrue(dead)
        self.assertIn("待确认", note)

    def test_15m_does_not_wait_for_next_close(self):
        self.assertFalse(SPEC_15M.confirm_next)
        self.assertFalse(SPEC_ETH_15M.confirm_next)
        # 2026-10-02: 15m 方向过滤从「价格突破上一根高低点」换成「MACD 能量柱正负」。
        self.assertFalse(SPEC_15M.require_break)
        self.assertFalse(SPEC_ETH_15M.require_break)
        self.assertTrue(SPEC_15M.require_macd)
        self.assertTrue(SPEC_ETH_15M.require_macd)
        self.assertFalse(SPEC_5M.require_break)
        self.assertFalse(SPEC_5M.require_macd)
        self.assertFalse(SPEC_ETH_5M.require_macd)


class TestPriceBreak(unittest.TestCase):
    def test_shallow_dead_does_not_break(self):
        # 17:15: 收盘只低于上根低点 29，ATR≈407，0.15×ATR≈61
        self.assertFalse(price_breaks(86097.2, 86269.0, 86126.2, 407.2, want=-1))

    def test_real_dead_breaks(self):
        # 13:15: 跌破上根低点 226 > 61
        self.assertTrue(price_breaks(86299.9, 86624.6, 86525.8, 417.4, want=-1))

    def test_gold_needs_clear_up_break(self):
        self.assertTrue(price_breaks(86034.0, 85965.6, 85753.2, 405.4, want=1))
        self.assertFalse(price_breaks(85980.0, 85965.6, 85753.2, 405.4, want=1))
        self.assertAlmostEqual(BREAK_ATR_MULT, 0.15)


class TestContraHalfSize(unittest.TestCase):
    def test_5m_halves_when_against_15m(self):
        qty, note = contra_5m_qty(0.190, interval="5m", want=-1, trend=1)
        self.assertAlmostEqual(qty, 0.095)
        self.assertIn("减半", note)

    def test_5m_full_when_with_15m_or_flat(self):
        qty, note = contra_5m_qty(0.190, interval="5m", want=-1, trend=-1)
        self.assertAlmostEqual(qty, 0.190)
        self.assertEqual(note, "")
        qty, note = contra_5m_qty(0.190, interval="5m", want=-1, trend=0)
        self.assertAlmostEqual(qty, 0.190)
        self.assertEqual(note, "")

    def test_15m_never_halves(self):
        qty, note = contra_5m_qty(0.187, interval="15m", want=1, trend=-1)
        self.assertAlmostEqual(qty, 0.187)
        self.assertEqual(note, "")

    def test_trend_side_reads_15m_only(self):
        st = {
            "strategies": {
                "kdj15": {"entry": {"side": 1, "qty": 0.187}},
                "kdj5": {"entry": {"side": -1, "qty": 0.191}},
                "eth15": {"entry": {"side": -1, "qty": 4.722}},
                "eth5": {"entry": {"side": 1, "qty": 2.356}},
            }
        }
        self.assertEqual(trend_side(st, "BTCUSDT"), 1)
        self.assertEqual(trend_side(st, "ETHUSDT"), -1)

    def test_eth5_also_halves_against_eth15(self):
        qty, note = contra_5m_qty(4.712, interval=SPEC_ETH_5M.interval,
                                  want=1, trend=-1)
        self.assertAlmostEqual(qty, 2.356)
        self.assertIn("减半", note)
        qty, note = contra_5m_qty(4.722, interval=SPEC_ETH_15M.interval,
                                  want=-1, trend=1)
        self.assertAlmostEqual(qty, 4.722)
        self.assertEqual(note, "")

    def test_each_symbol_processes_15m_before_5m(self):
        self.assertEqual(
            [spec.interval for spec in specs_for_symbol("BTCUSDT")],
            ["15m", "5m"],
        )
        self.assertEqual(
            [spec.interval for spec in specs_for_symbol("ETHUSDT")],
            ["15m", "5m"],
        )


class TestEthColdStart(unittest.TestCase):
    def test_eth_15m_does_not_trade_historical_bars(self):
        from shadow.strategy_books import SPEC_ETH_15M
        tmp = tempfile.TemporaryDirectory()
        old = deploy.READING_ETH15
        deploy.READING_ETH15 = os.path.join(tmp.name, "latest_reading_eth15.json")
        try:
            n = 40
            ts = np.arange(n, dtype=np.int64) * SPEC_ETH_15M.interval_ms + 1_800_000_000_000
            px = np.linspace(2500.0, 2600.0, n)
            bars = {
                "ts": ts, "open": px, "high": px + 1, "low": px - 1,
                "close": px, "volume": np.ones(n),
            }
            atr = np.full(n, 40.0)
            book = empty_book()
            changed = process_strategy(
                SPEC_ETH_15M, book, bars, atr, equity=5000.0, block=False,
                now=int(ts[-1] + SPEC_ETH_15M.interval_ms), execute=False,
            )
            self.assertFalse(changed)
            self.assertTrue(book["armed"])
            self.assertEqual(book["last_ts"], int(ts[-1]))
            self.assertIsNone(book["entry"])
        finally:
            deploy.READING_ETH15 = old
            tmp.cleanup()


class TestFiveMinuteColdStart(unittest.TestCase):
    def test_process_strategy_5m_does_not_trade_historical_bars(self):
        tmp = tempfile.TemporaryDirectory()
        old = deploy.READING_5M
        deploy.READING_5M = os.path.join(tmp.name, "latest_reading_5m.json")
        try:
            n = 40
            ts = np.arange(n, dtype=np.int64) * SPEC_5M.interval_ms + 1_800_000_000_000
            px = np.linspace(100.0, 110.0, n)
            bars = {
                "ts": ts, "open": px, "high": px + 1, "low": px - 1,
                "close": px, "volume": np.ones(n),
            }
            atr = np.full(n, 12.0)
            book = empty_book()
            changed = process_strategy(
                SPEC_5M, book, bars, atr, equity=5000.0, block=False,
                now=int(ts[-1] + SPEC_5M.interval_ms), execute=False,
            )
            self.assertFalse(changed)
            self.assertTrue(book["armed"])
            self.assertEqual(book["last_ts"], int(ts[-1]))
            self.assertIsNone(book["entry"])
        finally:
            deploy.READING_5M = old
            tmp.cleanup()


class TestProcessStrategyFourRules(unittest.TestCase):
    """把四条策略的开仓规则接到 process_strategy 上核对。"""

    def _bars(self, n=8, interval_ms=300_000, px=2700.0):
        ts = np.arange(n, dtype=np.int64) * interval_ms + 1_800_000_000_000
        price = np.full(n, px)
        return {
            "ts": ts, "open": price, "high": price + 1, "low": price - 1,
            "close": price, "volume": np.ones(n),
        }, np.full(n, 16.409)

    def _armed(self, bars):
        book = empty_book()
        book["armed"] = True
        book["last_ts"] = int(bars["ts"][-2])
        return book

    def _macd(self, bars, hist_value):
        """把 MACD 能量柱固定成指定值, 单独验证闸门方向判定。"""
        n = len(bars["ts"])
        hist = np.full(n, float(hist_value))
        zero = np.zeros(n)
        return patch("shadow.deploy.macd", return_value=(zero, zero, hist))

    def test_eth5_opens_half_when_against_15m_short(self):
        tmp = tempfile.TemporaryDirectory()
        old = deploy.READING_ETH5
        old_log = deploy.TRADE_LOG
        deploy.READING_ETH5 = os.path.join(tmp.name, "eth5.json")
        deploy.TRADE_LOG = os.path.join(tmp.name, "trades.csv")
        bars, atr = self._bars()
        n = len(bars["ts"])
        k = np.full(n, 20.0)
        d = np.full(n, 22.0)
        k[-1], d[-1] = 25.38, 20.97
        equity = 5155.61
        try:
            with patch("shadow.deploy.kdj", return_value=(k, d, k)):
                book = self._armed(bars)
                process_strategy(
                    SPEC_ETH_5M, book, bars, atr, equity=equity,
                    block=False, now=int(bars["ts"][-1] + 300_000),
                    execute=False, trend=-1,
                )
            full = floor_step(equity * RISK_R / (ATR_MULT_K * 16.409))
            self.assertEqual(book["entry"]["side"], 1)
            self.assertAlmostEqual(book["entry"]["qty"], full * 0.5, places=3)
        finally:
            deploy.READING_ETH5 = old
            deploy.TRADE_LOG = old_log
            tmp.cleanup()

    def test_btc15_discards_dead_cross_when_hist_positive(self):
        """死叉但 MACD 是红柱 → 方向背离, 丢弃不操作。"""
        tmp = tempfile.TemporaryDirectory()
        old = deploy.READING
        old_log = deploy.TRADE_LOG
        deploy.READING = os.path.join(tmp.name, "btc15.json")
        deploy.TRADE_LOG = os.path.join(tmp.name, "trades.csv")
        bars, atr = self._bars(interval_ms=SPEC_15M.interval_ms, px=86150.0)
        n = len(bars["ts"])
        atr[:] = 407.2
        k = np.full(n, 70.0)
        d = np.full(n, 68.0)
        k[-1], d[-1] = 60.4, 61.2
        try:
            with patch("shadow.deploy.kdj", return_value=(k, d, k)), \
                    self._macd(bars, 12.5):
                book = self._armed(bars)
                changed = process_strategy(
                    SPEC_15M, book, bars, atr, equity=5000.0,
                    block=False, now=int(bars["ts"][-1] + SPEC_15M.interval_ms),
                    execute=False, trend=1,
                )
            self.assertFalse(changed)
            self.assertIsNone(book["entry"])
            with open(deploy.READING, encoding="utf-8") as fh:
                reading = json.load(fh)
            self.assertTrue(reading["dead"])
            self.assertFalse(reading["signal_short"])
            self.assertTrue(reading["require_macd"])
            self.assertIn("背离", reading["macd_note"])
        finally:
            deploy.READING = old
            deploy.TRADE_LOG = old_log
            tmp.cleanup()

    def test_eth15_discards_dead_cross_when_hist_positive(self):
        tmp = tempfile.TemporaryDirectory()
        old = deploy.READING_ETH15
        old_log = deploy.TRADE_LOG
        deploy.READING_ETH15 = os.path.join(tmp.name, "eth15.json")
        deploy.TRADE_LOG = os.path.join(tmp.name, "trades.csv")
        bars, atr = self._bars(interval_ms=SPEC_ETH_15M.interval_ms, px=2750.0)
        n = len(bars["ts"])
        atr[:] = 16.409
        k = np.full(n, 70.0)
        d = np.full(n, 68.0)
        k[-1], d[-1] = 60.4, 61.2
        try:
            with patch("shadow.deploy.kdj", return_value=(k, d, k)), \
                    self._macd(bars, 3.2):
                book = self._armed(bars)
                changed = process_strategy(
                    SPEC_ETH_15M, book, bars, atr, equity=5000.0,
                    block=False, now=int(bars["ts"][-1] + SPEC_ETH_15M.interval_ms),
                    execute=False,
                )
            self.assertFalse(changed)
            self.assertIsNone(book["entry"])
            with open(deploy.READING_ETH15, encoding="utf-8") as fh:
                reading = json.load(fh)
            self.assertTrue(reading["dead"])
            self.assertFalse(reading["signal_short"])
            self.assertIn("背离", reading["macd_note"])
        finally:
            deploy.READING_ETH15 = old
            deploy.TRADE_LOG = old_log
            tmp.cleanup()

    def test_btc15_shorts_when_dead_breaks_prior_low(self):
        """死叉且 MACD 是绿柱 → 正常做空。"""
        tmp = tempfile.TemporaryDirectory()
        old = deploy.READING
        old_log = deploy.TRADE_LOG
        deploy.READING = os.path.join(tmp.name, "btc15.json")
        deploy.TRADE_LOG = os.path.join(tmp.name, "trades.csv")
        bars, atr = self._bars(interval_ms=SPEC_15M.interval_ms, px=86500.0)
        n = len(bars["ts"])
        atr[:] = 417.4
        k = np.full(n, 70.0)
        d = np.full(n, 68.0)
        k[-1], d[-1] = 89.3, 91.5
        try:
            with patch("shadow.deploy.kdj", return_value=(k, d, k)), \
                    self._macd(bars, -8.0):
                book = self._armed(bars)
                process_strategy(
                    SPEC_15M, book, bars, atr, equity=5000.0,
                    block=False, now=int(bars["ts"][-1] + SPEC_15M.interval_ms),
                    execute=False, trend=1,
                )
            self.assertEqual(book["entry"]["side"], -1)
            self.assertGreater(book["entry"]["qty"], 0)
        finally:
            deploy.READING = old
            deploy.TRADE_LOG = old_log
            tmp.cleanup()

    def test_btc15_opens_long_when_gold_cross_with_red_hist(self):
        """金叉且 MACD 是红柱 → 正常做多。"""
        tmp = tempfile.TemporaryDirectory()
        old = deploy.READING
        old_log = deploy.TRADE_LOG
        deploy.READING = os.path.join(tmp.name, "btc15.json")
        deploy.TRADE_LOG = os.path.join(tmp.name, "trades.csv")
        bars, atr = self._bars(interval_ms=SPEC_15M.interval_ms, px=86000.0)
        n = len(bars["ts"])
        atr[:] = 405.4
        k = np.full(n, 28.0)
        d = np.full(n, 30.0)
        k[-1], d[-1] = 34.8, 30.1
        try:
            with patch("shadow.deploy.kdj", return_value=(k, d, k)), \
                    self._macd(bars, 6.4):
                book = self._armed(bars)
                process_strategy(
                    SPEC_15M, book, bars, atr, equity=5000.0,
                    block=False, now=int(bars["ts"][-1] + SPEC_15M.interval_ms),
                    execute=False, trend=1,
                )
            self.assertEqual(book["entry"]["side"], 1)
            self.assertGreater(book["entry"]["qty"], 0)
        finally:
            deploy.READING = old
            deploy.TRADE_LOG = old_log
            tmp.cleanup()

    def test_btc5_ignores_gold_when_k_not_below_30(self):
        tmp = tempfile.TemporaryDirectory()
        old = deploy.READING_5M
        old_log = deploy.TRADE_LOG
        deploy.READING_5M = os.path.join(tmp.name, "btc5.json")
        deploy.TRADE_LOG = os.path.join(tmp.name, "trades.csv")
        bars, atr = self._bars(px=86000.0)
        n = len(bars["ts"])
        k = np.full(n, 28.0)
        d = np.full(n, 30.0)
        k[-1], d[-1] = 32.34, 28.13
        try:
            with patch("shadow.deploy.kdj", return_value=(k, d, k)):
                book = self._armed(bars)
                changed = process_strategy(
                    SPEC_5M, book, bars, atr, equity=5000.0,
                    block=False, now=int(bars["ts"][-1] + 300_000),
                    execute=False, trend=-1,
                )
            self.assertFalse(changed)
            self.assertIsNone(book["entry"])
        finally:
            deploy.READING_5M = old
            deploy.TRADE_LOG = old_log
            tmp.cleanup()


class TestFiveMinuteReadingPath(unittest.TestCase):
    def test_save_signal_reading_can_write_5m_file(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "latest_reading_5m.json")
        ts = np.array([1000, 2000], dtype=np.int64)
        values = np.array([100.0, 101.0])
        save_signal_reading(
            {"missed_bars": 0, "missed_signals": 0}, i=1, ts=ts,
            o=values, h=values, l=values, c=values,
            k=np.array([20.0, 28.0]), d=np.array([25.0, 26.0]),
            atr_al=np.array([10.0, 10.0]), up=np.array([102.0, 103.0]),
            lb=np.array([98.0, 99.0]), now=3000, execute=True,
            pos_side="FLAT", interval="5m",
            signal_rule=SPEC_5M.signal_rule,
            k_long_max=30, k_short_min=70, path=path,
        )
        import json
        with open(path, encoding="utf-8") as fh:
            out = json.load(fh)
        self.assertEqual(out["interval"], "5m")
        self.assertTrue(out["signal_long"])
        self.assertTrue(out["signal_needs_k_extreme"])
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
