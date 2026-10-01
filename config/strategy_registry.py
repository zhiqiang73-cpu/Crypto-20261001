"""Claude 策略结论的声明式接入层。

策略只允许描述条件、风险和退出规则，不执行任意 Python/JS；通过校验后进入
待验证注册表，必须经过回测/模拟盘观察后才允许绑定到执行器。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.review import PROJECT_ROOT

REGISTRY_PATH = PROJECT_ROOT / "runtime" / "strategies" / "registry.json"
_ALLOWED_KINDS = {"mean_reversion", "trend_following", "breakout", "custom_signal"}
_ALLOWED_SIDES = {"long", "short", "both"}
_REQUIRED = {"strategy_id", "name", "kind", "timeframe", "entry", "exit", "risk"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,48}$")


def _error(message: str) -> ValueError:
    return ValueError(message)


def validate_strategy(spec: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        raise _error("strategy must be an object")
    missing = sorted(_REQUIRED - set(spec))
    if missing:
        raise _error("missing fields: " + ", ".join(missing))
    strategy_id = str(spec["strategy_id"]).strip().lower()
    if not _ID_RE.match(strategy_id):
        raise _error("strategy_id must match [a-z0-9][a-z0-9_-]{2,48}")
    kind = str(spec["kind"]).strip().lower()
    if kind not in _ALLOWED_KINDS:
        raise _error("unsupported kind: " + kind)
    timeframe = str(spec["timeframe"]).strip()
    if not timeframe or len(timeframe) > 16:
        raise _error("timeframe is required and must be short")
    for block in ("entry", "exit", "risk"):
        if not isinstance(spec[block], dict) or not spec[block]:
            raise _error(f"{block} must be a non-empty object")
    side = str(spec.get("side", "both")).lower()
    if side not in _ALLOWED_SIDES:
        raise _error("side must be long, short or both")
    if "leverage" in spec["risk"] and not 1 <= float(spec["risk"]["leverage"]) <= 5:
        raise _error("risk.leverage must be between 1 and 5")
    if "max_risk_pct" in spec["risk"]:
        risk = float(spec["risk"]["max_risk_pct"])
        if not 0 < risk <= 0.02:
            raise _error("risk.max_risk_pct must be in (0, 0.02]")
    clean = dict(spec)
    clean.update({"strategy_id": strategy_id, "kind": kind, "timeframe": timeframe, "side": side})
    clean.setdefault("status", "paper_only")
    clean.setdefault("enabled", False)
    clean.setdefault("created_at_ms", int(time.time() * 1000))
    clean.setdefault("source", "claude_conclusion")
    if clean["status"] not in {"paper_only", "testnet", "approved"}:
        raise _error("status must be paper_only, testnet or approved")
    # 防止把可执行代码塞入声明式策略
    if any(k in clean for k in ("python", "javascript", "exec", "shell", "code")):
        raise _error("executable code is not accepted in strategy specs")
    return clean


def load_registry(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    target = path or REGISTRY_PATH
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def upsert_strategy(spec: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    clean = validate_strategy(spec)
    target = path or REGISTRY_PATH
    rows = [row for row in load_registry(target) if row.get("strategy_id") != clean["strategy_id"]]
    rows.append(clean)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return clean
