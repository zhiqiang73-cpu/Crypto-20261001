"""共享工具: 锚点线性插值、费率年化、时区判定。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Dict, Sequence, Tuple

from models.snapshots import SessionZone


def strategy_mapping_value(strategy_params: Optional[dict], name: str, default):
    """Read a mapping value from the sealed strategy snapshot.

    Callers may omit strategy_params only for legacy/offline compatibility.
    Production passes the full immutable decision snapshot.
    """
    mapping = (strategy_params or {}).get("MAPPING") or {}
    return mapping.get(name, default)


def interpolate_anchors(
    value: float,
    anchors: Sequence[Tuple[float, float]],
    clamp_score: Tuple[float, float] = (-100.0, 100.0),
) -> float:
    """在 (raw, score) 锚点之间线性插值, 两端钳制.

    anchors 必须按 raw 升序排列。
    """
    if not anchors:
        return 0.0

    sorted_a = sorted(anchors, key=lambda x: x[0])
    lo_raw, lo_score = sorted_a[0]
    hi_raw, hi_score = sorted_a[-1]

    if value <= lo_raw:
        return max(clamp_score[0], min(clamp_score[1], lo_score))
    if value >= hi_raw:
        return max(clamp_score[0], min(clamp_score[1], hi_score))

    for i in range(len(sorted_a) - 1):
        r0, s0 = sorted_a[i]
        r1, s1 = sorted_a[i + 1]
        if r0 <= value <= r1:
            if r1 == r0:
                score = s0
            else:
                t = (value - r0) / (r1 - r0)
                score = s0 + t * (s1 - s0)
            return max(clamp_score[0], min(clamp_score[1], score))

    return 0.0


def annualize_funding_rate(
    period_rate: float,
    period_hours: float = 8.0,
) -> float:
    """将当期资金费率年化为小数 (0.30 = 30% 年化).

    默认 Binance 8h 结算: annual = period * (24/period_hours) * 365
    """
    if period_hours <= 0:
        period_hours = 8.0
    periods_per_day = 24.0 / period_hours
    return period_rate * periods_per_day * 365.0


def infer_funding_period_hours(
    next_funding_time_ms: Optional[int],
    event_time_ms: Optional[int],
    default_hours: float = 8.0,
) -> float:
    """从下次结算时间粗估周期; 无法推断时回退默认 8h."""
    if next_funding_time_ms is None or event_time_ms is None:
        return default_hours
    delta_ms = next_funding_time_ms - event_time_ms
    if delta_ms <= 0:
        return default_hours
    hours = delta_ms / 3_600_000.0
    if 0.5 <= hours <= 12.0:
        for cand in (1.0, 4.0, 8.0):
            if hours <= cand + 0.1:
                return cand
        return default_hours
    return default_hours


def detect_session_zone(ts_ms: Optional[int] = None) -> SessionZone:
    """按 UTC 小时粗分美盘 / 亚洲盘.

    美盘约 UTC 13:00–21:00; 亚洲盘约 UTC 00:00–08:00.
    """
    if ts_ms is None:
        now = datetime.now(timezone.utc)
    else:
        now = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    hour = now.hour
    if 13 <= hour < 21:
        return SessionZone.US
    if 0 <= hour < 8:
        return SessionZone.ASIA
    return SessionZone.OTHER


def clamp(value: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def renormalized_weighted_sum(
    scores: dict,
    weights: dict,
    missing: Optional[list] = None,
) -> float:
    """只对有源指标加权; 缺项权重按比例分给可用项 (兜底).

    missing 中的键、权重 <= 0 的键均不参与. 可用权重之和为 0 → 0.
    """
    miss = set(missing or [])
    available = {
        k: float(w)
        for k, w in weights.items()
        if k not in miss and float(w) > 0
    }
    total = sum(available.values())
    if total <= 0:
        return 0.0
    return sum((w / total) * float(scores.get(k, 0.0)) for k, w in available.items())


def agreement_boost(
    face_scores: dict,
    weights: dict,
    boost_lo: float = 0.7,
    boost_hi: float = 1.3,
) -> float:
    """四面方向一致性放大: 全同向 → boost_hi; 对冲 → boost_lo.

    返回乘数 [boost_lo, boost_hi], 无可用面时返回 1.0.
    """
    w_sum = sum(float(w) for w in weights.values() if float(w) > 0)
    if w_sum <= 0:
        return 1.0
    base = sum(float(weights.get(k, 0)) * float(face_scores.get(k, 0)) for k in weights)
    if abs(base) < 1e-9:
        # 对冲到接近 0 → 压缩
        return boost_lo
    sign = 1.0 if base > 0 else -1.0
    agree_w = sum(
        float(w)
        for k, w in weights.items()
        if float(w) > 0 and float(face_scores.get(k, 0)) * sign > 0
    )
    agree_ratio = agree_w / w_sum  # [0, 1]
    return boost_lo + (boost_hi - boost_lo) * agree_ratio




def detect_collinear_whale(
    face_scores: Dict[str, float],
    whale_parts: Optional[Dict[str, float]] = None,
    *,
    min_abs: float = 10.0,
) -> Dict[str, float]:
    """从真实巨鲸子贡献识别跨面共线；生产路径调用，禁止仅靠测试塞标志。"""
    out = dict(face_scores)
    parts = whale_parts or {}
    wn = abs(float(parts.get("news_whale") or parts.get("whale_institutional") or 0))
    wd = abs(float(parts.get("data_whale") or parts.get("whale_transfers") or 0))
    if wn >= min_abs and wd >= min_abs:
        # 同向才标记
        s_news = float(face_scores.get("news") or 0)
        s_data = float(face_scores.get("data") or 0)
        if s_news * s_data > 0:
            out["_collinear_whale"] = True  # type: ignore
    return out

def apply_collinear_caps(
    face_scores: dict,
    weights: dict,
    confidences: Optional[dict] = None,
) -> tuple:
    """限制已知共线组对面贡献；返回 (adjusted_weights, note)."""
    try:
        groups = None
        # 优先使用引擎上的密封共线组（来自策略快照）
        # 调用方可通过 confidences["_collinear_groups"] 注入（测试）
        if confidences and confidences.get("_collinear_groups"):
            groups = confidences.get("_collinear_groups")
        if groups is None:
            from config.weights import COLLINEAR_GROUPS
            groups = COLLINEAR_GROUPS
    except Exception:
        return weights, {}
    w = {k: float(weights.get(k, 0)) for k in weights}
    notes = {}
    whale = (groups or {}).get("whale") or {}
    max_c = float(whale.get("max_combined_face_contrib") or 0)
    # 仅当映射层标记巨鲸两侧同时有贡献时才封顶（禁止把一切 news+data 同向都当巨鲸）
    whale_active = bool(face_scores.get("_collinear_whale")) or bool(
        (confidences or {}).get("_collinear_whale")
    )
    if max_c > 0 and whale_active:
        s_news = float(face_scores.get("news") or 0)
        s_data = float(face_scores.get("data") or 0)
        if s_news * s_data > 0:
            wn = w.get("news", 0)
            wd = w.get("data", 0)
            if wn + wd > max_c:
                scale = max_c / (wn + wd)
                w["news"] = wn * scale
                w["data"] = wd * scale
                notes["whale_cap_scale"] = scale
    return w, notes

def compute_cs_with_boost(
    face_scores: dict,
    weights: dict,
    boost_lo: float = 0.7,
    boost_hi: float = 1.3,
    confidences: Optional[dict] = None,
    *,
    enable_boost: Optional[bool] = None,
) -> tuple:
    """置信度加权 CS × 一致性放大, 返回 (cs, boost).

    enable_boost 默认读 config.weights.ENABLE_AGREEMENT_BOOST (默认 False).
    """
    weights, _cap_notes = apply_collinear_caps(face_scores, weights, confidences)
    if confidences:
        base, _ = confidence_weighted_cs(face_scores, weights, confidences)
    else:
        base = sum(
            float(weights.get(k, 0)) * float(face_scores.get(k, 0)) for k in weights
        )
    if enable_boost is None:
        try:
            from config.weights import ENABLE_AGREEMENT_BOOST
            enable_boost = bool(ENABLE_AGREEMENT_BOOST)
        except Exception:
            enable_boost = False
    if not enable_boost:
        return clamp(base), 1.0
    boost = agreement_boost(face_scores, weights, boost_lo, boost_hi)
    return clamp(base * boost), boost


def available_weight_ratio(
    weights: dict,
    missing: Optional[list] = None,
) -> float:
    """可用权重占比 [0, 1]. 全部缺失 → 0; 无正权重 → 0."""
    miss = set(missing or [])
    total = sum(float(w) for w in weights.values() if float(w) > 0)
    if total <= 0:
        return 0.0
    avail = sum(
        float(w)
        for k, w in weights.items()
        if k not in miss and float(w) > 0
    )
    return max(0.0, min(1.0, avail / total))


def shrink_by_confidence(score: float, confidence: float) -> float:
    """V8.2: 不再做 √C 面分收缩 — 置信度仅在 CS 合成层 (Wi×Ci) 生效.

    保留函数签名以兼容调用方; confidence 参数忽略.
    """
    return float(score)


def confidence_weighted_cs(
    face_scores: dict,
    weights: dict,
    confidences: Optional[dict] = None,
) -> tuple:
    """CS = Σ (Wi × Ci × Si) / Σ (Wi × Ci); 无置信度时退化为线性加权.

    返回 (cs, effective_weights_dict).
    """
    conf = confidences or {}
    eff: dict = {}
    for k, w in weights.items():
        fw = float(w)
        if fw <= 0:
            continue
        if k not in face_scores or face_scores[k] is None:
            continue
        c = float(conf.get(k, 1.0))
        c = max(0.0, min(1.0, c))
        if c <= 0:
            continue
        eff[k] = fw * c
    total = sum(eff.values())
    if total <= 0:
        return 0.0, {}
    cs = sum((eff[k] / total) * float(face_scores[k]) for k in eff)
    return clamp(cs), eff


def apply_consistency_damping(
    face_scores: dict,
    confidences: Optional[dict] = None,
    spread_trigger: float = 50.0,
    outlier_gap: float = 25.0,
) -> dict:
    """面间最大差异 > spread_trigger 时, 对离群面拉向中位数.

    alpha 与该面 confidence 正相关: 高置信保留更多原始值.
    返回新 dict, 不修改入参.
    """
    conf = confidences or {}
    keys = [k for k, v in face_scores.items() if v is not None]
    if len(keys) < 2:
        return dict(face_scores)

    vals = [float(face_scores[k]) for k in keys]
    lo, hi = min(vals), max(vals)
    if hi - lo <= spread_trigger:
        return dict(face_scores)

    sorted_v = sorted(vals)
    n = len(sorted_v)
    if n % 2 == 1:
        median = sorted_v[n // 2]
    else:
        median = 0.5 * (sorted_v[n // 2 - 1] + sorted_v[n // 2])

    out = dict(face_scores)
    for k in keys:
        s = float(face_scores[k])
        if abs(s - median) <= outlier_gap:
            continue
        # confidence 高 → alpha 高 → 更保留原值; 缺省 0.6
        c = float(conf.get(k, 0.6))
        c = max(0.0, min(1.0, c))
        alpha = 0.35 + 0.55 * c  # confidence=0 → 0.35; =1 → 0.90
        out[k] = s * alpha + median * (1.0 - alpha)
    return out


def estimate_neutral_prob(rel: float, *, strategy_params: Optional[dict] = None) -> float:
    """阈值相对距离 → 公平 Yes 概率 (上行 above 合约).

    rel = (threshold - mark) / mark. 负 rel (阈值略低于现价) → 公平概率 > 0.5.
    """
    from config.mapping import NEUTRAL_PROB_BY_REL

    anchors = strategy_mapping_value(
        strategy_params, "NEUTRAL_PROB_BY_REL", NEUTRAL_PROB_BY_REL
    )
    abs_rel = abs(float(rel))
    fair_above = interpolate_anchors(
        abs_rel, anchors, clamp_score=(0.01, 0.99)
    )
    if rel >= 0:
        return fair_above
    return 1.0 - fair_above


def map_polymarket_relative(
    prob: float,
    threshold: Optional[float],
    mark_price: Optional[float],
    *,
    strategy_params: Optional[dict] = None,
) -> float:
    """Polymarket 阈值概率 → 方向分 (阈值距离校准).

    有阈值+现价时: (实际 - 公平) 映射; 否则退回绝对概率锚点.
    """
    from config.mapping import PROBABILITY_ANCHORS, RELATIVE_PROB_DELTA_ANCHORS
    probability_anchors = strategy_mapping_value(
        strategy_params, "PROBABILITY_ANCHORS", PROBABILITY_ANCHORS
    )
    relative_anchors = strategy_mapping_value(
        strategy_params, "RELATIVE_PROB_DELTA_ANCHORS", RELATIVE_PROB_DELTA_ANCHORS
    )

    if (
        threshold is None
        or mark_price is None
        or mark_price <= 0
        or threshold <= 0
    ):
        return interpolate_anchors(prob, probability_anchors)

    rel = (float(threshold) - float(mark_price)) / float(mark_price)
    neutral = estimate_neutral_prob(rel, strategy_params=strategy_params)
    delta = float(prob) - neutral
    return interpolate_anchors(delta, relative_anchors)
