"""KDJ(9,3,3) 信号的唯一来源：仅用已收盘的相邻两根 K/D 值。

2026-10-02 用户更改策略：入场和反手只看交叉，不再限制 K<30/K>70。
K、D 数值仍供诊断和审计展示，不作为信号过滤条件。
"""
from __future__ import annotations

import math
from typing import Optional, Tuple


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

    15m 策略不传阈值，交叉即信号。
    5m 策略传 k_long_max=30、k_short_min=70：金叉且 K<30 做多，死叉且 K>70 做空。
    """
    gold, dead = crossing(k_previous, d_previous, k_current, d_current)
    sig_long = bool(gold and (k_long_max is None or k_current < k_long_max))
    sig_short = bool(dead and (k_short_min is None or k_current > k_short_min))
    return sig_long, sig_short, gold, dead
