"""Volume Profile (简易固定区间直方图) + PDH/PDL + 量价/形态."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from indicators.ohlcv import Candle, sma, volumes


@dataclass
class VolumeProfileResult:
    poc: float
    hvn_low: float
    hvn_high: float
    lvn_levels: List[float]
    bins: List[Tuple[float, float]]  # (price_mid, volume)


def volume_profile(candles: Sequence[Candle], bins: int = 24) -> Optional[VolumeProfileResult]:
    if not candles or bins < 4:
        return None
    lo = min(c.low for c in candles)
    hi = max(c.high for c in candles)
    if hi <= lo:
        return None
    width = (hi - lo) / bins
    vol = [0.0] * bins
    for c in candles:
        # 将整根 K 线成交量按触及 bin 均分 (简化)
        i0 = int((c.low - lo) / width)
        i1 = int((c.high - lo) / width)
        i0 = max(0, min(bins - 1, i0))
        i1 = max(0, min(bins - 1, i1))
        span = max(1, i1 - i0 + 1)
        share = c.volume / span
        for i in range(i0, i1 + 1):
            vol[i] += share

    poc_idx = max(range(bins), key=lambda i: vol[i])
    poc = lo + (poc_idx + 0.5) * width

    # HVN: POC 邻近成交量 > 均值的连续带
    mean_v = sum(vol) / bins
    left = poc_idx
    right = poc_idx
    while left > 0 and vol[left - 1] >= mean_v * 0.8:
        left -= 1
    while right < bins - 1 and vol[right + 1] >= mean_v * 0.8:
        right += 1
    hvn_low = lo + left * width
    hvn_high = lo + (right + 1) * width

    # LVN: 成交量最低的若干 bin
    threshold = mean_v * 0.35
    lvn = [lo + (i + 0.5) * width for i, v in enumerate(vol) if v <= threshold]

    mid_bins = [(lo + (i + 0.5) * width, vol[i]) for i in range(bins)]
    return VolumeProfileResult(poc=poc, hvn_low=hvn_low, hvn_high=hvn_high, lvn_levels=lvn, bins=mid_bins)


def pdh_pdl(daily_candles: Sequence[Candle]) -> Tuple[Optional[float], Optional[float]]:
    """取倒数第二根日 K 作为前一日 (最后一根可能未收盘)."""
    if len(daily_candles) < 2:
        if len(daily_candles) == 1:
            return daily_candles[0].high, daily_candles[0].low
        return None, None
    prev = daily_candles[-2]
    return prev.high, prev.low


def volume_score(candles: Sequence[Candle], ma_period: int = 20) -> float:
    """手册 E1: 上涨放量 → +60; 上涨缩量 → -20; 下跌放量 → -60; 下跌缩量 → +20."""
    if len(candles) < ma_period + 1:
        return 0.0
    vols = volumes(candles)
    ma = sma(vols, ma_period)
    last_ma = ma[-1]
    if last_ma is None or last_ma <= 0:
        return 0.0
    c = candles[-1]
    prev = candles[-2]
    up = c.close > prev.close
    down = c.close < prev.close
    heavy = c.volume > 2.0 * last_ma
    light = c.volume < 0.7 * last_ma
    if up and heavy:
        return 60.0
    if up and light:
        return -20.0
    if down and heavy:
        return -60.0
    if down and light:
        return 20.0
    return 0.0


def is_pin_bar(c: Candle, bullish: bool) -> bool:
    if c.range <= 0:
        return False
    body = c.body
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    if bullish:
        return lower >= 0.60 * c.range and body <= 0.30 * c.range
    return upper >= 0.60 * c.range and body <= 0.30 * c.range


def is_engulfing(prev: Candle, cur: Candle, bullish: bool) -> bool:
    if bullish:
        return (not prev.bullish) and cur.bullish and cur.open <= prev.close and cur.close >= prev.open
    return prev.bullish and (not cur.bullish) and cur.open >= prev.close and cur.close <= prev.open


def inside_bar_breakout(candles: Sequence[Candle], min_inside: int = 3) -> float:
    """连续 inside bar 后突破: 上破 +50 / 下破 -50 / 未破 0."""
    if len(candles) < min_inside + 2:
        return 0.0
    # 找母线
    mother = candles[-(min_inside + 1)]
    insides = candles[-min_inside:-1]
    if not all(c.high <= mother.high and c.low >= mother.low for c in insides):
        return 0.0
    last = candles[-1]
    if last.close > mother.high:
        return 50.0
    if last.close < mother.low:
        return -50.0
    return 0.0


def near_level(price: float, level: float, atr_val: float, tol_mult: float = 0.25) -> bool:
    if atr_val <= 0:
        return abs(price - level) / price < 0.001
    return abs(price - level) <= atr_val * tol_mult
