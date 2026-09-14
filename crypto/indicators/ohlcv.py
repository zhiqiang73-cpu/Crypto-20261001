"""OHLCV 数据结构与基础序列工具."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int = 0

    @property
    def bullish(self) -> bool:
        return self.close >= self.open

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)


def closes(candles: Sequence[Candle]) -> List[float]:
    return [c.close for c in candles]


def highs(candles: Sequence[Candle]) -> List[float]:
    return [c.high for c in candles]


def lows(candles: Sequence[Candle]) -> List[float]:
    return [c.low for c in candles]


def volumes(candles: Sequence[Candle]) -> List[float]:
    return [c.volume for c in candles]


def sma(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window = 0.0
    for i, v in enumerate(values):
        window += v
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or not values:
        return out
    k = 2.0 / (period + 1)
    # seed with SMA
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def true_range(candles: Sequence[Candle]) -> List[float]:
    out: List[float] = []
    prev_close: Optional[float] = None
    for c in candles:
        if prev_close is None:
            out.append(c.high - c.low)
        else:
            out.append(max(
                c.high - c.low,
                abs(c.high - prev_close),
                abs(c.low - prev_close),
            ))
        prev_close = c.close
    return out
