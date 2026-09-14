"""复盘 / 自我改进回路的强类型模型。

约定:
  * TradeRecord  — 一笔成交的完整档案 (四面读数 + 结算结果 + 备注)
  * ReviewStats  — 复盘池统计 (分母是有效样本, 不是成交笔数)
  * ProposalSet  — 模型的一次参数微调建议 (未采纳前不产生任何副作用)
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SettleStatus(str, Enum):
    """一笔交易的结算状态."""

    PENDING = "pending"     # 观察窗口未走完, 暂不计入分母
    CORRECT = "correct"     # 先触及目标 → 计入分母, 分子 +1
    WRONG = "wrong"         # 先触及止损 → 计入分母, 错误样本进复盘池
    INVALID = "invalid"     # 窗口走完两边都没触 (横盘) → 不计入分母
    EXCLUDED = "excluded"   # 中性不操作 / 被黑天鹅覆盖 → 不计入分母


class ProposalStatus(str, Enum):
    PENDING = "pending"     # 等人工确认
    ACCEPTED = "accepted"   # 已采纳并写入新版本
    REJECTED = "rejected"   # 已驳回
    BLOCKED = "blocked"     # 未过护栏, 整批作废


@dataclass
class TradeRecord:
    """一笔成交的完整档案."""

    trade_id: str
    opened_at_ms: int                 # 成交时间
    horizon: str                      # "short_term" | "long_term"
    entry_price: float
    scores: Dict[str, float]          # {"news":.., "data":.., "tech":.., "prediction":..}
    composite_score: float            # 四面加权后的 CS
    decision: str                     # ActionDecision.value
    atr: Optional[float] = None       # 入场时的 ATR (结算用)
    safety_valve: bool = False        # 当时安全阀是否触发
    overridden: bool = False          # 当时是否处于黑天鹅覆盖态
    missing_dimensions: List[str] = field(default_factory=list)
    source: str = "manual"            # "live_loop" | "manual"
    note: str = ""                    # 人工备注
    model_note: str = ""              # 模型对该笔的分析 (仅在错误样本上生成)
    config_version: str = ""          # 当时生效的配置版本, 便于归因

    # --- 结算结果 ---
    status: str = SettleStatus.PENDING.value
    settled_at_ms: Optional[int] = None
    exit_price: Optional[float] = None
    max_favorable_atr: Optional[float] = None   # 窗口内最有利走了多少倍 ATR
    max_adverse_atr: Optional[float] = None     # 窗口内最不利走了多少倍 ATR
    settle_detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TradeRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def direction(self) -> str:
        if self.composite_score > 0:
            return "LONG"
        if self.composite_score < 0:
            return "SHORT"
        return "NEUTRAL"

    @property
    def is_valid_sample(self) -> bool:
        """只有对/错进分母."""
        return self.status in (SettleStatus.CORRECT.value, SettleStatus.WRONG.value)


@dataclass
class ReviewStats:
    """复盘池统计. 分母 = 有效样本 (对 + 错)."""

    total: int = 0
    valid: int = 0
    correct: int = 0
    wrong: int = 0
    invalid: int = 0
    pending: int = 0
    excluded: int = 0

    win_rate: Optional[float] = None            # correct / valid
    target: int = 100
    remaining: int = 100                        # 距离达标线还差多少有效样本
    ready: bool = False                         # valid >= target
    by_horizon: Dict[str, Dict[str, int]] = field(default_factory=dict)
    by_tier: Dict[str, Dict[str, int]] = field(default_factory=dict)
    by_direction: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # 各面在「对」与「错」两组里的平均读数 — 用来找哪一面在骗人
    face_means: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    errors_since_last_review: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParamChange:
    """模型建议的单个参数改动 (已过护栏夹取)."""

    param: str                  # 点分路径, 必须在 TUNABLE_PARAMS 白名单内
    current: float
    proposed: float
    rationale: str = ""         # 为什么改
    expected_effect: str = ""   # 预期影响
    confidence: Optional[float] = None   # 0~1, 模型自评把握
    clamped: bool = False       # 是否被护栏夹取过
    clamp_note: str = ""

    @property
    def delta(self) -> float:
        return round(self.proposed - self.current, 6)

    @property
    def delta_pct(self) -> Optional[float]:
        if not self.current:
            return None
        return round((self.proposed - self.current) / abs(self.current), 4)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["delta"] = self.delta
        d["delta_pct"] = self.delta_pct
        return d


@dataclass
class ProposalSet:
    """模型的一次参数微调建议. 未采纳前不产生任何副作用."""

    proposal_id: str
    created_at_ms: int
    model: str
    valid_sample_count: int                 # 出建议时的有效样本数
    diagnosis: str = ""                     # 模型对错因的整体诊断
    changes: List[ParamChange] = field(default_factory=list)
    risks: str = ""                         # 模型自陈的过拟合/失效风险
    stats_snapshot: Dict[str, Any] = field(default_factory=dict)
    status: str = ProposalStatus.PENDING.value
    blocked_reason: str = ""
    applied_version: str = ""               # 采纳后写入的配置版本
    decided_at_ms: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["changes"] = [c.to_dict() for c in self.changes]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProposalSet":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in d.items() if k in known}
        payload["changes"] = [ParamChange(**c) for c in d.get("changes", [])]
        return cls(**payload)


def now_ms() -> int:
    return int(time.time() * 1000)
