"""风险预算、往返成本与净保本价计算。

本模块只做算术，不碰网络、不碰账本、不碰交易所 —— 所有函数都是纯函数，
便于逐项核对口径。交易所侧的挂单/撤单在 trading/protective_orders.py。

口径来源（2026-10-04 用真实成交反算确认）：
    币安 USDⓈ-M 测试网，maker 2 bp、taker 4 bp。
    实测：BTC 全 taker 往返 = 8.00 bp；混合 = 5.95 bp；ETH 混合 = 6.00 bp。

⚠ 本模块给出的是**计划风险预算**，不是「保证最大亏损」。
   STOP_MARKET 触发后按市价成交，实际成交价可能劣于触发价；滑点预算是一
   个保守假设，不是上限。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# 费率与滑点预算
# ---------------------------------------------------------------------------
MAKER_FEE_RATE = 0.0002      # 2 bp —— post-only 挂单成交
TAKER_FEE_RATE = 0.0004      # 4 bp —— 穿盘口成交 / STOP_MARKET 触发

# 止损退出的滑点预算。STOP_MARKET 触发后以市价成交，成交价通常劣于触发价。
# 取 5 bp 作为保守假设：BTC 15m 的 ATR 约 0.15%~0.25%，5 bp 约为其 1/4。
# 这不是上限，只是预算；真实滑点在剧烈行情下可能更大。
DEFAULT_SLIPPAGE_RATE = 0.0005

# 资金费预留：持仓可能跨过多个资金费结算点。每 8 小时一次，按名义收取。
# 作为预算项预留，不是已发生额。
DEFAULT_FUNDING_RESERVE_RATE = 0.0002   # 2 bp

# ---------------------------------------------------------------------------
# 风险预算（按当时账户权益的比例）
# ---------------------------------------------------------------------------
RISK_PER_SYMBOL_RATIO = 0.005    # 单标的计划最大亏损 = 权益的 0.5%
RISK_PORTFOLIO_RATIO = 0.01      # BTC+ETH 合计 = 权益的 1.0%


@dataclass
class RiskBreakdown:
    """单笔交易的每单位风险拆解（单位：计价货币 / 每 1 个标的单位）。

    每单位风险 = 止损距离 + 入场费 + 止损退出费 + 滑点预算 + 资金费预留
    """
    stop_distance: float = 0.0
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    slippage: float = 0.0
    funding: float = 0.0

    @property
    def total(self) -> float:
        return (self.stop_distance + self.entry_fee + self.exit_fee
                + self.slippage + self.funding)

    def as_dict(self) -> dict:
        return {
            "止损距离": round(self.stop_distance, 8),
            "入场手续费": round(self.entry_fee, 8),
            "止损退出手续费": round(self.exit_fee, 8),
            "滑点预算": round(self.slippage, 8),
            "资金费预留": round(self.funding, 8),
            "合计每单位风险": round(self.total, 8),
        }


@dataclass
class SizingPlan:
    """按风险预算倒算出来的仓位计划。"""
    ok: bool
    quantity: float = 0.0
    risk_per_unit: float = 0.0
    planned_loss: float = 0.0          # = quantity × risk_per_unit
    risk_budget: float = 0.0
    notional: float = 0.0
    reason: str = ""
    breakdown: RiskBreakdown = field(default_factory=RiskBreakdown)

    def as_dict(self) -> dict:
        return {
            "可下单": self.ok,
            "数量": round(self.quantity, 8),
            "每单位风险": round(self.risk_per_unit, 8),
            "计划最大亏损": round(self.planned_loss, 4),
            "风险预算": round(self.risk_budget, 4),
            "名义价值": round(self.notional, 2),
            "说明": self.reason,
            **self.breakdown.as_dict(),
        }


# ---------------------------------------------------------------------------
# 每单位风险
# ---------------------------------------------------------------------------
def risk_per_unit(
    *,
    entry_price: float,
    stop_price: float,
    entry_fee_rate: float = TAKER_FEE_RATE,
    exit_fee_rate: float = TAKER_FEE_RATE,
    slippage_rate: float = DEFAULT_SLIPPAGE_RATE,
    funding_rate: float = DEFAULT_FUNDING_RESERVE_RATE,
) -> RiskBreakdown:
    """单笔新开仓的每单位风险拆解。

    entry_fee 用**预计**入场费率；已有仓位要算真实风险时，请改用
    risk_per_unit_existing()，它吃真实已付手续费与已发生资金费。
    """
    if entry_price <= 0 or stop_price <= 0:
        return RiskBreakdown()
    dist = abs(entry_price - stop_price)
    return RiskBreakdown(
        stop_distance=dist,
        entry_fee=entry_price * entry_fee_rate,
        # 退出按止损价计：止损成交时的名义是 stop_price × qty
        exit_fee=stop_price * exit_fee_rate,
        slippage=stop_price * slippage_rate,
        funding=entry_price * funding_rate,
    )


def risk_per_unit_existing(
    *,
    avg_price: float,
    stop_price: float,
    quantity: float,
    entry_fee_paid: float,
    funding_paid: float,
    exit_fee_rate: float = TAKER_FEE_RATE,
    slippage_rate: float = DEFAULT_SLIPPAGE_RATE,
) -> RiskBreakdown:
    """已有仓位的每单位风险 —— 用**真实已发生**费用，不用费率假设。

    ⚠ 与 risk_per_unit 的区别：入场费和资金费来自交易所流水，是事实；
    退出费与滑点是预算。费用缺失时（拿不到流水）由调用方决定是否放弃，
    本函数不替调用方猜。
    """
    if avg_price <= 0 or stop_price <= 0 or quantity <= 0:
        return RiskBreakdown()
    dist = abs(avg_price - stop_price)
    return RiskBreakdown(
        stop_distance=dist,
        entry_fee=abs(entry_fee_paid) / quantity,
        exit_fee=stop_price * exit_fee_rate,
        slippage=stop_price * slippage_rate,
        funding=abs(funding_paid) / quantity,
    )


def planned_loss(
    *, quantity: float, entry_price: float, stop_price: float,
    entry_fee_paid: float = 0.0, funding_paid: float = 0.0,
    exit_fee_rate: float = TAKER_FEE_RATE,
    slippage_rate: float = DEFAULT_SLIPPAGE_RATE,
) -> float:
    """按「当前仓位 + 给定止损价」算计划最大亏损（计价货币）。

    这是保护单真正要覆盖的金额，用于「加层后是否超预算」的判定。
    """
    if quantity <= 0 or entry_price <= 0 or stop_price <= 0:
        return 0.0
    rb = risk_per_unit_existing(
        avg_price=entry_price, stop_price=stop_price, quantity=quantity,
        entry_fee_paid=entry_fee_paid, funding_paid=funding_paid,
        exit_fee_rate=exit_fee_rate, slippage_rate=slippage_rate,
    )
    return quantity * rb.total


# ---------------------------------------------------------------------------
# 按风险预算倒算仓位
# ---------------------------------------------------------------------------
def _round_down_to_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    # 用整数刻度做地板，避免浮点误差把 0.296 变成 0.29599999
    n = math.floor(round(qty / step, 9))
    return round(n * step, 12)


def plan_quantity(
    *,
    equity: float,
    entry_price: float,
    stop_price: float,
    step: float,
    min_notional: float = 0.0,
    entry_fee_rate: float = TAKER_FEE_RATE,
    exit_fee_rate: float = TAKER_FEE_RATE,
    slippage_rate: float = DEFAULT_SLIPPAGE_RATE,
    funding_rate: float = DEFAULT_FUNDING_RESERVE_RATE,
    risk_budget: Optional[float] = None,
    risk_ratio: float = RISK_PER_SYMBOL_RATIO,
) -> SizingPlan:
    """按风险预算倒算允许的仓位数量，按交易所步长**向下**取整。

    不靠拉近止损来硬凑仓位 —— 止损距离是输入，不是可调项。
    算出来低于最小名义价值时返回 ok=False，由调用方跳过该笔交易。
    """
    if risk_budget is None:
        risk_budget = max(0.0, float(equity) * risk_ratio)
    if risk_budget <= 0:
        return SizingPlan(ok=False, reason="风险预算为 0（权益未知或非正）")
    if entry_price <= 0 or stop_price <= 0:
        return SizingPlan(ok=False, reason="入场价或止损价非法")
    if abs(entry_price - stop_price) <= 0:
        return SizingPlan(ok=False, risk_budget=risk_budget,
                          reason="止损距离为 0，无法按风险定仓")

    rb = risk_per_unit(
        entry_price=entry_price, stop_price=stop_price,
        entry_fee_rate=entry_fee_rate, exit_fee_rate=exit_fee_rate,
        slippage_rate=slippage_rate, funding_rate=funding_rate,
    )
    if rb.total <= 0:
        return SizingPlan(ok=False, risk_budget=risk_budget,
                          reason="每单位风险为 0")

    raw_qty = risk_budget / rb.total
    qty = _round_down_to_step(raw_qty, step)
    if qty <= 0:
        return SizingPlan(ok=False, risk_budget=risk_budget,
                          risk_per_unit=rb.total, breakdown=rb,
                          reason=f"风险预算 {risk_budget:.2f} 只够 "
                                 f"{raw_qty:.8f}，低于步长 {step}")
    notional = qty * entry_price
    if min_notional > 0 and notional < min_notional:
        return SizingPlan(ok=False, quantity=qty, risk_budget=risk_budget,
                          risk_per_unit=rb.total, notional=notional,
                          breakdown=rb,
                          reason=f"名义 {notional:.2f} 低于最小下单额 "
                                 f"{min_notional:.2f}，跳过该笔")
    return SizingPlan(
        ok=True, quantity=qty, risk_per_unit=rb.total,
        planned_loss=qty * rb.total, risk_budget=risk_budget,
        notional=notional, breakdown=rb,
        reason="按风险预算定仓",
    )


# ---------------------------------------------------------------------------
# 组合级风险闸门
# ---------------------------------------------------------------------------
def portfolio_budget(equity: float) -> float:
    """组合级计划最大亏损预算（BTC+ETH 合计）。"""
    return max(0.0, float(equity) * RISK_PORTFOLIO_RATIO)


def symbol_budget(equity: float) -> float:
    """单标的计划最大亏损预算。"""
    return max(0.0, float(equity) * RISK_PER_SYMBOL_RATIO)


def layer_allowed(
    *,
    equity: float,
    symbol_loss_after: float,
    portfolio_loss_after: float,
    symbol_ratio: float = RISK_PER_SYMBOL_RATIO,
    portfolio_ratio: float = RISK_PORTFOLIO_RATIO,
) -> tuple:
    """加层前的闸门：按**加层后的实际净仓**重新计算，超预算就不加。

    返回 (是否允许, 原因)。不修改任何已有止损 —— 放宽止损来腾预算是不允许的。
    """
    sb = max(0.0, float(equity) * symbol_ratio)
    pb = max(0.0, float(equity) * portfolio_ratio)
    if symbol_loss_after > sb:
        return False, (f"加层后单标的风险 {symbol_loss_after:.2f} > "
                       f"预算 {sb:.2f}（权益 {equity:.2f} × {symbol_ratio:.3%}）")
    if portfolio_loss_after > pb:
        return False, (f"加层后组合风险 {portfolio_loss_after:.2f} > "
                       f"预算 {pb:.2f}（权益 {equity:.2f} × {portfolio_ratio:.3%}）")
    return True, "在预算内"


# ---------------------------------------------------------------------------
# 净保本价（盈利保护用）
# ---------------------------------------------------------------------------
def net_break_even_price(
    *,
    avg_price: float,
    side: int,
    quantity: float,
    entry_fee_paid: float,
    funding_paid: float,
    tick: float,
    exit_fee_rate: float = TAKER_FEE_RATE,
    slippage_rate: float = DEFAULT_SLIPPAGE_RATE,
    entry_fee_missing: bool = False,
) -> tuple:
    """扣掉全部成本后「预计不亏」的止损价。

    每单位成本 = 入场手续费/数量 + 已发生资金费/数量
                 + 预计退出手续费 + 滑点预算

        退出手续费按保本价本身计（名义 = 保本价 × 数量），所以先解方程：
            cost_per_unit = (E + F)/Q + P×r_exit + P×r_slip
        而 P = avg + side × cost_per_unit  ⇒
            P = (avg + side×(E+F)/Q) / (1 - side×(r_exit + r_slip))

    多仓取 avg 上方，空仓取 avg 下方；最后按 tick 向**保护方向**取整
    （多仓向上、空仓向下），宁可多留一点缓冲，不可少留。

    费用缺失时的保守回退：entry_fee_missing=True 时用 taker 费率假设
    入场费（保守高估），并在返回的说明里标出来。

    返回 (保本价, 说明字符串)。
    """
    if avg_price <= 0 or quantity <= 0:
        return 0.0, "均价或数量非法"
    side = 1 if side > 0 else -1
    r = exit_fee_rate + slippage_rate
    denom = 1.0 - side * r
    if denom <= 0:
        # 费率大到方程无解，说明参数异常；保守返回均价 + 一个 tick 缓冲
        return avg_price, "费率异常，退回均价"
    note = []
    if entry_fee_missing:
        entry_fee_paid = avg_price * quantity * TAKER_FEE_RATE
        note.append("入场费流水缺失，按 taker 费率保守估算")
    fixed = (abs(entry_fee_paid) + abs(funding_paid)) / quantity
    raw = (avg_price + side * fixed) / denom
    px = _round_toward_protection(raw, tick, side)
    note.append(f"每单位成本 {abs(px - avg_price):.6f}")
    return px, "；".join(note)


def _round_toward_protection(price: float, tick: float, side: int) -> float:
    """按 tick 取整，方向偏向「更安全」的一侧。

    多仓止损价向上取整（多留缓冲），空仓向下取整。
    """
    if tick <= 0:
        return price
    n = price / tick
    n = math.ceil(round(n, 9)) if side > 0 else math.floor(round(n, 9))
    return round(n * tick, 12)


def stop_price_from_avg(*, avg_price: float, side: int, atr_1h: float,
                        multiple: float, tick: float) -> float:
    """以**交易所实际持仓均价**为基准的初始止损价。

    多仓 = 均价 − multiple×ATR_1H；空仓 = 均价 + multiple×ATR_1H。
    同样向保护方向取整。
    """
    if avg_price <= 0 or atr_1h <= 0:
        return 0.0
    side = 1 if side > 0 else -1
    raw = avg_price - side * multiple * atr_1h
    if raw <= 0:
        return 0.0
    return _round_toward_protection(raw, tick, side)


def stop_is_tighter(*, new_stop: float, old_stop: float, side: int) -> bool:
    """新止损是否**更紧**（只能向保护利润方向移动，不能放宽）。

    多仓：新止损必须 ≥ 旧止损；空仓：新止损必须 ≤ 旧止损。
    相等视为「没有变化」，不算更紧。
    """
    side = 1 if side > 0 else -1
    if old_stop <= 0:
        return True          # 原本没有止损，任何保护都是改善
    if new_stop <= 0:
        return False
    return (new_stop > old_stop) if side > 0 else (new_stop < old_stop)
