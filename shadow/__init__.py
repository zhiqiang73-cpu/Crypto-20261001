"""影子模式交易系统 (阶段 1)。

严格按用户规格实现: KDJ(9,3,3)+ATR(14)+BOLL(20,2), 15m 信号 / 1H 风险参数。
只记录信号与虚拟成交, 真实下单接口保持关闭。
"""

from shadow.engine import ShadowConfig, ShadowResult, run_shadow  # noqa: F401
from shadow.indicators import atr_wilder, boll, kdj  # noqa: F401

__all__ = ["ShadowConfig", "ShadowResult", "run_shadow", "kdj", "boll", "atr_wilder"]
