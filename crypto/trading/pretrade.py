"""成交前复核：信号有效期、执行场所新鲜价、不利偏移、点差。

工程保守初值（可配置）；统计校准值需样本外验证后另档，不得为过测试而放宽。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PretradeLimits:
    signal_ttl_sec: float = 120.0
    max_quote_age_sec: float = 5.0
    max_adverse_atr: float = 0.5
    max_spread_bps: float = 8.0
    min_remaining_rr: float = 0.8  # 相对设计 RR 的保守残留比；无目标时跳过


@dataclass
class PretradeResult:
    ok: bool
    reasons: List[str] = field(default_factory=list)
    quote_age_sec: Optional[float] = None
    adverse_move: Optional[float] = None
    adverse_atr: Optional[float] = None
    spread_bps: Optional[float] = None
    signal_age_sec: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def recheck_entry(
    *,
    side: str,
    signal_price: float,
    signal_ts_ms: int,
    now_ms: int,
    exec_mark: float,
    quote_ts_ms: Optional[int],
    atr: Optional[float],
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    limits: Optional[PretradeLimits] = None,
    target_rr: Optional[float] = None,
    hard_sl_distance: Optional[float] = None,
    tp_distance: Optional[float] = None,
) -> PretradeResult:
    lim = limits or PretradeLimits()
    reasons: List[str] = []
    signal_age = max(0.0, (now_ms - signal_ts_ms) / 1000.0)
    if signal_age > lim.signal_ttl_sec:
        reasons.append("signal_expired")

    quote_age = None
    if quote_ts_ms is None:
        reasons.append("quote_timestamp_missing")
    else:
        quote_age = max(0.0, (now_ms - quote_ts_ms) / 1000.0)
        if quote_age > lim.max_quote_age_sec:
            reasons.append("quote_stale")

    if exec_mark <= 0 or signal_price <= 0:
        reasons.append("invalid_price")
        return PretradeResult(ok=False, reasons=reasons, signal_age_sec=signal_age, quote_age_sec=quote_age)

    if side.upper() == "LONG":
        adverse = max(0.0, exec_mark - signal_price)
    else:
        adverse = max(0.0, signal_price - exec_mark)

    adverse_atr = None
    if atr is not None and atr > 0:
        adverse_atr = adverse / atr
        if adverse_atr > lim.max_adverse_atr:
            reasons.append("adverse_move_atr")
    else:
        reasons.append("atr_missing")

    spread_bps = None
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        mid = 0.5 * (bid + ask)
        spread_bps = (ask - bid) / mid * 10000.0
        if spread_bps > lim.max_spread_bps:
            reasons.append("spread_too_wide")

    # 剩余 RR：价格已朝不利方向走后，相对原 TP/SL 是否还够
    if (
        hard_sl_distance is not None
        and tp_distance is not None
        and hard_sl_distance > 0
        and tp_distance > 0
    ):
        if side.upper() == "LONG":
            rem_tp = max(0.0, (signal_price + tp_distance) - exec_mark)
            rem_sl = max(1e-9, exec_mark - (signal_price - hard_sl_distance))
        else:
            rem_tp = max(0.0, exec_mark - (signal_price - tp_distance))
            rem_sl = max(1e-9, (signal_price + hard_sl_distance) - exec_mark)
        rem_rr = rem_tp / rem_sl
        design_rr = tp_distance / hard_sl_distance
        if design_rr > 0 and rem_rr / design_rr < lim.min_remaining_rr:
            reasons.append("remaining_rr_insufficient")
        # 禁止「平移止盈止损假装 RR 不变」——此处只评估相对原目标的残留
        _ = target_rr  # 保留接口；不根据新价重画目标

    return PretradeResult(
        ok=not reasons,
        reasons=reasons,
        quote_age_sec=quote_age,
        adverse_move=adverse,
        adverse_atr=adverse_atr,
        spread_bps=spread_bps,
        signal_age_sec=signal_age,
    )
