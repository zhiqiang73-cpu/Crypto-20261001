"""完整策略配置包：参数全集、内容哈希、实现身份、深度不可变。

本模块只做配置治理，不改变任何策略数值含义。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import config.mapping as M
import config.weights as W
from config.review import (
    EXIT_STRATEGY,
    LEVERAGE_LONG_TERM,
    LEVERAGE_SHORT_TERM,
    MAX_NOTIONAL_PCT,
    POSITION_CS_STRONG_MULT,
    POSITION_CS_STRONG_THRESHOLD,
    POSITION_CONF_LOW_MULT,
    POSITION_CONF_LOW_THRESHOLD,
    POSITION_ATR_HIGH_MULT,
    POSITION_ATR_HIGH_RATIO,
    RISK_PER_TRADE_PCT,
    STALENESS_LIMITS,
    TOTAL_MAX_NOTIONAL_PCT,
)
from trading.pretrade import PretradeLimits

SCHEMA_VERSION = "strategy_bundle.v1"

# 参与 implementation_id 的源文件（解释参数的代码，不含默认数值文件）
_IMPL_FILES = (
    "indicators/engine.py",
    "engine/scorer.py",
    "mappers/data_mapper.py",
    "mappers/news_mapper.py",
    "mappers/tech_mapper.py",
    "mappers/prediction_mapper.py",
    "utils/scoring.py",
    "trading/pretrade.py",
    "trading/executor.py",
    "trading/risk_guardian.py",
    "trading/exit_checker.py",
    "trading/position_manager.py",
    "runtime/live_loop.py",
    "config/effective_config.py",
    "config/strategy_contract.py",
    "config/strategy_bundle.py",
)

# 纳入 parameters 的映射锚点（影响方向分）
_MAPPING_ANCHOR_NAMES = (
    "CPI_YOY_CHANGE_ANCHORS",
    "DXY_TREND_ANCHORS",
    "ETF_DAILY_FLOW_ANCHORS",
    "INDPRO_YOY_ANCHORS",
    "M2_YOY_ANCHORS",
    "USDT_NET_MINT_ANCHORS",
    "YIELD_CURVE_ANCHORS",
    "FUNDING_RATE_ANCHORS",
    "CVD_5M_ANCHORS",
    "HEATMAP_MAGNET_ANCHORS",
    "OI_CHANGE_5M_ANCHORS",
    "LIQ_REALTIME_USD_ANCHORS",
    "LIQ_SPEED_CLEARED_ANCHORS",
    "LONG_SHORT_RATIO_ANCHORS",
    "IV_ANCHORS",
    "MAX_PAIN_DISTANCE_ANCHORS",
    "BID_ASK_RATIO_ANCHORS",
    "WHALE_NET_BTC_ANCHORS",
    "HASHRATE_CHANGE_ANCHORS",
    "EXCHANGE_RESERVES_PROXY_ANCHORS",
    "MVRV_ANCHORS",
    "PREDICT_FUN_UP_ANCHORS",
    "FEAR_GREED_ANCHORS",
    "PROBABILITY_CHANGE_ANCHORS",
    "PROBABILITY_ANCHORS",
    "RELATIVE_PROB_DELTA_ANCHORS",
    "NEUTRAL_PROB_BY_REL",
    "FEDWATCH_PROXY_ANCHORS",
    "FEDWATCH_CUT_ONLY_ANCHORS",
    "FEDWATCH_HIKE_ONLY_ANCHORS",
)


Jsonable = Union[None, bool, int, float, str, List[Any], Dict[str, Any]]


def deep_freeze(obj: Any) -> Any:
    """递归不可变：dict→MappingProxyType，list→tuple。"""
    if isinstance(obj, Mapping) and not isinstance(obj, MappingProxyType):
        return MappingProxyType({str(k): deep_freeze(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(deep_freeze(v) for v in obj)
    if isinstance(obj, set):
        return tuple(sorted(deep_freeze(v) for v in obj))
    return obj


def deep_unfreeze(obj: Any) -> Any:
    """对外序列化用的可变深拷贝。"""
    if isinstance(obj, Mapping):
        return {k: deep_unfreeze(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        # 锚点等应为 list-of-pairs
        return [deep_unfreeze(v) for v in obj]
    return copy.deepcopy(obj) if isinstance(obj, (list, dict)) else obj


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parameters_hash(params: Mapping[str, Any]) -> str:
    plain = deep_unfreeze(params)
    return hashlib.sha256(canonical_json(plain).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_implementation_id(repo_root: Optional[Path] = None) -> str:
    """基于策略解释代码内容指纹，不单独信任可变的 Git HEAD。"""
    root = repo_root or Path(__file__).resolve().parents[1]
    parts: List[str] = []
    for rel in _IMPL_FILES:
        p = root / rel
        if p.is_file():
            parts.append(f"{rel}:{_file_sha256(p)}")
        else:
            parts.append(f"{rel}:MISSING")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _anchors_dict() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in _MAPPING_ANCHOR_NAMES:
        if hasattr(M, name):
            val = getattr(M, name)
            # 转为可 JSON 的 list
            if isinstance(val, (list, tuple)):
                out[name] = [list(x) if isinstance(x, (list, tuple)) else x for x in val]
            else:
                out[name] = val
    # 标量映射相关
    for name in (
        "BLACK_SWAN_LIQ_5M_USD",
        "SPREAD_BLACK_SWAN_MULT",
        "SPREAD_DANGER_MULT",
        "OI_DROP_5M_PCT",
        "OI_EXTREME_BUILD_24H_PCT",
        "FUNDING_EXTREME_ANNUAL_PCT",
        "FUNDING_NORMAL_ANNUAL_PCT",
        "SESSION_ASIA_MULTIPLIER",
        "SESSION_US_MULTIPLIER",
        "WHALE_EXCHANGE_BTC_THRESHOLD",
    ):
        if hasattr(M, name):
            out[name] = getattr(M, name)
    return out


def collect_factory_parameters() -> Dict[str, Any]:
    """从当前代码默认值采集完整行为参数（数值原样，不调优）。"""
    pre = PretradeLimits()
    return {
        "DIMENSION_WEIGHTS": copy.deepcopy(W.DIMENSION_WEIGHTS),
        "DECISION_THRESHOLDS": copy.deepcopy(W.DECISION_THRESHOLDS),
        "SAFETY_VALVE_THRESHOLD": float(W.SAFETY_VALVE_THRESHOLD),
        "NEWS_SUB_WEIGHTS": copy.deepcopy(W.NEWS_SUB_WEIGHTS),
        "DATA_LAYER_WEIGHTS": copy.deepcopy(W.DATA_LAYER_WEIGHTS),
        "ONCHAIN_INDICATOR_WEIGHTS": copy.deepcopy(W.ONCHAIN_INDICATOR_WEIGHTS),
        "DERIVATIVES_INDICATOR_WEIGHTS": copy.deepcopy(W.DERIVATIVES_INDICATOR_WEIGHTS),
        "MICROSTRUCTURE_INDICATOR_WEIGHTS": copy.deepcopy(W.MICROSTRUCTURE_INDICATOR_WEIGHTS),
        "TECH_INDICATOR_WEIGHTS": copy.deepcopy(W.TECH_INDICATOR_WEIGHTS),
        "PREDICTION_SUB_WEIGHTS": copy.deepcopy(W.PREDICTION_SUB_WEIGHTS),
        "ADX_STRONG": float(W.ADX_STRONG),
        "ADX_WEAK": float(W.ADX_WEAK),
        "ADX_BOOST": float(W.ADX_BOOST),
        "ADX_DAMPEN": float(W.ADX_DAMPEN),
        "BOLL_SQUEEZE_PERCENTILE": float(W.BOLL_SQUEEZE_PERCENTILE),
        "BOLL_SQUEEZE_BOOST": float(W.BOLL_SQUEEZE_BOOST),
        "ENABLE_AGREEMENT_BOOST": bool(W.ENABLE_AGREEMENT_BOOST),
        "AGREEMENT_BOOST_LO": float(W.AGREEMENT_BOOST_LO),
        "AGREEMENT_BOOST_HI": float(W.AGREEMENT_BOOST_HI),
        "ENABLE_CONSISTENCY_DAMPING": bool(getattr(W, "ENABLE_CONSISTENCY_DAMPING", False)),
        "COLLINEAR_GROUPS": copy.deepcopy(W.COLLINEAR_GROUPS),
        "RISK_PER_TRADE_PCT": copy.deepcopy(RISK_PER_TRADE_PCT),
        "MAX_NOTIONAL_PCT": copy.deepcopy(MAX_NOTIONAL_PCT),
        "TOTAL_MAX_NOTIONAL_PCT": float(TOTAL_MAX_NOTIONAL_PCT),
        "LEVERAGE_SHORT_TERM": int(LEVERAGE_SHORT_TERM),
        "LEVERAGE_LONG_TERM": int(LEVERAGE_LONG_TERM),
        "POSITION_CS_STRONG_MULT": float(POSITION_CS_STRONG_MULT),
        "POSITION_CS_STRONG_THRESHOLD": float(POSITION_CS_STRONG_THRESHOLD),
        "POSITION_CONF_LOW_THRESHOLD": float(POSITION_CONF_LOW_THRESHOLD),
        "POSITION_CONF_LOW_MULT": float(POSITION_CONF_LOW_MULT),
        "POSITION_ATR_HIGH_MULT": float(POSITION_ATR_HIGH_MULT),
        "POSITION_ATR_HIGH_RATIO": float(POSITION_ATR_HIGH_RATIO),
        "EXIT_STRATEGY": copy.deepcopy(EXIT_STRATEGY),
        "STALENESS_LIMITS": copy.deepcopy(STALENESS_LIMITS),
        "PRETRADE_LIMITS": {
            "signal_ttl_sec": float(pre.signal_ttl_sec),
            "max_quote_age_sec": float(pre.max_quote_age_sec),
            "max_adverse_atr": float(pre.max_adverse_atr),
            "max_spread_bps": float(pre.max_spread_bps),
            "min_remaining_rr": float(pre.min_remaining_rr),
        },
        "MAPPING": _anchors_dict(),
    }


def apply_flat_overlays(params: Dict[str, Any], flat: Mapping[str, float]) -> Dict[str, Any]:
    """将旧版 flat ACTIVE 覆盖写进完整 parameters（不增删未覆盖键）。"""
    out = copy.deepcopy(params)
    for path, value in (flat or {}).items():
        segs = path.split(".")
        if segs[0] == "DIMENSION_WEIGHTS" and len(segs) == 3:
            out.setdefault("DIMENSION_WEIGHTS", {}).setdefault(segs[1], {})[segs[2]] = float(value)
        elif segs[0] == "DECISION_THRESHOLDS" and len(segs) == 2:
            out.setdefault("DECISION_THRESHOLDS", {})[segs[1]] = float(value)
        elif path == "SAFETY_VALVE_THRESHOLD":
            out["SAFETY_VALVE_THRESHOLD"] = float(value)
        elif path in ("ADX_BOOST", "ADX_DAMPEN", "BOLL_SQUEEZE_BOOST"):
            out[path] = float(value)
        # 未知 flat 键忽略（旧格式可能有咨询项）
    return out


def _finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _weight_group_sum_ok(weights: Mapping[str, Any], *, allow_zero: bool = True) -> Optional[str]:
    if not isinstance(weights, Mapping):
        return "not a mapping"
    s = 0.0
    for k, v in weights.items():
        if not _finite(v):
            return f"non-finite {k}"
        fv = float(v)
        if fv < 0:
            return f"negative {k}"
        s += fv
    if abs(s) < 1e-12:
        return None if allow_zero else "all zero"
    # 允许历史不足 1 的组（运行时 renormalize）；禁止超过 1
    if s > 1.0 + 1e-6:
        return f"sum={s} > 1"
    return None


def validate_parameters(params: Mapping[str, Any]) -> Optional[str]:
    required = (
        "DIMENSION_WEIGHTS", "DECISION_THRESHOLDS", "SAFETY_VALVE_THRESHOLD",
        "NEWS_SUB_WEIGHTS", "DATA_LAYER_WEIGHTS", "ONCHAIN_INDICATOR_WEIGHTS",
        "DERIVATIVES_INDICATOR_WEIGHTS", "MICROSTRUCTURE_INDICATOR_WEIGHTS",
        "TECH_INDICATOR_WEIGHTS", "PREDICTION_SUB_WEIGHTS",
        "RISK_PER_TRADE_PCT", "EXIT_STRATEGY", "STALENESS_LIMITS", "PRETRADE_LIMITS",
        "MAPPING",
    )
    for k in required:
        if k not in params:
            return f"missing field {k}"

    allowed = {
        "DIMENSION_WEIGHTS", "DECISION_THRESHOLDS", "SAFETY_VALVE_THRESHOLD",
        "NEWS_SUB_WEIGHTS", "DATA_LAYER_WEIGHTS", "ONCHAIN_INDICATOR_WEIGHTS",
        "DERIVATIVES_INDICATOR_WEIGHTS", "MICROSTRUCTURE_INDICATOR_WEIGHTS",
        "TECH_INDICATOR_WEIGHTS", "PREDICTION_SUB_WEIGHTS", "ADX_STRONG",
        "ADX_WEAK", "ADX_BOOST", "ADX_DAMPEN", "BOLL_SQUEEZE_PERCENTILE",
        "BOLL_SQUEEZE_BOOST", "ENABLE_AGREEMENT_BOOST", "AGREEMENT_BOOST_LO",
        "AGREEMENT_BOOST_HI", "ENABLE_CONSISTENCY_DAMPING", "COLLINEAR_GROUPS",
        "RISK_PER_TRADE_PCT", "MAX_NOTIONAL_PCT", "TOTAL_MAX_NOTIONAL_PCT",
        "LEVERAGE_SHORT_TERM", "LEVERAGE_LONG_TERM", "POSITION_CS_STRONG_MULT",
        "POSITION_CS_STRONG_THRESHOLD", "POSITION_CONF_LOW_THRESHOLD",
        "POSITION_CONF_LOW_MULT", "POSITION_ATR_HIGH_MULT", "POSITION_ATR_HIGH_RATIO",
        "EXIT_STRATEGY", "STALENESS_LIMITS", "PRETRADE_LIMITS", "MAPPING",
    }
    unknown = sorted(set(params) - allowed)
    if unknown:
        return f"unknown fields: {', '.join(unknown)}"

    dw = params["DIMENSION_WEIGHTS"]
    if set(dw) != {"short_term", "long_term"}:
        return "DIMENSION_WEIGHTS must contain short_term and long_term only"
    for hz in ("short_term", "long_term"):
        group = dw.get(hz) or {}
        if set(group) != {"news", "data", "tech", "prediction"}:
            return f"DIMENSION_WEIGHTS.{hz} has incomplete/unknown dimensions"
        err = _weight_group_sum_ok(group, allow_zero=False)
        if err:
            return f"DIMENSION_WEIGHTS.{hz}: {err}"
        if abs(sum(float(v) for v in group.values()) - 1.0) > 1e-6:
            return f"DIMENSION_WEIGHTS.{hz}: sum must equal 1"

    th = params["DECISION_THRESHOLDS"]
    for k in (
        "strong_long", "standard_long", "watch_long",
        "neutral_upper", "neutral_lower",
        "watch_short", "standard_short", "strong_short",
    ):
        if k not in th or not _finite(th[k]):
            return f"thresholds missing/invalid {k}"
    if not (
        float(th["strong_long"]) >= float(th["standard_long"])
        >= float(th["watch_long"]) >= float(th["neutral_upper"])
    ):
        return "long thresholds not monotonic"
    if not (
        float(th["strong_short"]) <= float(th["standard_short"])
        <= float(th.get("watch_short", th["neutral_lower"]))
        <= float(th["neutral_lower"])
    ):
        return "short thresholds not monotonic"

    for layer_name in (
        "ONCHAIN_INDICATOR_WEIGHTS",
        "DERIVATIVES_INDICATOR_WEIGHTS",
        "MICROSTRUCTURE_INDICATOR_WEIGHTS",
        "TECH_INDICATOR_WEIGHTS",
        "PREDICTION_SUB_WEIGHTS",
        "DATA_LAYER_WEIGHTS",
    ):
        block = params[layer_name]
        for hz, w in block.items():
            err = _weight_group_sum_ok(w)
            if err:
                return f"{layer_name}.{hz}: {err}"

    news = params["NEWS_SUB_WEIGHTS"]
    for hz, w in news.items():
        err = _weight_group_sum_ok(w)
        if err:
            return f"NEWS_SUB_WEIGHTS.{hz}: {err}"

    mapping = params.get("MAPPING") or {}
    expected_mapping = set(_anchors_dict())
    if set(mapping) != expected_mapping:
        return "MAPPING has missing/unknown fields"
    for name, anchors in mapping.items():
        if name.endswith("_ANCHORS") and isinstance(anchors, (list, tuple)):
            prev = None
            for pair in anchors:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    return f"bad anchor {name}"
                if not _finite(pair[0]) or not _finite(pair[1]):
                    return f"non-finite anchor {name}"
                if prev is not None and float(pair[0]) < prev:
                    return f"anchor {name} not sorted by raw"
                prev = float(pair[0])

    risk = params["RISK_PER_TRADE_PCT"]
    for hz in ("short_term", "long_term"):
        if hz not in risk or not _finite(risk[hz]) or float(risk[hz]) <= 0:
            return f"bad RISK_PER_TRADE_PCT.{hz}"

    pre = params["PRETRADE_LIMITS"]
    expected_pre = {
        "signal_ttl_sec", "max_quote_age_sec", "max_adverse_atr",
        "max_spread_bps", "min_remaining_rr",
    }
    if set(pre) != expected_pre:
        return "PRETRADE_LIMITS has missing/unknown fields"
    for key in expected_pre:
        if not _finite(pre[key]) or float(pre[key]) < 0:
            return f"bad PRETRADE_LIMITS.{key}"

    for hz, ex in (params["EXIT_STRATEGY"] or {}).items():
        if float(ex.get("hard_sl_atr") or 0) <= 0 and float(ex.get("hard_sl_pct") or 0) <= 0:
            return f"EXIT_STRATEGY.{hz} missing hard SL"
    return None


@dataclass(frozen=True)
class StrategyBundle:
    schema_version: str
    strategy_version: str
    parent_version: Optional[str]
    created_at_ms: int
    change_reason: str
    parameters: Any  # frozen mapping
    parameters_hash: str
    implementation_id: str
    strategy_identity: str
    migration_note: str = ""
    unknown_historical_fields: Tuple[str, ...] = ()
    load_ok: bool = True
    load_error: str = ""

    @property
    def dimension_weights(self) -> Any:
        return self.parameters["DIMENSION_WEIGHTS"]

    @property
    def decision_thresholds(self) -> Any:
        return self.parameters["DECISION_THRESHOLDS"]

    @property
    def safety_valve_threshold(self) -> float:
        return float(self.parameters["SAFETY_VALVE_THRESHOLD"])

    def get(self, key: str, default: Any = None) -> Any:
        return self.parameters[key] if key in self.parameters else default

    def to_public_dict(self) -> Dict[str, Any]:
        """独立可变副本，修改不影响本 bundle。"""
        return {
            "schema_version": self.schema_version,
            "strategy_version": self.strategy_version,
            "parent_version": self.parent_version,
            "created_at_ms": self.created_at_ms,
            "change_reason": self.change_reason,
            "parameters": deep_unfreeze(self.parameters),
            "parameters_hash": self.parameters_hash,
            "implementation_id": self.implementation_id,
            "strategy_identity": self.strategy_identity,
            "migration_note": self.migration_note,
            "unknown_historical_fields": list(self.unknown_historical_fields),
            "load_ok": self.load_ok,
            "load_error": self.load_error,
        }

    def to_snapshot_dict(self) -> Dict[str, Any]:
        """兼容现有 live_loop / panel 字段，并附完整身份。"""
        p = deep_unfreeze(self.parameters)
        return {
            "version": self.strategy_version,
            "content_hash": self.parameters_hash,
            "parameters_hash": self.parameters_hash,
            "implementation_id": self.implementation_id,
            "strategy_identity": self.strategy_identity,
            "schema_version": self.schema_version,
            "fixed_at_ms": self.created_at_ms,
            "params": {},  # 旧 flat；完整参数见 parameters
            "parameters": p,
            "dimension_weights": p["DIMENSION_WEIGHTS"],
            "decision_thresholds": p["DECISION_THRESHOLDS"],
            "safety_valve_threshold": p["SAFETY_VALVE_THRESHOLD"],
            "risk_per_trade_pct": p["RISK_PER_TRADE_PCT"],
            "max_notional_pct": p["MAX_NOTIONAL_PCT"],
            "total_max_notional_pct": p["TOTAL_MAX_NOTIONAL_PCT"],
            "staleness_limits": p["STALENESS_LIMITS"],
            "exit_strategy": p["EXIT_STRATEGY"],
            "pretrade_limits": p["PRETRADE_LIMITS"],
            "load_ok": self.load_ok,
            "load_error": self.load_error,
            "migration_note": self.migration_note,
        }


def build_strategy_identity(parameters_hash: str, implementation_id: str) -> str:
    return hashlib.sha256(f"{parameters_hash}|{implementation_id}".encode("utf-8")).hexdigest()


def make_bundle(
    *,
    strategy_version: str,
    parameters: Dict[str, Any],
    parent_version: Optional[str] = None,
    change_reason: str = "",
    migration_note: str = "",
    unknown_historical_fields: Sequence[str] = (),
    implementation_id: Optional[str] = None,
    created_at_ms: Optional[int] = None,
    allow_invalid: bool = False,
) -> StrategyBundle:
    err = validate_parameters(parameters)
    impl = implementation_id or compute_implementation_id()
    ph = parameters_hash(parameters)
    ts = int(created_at_ms if created_at_ms is not None else time.time() * 1000)
    ok = err is None
    if err and not allow_invalid:
        ok = False
    elif err and allow_invalid:
        ok = False
    return StrategyBundle(
        schema_version=SCHEMA_VERSION,
        strategy_version=strategy_version,
        parent_version=parent_version,
        created_at_ms=ts,
        change_reason=change_reason,
        parameters=deep_freeze(parameters),
        parameters_hash=ph,
        implementation_id=impl,
        strategy_identity=build_strategy_identity(ph, impl),
        migration_note=migration_note,
        unknown_historical_fields=tuple(unknown_historical_fields),
        load_ok=ok,
        load_error=err or "",
    )


def bundle_from_document(doc: Mapping[str, Any], *, verify_hash: bool = True) -> StrategyBundle:
    schema = doc.get("schema_version")
    if schema != SCHEMA_VERSION:
        return StrategyBundle(
            schema_version=str(schema or ""),
            strategy_version=str(doc.get("strategy_version") or "INVALID"),
            parent_version=doc.get("parent_version"),
            created_at_ms=int(doc.get("created_at_ms") or 0),
            change_reason=str(doc.get("change_reason") or ""),
            parameters=deep_freeze(doc.get("parameters") or {}),
            parameters_hash=str(doc.get("parameters_hash") or ""),
            implementation_id=str(doc.get("implementation_id") or ""),
            strategy_identity=str(doc.get("strategy_identity") or ""),
            migration_note=str(doc.get("migration_note") or ""),
            unknown_historical_fields=tuple(doc.get("unknown_historical_fields") or ()),
            load_ok=False,
            load_error=f"schema_version want {SCHEMA_VERSION} got {schema}",
        )
    for identity_field in (
        "strategy_version", "parameters", "parameters_hash",
        "implementation_id", "strategy_identity",
    ):
        if not doc.get(identity_field):
            return StrategyBundle(
                schema_version=SCHEMA_VERSION,
                strategy_version=str(doc.get("strategy_version") or "INVALID"),
                parent_version=doc.get("parent_version"),
                created_at_ms=int(doc.get("created_at_ms") or 0),
                change_reason=str(doc.get("change_reason") or ""),
                parameters=deep_freeze(doc.get("parameters") or {}),
                parameters_hash=str(doc.get("parameters_hash") or ""),
                implementation_id=str(doc.get("implementation_id") or ""),
                strategy_identity=str(doc.get("strategy_identity") or ""),
                load_ok=False,
                load_error=f"missing identity field {identity_field}",
            )
    params = dict(doc.get("parameters") or {})
    ph = parameters_hash(params)
    if verify_hash and doc.get("parameters_hash") and doc["parameters_hash"] != ph:
        return StrategyBundle(
            schema_version=SCHEMA_VERSION,
            strategy_version=str(doc.get("strategy_version") or "INVALID"),
            parent_version=doc.get("parent_version"),
            created_at_ms=int(doc.get("created_at_ms") or 0),
            change_reason=str(doc.get("change_reason") or ""),
            parameters=deep_freeze(params),
            parameters_hash=ph,
            implementation_id=str(doc.get("implementation_id") or compute_implementation_id()),
            strategy_identity=str(doc.get("strategy_identity") or ""),
            migration_note=str(doc.get("migration_note") or ""),
            unknown_historical_fields=tuple(doc.get("unknown_historical_fields") or ()),
            load_ok=False,
            load_error="parameters_hash mismatch",
        )
    bundle = make_bundle(
        strategy_version=str(doc["strategy_version"]),
        parameters=params,
        parent_version=doc.get("parent_version"),
        change_reason=str(doc.get("change_reason") or ""),
        migration_note=str(doc.get("migration_note") or ""),
        unknown_historical_fields=doc.get("unknown_historical_fields") or (),
        implementation_id=str(doc.get("implementation_id") or compute_implementation_id()),
        created_at_ms=int(doc.get("created_at_ms") or time.time() * 1000),
    )
    if bundle.strategy_identity != str(doc.get("strategy_identity")):
        return StrategyBundle(**{
            **bundle.__dict__,
            "load_ok": False,
            "load_error": "strategy_identity mismatch",
        })
    return bundle


def compose_legacy_active_bundle() -> StrategyBundle:
    """当前生产兼容：完整工厂参数 ⊕ 旧 weights ACTIVE flat 覆盖。

    标记为 legacy_compose，不宣称是历史 v1 的完整恢复。
    """
    from review.overrides import active_version_name, effective_params

    params = collect_factory_parameters()
    flat = effective_params()
    params = apply_flat_overlays(params, flat)
    ver = active_version_name() or "factory"
    unknown = [
        "historical_sub_weights_at_v1_creation",
        "historical_mapping_anchors_at_v1_creation",
        "historical_exit_and_risk_at_v1_creation",
    ]
    return make_bundle(
        strategy_version=f"legacy_compose:{ver}",
        parameters=params,
        parent_version=ver,
        change_reason="legacy_compose_from_weights_ACTIVE_plus_factory_subparams",
        migration_note=(
            "旧 ACTIVE 仅含顶层权重/阈值；子权重与映射取自当前代码默认。"
            "不能冒充历史完整策略恢复。"
        ),
        unknown_historical_fields=unknown,
    )
