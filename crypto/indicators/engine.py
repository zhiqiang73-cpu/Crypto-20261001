"""从 OHLCV 提取技术面原始特征 → TechSnapshot."""

from __future__ import annotations

from typing import List, Optional, Sequence

from indicators.classic import (
    adx,
    atr,
    bollinger,
    last,
    macd,
    obv,
    rsi,
    session_vwap,
)
from indicators.ohlcv import Candle, closes, ema
from indicators.profile_patterns import (
    inside_bar_breakout,
    is_engulfing,
    is_pin_bar,
    near_level,
    pdh_pdl,
    volume_profile,
    volume_score,
)
from indicators.structure import (
    StructureBias,
    detect_structure,
    structure_score,
    swing_levels,
)
from models.snapshots import TechSnapshot


def detect_rsi_divergence(
    price: Sequence[float],
    rsi_vals: Sequence[Optional[float]],
    lookback: int = 30,
) -> float:
    """简化背离: 窗口内价格新低但 RSI 抬高 → +70; 价格新高 RSI 走低 → -70."""
    n = len(price)
    if n < lookback or lookback < 5:
        return 0.0
    start = n - lookback
    # 找窗口内两个低点 / 高点
    window_p = list(price[start:])
    window_r = rsi_vals[start:]
    # 使用三分位近似局部极值
    third = lookback // 3
    if third < 2:
        return 0.0

    def _min_idx(arr, a, b):
        seg = [(i, arr[i]) for i in range(a, b) if arr[i] is not None]
        if not seg:
            return None
        return min(seg, key=lambda x: x[1])[0]

    def _max_idx(arr, a, b):
        seg = [(i, arr[i]) for i in range(a, b) if arr[i] is not None]
        if not seg:
            return None
        return max(seg, key=lambda x: x[1])[0]

    # bullish div
    i1 = _min_idx(window_p, 0, third * 2)
    i2 = _min_idx(window_p, third, lookback)
    if i1 is not None and i2 is not None and i2 > i1:
        if window_p[i2] < window_p[i1] and window_r[i1] is not None and window_r[i2] is not None:
            if window_r[i2] > window_r[i1]:  # type: ignore
                return 70.0

    j1 = _max_idx(window_p, 0, third * 2)
    j2 = _max_idx(window_p, third, lookback)
    if j1 is not None and j2 is not None and j2 > j1:
        if window_p[j2] > window_p[j1] and window_r[j1] is not None and window_r[j2] is not None:
            if window_r[j2] < window_r[j1]:  # type: ignore
                return -70.0
    return 0.0


def macd_hist_score(hist: Sequence[Optional[float]], atr_v: Optional[float] = None) -> float:
    """MACD 柱连续映射: hist / ATR * 50, 钳制到 [-80, +80].

    无 ATR 时回退到档位制 (兼容旧调用).
    """
    vals = [h for h in hist if h is not None]
    if not vals:
        return 0.0
    last_h = float(vals[-1])
    if atr_v and atr_v > 0:
        score = max(-80.0, min(80.0, last_h / atr_v * 50.0))
        return round(score, 2)
    # 回退档位
    if len(vals) < 4:
        return 0.0
    a, b, c, d = vals[-4], vals[-3], vals[-2], vals[-1]
    if b <= 0 < c and d > c:
        return 40.0
    if c > 0 and d > c and b > 0:
        return 50.0
    if d < c and c > 0:
        return -20.0
    if b >= 0 > c and d < c:
        return -40.0
    return 0.0


def ema_stack_score(
    price: float,
    e21: Optional[float],
    e50: Optional[float],
    e200: Optional[float],
    atr_v: Optional[float] = None,
) -> float:
    """EMA 堆叠: 档位基分 + 相对 EMA21 的连续偏移."""
    if e21 is None or e50 is None or e200 is None:
        return 0.0
    bull_align = e21 > e50 > e200
    bear_align = e21 < e50 < e200
    spread = max(e21, e50, e200) - min(e21, e50, e200)
    tangled = spread / price < 0.005 if price else False

    if bull_align and price > e200:
        base = 70.0
    elif bear_align and price < e200:
        base = -70.0
    elif price > e200 and tangled:
        base = 20.0
    elif tangled:
        base = 0.0
    elif price < e200:
        base = -40.0
    elif price > e200:
        base = 20.0
    else:
        base = 0.0

    # 连续偏移: 价格距 EMA21 的 ATR 倍数
    if atr_v and atr_v > 0 and e21:
        bonus = max(-30.0, min(30.0, (price - e21) / atr_v * 15.0))
        return round(max(-100.0, min(100.0, base + bonus)), 2)
    return base


def extract_tech_features(
    candles: Sequence[Candle],
    daily_candles: Optional[Sequence[Candle]] = None,
) -> TechSnapshot:
    """主入口: 15m (或指定周期) K 线 + 可选日 K → TechSnapshot."""
    snap = TechSnapshot()
    if len(candles) < 30:
        snap.available = False
        return snap

    price_series = closes(candles)
    price = price_series[-1]
    snap.price = price
    snap.interval_bars = len(candles)

    # EMA
    e21 = ema(price_series, 21)
    e50 = ema(price_series, 50)
    e200 = ema(price_series, 200)
    snap.ema21 = last(e21)
    snap.ema50 = last(e50)
    snap.ema200 = last(e200)

    # Structure
    bias = detect_structure(candles)
    snap.structure_bias = bias.value
    snap.structure_score = structure_score(candles)

    # ADX / ATR / Boll — ATR 先算, 供连续映射使用
    adx_s = adx(candles)
    atr_s = atr(candles)
    snap.adx = last(adx_s)
    snap.atr = last(atr_s)
    snap.atr_pct = (snap.atr / price) if snap.atr and price else None

    # EMA 连续分 (档位 + ATR 偏移)
    snap.ema_score = ema_stack_score(
        price, snap.ema21, snap.ema50, snap.ema200, atr_v=snap.atr
    )

    _mid, _up, _lo, bw = bollinger(price_series)
    snap.boll_bandwidth = last(bw)
    # 挤压: 当前 BW 处于近 20 个有效 BW 的最低 10%
    recent_bw = [x for x in bw[-40:] if x is not None]
    if recent_bw and snap.boll_bandwidth is not None:
        sorted_bw = sorted(recent_bw)
        cutoff = sorted_bw[max(0, int(len(sorted_bw) * 0.10) - 1)]
        snap.boll_squeeze = snap.boll_bandwidth <= cutoff
    else:
        snap.boll_squeeze = False

    # RSI / MACD / OBV
    rsi_s = rsi(price_series)
    snap.rsi = last(rsi_s)
    snap.rsi_divergence_score = detect_rsi_divergence(price_series, rsi_s)

    _line, _sig, hist = macd(price_series)
    snap.macd_hist = last(hist)
    snap.macd_score = macd_hist_score(hist, atr_v=snap.atr)

    obv_s = obv(candles)
    snap.obv_score = _obv_score(price_series, obv_s)

    # VWAP
    vwap_s = session_vwap(candles)
    snap.vwap = last(vwap_s)
    snap.vwap_score = _vwap_score(price, snap.vwap, snap.atr)

    # Volume profile (最近 96 根)
    vp_window = candles[-96:] if len(candles) >= 96 else candles
    vp = volume_profile(vp_window)
    if vp:
        snap.vp_poc = vp.poc
        snap.vp_hvn_low = vp.hvn_low
        snap.vp_hvn_high = vp.hvn_high
        snap.vp_score = _vp_score(price, vp, price_series)

    # S/R
    supports, resistances = swing_levels(candles)
    snap.nearest_support = max([s for s in supports if s <= price], default=None)
    snap.nearest_resistance = min([r for r in resistances if r >= price], default=None)
    snap.sr_score = _sr_score(price, candles, supports, resistances, snap.atr or 0)

    # PDH/PDL
    if daily_candles:
        pdh, pdl = pdh_pdl(daily_candles)
        snap.pdh = pdh
        snap.pdl = pdl
        snap.pdh_pdl_score = _pdh_pdl_score(candles, pdh, pdl)

    snap.volume_score = volume_score(candles)

    # Patterns
    cur = candles[-1]
    prev = candles[-2]
    atr_v = snap.atr or (price * 0.01)
    near_sup = snap.nearest_support and near_level(price, snap.nearest_support, atr_v)
    near_res = snap.nearest_resistance and near_level(price, snap.nearest_resistance, atr_v)

    if is_pin_bar(cur, bullish=True):
        snap.pin_bar_score = 70.0 if near_sup else 20.0
    elif is_pin_bar(cur, bullish=False):
        snap.pin_bar_score = -70.0 if near_res else -20.0
    else:
        snap.pin_bar_score = 0.0

    if is_engulfing(prev, cur, bullish=True):
        snap.engulfing_score = 60.0 if near_sup else 20.0
    elif is_engulfing(prev, cur, bullish=False):
        snap.engulfing_score = -60.0 if near_res else -20.0
    else:
        snap.engulfing_score = 0.0

    snap.inside_bar_score = inside_bar_breakout(candles)
    snap.available = True
    return snap


def _obv_score(prices: Sequence[float], obv_vals: Sequence[float]) -> float:
    if len(prices) < 10 or len(obv_vals) < 10:
        return 0.0
    p_flat = abs(prices[-1] - prices[-10]) / prices[-10] < 0.005
    obv_up = obv_vals[-1] > obv_vals[-10]
    obv_down = obv_vals[-1] < obv_vals[-10]
    price_up = prices[-1] > prices[-10]
    if obv_up and p_flat:
        return 50.0
    if obv_down and price_up:
        return -50.0
    return 0.0


def _vwap_score(price: float, vwap: Optional[float], atr_v: Optional[float]) -> float:
    """VWAP 连续映射: (price - vwap) / ATR * 40 → [-80, +80]."""
    if vwap is None or not atr_v or atr_v <= 0:
        return 0.0
    return round(max(-80.0, min(80.0, (price - vwap) / atr_v * 40.0)), 2)


def _vp_score(price: float, vp, prices: Sequence[float]) -> float:
    # POC 附近 → 0
    band = (vp.hvn_high - vp.hvn_low) * 0.15 or price * 0.001
    if abs(price - vp.poc) <= band:
        return 0.0
    # HVN 下沿支撑
    if abs(price - vp.hvn_low) <= band and prices[-1] >= prices[-3]:
        return 50.0
    # LVN 真空
    in_lvn = any(abs(price - lv) <= band for lv in vp.lvn_levels)
    if in_lvn:
        if prices[-1] > prices[-5]:
            return 60.0
        if prices[-1] < prices[-5]:
            return -60.0
    return 0.0


def _sr_score(price, candles, supports, resistances, atr_v) -> float:
    if not supports and not resistances:
        return 0.0
    # 强支撑: 多次测试 — 简化为 swing 密集
    strong_sup = None
    for s in supports:
        touches = sum(1 for c in candles[-40:] if near_level(c.low, s, atr_v or price * 0.01))
        if touches >= 3:
            strong_sup = s
            break
    strong_res = None
    for r in reversed(resistances):
        touches = sum(1 for c in candles[-40:] if near_level(c.high, r, atr_v or price * 0.01))
        if touches >= 3:
            strong_res = r
            break

    if strong_sup and price > strong_sup and near_level(price, strong_sup, atr_v or price * 0.01, 0.5):
        return 50.0
    if strong_res and price < strong_res and near_level(price, strong_res, atr_v or price * 0.01, 0.5):
        return -50.0

    # 突破阻力回踩
    if resistances:
        r = min(resistances, key=lambda x: abs(x - price))
        if candles[-3].close < r <= candles[-2].close and near_level(candles[-1].low, r, atr_v or price * 0.01):
            return 70.0
    if supports:
        s = min(supports, key=lambda x: abs(x - price))
        if candles[-3].close > s >= candles[-2].close:
            return -70.0
    return 0.0


def _pdh_pdl_score(candles, pdh, pdl) -> float:
    if pdh is None or pdl is None or len(candles) < 3:
        return 0.0
    c = candles[-1]
    vol_ma = sum(x.volume for x in candles[-20:]) / min(20, len(candles))
    heavy = c.volume > 1.5 * vol_ma if vol_ma else False
    if c.close > pdh and candles[-2].close <= pdh and heavy:
        return 70.0
    if c.close > pdh and near_level(c.low, pdh, (pdh - pdl) * 0.1 or pdh * 0.001):
        return 50.0
    if c.close < pdl and candles[-2].close >= pdl:
        return -70.0
    if pdl <= c.close <= pdh:
        return 0.0
    return 0.0
