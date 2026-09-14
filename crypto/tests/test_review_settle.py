"""结算逻辑单测 — 全部离线, 用手工构造的 K 线, 不碰网络."""

import sys, os, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from indicators.ohlcv import Candle
from models.review import SettleStatus, TradeRecord
from review.settle import interval_hours, plan_levels, settle_record

BAR = 300_000              # 5m (与 short_term kline_interval 对齐)
WINDOW = 3_600_000         # 1h (short_term)


def candle(idx: int, high: float, low: float, close: float = None) -> Candle:
    """第 idx 根 5m K 线."""
    o = idx * BAR
    return Candle(
        open_time_ms=o,
        open=(high + low) / 2,
        high=high,
        low=low,
        close=close if close is not None else (high + low) / 2,
        volume=1.0,
        close_time_ms=o + BAR - 1,
    )


def make_record(cs: float = 50.0, atr: float = 10.0, entry: float = 100.0,
                horizon: str = "short_term", overridden: bool = False,
                opened_at_ms: int = 0) -> TradeRecord:
    return TradeRecord(
        trade_id="T-test",
        opened_at_ms=opened_at_ms,
        horizon=horizon,
        entry_price=entry,
        scores={"news": 0.0, "data": cs, "tech": cs, "prediction": 0.0},
        composite_score=cs,
        decision="STANDARD_LONG" if cs > 0 else "STANDARD_SHORT",
        atr=atr,
        overridden=overridden,
    )


class TestLevels(unittest.TestCase):
    def test_long_target_above_stop_below(self):
        rec = make_record(cs=50, atr=10, entry=100)
        direction, target, stop, atr_used = plan_levels(rec)
        self.assertEqual(direction, "LONG")
        self.assertAlmostEqual(target, 110.0)
        self.assertAlmostEqual(stop, 90.0)
        self.assertAlmostEqual(atr_used, 10.0)

    def test_short_target_below_stop_above(self):
        rec = make_record(cs=-50, atr=10, entry=100)
        direction, target, stop, _ = plan_levels(rec)
        self.assertEqual(direction, "SHORT")
        self.assertAlmostEqual(target, 90.0)
        self.assertAlmostEqual(stop, 110.0)

    def test_atr_fallback_when_missing(self):
        rec = make_record(cs=50, entry=100)
        rec.atr = None
        _, target, _, atr_used = plan_levels(rec)
        self.assertAlmostEqual(atr_used, 1.0)          # 1% of 100
        self.assertAlmostEqual(target, 101.0)

    def test_interval_hours(self):
        self.assertAlmostEqual(interval_hours("5m"), 5.0 / 60.0)
        self.assertAlmostEqual(interval_hours("15m"), 0.25)
        self.assertAlmostEqual(interval_hours("4h"), 4.0)
        self.assertAlmostEqual(interval_hours("1d"), 24.0)


class TestSettlement(unittest.TestCase):
    def test_long_target_first_is_correct(self):
        rec = make_record(cs=50)
        candles = [candle(0, 105, 95), candle(1, 112, 98), candle(2, 108, 96)]
        settle_record(rec, candles, now=WINDOW + 1)
        self.assertEqual(rec.status, SettleStatus.CORRECT.value)
        self.assertAlmostEqual(rec.exit_price, 110.0)
        self.assertEqual(rec.settle_detail["reason"], "target_first")
        self.assertEqual(rec.settle_detail["bars_to_hit"], 2)

    def test_long_stop_first_is_wrong(self):
        rec = make_record(cs=50)
        candles = [candle(0, 101, 97), candle(1, 105, 88), candle(2, 112, 90)]
        settle_record(rec, candles, now=WINDOW + 1)
        self.assertEqual(rec.status, SettleStatus.WRONG.value)
        self.assertAlmostEqual(rec.exit_price, 90.0)
        self.assertEqual(rec.settle_detail["reason"], "stop_first")

    def test_short_target_first_is_correct(self):
        rec = make_record(cs=-50)
        candles = [candle(0, 103, 97), candle(1, 102, 88)]
        settle_record(rec, candles, now=WINDOW + 1)
        self.assertEqual(rec.status, SettleStatus.CORRECT.value)
        self.assertAlmostEqual(rec.exit_price, 90.0)

    def test_short_stop_first_is_wrong(self):
        rec = make_record(cs=-50)
        candles = [candle(0, 102, 98), candle(1, 114, 100)]
        settle_record(rec, candles, now=WINDOW + 1)
        self.assertEqual(rec.status, SettleStatus.WRONG.value)
        self.assertAlmostEqual(rec.exit_price, 110.0)

    def test_chop_is_invalid_and_not_in_denominator(self):
        rec = make_record(cs=50)
        candles = [candle(0, 104, 96), candle(1, 105, 95), candle(2, 106, 94)]
        settle_record(rec, candles, now=WINDOW + 1)
        self.assertEqual(rec.status, SettleStatus.INVALID.value)
        self.assertEqual(rec.settle_detail["reason"], "chop_no_touch")
        self.assertFalse(rec.is_valid_sample)

    def test_window_not_elapsed_is_pending(self):
        rec = make_record(cs=50)
        candles = [candle(0, 104, 96), candle(1, 105, 95)]
        settle_record(rec, candles, now=1_000_000)
        self.assertEqual(rec.status, SettleStatus.PENDING.value)
        self.assertFalse(rec.is_valid_sample)

    def test_no_candles_yet_is_pending(self):
        rec = make_record(cs=50)
        settle_record(rec, [], now=1_000_000)
        self.assertEqual(rec.status, SettleStatus.PENDING.value)

    def test_out_of_range_window_is_invalid(self):
        # 档案很老, 窗口早就结束, 但可取 K 线已经覆盖不到它
        rec = make_record(cs=50, opened_at_ms=0)
        far_future = [candle(1000, 104, 96), candle(1001, 105, 95)]
        settle_record(rec, far_future, now=WINDOW * 100)
        self.assertEqual(rec.status, SettleStatus.INVALID.value)
        self.assertEqual(rec.settle_detail["reason"], "out_of_range")

    def test_ambiguous_bar_counts_as_wrong(self):
        # 同一根 K 线里同时摸到目标和止损 → 无法判先后, 保守记错
        rec = make_record(cs=50)
        candles = [candle(0, 112, 88)]
        settle_record(rec, candles, now=WINDOW + 1)
        self.assertEqual(rec.status, SettleStatus.WRONG.value)
        self.assertTrue(rec.settle_detail["ambiguous"])

    def test_neutral_is_excluded(self):
        rec = make_record(cs=0)
        rec.decision = "NEUTRAL"
        settle_record(rec, [candle(0, 104, 96)], now=WINDOW + 1)
        self.assertEqual(rec.status, SettleStatus.EXCLUDED.value)
        self.assertEqual(rec.settle_detail["reason"], "neutral_no_trade")
        self.assertFalse(rec.is_valid_sample)

    def test_overridden_is_excluded(self):
        rec = make_record(cs=-70, overridden=True)
        settle_record(rec, [candle(0, 104, 80)], now=WINDOW + 1)
        self.assertEqual(rec.status, SettleStatus.EXCLUDED.value)
        self.assertEqual(rec.settle_detail["reason"], "overridden")

    def test_mfe_mae_recorded_even_on_chop(self):
        rec = make_record(cs=50, atr=10)
        candles = [candle(0, 106, 96)]
        settle_record(rec, candles, now=WINDOW + 1)
        self.assertAlmostEqual(rec.max_favorable_atr, 0.6)
        self.assertAlmostEqual(rec.max_adverse_atr, 0.4)

    def test_candles_before_entry_are_ignored(self):
        # 入场前那根 K 线摸到了目标 (high 120 ≥ 110), 但那时还没进场, 不该算
        rec = make_record(cs=50, opened_at_ms=2 * BAR)
        candles = [candle(0, 120, 80), candle(2, 104, 96), candle(3, 105, 95)]
        settle_record(rec, candles, now=2 * BAR + WINDOW + 1)   # 窗口已走完
        self.assertEqual(rec.status, SettleStatus.INVALID.value)
        self.assertEqual(rec.settle_detail["bars_used"], 2)

    def test_watch_decision_is_excluded_not_settled(self):
        # 偏多观望: 分数是正的, 但系统没开仓 → 不该拿事后价格判它对错
        rec = make_record(cs=20)
        rec.decision = "WATCH_LONG"
        settle_record(rec, [candle(0, 112, 88)], now=WINDOW + 1)
        self.assertEqual(rec.status, SettleStatus.EXCLUDED.value)
        self.assertEqual(rec.settle_detail["reason"], "watch_no_position")
        self.assertFalse(rec.is_valid_sample)

    def test_watch_short_decision_is_excluded(self):
        rec = make_record(cs=-20)
        rec.decision = "WATCH_SHORT"
        settle_record(rec, [candle(0, 112, 88)], now=WINDOW + 1)
        self.assertEqual(rec.status, SettleStatus.EXCLUDED.value)

    def test_direction_follows_decision_not_score_sign(self):
        # 人工补录时 CS 与决策填反了 → 以决策为准, 别把多空判反
        rec = make_record(cs=50)          # 分数是正
        rec.decision = "STANDARD_SHORT"   # 但系统下的是空单
        direction, target, stop, _ = plan_levels(rec)
        self.assertEqual(direction, "SHORT")
        self.assertLess(target, rec.entry_price)
        self.assertGreater(stop, rec.entry_price)

    def test_long_term_uses_month_window(self):
        rec = make_record(cs=50, atr=10, horizon="long_term", opened_at_ms=0)
        candles = [candle(0, 104, 96)]
        settle_record(rec, candles, now=100 * 3_600_000)   # 100h < 720h
        self.assertEqual(rec.status, SettleStatus.PENDING.value)
        settle_record(rec, candles, now=200 * 3_600_000)   # 200h 仍 < 720h
        self.assertEqual(rec.status, SettleStatus.PENDING.value)


if __name__ == "__main__":
    unittest.main()
