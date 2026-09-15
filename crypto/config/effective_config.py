"""单一生效配置：出厂默认 ⊕ ACTIVE 版本 → 不可变决策快照.

页面 / 评分 / 执行必须共用同一份快照，禁止各自读不同常量。
ACTIVE 非法或无法加载时：交易路径应阻断，不得静默回退出厂默认继续下单。
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple

import config.weights as W
from config.review import (
    EXIT_STRATEGY,
    MAX_NOTIONAL_PCT,
    RISK_PER_TRADE_PCT,
    STALENESS_LIMITS,
    TOTAL_MAX_NOTIONAL_PCT,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EffectiveConfig:
    version: str
    content_hash: str
    fixed_at_ms: int
    dimension_weights: Dict[str, Dict[str, float]]
    decision_thresholds: Dict[str, float]
    safety_valve_threshold: float
    flat_params: Dict[str, float]
    risk_per_trade_pct: Dict[str, float] = field(default_factory=dict)
    max_notional_pct: Dict[str, float] = field(default_factory=dict)
    total_max_notional_pct: float = 0.60
    exit_strategy: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    staleness_limits: Dict[str, float] = field(default_factory=dict)
    load_ok: bool = True
    load_error: str = ""

    def to_snapshot_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "content_hash": self.content_hash,
            "fixed_at_ms": self.fixed_at_ms,
            "params": dict(self.flat_params),
            "dimension_weights": copy.deepcopy(self.dimension_weights),
            "decision_thresholds": dict(self.decision_thresholds),
            "safety_valve_threshold": self.safety_valve_threshold,
            "risk_per_trade_pct": dict(self.risk_per_trade_pct),
            "max_notional_pct": dict(self.max_notional_pct),
            "total_max_notional_pct": self.total_max_notional_pct,
            "staleness_limits": dict(self.staleness_limits),
            "load_ok": self.load_ok,
            "load_error": self.load_error,
        }


def factory_nested() -> Tuple[Dict, Dict, float]:
    weights = {k: dict(v) for k, v in W.DIMENSION_WEIGHTS.items()}
    thresholds = dict(W.DECISION_THRESHOLDS)
    safety = float(W.SAFETY_VALVE_THRESHOLD)
    return weights, thresholds, safety


def apply_flat_params(
    flat: Dict[str, float],
    weights: Dict[str, Dict[str, float]],
    thresholds: Dict[str, float],
    safety: float,
) -> Tuple[Dict, Dict, float]:
    w = {k: dict(v) for k, v in weights.items()}
    th = dict(thresholds)
    sv = float(safety)
    for path, value in (flat or {}).items():
        segs = path.split(".")
        if segs[0] == "DIMENSION_WEIGHTS" and len(segs) == 3:
            w.setdefault(segs[1], {})[segs[2]] = float(value)
        elif segs[0] == "DECISION_THRESHOLDS" and len(segs) == 2:
            th[segs[1]] = float(value)
        elif path == "SAFETY_VALVE_THRESHOLD":
            sv = float(value)
    return w, th, sv


def validate_config(
    weights: Dict[str, Dict[str, float]],
    thresholds: Dict[str, float],
) -> Optional[str]:
    for hz, w in weights.items():
        s = sum(float(x) for x in w.values())
        if abs(s - 1.0) > 1e-6:
            return f"dimension_weights[{hz}] sum={s} != 1"
        for k in ("news", "data", "tech", "prediction"):
            if k not in w:
                return f"dimension_weights[{hz}] missing {k}"
    required = (
        "strong_long", "standard_long", "watch_long",
        "neutral_upper", "neutral_lower",
        "watch_short", "standard_short", "strong_short",
    )
    for k in required:
        if k not in thresholds:
            return f"decision_thresholds missing {k}"
    # 单调：多头阈值递减，空头阈值递增（数值上 strong < standard < watch 对空头）
    if not (
        thresholds["strong_long"] >= thresholds["standard_long"]
        >= thresholds["watch_long"]
        >= thresholds["neutral_upper"]
    ):
        return "long thresholds not monotonic"
    if not (
        thresholds["strong_short"] <= thresholds["standard_short"]
        <= thresholds.get("watch_short", thresholds["neutral_lower"])
        <= thresholds["neutral_lower"]
    ):
        return "short thresholds not monotonic"
    return None


def freeze_effective_config(
    *,
    allow_factory_fallback: bool = False,
    now_ms: Optional[int] = None,
) -> EffectiveConfig:
    """评分开始前钉死配置.

    allow_factory_fallback=True 仅用于纯展示/离线研究；
    交易路径必须 allow_factory_fallback=False，加载失败则 load_ok=False。
    """
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    weights, thresholds, safety = factory_nested()
    version = "factory"
    flat: Dict[str, float] = {}
    content = ""
    load_ok = True
    load_error = ""
    try:
        from review.overrides import (
            active_version_name,
            content_hash,
            effective_params,
        )
        flat = dict(effective_params())
        version = active_version_name() or "factory"
        content = content_hash(flat)
        weights, thresholds, safety = apply_flat_params(flat, weights, thresholds, safety)
    except Exception as exc:  # noqa: BLE001
        load_ok = False
        load_error = f"load_effective_params: {exc}"
        logger.error(load_error)
        if not allow_factory_fallback:
            return EffectiveConfig(
                version="INVALID",
                content_hash="",
                fixed_at_ms=ts,
                dimension_weights=weights,
                decision_thresholds=thresholds,
                safety_valve_threshold=safety,
                flat_params={},
                risk_per_trade_pct=dict(RISK_PER_TRADE_PCT),
                max_notional_pct=dict(MAX_NOTIONAL_PCT),
                total_max_notional_pct=float(TOTAL_MAX_NOTIONAL_PCT),
                exit_strategy=copy.deepcopy(EXIT_STRATEGY),
                staleness_limits=dict(STALENESS_LIMITS),
                load_ok=False,
                load_error=load_error,
            )
        version = "factory_fallback"
        from review.overrides import content_hash as _ch, flatten_defaults
        flat = flatten_defaults()
        content = _ch(flat)

    err = validate_config(weights, thresholds)
    if err:
        load_ok = False
        load_error = err
        if not allow_factory_fallback:
            return EffectiveConfig(
                version=version,
                content_hash=content,
                fixed_at_ms=ts,
                dimension_weights=weights,
                decision_thresholds=thresholds,
                safety_valve_threshold=safety,
                flat_params=flat,
                risk_per_trade_pct=dict(RISK_PER_TRADE_PCT),
                max_notional_pct=dict(MAX_NOTIONAL_PCT),
                total_max_notional_pct=float(TOTAL_MAX_NOTIONAL_PCT),
                exit_strategy=copy.deepcopy(EXIT_STRATEGY),
                staleness_limits=dict(STALENESS_LIMITS),
                load_ok=False,
                load_error=load_error,
            )

    return EffectiveConfig(
        version=version,
        content_hash=content,
        fixed_at_ms=ts,
        dimension_weights=weights,
        decision_thresholds=thresholds,
        safety_valve_threshold=float(safety),
        flat_params=flat,
        risk_per_trade_pct=dict(RISK_PER_TRADE_PCT),
        max_notional_pct=dict(MAX_NOTIONAL_PCT),
        total_max_notional_pct=float(TOTAL_MAX_NOTIONAL_PCT),
        exit_strategy=copy.deepcopy(EXIT_STRATEGY),
        staleness_limits=dict(STALENESS_LIMITS),
        load_ok=load_ok,
        load_error=load_error,
    )


def apply_config_to_engine(engine: Any, cfg: EffectiveConfig) -> None:
    engine.weights = copy.deepcopy(cfg.dimension_weights)
    engine.th = dict(cfg.decision_thresholds)
    engine.safety_valve_threshold = float(cfg.safety_valve_threshold)
    engine.config_version = cfg.version
    engine.config_hash = cfg.content_hash
