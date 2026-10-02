"""KDJ(9,3,3) 信号的唯一来源：仅用已收盘的相邻两根 K/D 值。

2026-10-02 用户更改策略：入场和反手只看交叉，不再限制 K<30/K>70。
K、D 数值仍供诊断和审计展示，不作为信号过滤条件。
"""
from __future__ import annotations

import math
from typing import Tuple


def crossing(k_previous: float, d_previous: float,
             k_current: float, d_current: float) -> Tuple[bool, bool]:
    """返回 (金叉做多, 死叉做空)。NaN 不能生成交易信号。"""
    if not all(math.isfinite(float(x)) for x in
               (k_previous, d_previous, k_current, d_current)):
        return False, False
    gold = k_previous <= d_previous and k_current > d_current
    dead = k_previous >= d_previous and k_current < d_current
    return gold, dead
