"""短期 / 长期独立仓位管理 (本地账本 + 交易所净敞口同步).

规则:
  * 每个 horizon 本地最多一个持仓; 平仓后才能开新仓 (反手 = 先平再开)
  * 短期 2x / 长期 3x (杠杆取当前净仓对应的更高者执行)
  * STRONG/STANDARD_* 开仓; NEUTRAL 平仓; WATCH_* 不动
  * V8: 支持分阶段减仓 (partial_close) + CS/置信度/ATR 仓位调节
  * 交易所只有一个 BTCUSDT 净仓: 两边本地仓位 signed 相加后同步
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from config.review import (
    EXIT_STRATEGY,
    LEVERAGE_LONG_TERM,
    LEVERAGE_SHORT_TERM,
    MAX_NOTIONAL_PCT,
    MIN_NOTIONAL_USDT,
    POSITION_ATR_HIGH_MULT,
    POSITION_ATR_HIGH_RATIO,
    POSITION_CONF_LOW_MULT,
    POSITION_CONF_LOW_THRESHOLD,
    POSITION_CS_STRONG_MULT,
    POSITION_NOTIONAL_PCT,
    RISK_PER_TRADE_PCT,
    TRADING_SYMBOL,
)
from models.signals import ActionDecision
from trading.binance_client import BinanceTestnetClient
from trading.models import ExitAction, HorizonPosition, OrderResult, TradeAction

logger = logging.getLogger(__name__)

OPEN_LONG = frozenset({
    ActionDecision.STRONG_LONG.value, ActionDecision.STANDARD_LONG.value,
    "STRONG_LONG", "STANDARD_LONG",
})
OPEN_SHORT = frozenset({
    ActionDecision.STRONG_SHORT.value, ActionDecision.STANDARD_SHORT.value,
    "STRONG_SHORT", "STANDARD_SHORT",
})
FLAT_DECISIONS = frozenset({ActionDecision.NEUTRAL.value, "NEUTRAL"})


def _decision_side(decision: str) -> Optional[str]:
    if decision in OPEN_LONG:
        return "LONG"
    if decision in OPEN_SHORT:
        return "SHORT"
    return None


def _signed_qty(pos: Optional[HorizonPosition]) -> float:
    if pos is None or pos.is_flat():
        return 0.0
    return pos.quantity if pos.side == "LONG" else -pos.quantity


class PositionManager:
    def __init__(
        self,
        client: BinanceTestnetClient,
        symbol: str = TRADING_SYMBOL,
    ) -> None:
        self.client = client
        self.symbol = symbol
        self.positions: Dict[str, Optional[HorizonPosition]] = {
            "short_term": None,
            "long_term": None,
        }
        self.leverage = {
            "short_term": LEVERAGE_SHORT_TERM,
            "long_term": LEVERAGE_LONG_TERM,
        }
        self._ready = False

    def get_position(self, horizon: str) -> Optional[HorizonPosition]:
        return self.positions.get(horizon)

    def snapshot(self) -> Dict[str, Optional[dict]]:
        out = {}
        for h, p in self.positions.items():
            out[h] = None if p is None else p.to_dict()
        return out

    async def bootstrap(self) -> None:
        if self._ready:
            return
        try:
            await self.client.set_one_way_mode()
            await self.client.set_margin_type_isolated(self.symbol)
            await self.client.set_leverage(max(self.leverage.values()), self.symbol)
        except Exception as exc:
            logger.warning("bootstrap: %s", exc)
        self._ready = True

    async def _calc_qty(
        self,
        horizon: str,
        price: float,
        *,
        cs: float = 0.0,
        min_confidence: float = 1.0,
        atr: Optional[float] = None,
        atr_mean: Optional[float] = None,
    ) -> tuple:
        """按单笔风险定仓: qty = risk_usdt / stop_distance.

        返回 (qty, risk_usdt, stop_distance).
        无 ATR 时回退到名义占比定仓.
        """
        bal = await self.client.get_balance()
        equity = float(bal.available_balance)
        if price <= 0 or equity <= 0:
            return 0.0, 0.0, 0.0

        risk_pct = float(RISK_PER_TRADE_PCT.get(horizon, 0.005))
        risk_usdt = equity * risk_pct

        cfg = EXIT_STRATEGY.get(horizon) or EXIT_STRATEGY["short_term"]
        stop_atr_mult = float(cfg.get("hard_sl_atr") or 1.0)
        stop_pct_fallback = float(cfg.get("hard_sl_pct") or 0.01)

        if atr is not None and atr > 0:
            stop_distance = atr * stop_atr_mult
        else:
            stop_distance = price * stop_pct_fallback

        if stop_distance <= 0:
            return 0.0, 0.0, 0.0

        qty = risk_usdt / stop_distance

        # CS / 置信度 / 高波动调节
        if abs(cs) >= 60:
            qty *= POSITION_CS_STRONG_MULT
            risk_usdt *= POSITION_CS_STRONG_MULT
        if min_confidence < POSITION_CONF_LOW_THRESHOLD:
            qty *= POSITION_CONF_LOW_MULT
            risk_usdt *= POSITION_CONF_LOW_MULT
        if (
            atr is not None
            and atr_mean is not None
            and atr_mean > 0
            and atr > atr_mean * POSITION_ATR_HIGH_RATIO
        ):
            qty *= POSITION_ATR_HIGH_MULT
            risk_usdt *= POSITION_ATR_HIGH_MULT

        # 名义硬顶
        max_pct = float(MAX_NOTIONAL_PCT.get(horizon) or POSITION_NOTIONAL_PCT.get(horizon, 0.1))
        max_notional = max(MIN_NOTIONAL_USDT, equity * max_pct)
        if qty * price > max_notional:
            qty = max_notional / price

        # 下限兜底
        min_qty = MIN_NOTIONAL_USDT / price
        if qty < min_qty:
            qty = min_qty

        return qty, risk_usdt, stop_distance

    def _local_net(self) -> float:
        return sum(_signed_qty(p) for p in self.positions.values())

    async def _sync_exchange_to_net(self, price: float) -> OrderResult:
        """把交易所净仓调到与本地账本一致."""
        target = self._local_net()
        try:
            exch = await self.client.get_position(self.symbol)
        except Exception as exc:
            return OrderResult(ok=False, error=f"get_position: {exc}")

        current = 0.0
        if exch.side == "LONG":
            current = exch.quantity
        elif exch.side == "SHORT":
            current = -exch.quantity

        delta = target - current
        if abs(delta) < 1e-6:
            return OrderResult(ok=True, status="SYNCED", avg_price=price)

        active_lev = LEVERAGE_SHORT_TERM
        for h, p in self.positions.items():
            if p and not p.is_flat():
                active_lev = max(active_lev, self.leverage[h])
        try:
            await self.client.set_leverage(active_lev, self.symbol)
        except Exception as exc:
            logger.warning("set_leverage: %s", exc)

        if abs(target) < 1e-6:
            return await self.client.market_close(self.symbol)

        if abs(current) > 1e-6 and (current * target < 0 or abs(target) < abs(current) - 1e-6):
            close_res = await self.client.market_close(self.symbol)
            if not close_res.ok and close_res.status != "FLAT":
                return close_res
            current = 0.0
            delta = target - current

        side = "LONG" if delta > 0 else "SHORT"
        return await self.client.market_open(
            side=side, quantity=abs(delta), symbol=self.symbol
        )

    async def on_signal(
        self,
        horizon: str,
        decision: str,
        price: float,
        *,
        cs: float = 0.0,
        min_confidence: float = 1.0,
        atr: Optional[float] = None,
        atr_mean: Optional[float] = None,
        entry_cs: Optional[float] = None,
    ) -> List[TradeAction]:
        if horizon not in ("short_term", "long_term"):
            return []
        await self.bootstrap()
        decision = str(decision)
        desired = _decision_side(decision)
        current = self.positions.get(horizon)
        actions: List[TradeAction] = []

        if (current is None or current.is_flat()) and desired is None:
            return []

        if current and not current.is_flat() and decision in FLAT_DECISIONS:
            act = await self._local_close(horizon, decision, price, "signal_neutral")
            if act:
                actions.append(act)
            return actions

        if current and not current.is_flat() and desired == current.side:
            return []

        if current and not current.is_flat() and desired and desired != current.side:
            close_act = await self._local_close(horizon, decision, price, "reverse")
            if close_act:
                actions.append(close_act)
            open_act = await self._local_open(
                horizon, desired, decision, price, "reverse",
                cs=cs, min_confidence=min_confidence, atr=atr, atr_mean=atr_mean,
                entry_cs=entry_cs if entry_cs is not None else cs,
            )
            if open_act:
                actions.append(open_act)
            return actions

        if (current is None or current.is_flat()) and desired:
            open_act = await self._local_open(
                horizon, desired, decision, price, "signal",
                cs=cs, min_confidence=min_confidence, atr=atr, atr_mean=atr_mean,
                entry_cs=entry_cs if entry_cs is not None else cs,
            )
            if open_act:
                actions.append(open_act)
        return actions

    async def apply_exit(
        self,
        horizon: str,
        exit_act: ExitAction,
        price: float,
        decision: str = "EXIT",
    ) -> Optional[TradeAction]:
        """执行 ExitChecker 产出的动作."""
        await self.bootstrap()
        pos = self.positions.get(horizon)
        if pos is None or pos.is_flat():
            return None

        if exit_act.kind == "tighten_trailing":
            pos.trailing_tightened = True
            if exit_act.new_trailing_pct is not None and pos.peak_price > 0:
                t = exit_act.new_trailing_pct
                if pos.side == "LONG":
                    pos.trailing_stop_price = pos.peak_price * (1.0 - t)
                else:
                    pos.trailing_stop_price = pos.peak_price * (1.0 + t)
            logger.info("收紧追踪止盈 %s trail=%s", horizon, exit_act.new_trailing_pct)
            return TradeAction(
                action="tighten_trailing",
                horizon=horizon,
                side=pos.side,
                quantity=0.0,
                price=price,
                leverage=pos.leverage,
                decision=decision,
                reason=exit_act.reason,
                trade_id=pos.trade_id,
            )

        if exit_act.kind == "full_close" or exit_act.close_pct >= 0.999:
            return await self._local_close(horizon, decision, price, exit_act.reason)

        return await self.partial_close(
            horizon, exit_act.close_pct, exit_act.reason, price, decision
        )

    async def partial_close(
        self,
        horizon: str,
        close_pct: float,
        reason: str,
        price: float,
        decision: str = "EXIT",
    ) -> Optional[TradeAction]:
        """相对当前剩余仓位减仓 close_pct (0~1)."""
        current = self.positions.get(horizon)
        if current is None or current.is_flat():
            return None
        close_pct = max(0.0, min(1.0, float(close_pct)))
        if close_pct <= 0:
            return None
        if close_pct >= 0.999:
            return await self._local_close(horizon, decision, price, reason)

        close_qty = current.quantity * close_pct
        if close_qty <= 0:
            return None

        backup = HorizonPosition(**{
            k: v for k, v in current.to_dict().items()
        })
        current.quantity = max(0.0, current.quantity - close_qty)
        current.remaining_pct = max(0.0, current.remaining_pct * (1.0 - close_pct))
        if reason == "tp1":
            current.tp_levels_hit = max(current.tp_levels_hit, 1)
        elif reason == "tp2":
            current.tp_levels_hit = max(current.tp_levels_hit, 2)
        elif reason == "cs_decay":
            current.cs_decay_done = True

        if current.quantity <= 1e-8:
            return await self._local_close(horizon, decision, price, reason)

        order = await self._sync_exchange_to_net(price)
        act = TradeAction(
            action="partial_close",
            horizon=horizon,
            side=current.side,
            quantity=close_qty,
            price=order.avg_price or price,
            leverage=current.leverage,
            decision=decision,
            reason=reason,
            order=order,
            trade_id=current.trade_id,
        )
        if not order.ok and order.status not in ("FLAT", "SYNCED"):
            self.positions[horizon] = backup
            logger.error("减仓同步失败 %s: %s", horizon, order.error)
            return act
        logger.info(
            "减仓 %s %s close=%.4f remain=%.4f (%s)",
            horizon, current.side, close_qty, current.quantity, reason,
        )
        return act

    async def _local_open(
        self,
        horizon: str,
        side: str,
        decision: str,
        price: float,
        reason: str,
        *,
        cs: float = 0.0,
        min_confidence: float = 1.0,
        atr: Optional[float] = None,
        atr_mean: Optional[float] = None,
        entry_cs: float = 0.0,
    ) -> Optional[TradeAction]:
        existing = self.positions.get(horizon)
        if existing and not existing.is_flat():
            logger.warning("position_mgr: %s 已有仓, 拒绝开仓", horizon)
            return None
        lev = self.leverage[horizon]
        qty, risk_usdt, stop_distance = await self._calc_qty(
            horizon, price,
            cs=cs, min_confidence=min_confidence, atr=atr, atr_mean=atr_mean,
        )
        if qty <= 0:
            logger.warning("position_mgr: qty=0, 跳过")
            return None

        entry_atr = float(atr or 0)
        if side == "LONG":
            sl_price = price - stop_distance
        else:
            sl_price = price + stop_distance

        self.positions[horizon] = HorizonPosition(
            horizon=horizon,
            side=side,
            entry_price=price,
            quantity=qty,
            leverage=lev,
            opened_at_ms=int(time.time() * 1000),
            mark_price=price,
            original_quantity=qty,
            remaining_pct=1.0,
            peak_price=price,
            tp_levels_hit=0,
            sl_price=sl_price,
            entry_cs=float(entry_cs or cs),
            entry_atr=entry_atr,
            risk_usdt=risk_usdt,
            cs_decay_done=False,
            trailing_tightened=False,
        )
        order = await self._sync_exchange_to_net(price)
        act = TradeAction(
            action="reverse_open" if reason == "reverse" else "open",
            horizon=horizon,
            side=side,
            quantity=qty,
            price=order.avg_price or price,
            leverage=lev,
            decision=decision,
            reason=reason,
            order=order,
        )
        if not order.ok:
            self.positions[horizon] = None
            logger.error("开仓同步失败 %s: %s", horizon, order.error)
            return act
        pos = self.positions[horizon]
        if pos:
            fill = order.avg_price or price
            fill_qty = order.quantity or qty
            pos.entry_price = fill
            pos.quantity = fill_qty
            pos.original_quantity = fill_qty
            pos.order_id = order.order_id
            pos.peak_price = fill
            if side == "LONG":
                pos.sl_price = fill - stop_distance
            else:
                pos.sl_price = fill + stop_distance
        logger.info(
            "开仓 %s %s qty=%.4f @ %.2f lev=%dx cs=%.1f risk=$%.1f atr=%.1f stop=%.1f",
            horizon, side, qty, act.price, lev, entry_cs or cs,
            risk_usdt, entry_atr, stop_distance,
        )
        return act

    async def _local_close(
        self,
        horizon: str,
        decision: str,
        price: float,
        reason: str,
    ) -> Optional[TradeAction]:
        current = self.positions.get(horizon)
        if current is None or current.is_flat():
            return None
        side = current.side
        qty = current.quantity
        lev = current.leverage
        trade_id = current.trade_id
        self.positions[horizon] = None
        order = await self._sync_exchange_to_net(price)
        act = TradeAction(
            action="reverse_close" if reason == "reverse" else "close",
            horizon=horizon,
            side=side,
            quantity=qty,
            price=order.avg_price or price,
            leverage=lev,
            decision=decision,
            reason=reason,
            order=order,
            trade_id=trade_id,
        )
        if not order.ok and order.status != "FLAT" and order.status != "SYNCED":
            self.positions[horizon] = current
            logger.error("平仓同步失败 %s: %s", horizon, order.error)
            return act
        logger.info("平仓 %s %s @ %.2f (%s)", horizon, side, act.price, reason)
        return act

    async def force_close(self, horizon: str) -> Optional[TradeAction]:
        try:
            price = await self.client.mark_price(self.symbol)
        except Exception:
            price = 0.0
        return await self._local_close(horizon, "NEUTRAL", price, "manual_force")

    async def refresh_marks(self) -> None:
        try:
            mark = await self.client.mark_price(self.symbol)
        except Exception:
            return
        for p in self.positions.values():
            if p and not p.is_flat():
                p.mark_price = mark
                sign = 1.0 if p.side == "LONG" else -1.0
                p.unrealized_pnl = sign * (mark - p.entry_price) * p.quantity
                if p.side == "LONG":
                    p.peak_price = max(p.peak_price or mark, mark)
                else:
                    if p.peak_price <= 0:
                        p.peak_price = mark
                    else:
                        p.peak_price = min(p.peak_price, mark)
