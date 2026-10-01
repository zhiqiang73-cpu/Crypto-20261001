"""短/长期策略合同 — 持有期、特征窗、准入门槛、冷却."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class StrategyContract:
    horizon: str
    hold_period: str
    feature_window: str
    decision_interval_sec: float
    min_coverage: float          # 可用权重占比门槛
    min_quality: float           # 数据质量门槛
    cooldown_after_exit_sec: float
    auto_trade_enabled: bool
    observe_only_reason: Optional[str] = None
    require_atr: bool = True
    neutral_forces_flat: bool = False  # NEUTRAL 不强制平仓

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


SHORT_TERM_CONTRACT = StrategyContract(
    horizon="short_term",
    hold_period="1h",
    feature_window="1h",
    decision_interval_sec=10.0,
    min_coverage=0.50,
    min_quality=0.60,
    cooldown_after_exit_sec=300.0,
    auto_trade_enabled=True,
    require_atr=True,
    neutral_forces_flat=False,
)

LONG_TERM_CONTRACT = StrategyContract(
    horizon="long_term",
    hold_period="30d",
    feature_window="1d",
    decision_interval_sec=120.0,
    min_coverage=0.60,
    min_quality=0.70,
    cooldown_after_exit_sec=3600.0,
    # 当前数据不足以支持月级论点 (新闻/宏观源不全、链上代理不可靠)
    auto_trade_enabled=False,
    observe_only_reason="长期策略待验证 — 自动交易暂禁, 仅保留评分观察",
    require_atr=True,
    neutral_forces_flat=False,
)

CONTRACTS = {
    "short_term": SHORT_TERM_CONTRACT,
    "long_term": LONG_TERM_CONTRACT,
}


def get_contract(horizon: str) -> StrategyContract:
    return CONTRACTS.get(horizon, SHORT_TERM_CONTRACT)
