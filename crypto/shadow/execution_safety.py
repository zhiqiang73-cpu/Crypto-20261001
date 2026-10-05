"""执行前只读风控：保守估计可用保证金、所有合约的总名义敞口。

限制是部署候选值，尚未经测试网交易验证；与策略权益/止损预算应联合审批。
"""
from __future__ import annotations

import math
from typing import Any, Iterable

# 10x 逐仓下仅准许钱包权益最多 5x 总名义额；预留 20% 可用余额。
MAX_GROSS_LEVERAGE = 5.0
AVAILABLE_RESERVE = 0.20
MARGIN_BUFFER = 1.20
PRICE_BUFFER = 1.01  # 盘口快照到实际限价可能发生跳价


class ExpansionBlocked(RuntimeError):
    """扩仓预检失败；不允许通过重复提交绕过。"""


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ExpansionBlocked(f"{name} 缺失或无效") from exc
    if not math.isfinite(number):
        raise ExpansionBlocked(f"{name} 非有限值")
    return number


def check_expansion(*, balance: Any, risks: Iterable[dict], symbol: str,
                    exchange_signed: float, quantity: float, book: dict,
                    leverage: int, min_qty: float) -> dict:
    """只检查从已确认净仓同向扩仓；减仓/平仓由 reduceOnly 单独保证。

    positionRisk 必须覆盖整个账户，包含非策略标的。快照缺损/不一致时拒绝扩仓。
    此函数不能防止外部并发交易：接入前仍需单实例与账户独占验证。
    """
    ex = _finite(exchange_signed, "净仓")
    qty = _finite(quantity, "扩仓数量")
    lev = _finite(leverage, "杠杆")
    if qty < min_qty or lev <= 0 or not symbol:
        raise ExpansionBlocked("扩仓数量或杠杆无效")
    wallet = _finite(getattr(balance, "total_wallet_balance", None), "钱包余额")
    available = _finite(getattr(balance, "available_balance", None), "可用保证金")
    upnl = _finite(getattr(balance, "total_unrealized_pnl", None), "未实现盈亏")
    equity = wallet + min(0.0, upnl)  # 正浮盈不作为扩仓额度
    if equity <= 0 or available <= 0:
        raise ExpansionBlocked(f"权益/可用保证金不足 equity={equity:.2f} available={available:.2f}")
    bid = _finite(book.get("bid"), "最优买价")
    ask = _finite(book.get("ask"), "最优卖价")
    if bid <= 0 or ask <= 0 or bid > ask:
        raise ExpansionBlocked("盘口无效")
    price = max(bid, ask) * PRICE_BUFFER
    gross = symbol_net = 0.0
    if not isinstance(risks, list):
        raise ExpansionBlocked("账户仓位快照不是完整列表")
    for row in risks:
        if not isinstance(row, dict):
            raise ExpansionBlocked("账户仓位数据损坏")
        amt = _finite(row.get("positionAmt"), "positionAmt")
        if abs(amt) < 1e-12:
            continue
        name = str(row.get("symbol") or "")
        mark = _finite(row.get("markPrice"), f"{name} 标记价")
        if not name or mark <= 0:
            raise ExpansionBlocked("持仓标的或标记价缺失")
        reported = row.get("notional")
        notion = abs(_finite(reported, f"{name} 名义价值")) if reported is not None else 0.0
        gross += max(notion, abs(amt * mark))
        if name == symbol:
            symbol_net += amt
    if abs(symbol_net - ex) >= min_qty / 2:
        raise ExpansionBlocked(f"{symbol} 仓位快照不一致 {symbol_net:.6f} != {ex:.6f}")
    incremental = qty * price
    required = incremental / lev * MARGIN_BUFFER
    if required > available * (1 - AVAILABLE_RESERVE) + 1e-9:
        raise ExpansionBlocked(f"保证金不足 need={required:.2f} safe_available={available * (1 - AVAILABLE_RESERVE):.2f}")
    gross_after = gross + incremental  # 不抵扣旧仓，保守上界
    limit = MAX_GROSS_LEVERAGE * equity
    if gross_after > limit + 1e-9:
        raise ExpansionBlocked(f"总名义敞口超限 after={gross_after:.2f} limit={limit:.2f}")
    return {"equity": equity, "available": available, "gross_before": gross,
            "gross_after": gross_after, "limit": limit, "required_margin": required}
