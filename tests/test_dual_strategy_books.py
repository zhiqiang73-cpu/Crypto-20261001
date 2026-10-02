"""双策略虚拟账本：15m 纯交叉 + 5m 交叉且 K 极值，共用净仓。"""
from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

import shadow.deploy as deploy
from shadow.deploy import process_strategy, save_signal_reading
from shadow.signals import crossing, entry_signal
from shadow.strategy_books import (SPEC_5M, SPEC_15M, apply_virtual_signal,
                                   desired_net, empty_book, migrate_state,
                                   position_sources, reduce_only_for_delta,
                                   runtime_view, signal_reason)


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
        self.assertIn("K<30", signal_reason(SPEC_5M, gold=True, dead=False, k=14.9))
        self.assertIn("K>70", signal_reason(SPEC_5M, gold=False, dead=True, k=90.7))

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
