"""单一生效配置：优先完整 StrategyBundle，否则 legacy_compose。

页面 / 评分 / 执行必须共用同一份快照。
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
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
    dimension_weights: Any
    decision_thresholds: Any
    safety_valve_threshold: float
    flat_params: Dict[str, float]
    risk_per_trade_pct: Any = field(default_factory=dict)
    max_notional_pct: Any = field(default_factory=dict)
    total_max_notional_pct: float = 0.60
    exit_strategy: Any = field(default_factory=dict)
    staleness_limits: Any = field(default_factory=dict)
    load_ok: bool = True
    load_error: str = ""
    # 完整策略身份
    strategy_bundle: Any = None
    parameters_hash: str = ""
    implementation_id: str = ""
    strategy_identity: str = ""
    schema_version: str = ""
    parameters: Any = None
    pretrade_limits: Any = field(default_factory=dict)
    migration_note: str = ""

    def to_snapshot_dict(self) -> Dict[str, Any]:
        if self.strategy_bundle is not None and getattr(self.strategy_bundle, "to_snapshot_dict", None):
            snap = self.strategy_bundle.to_snapshot_dict()
            snap["fixed_at_ms"] = self.fixed_at_ms
            snap["load_ok"] = self.load_ok
            snap["load_error"] = self.load_error
            return snap
        from config.strategy_bundle import deep_unfreeze
        return {
            "version": self.version,
            "content_hash": self.content_hash,
            "parameters_hash": self.parameters_hash or self.content_hash,
            "implementation_id": self.implementation_id,
            "strategy_identity": self.strategy_identity,
            "schema_version": self.schema_version,
            "fixed_at_ms": self.fixed_at_ms,
            "params": dict(self.flat_params),
            "parameters": deep_unfreeze(self.parameters) if self.parameters is not None else {},
            "dimension_weights": deep_unfreeze(self.dimension_weights),
            "decision_thresholds": deep_unfreeze(self.decision_thresholds),
            "safety_valve_threshold": self.safety_valve_threshold,
            "risk_per_trade_pct": deep_unfreeze(self.risk_per_trade_pct),
            "max_notional_pct": deep_unfreeze(self.max_notional_pct),
            "total_max_notional_pct": self.total_max_notional_pct,
            "staleness_limits": deep_unfreeze(self.staleness_limits),
            "exit_strategy": deep_unfreeze(self.exit_strategy),
            "pretrade_limits": deep_unfreeze(self.pretrade_limits),
            "load_ok": self.load_ok,
            "load_error": self.load_error,
            "migration_note": self.migration_note,
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


def _from_bundle(bundle: Any, ts: int) -> EffectiveConfig:
    from config.strategy_bundle import deep_unfreeze
    p = bundle.parameters
    return EffectiveConfig(
        version=bundle.strategy_version,
        content_hash=bundle.parameters_hash,
        fixed_at_ms=ts,
        dimension_weights=p["DIMENSION_WEIGHTS"],
        decision_thresholds=p["DECISION_THRESHOLDS"],
        safety_valve_threshold=float(p["SAFETY_VALVE_THRESHOLD"]),
        flat_params={},
        risk_per_trade_pct=p["RISK_PER_TRADE_PCT"],
        max_notional_pct=p["MAX_NOTIONAL_PCT"],
        total_max_notional_pct=float(p["TOTAL_MAX_NOTIONAL_PCT"]),
        exit_strategy=p["EXIT_STRATEGY"],
        staleness_limits=p["STALENESS_LIMITS"],
        load_ok=bool(bundle.load_ok),
        load_error=str(bundle.load_error or ""),
        strategy_bundle=bundle,
        parameters_hash=bundle.parameters_hash,
        implementation_id=bundle.implementation_id,
        strategy_identity=bundle.strategy_identity,
        schema_version=bundle.schema_version,
        parameters=p,
        pretrade_limits=p.get("PRETRADE_LIMITS") or {},
        migration_note=str(getattr(bundle, "migration_note", "") or ""),
    )


def freeze_effective_config(
    *,
    allow_factory_fallback: bool = False,
    now_ms: Optional[int] = None,
    bundle: Any = None,
) -> EffectiveConfig:
    """评分开始前钉死完整策略配置。

    优先 StrategyBundle ACTIVE；否则 legacy_compose（完整工厂⊕旧 flat）。
    交易路径 allow_factory_fallback=False：失败则 load_ok=False，不得静默开仓。
    """
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    try:
        if bundle is None:
            from config.strategy_store import load_active_bundle
            bundle = load_active_bundle(allow_legacy_compose=True)
        cfg = _from_bundle(bundle, ts)
        if not cfg.load_ok and not allow_factory_fallback:
            return cfg
        if not cfg.load_ok and allow_factory_fallback:
            logger.warning("bundle invalid, research fallback: %s", cfg.load_error)
        return cfg
    except Exception as exc:  # noqa: BLE001
        load_error = f"load_strategy_bundle: {exc}"
        logger.error(load_error)
        weights, thresholds, safety = factory_nested()
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
        return EffectiveConfig(
            version="factory_fallback",
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
            load_ok=True,
            load_error=load_error,
        )


def apply_config_to_engine(engine: Any, cfg: EffectiveConfig) -> None:
    from config.strategy_bundle import deep_unfreeze
    engine.weights = deep_unfreeze(cfg.dimension_weights)
    engine.th = deep_unfreeze(cfg.decision_thresholds)
    engine.safety_valve_threshold = float(cfg.safety_valve_threshold)
    engine.config_version = cfg.version
    engine.config_hash = cfg.content_hash or cfg.parameters_hash
    engine.strategy_identity = cfg.strategy_identity
    engine.implementation_id = cfg.implementation_id
    params = deep_unfreeze(cfg.parameters) if cfg.parameters is not None else {}
    engine.enable_agreement_boost = bool(params.get("ENABLE_AGREEMENT_BOOST", False))
    engine.collinear_groups = params.get("COLLINEAR_GROUPS")
