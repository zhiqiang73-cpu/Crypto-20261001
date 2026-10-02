"""指标计算 —— 严格按用户规格逐字实现, 不替换成库函数默认口径.

规格原文:
    KDJ(9, 3, 3) —— 15 分钟
        RSV_t = (C_t − LLV(L, 9)_t) / (HHV(H, 9)_t − LLV(L, 9)_t) × 100
            若分母为 0, 则 RSV_t = 50
        K_t = (2 × K_{t−1} + RSV_t) / 3
        D_t = (2 × D_{t−1} + K_t) / 3
            初值: K_0 = 50, D_0 = 50
        J 不使用。            (但日志要求记录 J, 故仍计算 J = 3K − 2D, 仅用于记录)

    ATR(14) —— 1 小时, Wilder 平滑
        TR_t  = max(H_t − L_t, |H_t − C_{t−1}|, |L_t − C_{t−1}|)
        ATR_t = (13 × ATR_{t−1} + TR_t) / 14
            初值: 前 14 根 TR 的简单平均

    BOLL(20, 2) —— 15 分钟
        MB_t = SMA(C, 20)
        σ_t  = 最近 20 根收盘价的样本标准差 (ddof = 1)
        UP_t = MB_t + 2 × σ_t
        LB_t = MB_t − 2 × σ_t
"""

from __future__ import annotations

from typing import Tuple

import numpy as np


def kdj(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        n: int = 9, m1: int = 3, m2: int = 3
        ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """KDJ(n, m1, m2). K = (2*K_prev + RSV)/3 等价于 alpha = 1/m1 的递归平滑。

    预热期 (不足 n 根时) 用"已收盘可得"的扩展窗口, 与国内平台 LLV/HHV 行为一致。
    """
    size = len(close)
    k = np.empty(size, dtype=np.float64)
    d = np.empty(size, dtype=np.float64)

    k_prev, d_prev = 50.0, 50.0
    for i in range(size):
        lo = max(0, i - n + 1)
        hh = float(np.max(high[lo:i + 1]))
        ll = float(np.min(low[lo:i + 1]))
        denom = hh - ll
        rsv = 50.0 if denom == 0 else (float(close[i]) - ll) / denom * 100.0
        k_cur = (2.0 * k_prev + rsv) / 3.0
        d_cur = (2.0 * d_prev + k_cur) / 3.0
        k[i] = k_cur
        d[i] = d_cur
        k_prev, d_prev = k_cur, d_cur

    j = 3.0 * k - 2.0 * d
    return k, d, j


def atr_wilder(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               period: int = 14) -> np.ndarray:
    """Wilder ATR。初值 = 前 `period` 根 TR 的简单平均; 之前的位置为 NaN。"""
    size = len(close)
    tr = np.empty(size, dtype=np.float64)
    tr[0] = float(high[0] - low[0])
    for i in range(1, size):
        pc = float(close[i - 1])
        tr[i] = max(float(high[i] - low[i]),
                    abs(float(high[i]) - pc),
                    abs(float(low[i]) - pc))

    out = np.full(size, np.nan, dtype=np.float64)
    if size < period:
        return out
    first = float(np.mean(tr[:period]))
    out[period - 1] = first
    prev = first
    for i in range(period, size):
        prev = (13.0 * prev + float(tr[i])) / 14.0
        out[i] = prev
    return out


def boll(close: np.ndarray, n: int = 20, k: float = 2.0
         ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """BOLL(n, k)。σ 用样本标准差 (ddof=1)。不足 n 根处为 NaN。"""
    size = len(close)
    mb = np.full(size, np.nan, dtype=np.float64)
    up = np.full(size, np.nan, dtype=np.float64)
    lb = np.full(size, np.nan, dtype=np.float64)
    sd = np.full(size, np.nan, dtype=np.float64)
    if size < n:
        return mb, up, lb, sd

    csum = np.concatenate(([0.0], np.cumsum(close)))
    csum2 = np.concatenate(([0.0], np.cumsum(close * close)))
    for i in range(n - 1, size):
        a = i - n + 1
        s = csum[i + 1] - csum[a]
        s2 = csum2[i + 1] - csum2[a]
        mean = s / n
        var = (s2 - n * mean * mean) / (n - 1)
        if var < 0.0:
            var = 0.0
        sigma = float(np.sqrt(var))
        mb[i] = mean
        sd[i] = sigma
        up[i] = mean + k * sigma
        lb[i] = mean - k * sigma
    return mb, up, lb, sd
