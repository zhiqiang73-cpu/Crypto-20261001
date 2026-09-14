"""生效配置的合并、留档与回滚.

为什么不直接改写 config/weights.py:
  * 版本留档与回滚需要「不可变的历史」, 改写单一文件会把历史冲掉;
  * 代码文件被程序改写容易和人工编辑冲突, 且难以 review。

做法: config/weights.py 永远是「出厂默认」; 每次采纳建议就落一个
config/weights_versions/v<N>.json 快照 + 更新 ACTIVE.json 指针。
旧版本保留 → 一键回滚。effective_params() 负责把默认与生效版合并。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import config.weights as W
from config.review import ACTIVE_VERSION_FILE, TUNABLE_PARAMS, VERSIONS_DIR
from models.review import ParamChange

logger = logging.getLogger(__name__)

# 引擎真的会消费的参数组; 其余属于「已入档但需在调用点接入」的咨询项
ENGINE_APPLIED_GROUPS = {"dim_weight", "threshold", "safety_valve"}
ADVISORY_GROUPS = {"tech_mult"}


# --------------------------------------------------------------------------- 路径读写
def _read_path(path: str) -> Any:
    """'DIMENSION_WEIGHTS.short_term.news' → 0.15"""
    segs = path.split(".")
    obj: Any = getattr(W, segs[0])
    for s in segs[1:]:
        obj = obj[s]
    return obj


def _write_path(target: Dict[str, Any], path: str, value: Any) -> None:
    segs = path.split(".")
    obj = target
    for s in segs[:-1]:
        obj = obj.setdefault(s, {})
    obj[segs[-1]] = value


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- 版本
def flatten_defaults() -> Dict[str, float]:
    """出厂默认值 (config/weights.py)."""
    return {path: _read_path(path) for path in TUNABLE_PARAMS}


def list_versions() -> List[Dict[str, Any]]:
    if not VERSIONS_DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for f in sorted(VERSIONS_DIR.glob("v*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            doc["_file"] = f.name
            out.append(doc)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("overrides: 跳过损坏版本文件 %s: %s", f.name, exc)
    out.sort(key=lambda d: int(d.get("version", "v0").lstrip("v") or 0))
    return out


def active_version_name() -> Optional[str]:
    if not ACTIVE_VERSION_FILE.exists():
        return None
    try:
        return json.loads(ACTIVE_VERSION_FILE.read_text(encoding="utf-8")).get("version")
    except (json.JSONDecodeError, OSError):
        return None


def active_doc() -> Optional[Dict[str, Any]]:
    name = active_version_name()
    if not name:
        return None
    path = VERSIONS_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def effective_params() -> Dict[str, float]:
    """出厂默认 ⊕ 生效版本. 没有生效版本就是出厂默认."""
    params = flatten_defaults()
    doc = active_doc()
    if doc:
        for k, v in (doc.get("params") or {}).items():
            if k in params:
                params[k] = v
    return params


def current_version_label() -> str:
    return active_version_name() or "v1 (出厂默认)"


def _next_version_name() -> str:
    versions = list_versions()
    if not versions:
        return "v1"
    highest = max(int(v["version"].lstrip("v") or 0) for v in versions)
    return f"v{highest + 1}"


def _current_effective_doc(version: str, parent: Optional[str]) -> Dict[str, Any]:
    return {
        "version": version,
        "created_at_ms": int(time.time() * 1000),
        "parent": parent,
        "kind": "baseline",
        "source_proposal": "",
        "model": "",
        "valid_sample_count": 0,
        "changes": [],
        "params": effective_params(),
        "applied_groups": sorted(ENGINE_APPLIED_GROUPS),
        "advisory_groups": sorted(ADVISORY_GROUPS),
        "note": "首次留档: 出厂默认 (config/weights.py)",
    }


def ensure_baseline() -> Dict[str, Any]:
    """确保至少存在一个 v1 基线版本, 之后所有改动都能回滚到它."""
    if list_versions():
        doc = active_doc()
        if doc:
            return doc
        first = list_versions()[0]
        _atomic_write_json(ACTIVE_VERSION_FILE, {"version": first["version"]})
        return first
    doc = _current_effective_doc("v1", None)
    _atomic_write_json(VERSIONS_DIR / "v1.json", doc)
    _atomic_write_json(ACTIVE_VERSION_FILE, {"version": "v1"})
    return doc


def commit_version(
    changes: List[ParamChange],
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把一组已过护栏的改动落成新版本, 并切换 ACTIVE 指针."""
    meta = meta or {}
    baseline = ensure_baseline()
    params = effective_params()

    applied: List[Dict[str, Any]] = []
    for ch in changes:
        if ch.param not in params:
            continue
        prev = params[ch.param]
        params[ch.param] = ch.proposed
        applied.append({
            "param": ch.param,
            "from": prev,
            "to": ch.proposed,
            "delta": round(ch.proposed - prev, 6),
            "rationale": ch.rationale,
            "expected_effect": ch.expected_effect,
            "confidence": ch.confidence,
            "clamped": ch.clamped,
        })

    version = _next_version_name()
    doc = {
        "version": version,
        "created_at_ms": int(time.time() * 1000),
        "parent": baseline.get("version"),
        "kind": "tuned",
        "source_proposal": meta.get("proposal_id", ""),
        "model": meta.get("model", ""),
        "valid_sample_count": meta.get("valid_sample_count", 0),
        "diagnosis": meta.get("diagnosis", ""),
        "risks": meta.get("risks", ""),
        "changes": applied,
        "params": params,
        "applied_groups": sorted(ENGINE_APPLIED_GROUPS),
        "advisory_groups": sorted(ADVISORY_GROUPS),
        "note": "由复盘建议采纳生成",
    }
    _atomic_write_json(VERSIONS_DIR / f"{version}.json", doc)
    _atomic_write_json(ACTIVE_VERSION_FILE, {"version": version})
    logger.info("overrides: 已写入配置版本 %s (%d 项改动)", version, len(applied))
    return doc


def rollback(version: str) -> Dict[str, Any]:
    """回滚到指定版本 (该版本必须已留档)."""
    path = VERSIONS_DIR / f"{version}.json"
    if not path.exists():
        raise FileNotFoundError(f"版本 {version} 不存在")
    doc = json.loads(path.read_text(encoding="utf-8"))
    _atomic_write_json(ACTIVE_VERSION_FILE, {"version": doc["version"]})
    logger.info("overrides: 已回滚到 %s", doc["version"])
    return doc


# --------------------------------------------------------------------------- 引擎接入
def apply_to_engine(engine: Any, params: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """把生效参数写进 FactorScoringEngine 实例. 返回实际写入的项."""
    params = params or effective_params()
    written: Dict[str, Any] = {"weights": {}, "thresholds": {}, "safety_valve": None}

    weights = {k: dict(v) for k, v in W.DIMENSION_WEIGHTS.items()}
    thresholds = dict(W.DECISION_THRESHOLDS)
    safety = W.SAFETY_VALVE_THRESHOLD

    for path, value in params.items():
        segs = path.split(".")
        if segs[0] == "DIMENSION_WEIGHTS":
            weights[segs[1]][segs[2]] = value
            written["weights"][path] = value
        elif segs[0] == "DECISION_THRESHOLDS":
            thresholds[segs[1]] = value
            written["thresholds"][path] = value
        elif path == "SAFETY_VALVE_THRESHOLD":
            safety = value
            written["safety_valve"] = value

    engine.weights = weights
    engine.th = thresholds
    if hasattr(engine, "safety_valve_threshold"):
        engine.safety_valve_threshold = safety
    return written


def advisory_params(params: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """已入档但需在调用点接入的参数 (tech_mult 组)."""
    params = params or effective_params()
    return {k: v for k, v in params.items()
            if TUNABLE_PARAMS.get(k, {}).get("group") in ADVISORY_GROUPS}
