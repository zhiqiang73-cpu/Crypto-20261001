"""市场结构: Swing High/Low, BOS, CHoCH."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence

from indicators.ohlcv import Candle


class StructureBias(Enum):
    UPTREND = "uptrend"          # HH+HL 连续
    DOWNTREND = "downtrend"      # LH+LL 连续
    BULLISH_CHOCH = "bull_choch"
    BEARISH_CHOCH = "bear_choch"
    RANGE = "range"


@dataclass
class SwingPoint:
    index: int
    price: float
    kind: str  # "high" | "low"


def find_swings(candles: Sequence[Candle], left: int = 2, right: int = 2) -> List[SwingPoint]:
    swings: List[SwingPoint] = []
    n = len(candles)
    for i in range(left, n - right):
        h = candles[i].high
        l = candles[i].low
        is_sh = all(h >= candles[i - j].high for j in range(1, left + 1)) and \
                all(h >= candles[i + j].high for j in range(1, right + 1))
        is_sl = all(l <= candles[i - j].low for j in range(1, left + 1)) and \
                all(l <= candles[i + j].low for j in range(1, right + 1))
        if is_sh:
            swings.append(SwingPoint(i, h, "high"))
        if is_sl:
            swings.append(SwingPoint(i, l, "low"))
    return swings


def detect_structure(candles: Sequence[Candle], left: int = 2, right: int = 2) -> StructureBias:
    """基于最近 swing 判定结构.

    HH+HL ×3 → UPTREND; LH+LL ×3 → DOWNTREND;
    刚破前高且先前下跌 → BULLISH_CHOCH; 刚破前低且先前上涨 → BEARISH_CHOCH.
    """
    swings = find_swings(candles, left, right)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return StructureBias.RANGE

    # 最近若干
    recent_h = highs[-4:]
    recent_l = lows[-4:]

    hh = sum(1 for i in range(1, len(recent_h)) if recent_h[i].price > recent_h[i - 1].price)
    hl = sum(1 for i in range(1, len(recent_l)) if recent_l[i].price > recent_l[i - 1].price)
    lh = sum(1 for i in range(1, len(recent_h)) if recent_h[i].price < recent_h[i - 1].price)
    ll = sum(1 for i in range(1, len(recent_l)) if recent_l[i].price < recent_l[i - 1].price)

    if hh >= 2 and hl >= 2:
        # 检查是否刚跌破最近 HL → bearish CHoCH
        last_hl = recent_l[-1].price
        if candles[-1].close < last_hl and candles[-2].close >= last_hl:
            return StructureBias.BEARISH_CHOCH
        return StructureBias.UPTREND

    if lh >= 2 and ll >= 2:
        last_lh = recent_h[-1].price
        if candles[-1].close > last_lh and candles[-2].close <= last_lh:
            return StructureBias.BULLISH_CHOCH
        return StructureBias.DOWNTREND

    # 弱趋势中的 CHoCH
    if len(highs) >= 2 and candles[-1].close > highs[-2].price and lh >= 1:
        return StructureBias.BULLISH_CHOCH
    if len(lows) >= 2 and candles[-1].close < lows[-2].price and hl >= 1:
        return StructureBias.BEARISH_CHOCH

    return StructureBias.RANGE


STRUCTURE_SCORES = {
    StructureBias.UPTREND: 80.0,
    StructureBias.BULLISH_CHOCH: 40.0,
    StructureBias.RANGE: 0.0,
    StructureBias.BEARISH_CHOCH: -40.0,
    StructureBias.DOWNTREND: -80.0,
}


def structure_score(candles: Sequence[Candle]) -> float:
    return STRUCTURE_SCORES[detect_structure(candles)]


def swing_levels(candles: Sequence[Candle], lookback: int = 50) -> tuple:
    """返回 (supports, resistances) 最近 swing 价位列表."""
    window = candles[-lookback:] if len(candles) > lookback else candles
    swings = find_swings(window)
    supports = sorted({s.price for s in swings if s.kind == "low"})
    resistances = sorted({s.price for s in swings if s.kind == "high"})
    return supports, resistances
