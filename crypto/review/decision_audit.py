"""每轮决策审计记录 — 解释「为何交易 / 为何不交易」.

写入隔离路径；不记录密钥。轮转由 max_bytes 控制。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "runtime" / "review" / "decision_audit.jsonl"
_lock = threading.Lock()


@dataclass
class DecisionAuditRecord:
    decision_id: str
    horizon: str
    config_version: str
    content_hash: str
    started_at_ms: int
    finished_at_ms: int
    mark_price: Optional[float]
    atr: Optional[float]
    face_scores: Dict[str, Optional[float]] = field(default_factory=dict)
    face_confidences: Dict[str, float] = field(default_factory=dict)
    effective_weights: Dict[str, float] = field(default_factory=dict)
    contributions: Dict[str, float] = field(default_factory=dict)
    cs_raw: Optional[float] = None
    cs_final: Optional[float] = None
    decision_raw: str = "NEUTRAL"
    decision_final: str = "NEUTRAL"
    is_full_cs: bool = False
    signal_state: str = "no_signal"  # no_signal|signal_ok|data_unfit|risk_blocked|stale|expired|queued|filled|exec_error
    block_reasons: List[str] = field(default_factory=list)
    primary_block: str = ""
    tradable: bool = False
    staleness_sec: Dict[str, float] = field(default_factory=dict)
    missing_fields: List[str] = field(default_factory=list)
    safety_valve: bool = False
    overridden: bool = False
    pretrade: Dict[str, Any] = field(default_factory=dict)
    order_intent_id: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def new_decision_id() -> str:
    return f"D{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"


class DecisionAuditLog:
    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        max_bytes: int = 50_000_000,
        keep_bytes: int = 30_000_000,
    ) -> None:
        self.path = Path(path or DEFAULT_PATH)
        self.max_bytes = max_bytes
        self.keep_bytes = keep_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, rec: DecisionAuditRecord) -> None:
        line = json.dumps(rec.to_dict(), ensure_ascii=False, default=str)
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._rotate_if_needed()

    def _rotate_if_needed(self) -> None:
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
            raw = self.path.read_bytes()
            trimmed = raw[-self.keep_bytes:]
            # 对齐到下一行
            nl = trimmed.find(b"\n")
            if nl >= 0:
                trimmed = trimmed[nl + 1:]
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(trimmed)
                os.replace(tmp, self.path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except OSError as exc:
            logger.warning("decision_audit rotate failed: %s", exc)

    def summarize(self, *, since_ms: Optional[int] = None) -> Dict[str, Any]:
        """从完整审计记录生成观察报告摘要（非稀疏采样推断）。"""
        counts: Dict[str, int] = {}
        blocks: Dict[str, int] = {}
        css: List[float] = []
        signals = 0
        trades = 0
        n = 0
        if not self.path.exists():
            return {
                "records": 0,
                "note": "无决策审计记录；不得用稀疏观察点推断全程",
                "sampling_limited": True,
            }
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_ms and int(d.get("finished_at_ms") or 0) < since_ms:
                    continue
                n += 1
                dec = str(d.get("decision_final") or "NEUTRAL")
                counts[dec] = counts.get(dec, 0) + 1
                cs = d.get("cs_final")
                if isinstance(cs, (int, float)):
                    css.append(float(cs))
                st = str(d.get("signal_state") or "")
                if st == "signal_ok":
                    signals += 1
                if st == "filled":
                    trades += 1
                pb = str(d.get("primary_block") or "")
                if pb:
                    blocks[pb] = blocks.get(pb, 0) + 1
                for b in d.get("block_reasons") or []:
                    blocks[str(b)] = blocks.get(str(b), 0) + 1
        return {
            "records": n,
            "decision_counts": counts,
            "signal_ok_count": signals,
            "filled_count": trades,
            "block_counts": blocks,
            "cs_min": min(css) if css else None,
            "cs_max": max(css) if css else None,
            "cs_mean": (sum(css) / len(css)) if css else None,
            "sampling_limited": False,
        }
