"""经典技术指标: EMA / RSI / MACD / Bollinger / ATR / ADX / OBV / VWAP."""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from indicators.ohlcv import Candle, closes, ema, sma, true_range, volumes


def rsi(values: Sequence[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period + 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    line: List[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            line[i] = ema_fast[i] - ema_slow[i]  # type: ignore
    # signal on non-None macd values — approximate by treating None as skip with forward fill seed
    compact = [x for x in line if x is not None]
    sig_compact = ema(compact, signal) if compact else []
    signal_line: List[Optional[float]] = [None] * len(values)
    hist: List[Optional[float]] = [None] * len(values)
    j = 0
    for i, v in enumerate(line):
        if v is None:
            continue
        sc = sig_compact[j] if j < len(sig_compact) else None
        signal_line[i] = sc
        if sc is not None:
            hist[i] = v - sc
        j += 1
    return line, signal_line, hist


def bollinger(
    values: Sequence[float],
    period: int = 20,
    num_std: float = 2.0,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """返回 mid, upper, lower, bandwidth=(upper-lower)/mid."""
    mid = sma(values, period)
    upper: List[Optional[float]] = [None] * len(values)
    lower: List[Optional[float]] = [None] * len(values)
    bandwidth: List[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1: i + 1]
        m = mid[i]
        if m is None:
            continue
        var = sum((x - m) ** 2 for x in window) / period
        std = var ** 0.5
        u = m + num_std * std
        l = m - num_std * std
        upper[i] = u
        lower[i] = l
        bandwidth[i] = (u - l) / m if m else None
    return mid, upper, lower, bandwidth


def atr(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    tr = true_range(candles)
    out: List[Optional[float]] = [None] * len(tr)
    if len(tr) < period:
        return out
    # Wilder smoothing
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def adx(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    n = len(candles)
    out: List[Optional[float]] = [None] * n
    if n < period * 2:
        return out
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = true_range(candles)
    for i in range(1, n):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0

    def _wilder(arr: List[float]) -> List[Optional[float]]:
        res: List[Optional[float]] = [None] * n
        if n < period:
            return res
        prev = sum(arr[1: period + 1])
        res[period] = prev
        for i in range(period + 1, n):
            prev = prev - prev / period + arr[i]
            res[i] = prev
        return res

    atr_w = _wilder(tr)
    plus_w = _wilder(plus_dm)
    minus_w = _wilder(minus_dm)
    dx: List[Optional[float]] = [None] * n
    for i in range(n):
        if atr_w[i] and atr_w[i] > 0 and plus_w[i] is not None and minus_w[i] is not None:
            pdi = 100.0 * (plus_w[i] / atr_w[i])  # type: ignore
            mdi = 100.0 * (minus_w[i] / atr_w[i])  # type: ignore
            denom = pdi + mdi
            dx[i] = 100.0 * abs(pdi - mdi) / denom if denom else 0.0

    # ADX = Wilder of DX
    start = period * 2
    if start >= n:
        return out
    seed_vals = [dx[i] for i in range(period + 1, start + 1) if dx[i] is not None]
    if len(seed_vals) < period:
        return out
    prev = sum(seed_vals[-period:]) / period
    out[start] = prev
    for i in range(start + 1, n):
        if dx[i] is None:
            continue
        prev = (prev * (period - 1) + dx[i]) / period  # type: ignore
        out[i] = prev
    return out


def obv(candles: Sequence[Candle]) -> List[float]:
    out: List[float] = []
    prev = 0.0
    prev_close: Optional[float] = None
    for c in candles:
        if prev_close is None:
            prev = c.volume
        elif c.close > prev_close:
            prev += c.volume
        elif c.close < prev_close:
            prev -= c.volume
        out.append(prev)
        prev_close = c.close
    return out


def session_vwap(candles: Sequence[Candle]) -> List[Optional[float]]:
    """简化: 全序列累计 VWAP (用 typical price)."""
    out: List[Optional[float]] = [None] * len(candles)
    cum_pv = 0.0
    cum_v = 0.0
    for i, c in enumerate(candles):
        tp = (c.high + c.low + c.close) / 3.0
        cum_pv += tp * c.volume
        cum_v += c.volume
        out[i] = cum_pv / cum_v if cum_v > 0 else None
    return out


def last(values: Sequence[Optional[float]]) -> Optional[float]:
    for v in reversed(values):
        if v is not None:
            return v
    return None
