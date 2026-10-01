"""策略包版本存储：不可覆盖写、原子激活、完整回滚。

与旧 config/weights_versions 并存；不自动改运行中 ACTIVE。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.review import PROJECT_ROOT
from config.strategy_bundle import (
    SCHEMA_VERSION,
    StrategyBundle,
    bundle_from_document,
    collect_factory_parameters,
    compose_legacy_active_bundle,
    compute_implementation_id,
    deep_unfreeze,
    make_bundle,
    apply_flat_overlays,
)

logger = logging.getLogger(__name__)

STRATEGY_VERSIONS_DIR = PROJECT_ROOT / "config" / "strategy_versions"
STRATEGY_ACTIVE_FILE = STRATEGY_VERSIONS_DIR / "ACTIVE.json"


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def list_strategy_versions(dir_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    root = dir_path or STRATEGY_VERSIONS_DIR
    if not root.exists():
        return []
    out = []
    for f in sorted(root.glob("sb_*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            doc["_file"] = f.name
            out.append(doc)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("skip bad strategy version %s: %s", f, exc)
    return out


def active_strategy_name(active_file: Optional[Path] = None) -> Optional[str]:
    path = active_file or STRATEGY_ACTIVE_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("strategy_version")
    except (json.JSONDecodeError, OSError):
        return None


def load_strategy_document(
    version: str, *, dir_path: Optional[Path] = None
) -> Dict[str, Any]:
    root = dir_path or STRATEGY_VERSIONS_DIR
    path = root / f"{version}.json"
    if not path.exists():
        raise FileNotFoundError(version)
    return json.loads(path.read_text(encoding="utf-8"))


def write_strategy_version(
    bundle: StrategyBundle,
    *,
    dir_path: Optional[Path] = None,
    overwrite: bool = False,
) -> Path:
    root = dir_path or STRATEGY_VERSIONS_DIR
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{bundle.strategy_version}.json"
    if path.exists() and not overwrite:
        raise FileExistsError(f"version immutable: {path.name}")
    if not bundle.load_ok:
        raise ValueError(f"refusing to write invalid bundle: {bundle.load_error}")
    doc = bundle.to_public_dict()
    # 元数据不进 parameters_hash；已在 bundle 内分离
    _atomic_write_json(path, doc)
    return path


def activate_strategy(
    version: str,
    *,
    dir_path: Optional[Path] = None,
    active_file: Optional[Path] = None,
    require_impl_match: bool = True,
    reason: str = "",
) -> StrategyBundle:
    root = dir_path or STRATEGY_VERSIONS_DIR
    active = active_file or STRATEGY_ACTIVE_FILE
    doc = load_strategy_document(version, dir_path=root)
    bundle = bundle_from_document(doc, verify_hash=True)
    if not bundle.load_ok:
        raise ValueError(f"activate rejected: {bundle.load_error}")
    current_impl = compute_implementation_id()
    if require_impl_match and bundle.implementation_id != current_impl:
        raise ValueError(
            "activate rejected: implementation_id mismatch — "
            "code changed since bundle was sealed; rollback code or re-seal"
        )
    prev = active_strategy_name(active)
    payload = {
        "strategy_version": bundle.strategy_version,
        "parameters_hash": bundle.parameters_hash,
        "implementation_id": bundle.implementation_id,
        "strategy_identity": bundle.strategy_identity,
        "activated_at_ms": int(time.time() * 1000),
        "previous": prev,
        "reason": reason or "activate",
    }
    _atomic_write_json(active, payload)
    logger.info(
        "strategy activated %s (was %s) identity=%s…",
        bundle.strategy_version, prev, bundle.strategy_identity[:12],
    )
    return bundle


def rollback_strategy(
    version: str,
    *,
    dir_path: Optional[Path] = None,
    active_file: Optional[Path] = None,
    reason: str = "rollback",
) -> StrategyBundle:
    return activate_strategy(
        version,
        dir_path=dir_path,
        active_file=active_file,
        require_impl_match=True,
        reason=reason,
    )


def reseal_active_strategy(
    *,
    dir_path: Optional[Path] = None,
    active_file: Optional[Path] = None,
    reason: str = "",
    note: str = "",
) -> StrategyBundle:
    """按当前实现指纹重新封存 ACTIVE 策略，建立新基线（历史版本保持不可变）。

    用于代码演进导致 implementation_id 漂移时**显式地**建立新基线，
    而不是关闭校验静默放行。行为：

      * 参数继承当前 ACTIVE 策略（不改策略数值）
      * implementation_id 取当前代码指纹
      * 生成全新版本号，绝不覆盖历史版本文件
      * 记录 parent_version、change_reason 与迁移说明，保证可审计
    """
    root = dir_path or STRATEGY_VERSIONS_DIR
    active = active_file or STRATEGY_ACTIVE_FILE
    current_impl = compute_implementation_id()
    name = active_strategy_name(active)

    parent: Optional[StrategyBundle] = None
    if name:
        try:
            parent = bundle_from_document(
                load_strategy_document(name, dir_path=root), verify_hash=True
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("reseal: 无法读取当前 ACTIVE %s: %s", name, exc)
            parent = None

    if parent is not None and parent.load_ok:
        parameters = deep_unfreeze(parent.parameters)
        parent_version: Optional[str] = parent.strategy_version
        previous_impl = parent.implementation_id
    else:
        parameters = collect_factory_parameters()
        parent_version = name
        previous_impl = ""

    new_version = f"sb_reseal_{int(time.time())}"
    bundle = make_bundle(
        strategy_version=new_version,
        parameters=parameters,
        parent_version=parent_version,
        change_reason=reason or "implementation_id_reseal",
        migration_note=note or (
            f"re-sealed against implementation_id {current_impl[:12]}; "
            f"previous sealed {(previous_impl or 'none')[:12]}"
        ),
        implementation_id=current_impl,
    )
    if not bundle.load_ok:
        raise ValueError(f"reseal produced invalid bundle: {bundle.load_error}")
    write_strategy_version(bundle, dir_path=root)
    activate_strategy(
        new_version,
        dir_path=root,
        active_file=active,
        require_impl_match=True,
        reason=reason or "implementation_id_reseal",
    )
    logger.warning(
        "strategy re-sealed: %s (parent=%s) impl %s → %s",
        new_version, parent_version, (previous_impl or "none")[:12], current_impl[:12],
    )
    return bundle


def load_active_bundle(
    *,
    dir_path: Optional[Path] = None,
    active_file: Optional[Path] = None,
    allow_legacy_compose: bool = True,
) -> StrategyBundle:
    """生产加载：优先完整策略 ACTIVE；否则 legacy_compose（明确非历史恢复）。"""
    active_path = active_file or STRATEGY_ACTIVE_FILE
    active_exists = active_path.exists()
    name = active_strategy_name(active_path)
    if active_exists and not name:
        return make_bundle(
            strategy_version="INVALID",
            parameters=collect_factory_parameters(),
            change_reason="corrupt_strategy_active",
            allow_invalid=True,
        ).__class__(
            **{
                **make_bundle(
                    strategy_version="INVALID",
                    parameters=collect_factory_parameters(),
                    change_reason="corrupt_strategy_active",
                ).__dict__,
                "load_ok": False,
                "load_error": "strategy ACTIVE exists but is unreadable or missing strategy_version",
            }
        )
    if name:
        try:
            doc = load_strategy_document(name, dir_path=dir_path)
            b = bundle_from_document(doc, verify_hash=True)
            if b.load_ok:
                # 实现漂移：阻断（load_ok=False）
                cur = compute_implementation_id()
                if b.implementation_id != cur:
                    return StrategyBundle(
                        schema_version=b.schema_version,
                        strategy_version=b.strategy_version,
                        parent_version=b.parent_version,
                        created_at_ms=b.created_at_ms,
                        change_reason=b.change_reason,
                        parameters=b.parameters,
                        parameters_hash=b.parameters_hash,
                        implementation_id=b.implementation_id,
                        strategy_identity=b.strategy_identity,
                        migration_note=b.migration_note,
                        unknown_historical_fields=b.unknown_historical_fields,
                        load_ok=False,
                        load_error=(
                            f"implementation_id drift: sealed={b.implementation_id[:12]} "
                            f"current={cur[:12]}"
                        ),
                    )
                return b
            return b
        except FileNotFoundError as exc:
            logger.error("ACTIVE strategy missing file: %s", exc)
            invalid = make_bundle(
                strategy_version="INVALID",
                parameters=collect_factory_parameters(),
                change_reason="active_target_missing",
            )
            return StrategyBundle(
                **{
                    **invalid.__dict__,
                    "load_ok": False,
                    "load_error": f"ACTIVE strategy file missing: {name}",
                }
            )
    if allow_legacy_compose:
        return compose_legacy_active_bundle()
    return make_bundle(
        strategy_version="INVALID",
        parameters=collect_factory_parameters(),
        change_reason="no_active",
        allow_invalid=True,
    )


def migrate_weights_active_to_bundle(
    *,
    new_version: str = "sb_m1_current_baseline",
    dir_path: Optional[Path] = None,
    write: bool = True,
) -> StrategyBundle:
    """将旧 weights ACTIVE 顶层值 + 当前代码子参数 → 完整迁移包。

    明确标记「当前行为基线迁移」，不覆盖原始 v1.json，不切换 ACTIVE。
    """
    from review.overrides import active_version_name, effective_params

    params = collect_factory_parameters()
    flat = effective_params()
    params = apply_flat_overlays(params, flat)
    parent = active_version_name() or "v1"
    unknown = [
        "sub_weights_at_original_v1_creation_time",
        "mapping_anchors_at_original_v1_creation_time",
        "exit_risk_pretrade_at_original_v1_creation_time",
        "implementation_id_at_original_v1_creation_time",
    ]
    bundle = make_bundle(
        strategy_version=new_version,
        parameters=params,
        parent_version=parent,
        change_reason="migration_from_partial_weights_ACTIVE",
        migration_note=(
            f"当前行为基线迁移：顶层来自 weights ACTIVE={parent}；"
            "其余参数来自选定工作基线（当前代码默认）。"
            "不能冒充历史 v1 的完整恢复。原始 config/weights_versions/v1.json 保持不变。"
        ),
        unknown_historical_fields=unknown,
    )
    if write and bundle.load_ok:
        write_strategy_version(bundle, dir_path=dir_path, overwrite=False)
    return bundle
