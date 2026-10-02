"""KDJ(9,3,3) 信号的唯一来源：仅用已收盘的相邻两根 K/D 值。

15m：当根收盘交叉，且价格真正突破上一根高低点，下一根开盘下限价单。
5m：金叉且 K<30 做多 / 死叉且 K>70 做空，当根收盘即可开仓。
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

# 15m 突破垫：死叉收盘须低于上一根最低 − 该值×ATR_1H；金叉对称。
# 0.15 能挡下 17:15 那种只破 29 点的擦线，同时放过 13:15 / 15:45 的真突破。
BREAK_ATR_MULT = 0.15


def crossing(k_previous: float, d_previous: float,
             k_current: float, d_current: float) -> Tuple[bool, bool]:
    """返回 (金叉做多, 死叉做空)。NaN 不能生成交易信号。"""
    if not all(math.isfinite(float(x)) for x in
               (k_previous, d_previous, k_current, d_current)):
        return False, False
    gold = k_previous <= d_previous and k_current > d_current
    dead = k_previous >= d_previous and k_current < d_current
    return gold, dead


def entry_signal(k_previous: float, d_previous: float,
                 k_current: float, d_current: float,
                 *,
                 k_long_max: Optional[float] = None,
                 k_short_min: Optional[float] = None
                 ) -> Tuple[bool, bool, bool, bool]:
    """返回 (做多, 做空, 金叉, 死叉)。

    5m 策略传 k_long_max=30、k_short_min=70：金叉且 K<30 做多，死叉且 K>70 做空。
    15m 不传阈值：交叉只是前提，还要过 price_breaks。
    """
    gold, dead = crossing(k_previous, d_previous, k_current, d_current)
    sig_long = bool(gold and (k_long_max is None or k_current < k_long_max))
    sig_short = bool(dead and (k_short_min is None or k_current > k_short_min))
    return sig_long, sig_short, gold, dead


def price_breaks(close: float, prev_high: float, prev_low: float, atr: float,
                 *, want: int) -> bool:
    """15m：金叉要涨破上一根最高，死叉要跌破上一根最低，幅度至少 0.15×ATR。"""
    vals = (close, prev_high, prev_low, atr)
    if want == 0 or not all(math.isfinite(float(x)) for x in vals) or atr <= 0:
        return False
    pad = BREAK_ATR_MULT * float(atr)
    if want > 0:
        return float(close) > float(prev_high) + pad
    return float(close) < float(prev_low) - pad


def confirmed_signal(
    k_prev2: float, d_prev2: float,
    k_prev: float, d_prev: float,
    k_cur: float, d_cur: float,
    *,
    k_long_max: Optional[float] = None,
    k_short_min: Optional[float] = None,
) -> Tuple[bool, bool, bool, bool, str]:
    """15m：上一根交叉，本根 K 仍在交叉一侧，才在本根下限价单。

    返回 (做多, 做空, 本根金叉, 本根死叉, 备注)。
    本根金叉/死叉只用于展示；开仓看的是上一根交叉是否站稳。
    """
    prev_long, prev_short, prev_gold, prev_dead = entry_signal(
        k_prev2, d_prev2, k_prev, d_prev,
        k_long_max=k_long_max, k_short_min=k_short_min,
    )
    gold, dead = crossing(k_prev, d_prev, k_cur, d_cur)
    if not all(math.isfinite(float(x)) for x in (k_cur, d_cur)):
        return False, False, gold, dead, "观察"
    hold_long = float(k_cur) > float(d_cur)
    hold_short = float(k_cur) < float(d_cur)
    sig_long = bool(prev_long and hold_long)
    sig_short = bool(prev_short and hold_short)
    if sig_long:
        note = "上一根金叉已确认，本根下限价单"
    elif sig_short:
        note = "上一根死叉已确认，本根下限价单"
    elif prev_gold or prev_dead:
        note = "上一根交叉未站稳，放弃"
    elif gold or dead:
        note = "交叉待确认，下一根再下单"
    else:
        note = "观察"
    return sig_long, sig_short, gold, dead, note
