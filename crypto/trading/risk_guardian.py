"""独立风控看门狗 — 不依赖评分循环.

规则:
  * 启动 NOT_READY，无有效 mark 禁止新开
  * 只减仓/平仓；平仓失败不得撤仍需要的保护
  * 净仓单一 Algo 保护（非按 horizon 各挂一张）
  * 保护成交必须回写策略账本
  * 评分失败需外部 note_score_result(False)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from config.review import (
    EXIT_STRATEGY,
    RISK_GUARDIAN_INTERVAL_SEC,
    STALENESS_LIMITS,
    TRADING_SYMBOL,
)
from trading.binance_client import BinanceTestnetClient
from trading.models import ExitAction, OrderState, SystemHealth
from trading.position_manager import PositionManager

logger = logging.getLogger(__name__)


class RiskGuardian:
    def __init__(
        self,
        client: BinanceTestnetClient,
        portfolio: PositionManager,
        *,
        interval_sec: float = RISK_GUARDIAN_INTERVAL_SEC,
        symbol: str = TRADING_SYMBOL,
    ) -> None:
        self.client = client
        self.portfolio = portfolio
        self.interval_sec = interval_sec
        self.symbol = symbol
        self.health = SystemHealth.NOT_READY
        self.last_mark: float = 0.0
        self.last_mark_ts: float = 0.0
        self.last_error: str = ""
        self.score_fail_streak: int = 0
        self._score_ok_streak: int = 0
        self.daily_pnl_pct: float = 0.0
        self.protection_ok: bool = True
        self._net_protection_id: Optional[str] = None  # 单一净仓保护
        self._stop_ids: Dict[str, str] = {}  # 兼容旧测试
        self._actions: list = []
        self._recover_observe: int = 0

    def note_score_result(self, ok: bool) -> None:
        if ok:
            self.score_fail_streak = 0
            self._score_ok_streak += 1
            # 需连续观察才从 DEGRADED 恢复，禁止一帧成功清故障
            if self.health == SystemHealth.DEGRADED and self._score_ok_streak >= 3:
                if self.last_mark_ts > 0:
                    self.health = SystemHealth.NORMAL
                    self._recover_observe = 0
        else:
            self._score_ok_streak = 0
            self.score_fail_streak += 1
            if self.score_fail_streak >= 3 and self.health in (
                SystemHealth.NORMAL, SystemHealth.NOT_READY
            ):
                self.health = SystemHealth.DEGRADED
                logger.warning("评分连续失败 → DEGRADED")

    def note_daily_pnl(self, pct: float) -> None:
        self.daily_pnl_pct = pct
        if pct <= -0.03 and self.health in (
            SystemHealth.NORMAL, SystemHealth.DEGRADED, SystemHealth.NOT_READY
        ):
            self.health = SystemHealth.REDUCE_ONLY
            logger.warning("日内亏损 %.2f%% → REDUCE_ONLY", pct * 100)

    def allow_new_entries(self) -> bool:
        if self.portfolio.reconciliation_needed:
            return False
        if self.health == SystemHealth.NOT_READY:
            return False
        if self.last_mark_ts <= 0:
            return False
        return self.health in (SystemHealth.NORMAL, SystemHealth.DEGRADED)

    def status(self) -> Dict[str, Any]:
        return {
            "health": self.health.value,
            "last_mark": self.last_mark,
            "last_mark_age_sec": (time.time() - self.last_mark_ts) if self.last_mark_ts else None,
            "score_fail_streak": self.score_fail_streak,
            "protection_ok": self.protection_ok,
            "net_protection_id": self._net_protection_id,
            "stop_ids": dict(self._stop_ids),
            "allow_new_entries": self.allow_new_entries(),
            "last_error": self.last_error,
            "recent_actions": list(self._actions[-5:]),
        }

    def _net_sl_price(self) -> Tuple[Optional[str], float, float]:
        """净仓保护: (side, qty, stop_price). 取最紧的硬止损."""
        active = self.portfolio.active_positions()
        if not active:
            return None, 0.0, 0.0
        net = self.portfolio._local_net()
        if abs(net) < 1e-9:
            return None, 0.0, 0.0
        side = "LONG" if net > 0 else "SHORT"
        qty = abs(net)
        stops = []
        for h, pos in active:
            if pos.sl_price <= 0:
                continue
            if side == "LONG" and pos.side == "LONG":
                stops.append(pos.sl_price)
            if side == "SHORT" and pos.side == "SHORT":
                stops.append(pos.sl_price)
        if not stops:
            return side, qty, 0.0
        stop = max(stops) if side == "SHORT" else min(stops)
        return side, qty, stop

    async def ensure_protection(self, horizon: str = "") -> bool:
        """维持单一净仓 Algo 保护。新单 ACK 后再撤旧单."""
        side, qty, stop = self._net_sl_price()
        if side is None or stop <= 0 or qty <= 0:
            # 无仓：可清理保护
            if self._net_protection_id:
                await self._cancel_net_protection()
            return True

        old_id = self._net_protection_id
        mo = await self.client.place_stop_market(
            side=side,
            quantity=qty,
            stop_price=stop,
            close_position=True,  # 净仓全平保护
        )
        if mo.state in (OrderState.REJECTED, OrderState.UNKNOWN) and mo.error:
            self.protection_ok = False
            logger.error("保护单失败: %s — 保留旧保护", mo.error)
            if self.health == SystemHealth.NORMAL:
                self.health = SystemHealth.DEGRADED
            # 旧保护仍在则不算 EMERGENCY
            if not old_id:
                opens = []
                try:
                    if hasattr(self.client, "get_open_algo_orders"):
                        opens = await self.client.get_open_algo_orders()
                    else:
                        opens = await self.client.get_open_orders()
                except Exception:
                    opens = []
                if not opens and self.portfolio.active_positions():
                    self.health = SystemHealth.EMERGENCY
            return False

        self._net_protection_id = mo.client_order_id
        if horizon:
            self._stop_ids[horizon] = mo.client_order_id
        self.portfolio._protection_ids["__net__"] = mo.client_order_id
        self.protection_ok = True
        # 新单成功后再撤旧
        if old_id and old_id != mo.client_order_id:
            try:
                if hasattr(self.client, "cancel_algo_order"):
                    await self.client.cancel_algo_order(client_algo_id=old_id)
                else:
                    await self.client.cancel_order(client_order_id=old_id)
            except Exception as exc:
                logger.warning("cancel old protection: %s", exc)
        self.portfolio._persist()
        return True

    async def _cancel_net_protection(self) -> None:
        cid = self._net_protection_id
        self._net_protection_id = None
        self._stop_ids.clear()
        self.portfolio._protection_ids.pop("__net__", None)
        if not cid:
            return
        try:
            if hasattr(self.client, "cancel_algo_order"):
                await self.client.cancel_algo_order(client_algo_id=cid)
            else:
                await self.client.cancel_order(client_order_id=cid)
        except Exception as exc:
            logger.warning("cancel net protection: %s", exc)

    async def release_protection_if_flat(self, confirmed_flat: bool) -> None:
        """只有确认无仓后才撤保护."""
        if not confirmed_flat:
            return
        if self.portfolio.active_positions():
            return
        try:
            exch = await self.client.get_position(self.symbol)
            if exch.side != "FLAT" and exch.quantity > 0:
                return
        except Exception:
            return
        await self._cancel_net_protection()
        self.portfolio._persist()

    async def cancel_protection(self, horizon: str) -> None:
        """兼容旧接口 — 仅在确认平坦时释放."""
        await self.release_protection_if_flat(
            not self.portfolio.active_positions()
        )

    async def reconcile_protections(self) -> None:
        try:
            if hasattr(self.client, "get_open_algo_orders"):
                opens = await self.client.get_open_algo_orders()
            else:
                opens = await self.client.get_open_orders()
        except Exception as exc:
            self.last_error = str(exc)
            return
        if self.portfolio.active_positions():
            ids = {
                str(o.get("clientAlgoId") or o.get("clientOrderId") or "")
                for o in (opens or [])
            }
            if self._net_protection_id and self._net_protection_id in ids:
                return
            await self.ensure_protection()

    async def reconcile_exchange_fills(self) -> List[Any]:
        """保护成交 / 外部平仓 → 仅本地记账，禁止为修平账本而下单."""
        actions = []
        # 在途订单：旧仓位观察不得解释为保护触发
        if getattr(self.portfolio, "has_inflight_orders", lambda: False)():
            logger.info("对账跳过：存在在途订单")
            return actions
        observed_version = getattr(self.portfolio, "state_version", 0)
        try:
            exch = await self.client.get_position(self.symbol)
        except Exception as exc:
            self.last_error = str(exc)
            return actions
        # 观察过期：本地在查询期间已变
        if getattr(self.portfolio, "state_version", 0) != observed_version:
            logger.info("对账跳过：观察过期 version changed")
            return actions
        actual = 0.0
        if exch.side == "LONG":
            actual = exch.quantity
        elif exch.side == "SHORT":
            actual = -exch.quantity
        local = self.portfolio._local_net()
        if abs(local - actual) < 1e-6:
            return actions
        # 外部已平 / 差异 → 本地 apply，不下单
        if hasattr(self.portfolio, "apply_external_position_update"):
            actions = self.portfolio.apply_external_position_update(
                exchange_signed=actual, reason="protect_or_external"
            )
        else:
            self.portfolio.reconciliation_needed = True
            self.portfolio._allow_new_entries = False
            logger.error("仓位对账失败 local=%.6f exch=%.6f", local, actual)
        if abs(actual) < 1e-9 and not self.portfolio.active_positions():
            await self.release_protection_if_flat(True)
            logger.warning("保护/外部平仓已确认，策略账本已清空")
        elif abs(actual) > 1e-9 and self.portfolio.active_positions():
            await self.ensure_protection()
        self._actions.extend([a.to_dict() for a in actions if hasattr(a, "to_dict")])
        return actions

    def _check_hard_sl(self, pos, mark: float) -> Optional[ExitAction]:
        if pos.sl_price <= 0:
            return None
        if pos.side == "LONG" and mark <= pos.sl_price:
            return ExitAction(kind="full_close", reason="hard_sl_guardian")
        if pos.side == "SHORT" and mark >= pos.sl_price:
            return ExitAction(kind="full_close", reason="hard_sl_guardian")
        return None

    def _check_trailing(self, pos, mark: float) -> Optional[ExitAction]:
        cfg = EXIT_STRATEGY.get(pos.horizon) or EXIT_STRATEGY["short_term"]
        mult = float(cfg.get("trailing_atr") or 1.0)
        if pos.side == "LONG":
            pos.peak_price = max(pos.peak_price or mark, mark)
            if pos.entry_atr > 0:
                new_trail = pos.peak_price - pos.entry_atr * mult
                if pos.trailing_stop_price <= 0:
                    pos.trailing_stop_price = new_trail
                else:
                    # 只准收紧（上移）
                    pos.trailing_stop_price = max(pos.trailing_stop_price, new_trail)
            if pos.trailing_stop_price > 0 and mark <= pos.trailing_stop_price:
                return ExitAction(kind="full_close", reason="trailing_guardian")
        else:
            if pos.peak_price <= 0:
                pos.peak_price = mark
            else:
                pos.peak_price = min(pos.peak_price, mark)
            if pos.entry_atr > 0:
                new_trail = pos.peak_price + pos.entry_atr * mult
                if pos.trailing_stop_price <= 0:
                    pos.trailing_stop_price = new_trail
                else:
                    pos.trailing_stop_price = min(pos.trailing_stop_price, new_trail)
            if pos.trailing_stop_price > 0 and mark >= pos.trailing_stop_price:
                return ExitAction(kind="full_close", reason="trailing_guardian")
        return None

    def _check_staleness(self) -> None:
        if not self.last_mark_ts:
            if self.health not in (SystemHealth.EMERGENCY, SystemHealth.HALTED):
                self.health = SystemHealth.NOT_READY
            return
        age = time.time() - self.last_mark_ts
        limit = float(STALENESS_LIMITS.get("mark_price", 60))
        if age > limit and self.health in (
            SystemHealth.NORMAL, SystemHealth.DEGRADED, SystemHealth.NOT_READY
        ):
            self.health = SystemHealth.REDUCE_ONLY
            logger.warning("行情断流 %.0fs → REDUCE_ONLY", age)

    async def tick(self) -> list:
        actions = []
        # 先对账保护成交
        actions.extend(await self.reconcile_exchange_fills())

        try:
            mark = await self.client.mark_price(self.symbol)
            self.last_mark = mark
            self.last_mark_ts = time.time()
            self.last_error = ""
            if self.health == SystemHealth.NOT_READY:
                self.health = SystemHealth.NORMAL
        except Exception as exc:
            self.last_error = str(exc)
            self._check_staleness()
            return actions

        self._check_staleness()

        if self.health == SystemHealth.EMERGENCY:
            for h, _ in list(self.portfolio.active_positions()):
                act = await self.portfolio.force_close(h)
                if act and act.order and act.order.ok:
                    actions.append(act)
            await self.release_protection_if_flat(not self.portfolio.active_positions())
            self._actions.extend([a.to_dict() for a in actions])
            return actions

        for h, pos in list(self.portfolio.active_positions()):
            ea = self._check_hard_sl(pos, mark) or self._check_trailing(pos, mark)
            if ea is None:
                continue
            tact = await self.portfolio.apply_exit(h, ea, mark, decision="GUARDIAN")
            if not tact:
                continue
            actions.append(tact)
            closed_ok = bool(tact.order and tact.order.ok) or tact.order and tact.order.status in (
                "FLAT", "SYNCED"
            )
            if ea.kind == "full_close":
                # 只有确认成交/平坦才撤保护；失败保留
                still = self.portfolio.get_position(h)
                if closed_ok and (still is None or still.is_flat()):
                    await self.release_protection_if_flat(
                        not self.portfolio.active_positions()
                    )
                else:
                    logger.error("平仓未确认，保留保护单")
                    self.protection_ok = False
            else:
                await self.ensure_protection(h)

        self._actions.extend([a.to_dict() for a in actions])
        return actions

    async def run(self, stop: asyncio.Event) -> None:
        logger.info("RiskGuardian started interval=%.1fs health=%s", self.interval_sec, self.health.value)
        await self.reconcile_protections()
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("RiskGuardian tick: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_sec)
            except asyncio.TimeoutError:
                pass
        logger.info("RiskGuardian stopped")
