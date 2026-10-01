"""短期 / 长期组合仓位管理 (策略账本 ≠ 交易所净仓).

规则:
  * 每个 horizon 维护独立策略账本 (虚拟持仓)
  * 组合层计算 signed 净目标, 只向交易所下净额订单
  * 已确认仓位只按成交量/外部成交事件更新, 意图不得直接变成确认仓
  * 同向对冲部分记为内部匹配 (is_internal_match), 不计真实手续费
  * 所有交易路径串行 (_trade_lock)
  * 仓位持久化到 positions.json, 重启对账
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    POSITION_CS_STRONG_THRESHOLD,
    POSITION_NOTIONAL_PCT,
    RISK_PER_TRADE_PCT,
    TOTAL_MAX_NOTIONAL_PCT,
    TRADING_SYMBOL,
)
from models.signals import ActionDecision
from trading.binance_client import BinanceTestnetClient
from trading.models import (
    OrderState,
    ExitAction,
    FillAllocation,
    HorizonPosition,
    OrderResult,
    TradeAction,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POSITIONS_PATH = PROJECT_ROOT / "runtime" / "review" / "positions.json"
QTY_EPS = 1e-6

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


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class PositionManager:
    """组合仓位管理器 (保留原类名以兼容 executor)."""

    def __init__(
        self,
        client: BinanceTestnetClient,
        symbol: str = TRADING_SYMBOL,
        persist_path: Optional[Path] = None,
    ) -> None:
        self.client = client
        self.symbol = symbol
        # 假盘默认不得写生产 positions.json（单测若显式传入路径仍可用）
        if persist_path is None and type(client).__name__ == "FakeBinanceClient":
            import tempfile
            self._fake_tmp = tempfile.TemporaryDirectory(prefix="crypto_pos_")
            persist_path = Path(self._fake_tmp.name) / "positions.json"
        self.persist_path = Path(persist_path) if persist_path else POSITIONS_PATH
        self.positions: Dict[str, Optional[HorizonPosition]] = {
            "short_term": None,
            "long_term": None,
        }
        self.leverage = {
            "short_term": LEVERAGE_SHORT_TERM,
            "long_term": LEVERAGE_LONG_TERM,
        }
        self._ready = False
        self._trade_lock: Optional[asyncio.Lock] = None
        self.reconciliation_needed = False
        self.orphan_exchange_qty: float = 0.0  # signed
        self.last_allocations: List[FillAllocation] = []
        self._protection_ids: Dict[str, str] = {}  # horizon -> client_order_id
        self._allow_new_entries = True
        self._inflight_orders: Dict[str, dict] = {}
        self._state_version: int = 0
        self._load_persisted()
        # restore inflight if persisted
        try:
            if self.persist_path.exists():
                data = json.loads(self.persist_path.read_text(encoding="utf-8"))
                self._inflight_orders = dict(data.get("inflight_orders") or {})
                self._state_version = int(data.get("state_version") or 0)
        except Exception:
            pass

    # ------------------------------------------------------------------ persist
    def _load_persisted(self) -> None:
        if not self.persist_path.exists():
            return
        try:
            data = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("positions load: %s", exc)
            return
        for h in ("short_term", "long_term"):
            raw = (data.get("positions") or {}).get(h)
            if not raw:
                self.positions[h] = None
                continue
            try:
                self.positions[h] = HorizonPosition(**{
                    k: v for k, v in raw.items()
                    if k in HorizonPosition.__dataclass_fields__
                })
            except TypeError as exc:
                logger.warning("positions hydrate %s: %s", h, exc)
                self.positions[h] = None
        self._protection_ids = dict(data.get("protection_ids") or {})
        logger.info(
            "positions restored short=%s long=%s",
            self.positions["short_term"].side if self.positions["short_term"] else "FLAT",
            self.positions["long_term"].side if self.positions["long_term"] else "FLAT",
        )

    def _persist(self) -> None:
        payload = {
            "updated_at_ms": int(time.time() * 1000),
            "positions": {
                h: (None if p is None else p.to_dict()) for h, p in self.positions.items()
            },
            "protection_ids": dict(self._protection_ids),
            "reconciliation_needed": self.reconciliation_needed,
            "inflight_orders": dict(self._inflight_orders),
            "state_version": self._state_version,
        }
        try:
            _atomic_write_json(self.persist_path, payload)
        except OSError as exc:
            logger.error("positions persist: %s", exc)

    def _lock(self) -> asyncio.Lock:
        if self._trade_lock is None:
            self._trade_lock = asyncio.Lock()
        return self._trade_lock

    def get_position(self, horizon: str) -> Optional[HorizonPosition]:
        return self.positions.get(horizon)

    def snapshot(self) -> Dict[str, Optional[dict]]:
        out = {}
        for h, p in self.positions.items():
            out[h] = None if p is None else p.to_dict()
        return out

    def active_positions(self) -> List[Tuple[str, HorizonPosition]]:
        return [
            (h, p) for h, p in self.positions.items()
            if p is not None and not p.is_flat()
        ]


    def bump_version(self) -> int:
        self._state_version += 1
        return self._state_version

    @property
    def state_version(self) -> int:
        return self._state_version

    def has_inflight_orders(self) -> bool:
        return bool(self._inflight_orders)

    def register_inflight_order(
        self, cid: str, *, horizon: str, side: str, qty: float
    ) -> None:
        self._inflight_orders[cid] = {
            "horizon": horizon, "side": side, "qty": qty, "ts": time.time(),
        }
        self.bump_version()
        self._persist()

    def clear_inflight_order(self, cid: str) -> None:
        self._inflight_orders.pop(cid, None)
        self.bump_version()
        self._persist()

    def apply_external_position_update(
        self, *, exchange_signed: float, reason: str = "external_fill"
    ) -> List:
        """仅本地记账：应用已发生的外部/保护成交。禁止下单。"""
        from trading.models import TradeAction, OrderResult
        actions = []
        local = self._local_net()
        if abs(exchange_signed) < QTY_EPS and abs(local) > QTY_EPS:
            for h, pos in list(self.active_positions()):
                qty = pos.quantity
                side = pos.side
                tid = pos.trade_id
                self.positions[h] = None
                actions.append(TradeAction(
                    action="close",
                    horizon=h,
                    side=side,
                    quantity=qty,
                    price=pos.mark_price or pos.entry_price,
                    leverage=pos.leverage,
                    decision="EXTERNAL",
                    reason=reason,
                    order=OrderResult(ok=True, status="EXTERNAL_FLAT", quantity=qty,
                                      cum_filled_qty=qty, avg_price=pos.mark_price or 0),
                    trade_id=tid,
                ))
            self.bump_version()
            self._persist()
            return actions
        if abs(exchange_signed) + QTY_EPS < abs(local) and exchange_signed * local >= 0:
            # 净仓减少但未到零：无法可靠按比例分摊时进入未解释差异
            self.reconciliation_needed = True
            self._allow_new_entries = False
            self.orphan_exchange_qty = exchange_signed
            self.bump_version()
            self._persist()
            logger.error(
                "未解释仓位差异 local=%.6f exch=%.6f — 不伪造归属、不下单",
                local, exchange_signed,
            )
            return actions
        if abs(exchange_signed - local) > QTY_EPS:
            self.reconciliation_needed = True
            self._allow_new_entries = False
            self.orphan_exchange_qty = exchange_signed
            self.bump_version()
            self._persist()
        return actions


    def _local_net(self) -> float:
        return sum(_signed_qty(p) for p in self.positions.values())

    async def bootstrap(self) -> None:
        if self._ready:
            return
        try:
            await self.client.set_one_way_mode()
            await self.client.set_margin_type_isolated(self.symbol)
            await self.client.set_leverage(max(self.leverage.values()), self.symbol)
        except Exception as exc:
            logger.warning("bootstrap: %s", exc)
        await self.reconcile_on_startup()
        self._ready = True

    async def reconcile_on_startup(self) -> None:
        """重启: 本地账本 vs 交易所净仓对账."""
        try:
            exch = await self.client.get_position(self.symbol)
        except Exception as exc:
            logger.warning("reconcile get_position: %s", exc)
            self.reconciliation_needed = True
            self._allow_new_entries = False
            return
        local = self._local_net()
        current = 0.0
        if exch.side == "LONG":
            current = exch.quantity
        elif exch.side == "SHORT":
            current = -exch.quantity
        if abs(local - current) > QTY_EPS:
            logger.error(
                "RECONCILIATION_NEEDED local_net=%.6f exchange=%.6f",
                local, current,
            )
            self.reconciliation_needed = True
            self._allow_new_entries = False
            if abs(local) < QTY_EPS and abs(current) > QTY_EPS:
                self.orphan_exchange_qty = current
                logger.error("orphan exchange position qty=%.6f — needs manual handling", current)
        else:
            self.reconciliation_needed = False
            self.orphan_exchange_qty = 0.0
            # 有持仓时仍允许减仓, 新开仓看策略开关
            self._allow_new_entries = True
        self._persist()

    async def flatten_orphan_exchange(self) -> OrderResult:
        """本地空、交易所有仓：市价平掉孤立仓并解除对账阻塞。不下策略归属。"""
        try:
            exch = await self.client.get_position(self.symbol)
        except Exception as exc:
            return OrderResult(ok=False, error=f"get_position: {exc}")
        if exch.side == "FLAT" or abs(exch.quantity) < QTY_EPS:
            self.reconciliation_needed = False
            self.orphan_exchange_qty = 0.0
            self._allow_new_entries = True
            self._persist()
            return OrderResult(ok=True, status="FLAT", error="already flat")
        if abs(self._local_net()) > QTY_EPS:
            return OrderResult(
                ok=False,
                error="local_not_flat — use strategy close, not orphan flatten",
            )
        res = await self.client.market_close(self.symbol)
        if res.ok or res.status == "FLAT":
            self.reconciliation_needed = False
            self.orphan_exchange_qty = 0.0
            self._allow_new_entries = True
            self.bump_version()
            self._persist()
            logger.warning(
                "orphan exchange flattened qty_was side=%s — recon cleared",
                exch.side,
            )
        return res

    def reconciliation_snapshot(self) -> Dict[str, Any]:
        """对账状态快照，供 API 与前端展示。"""
        return {
            "reconciliation_needed": self.reconciliation_needed,
            "allow_new_entries": self._allow_new_entries,
            "local_net": self._local_net(),
            "orphan_exchange_qty": self.orphan_exchange_qty,
            "positions": self.snapshot(),
            "state_version": self._state_version,
        }

    async def reconcile_now(self, *, reason: str = "manual") -> Dict[str, Any]:
        """任意时刻从交易所实时状态重新推导一致性，并决定是否解除开仓阻塞。

        与 reconcile_on_startup 的区别：可在运行期调用，用于清除陈旧的对账标志，
        无需重启进程。判定规则：

          1. 交易所净仓与本地账本一致 → 清除阻塞，恢复开仓
          2. 本地空仓、交易所有仓 → 记为孤立仓，保持阻塞（需人工决定归属或平仓）
          3. 本地有仓、交易所已空 → 视为外部平仓，清空本地账本后解除阻塞
          4. 两边都有仓但数量不符 → 无法可靠归属，保持阻塞

        原则：一致性只能从交易所实时状态重新推导，不能依赖内存里的陈旧布尔值。
        """
        before = self.reconciliation_snapshot()
        try:
            exch = await self.client.get_position(self.symbol)
        except Exception as exc:
            self.reconciliation_needed = True
            self._allow_new_entries = False
            self._persist()
            return {
                "ok": False, "reason": reason, "stage": "get_position",
                "error": str(exc), "before": before,
                "after": self.reconciliation_snapshot(), "actions": [],
            }

        exchange_signed = 0.0
        if getattr(exch, "side", "FLAT") == "LONG":
            exchange_signed = float(exch.quantity or 0)
        elif getattr(exch, "side", "FLAT") == "SHORT":
            exchange_signed = -float(exch.quantity or 0)
        local = self._local_net()
        actions: List[Any] = []

        # 规则 1：一致
        if abs(local - exchange_signed) <= QTY_EPS:
            self.reconciliation_needed = False
            self.orphan_exchange_qty = 0.0
            self._allow_new_entries = True
            self.bump_version()
            self._persist()
            return {
                "ok": True, "reason": reason, "stage": "consistent",
                "local_net": local, "exchange_signed": exchange_signed,
                "before": before, "after": self.reconciliation_snapshot(),
                "actions": [],
            }

        # 规则 3：本地有仓、交易所已空 → 外部平仓，清空本地账本
        if abs(exchange_signed) <= QTY_EPS and abs(local) > QTY_EPS:
            actions = self.apply_external_position_update(
                exchange_signed=0.0, reason=f"external_flat:{reason}"
            )
            self.reconciliation_needed = False
            self.orphan_exchange_qty = 0.0
            self._allow_new_entries = True
            self.bump_version()
            self._persist()
            logger.warning(
                "对账：交易所已空而本地有仓 %.6f → 按外部平仓清空本地账本", local
            )
            return {
                "ok": True, "reason": reason, "stage": "external_flat_applied",
                "local_net": local, "exchange_signed": exchange_signed,
                "before": before, "after": self.reconciliation_snapshot(),
                "actions": [a.to_dict() for a in actions if hasattr(a, "to_dict")],
            }

        # 规则 2：本地空仓、交易所有仓 → 孤立仓
        if abs(local) <= QTY_EPS and abs(exchange_signed) > QTY_EPS:
            self.reconciliation_needed = True
            self._allow_new_entries = False
            self.orphan_exchange_qty = exchange_signed
            self.bump_version()
            self._persist()
            return {
                "ok": False, "reason": reason,
                "stage": "orphan_exchange_position",
                "error": "交易所存在本地账本无法归属的仓位，需人工决定平仓或归属",
                "local_net": local, "exchange_signed": exchange_signed,
                "orphan_exchange_qty": exchange_signed,
                "before": before, "after": self.reconciliation_snapshot(),
                "actions": [],
            }

        # 规则 4：两边都有仓但数量不符
        self.reconciliation_needed = True
        self._allow_new_entries = False
        self.orphan_exchange_qty = exchange_signed
        self.bump_version()
        self._persist()
        return {
            "ok": False, "reason": reason, "stage": "quantity_mismatch",
            "error": f"本地净仓 {local:.6f} 与交易所 {exchange_signed:.6f} 不符",
            "local_net": local, "exchange_signed": exchange_signed,
            "before": before, "after": self.reconciliation_snapshot(),
            "actions": [],
        }

    async def _calc_qty(
        self,
        horizon: str,
        price: float,
        *,
        cs: float = 0.0,
        min_confidence: float = 1.0,
        atr: Optional[float] = None,
        atr_mean: Optional[float] = None,
        require_atr: bool = True,
        strategy_params: Optional[dict] = None,
    ) -> tuple:
        bal = await self.client.get_balance()
        # 策略权益 = wallet + upnl；禁止 wallet/available 互兜底冒充
        wallet = float(bal.total_wallet_balance or 0)
        upnl = float(bal.total_unrealized_pnl or 0)
        equity = wallet + upnl if wallet > 0 else 0.0
        if equity <= 0:
            return 0.0, 0.0, 0.0
        if price <= 0:
            return 0.0, 0.0, 0.0
        if require_atr and (atr is None or atr <= 0):
            logger.warning("拒绝开仓: 无有效 ATR horizon=%s", horizon)
            return 0.0, 0.0, 0.0

        sp = strategy_params or {}
        risk_table = sp.get("RISK_PER_TRADE_PCT", RISK_PER_TRADE_PCT)
        exit_table = sp.get("EXIT_STRATEGY", EXIT_STRATEGY)
        risk_pct = float(risk_table.get(horizon, 0.005))
        risk_usdt = equity * risk_pct
        risk_cap = risk_usdt  # 强信号也不得超过此上限

        cfg = exit_table.get(horizon) or exit_table["short_term"]
        stop_atr_mult = float(cfg.get("hard_sl_atr") or 1.0)
        stop_pct_fallback = float(cfg.get("hard_sl_pct") or 0.01)

        if atr is not None and atr > 0:
            stop_distance = atr * stop_atr_mult
        else:
            stop_distance = price * stop_pct_fallback
        if stop_distance <= 0:
            return 0.0, 0.0, 0.0

        qty = risk_usdt / stop_distance

        strong_threshold = float(sp.get("POSITION_CS_STRONG_THRESHOLD", POSITION_CS_STRONG_THRESHOLD))
        strong_mult = float(sp.get("POSITION_CS_STRONG_MULT", POSITION_CS_STRONG_MULT))
        conf_threshold = float(sp.get("POSITION_CONF_LOW_THRESHOLD", POSITION_CONF_LOW_THRESHOLD))
        conf_mult = float(sp.get("POSITION_CONF_LOW_MULT", POSITION_CONF_LOW_MULT))
        atr_ratio = float(sp.get("POSITION_ATR_HIGH_RATIO", POSITION_ATR_HIGH_RATIO))
        atr_mult = float(sp.get("POSITION_ATR_HIGH_MULT", POSITION_ATR_HIGH_MULT))
        if abs(cs) >= strong_threshold:
            # 强信号可在预算内加大仓位意图，但 risk_usdt 不得超过声明上限 risk_cap
            qty *= strong_mult
            # 立即按上限回钳数量
            max_qty_by_risk = risk_cap / stop_distance
            if qty > max_qty_by_risk:
                qty = max_qty_by_risk
            risk_usdt = min(qty * stop_distance, risk_cap)
        if min_confidence < conf_threshold:
            qty *= conf_mult
            risk_usdt *= conf_mult
        if (
            atr is not None
            and atr_mean is not None
            and atr_mean > 0
            and atr > atr_mean * atr_ratio
        ):
            qty *= atr_mult
            risk_usdt *= atr_mult

        max_table = sp.get("MAX_NOTIONAL_PCT", MAX_NOTIONAL_PCT)
        max_pct = float(max_table.get(horizon) or POSITION_NOTIONAL_PCT.get(horizon, 0.1))
        max_notional = max(MIN_NOTIONAL_USDT, equity * max_pct)
        other_notional = 0.0
        for h, p in self.positions.items():
            if h == horizon:
                continue
            if p is not None and not p.is_flat():
                other_notional += abs(p.quantity) * price
        total_cap = equity * float(sp.get("TOTAL_MAX_NOTIONAL_PCT", TOTAL_MAX_NOTIONAL_PCT))
        remaining_cap = max(0.0, total_cap - other_notional)
        max_notional = min(max_notional, remaining_cap)

        qty_raw = qty
        if max_notional <= 0:
            return 0.0, 0.0, stop_distance
        if qty * price > max_notional:
            qty = max_notional / price

        min_qty = MIN_NOTIONAL_USDT / price
        if qty < min_qty:
            # 最小名义不得突破预算
            if min_qty * price > max_notional + 1e-9:
                return 0.0, 0.0, stop_distance
            if min_qty * stop_distance > risk_cap + 1e-9:
                logger.warning("最小名义会突破风险预算, 跳过")
                return 0.0, 0.0, stop_distance
            qty = min_qty

        if qty < qty_raw - 1e-12:
            risk_usdt = qty * stop_distance
        # 硬上限: 不得超过放大后的风险预算
        risk_usdt = min(risk_usdt, risk_cap)
        # 最终按舍入前数量再算；调用方成交后按 filled 重算
        if qty * stop_distance > risk_cap + 1e-9:
            qty = risk_cap / stop_distance
            risk_usdt = risk_cap
        return qty, risk_usdt, stop_distance

    # ------------------------------------------------------------------ sync
    async def _sync_exchange_to_net(self, price: float) -> OrderResult:
        """把交易所净仓调到策略账本合计. 策略数量已在本地更新."""
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
        if abs(delta) < QTY_EPS:
            return OrderResult(ok=True, status="SYNCED", avg_price=price)

        active_lev = LEVERAGE_SHORT_TERM
        for h, p in self.positions.items():
            if p and not p.is_flat():
                active_lev = max(active_lev, int(p.leverage or self.leverage[h]))
        try:
            await self.client.set_leverage(active_lev, self.symbol)
        except Exception as exc:
            logger.warning("set_leverage: %s", exc)

        if abs(target) < QTY_EPS:
            return await self.client.market_close(self.symbol)

        # 同号缩小: 优先 reduceOnly 减仓, 不要先全平再开
        if abs(current) > QTY_EPS and current * target > 0 and abs(target) < abs(current) - QTY_EPS:
            reduce_side = "SHORT" if current > 0 else "LONG"
            reduce_qty = abs(current) - abs(target)
            return await self.client.market_open(
                side=reduce_side, quantity=reduce_qty, symbol=self.symbol, reduce_only=True
            )

        # 翻向: 先 reduceOnly 平掉现有, 再开新方向
        if abs(current) > QTY_EPS and current * target < 0:
            close_side = "SHORT" if current > 0 else "LONG"
            close_res = await self.client.market_open(
                side=close_side, quantity=abs(current), symbol=self.symbol, reduce_only=True
            )
            if not close_res.ok and close_res.status != "FLAT":
                return close_res
            current = 0.0
            delta = target - current

        side = "LONG" if delta > 0 else "SHORT"
        return await self.client.market_open(
            side=side, quantity=abs(delta), symbol=self.symbol
        )

    def _record_internal_match(
        self, price: float, before_net: float, after_intended: Dict[str, float]
    ) -> List[FillAllocation]:
        """记录内部虚拟匹配 (对冲部分)."""
        allocs: List[FillAllocation] = []
        # 简化: 若两策略反向, 重叠量记内部匹配
        s = after_intended.get("short_term", 0.0)
        l = after_intended.get("long_term", 0.0)
        if s * l < 0:
            overlap = min(abs(s), abs(l))
            if overlap > QTY_EPS:
                allocs.append(FillAllocation(
                    horizon="short_term",
                    side="LONG" if s > 0 else "SHORT",
                    quantity=overlap,
                    price=price,
                    is_internal_match=True,
                ))
                allocs.append(FillAllocation(
                    horizon="long_term",
                    side="LONG" if l > 0 else "SHORT",
                    quantity=overlap,
                    price=price,
                    is_internal_match=True,
                ))
        self.last_allocations = allocs
        return allocs

    # ------------------------------------------------------------------ signals
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
        config_snapshot: Optional[dict] = None,
    ) -> List[TradeAction]:
        if horizon not in ("short_term", "long_term"):
            return []
        async with self._lock():
            return await self._on_signal_locked(
                horizon, decision, price,
                cs=cs, min_confidence=min_confidence, atr=atr, atr_mean=atr_mean,
                entry_cs=entry_cs,
                config_snapshot=config_snapshot,
            )

    async def _on_signal_locked(
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
        config_snapshot: Optional[dict] = None,
    ) -> List[TradeAction]:
        await self.bootstrap()
        decision = str(decision)
        desired = _decision_side(decision)
        current = self.positions.get(horizon)
        actions: List[TradeAction] = []

        if (current is None or current.is_flat()) and desired is None:
            return []

        # NEUTRAL: 不强制平仓 (策略合同: 不扩仓)
        if current and not current.is_flat() and decision in FLAT_DECISIONS:
            return []

        if current and not current.is_flat() and desired == current.side:
            return []

        if current and not current.is_flat() and desired and desired != current.side:
            close_act = await self._local_close(horizon, decision, price, "reverse")
            if close_act:
                actions.append(close_act)
                if not (close_act.order and close_act.order.ok) and close_act.order and close_act.order.status not in ("FLAT", "SYNCED"):
                    return actions  # 反手前原仓未确认关闭
            open_act = await self._local_open(
                horizon, desired, decision, price, "reverse",
                cs=cs, min_confidence=min_confidence, atr=atr, atr_mean=atr_mean,
                entry_cs=entry_cs if entry_cs is not None else cs,
                config_snapshot=config_snapshot,
            )
            if open_act:
                actions.append(open_act)
            return actions

        if (current is None or current.is_flat()) and desired:
            if not self._allow_new_entries or self.reconciliation_needed:
                logger.warning("新开仓被阻止: reconcile=%s allow=%s", self.reconciliation_needed, self._allow_new_entries)
                return []
            open_act = await self._local_open(
                horizon, desired, decision, price, "signal",
                cs=cs, min_confidence=min_confidence, atr=atr, atr_mean=atr_mean,
                entry_cs=entry_cs if entry_cs is not None else cs,
                config_snapshot=config_snapshot,
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
        async with self._lock():
            return await self._apply_exit_locked(horizon, exit_act, price, decision)

    async def _apply_exit_locked(
        self,
        horizon: str,
        exit_act: ExitAction,
        price: float,
        decision: str = "EXIT",
    ) -> Optional[TradeAction]:
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
            self._persist()
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

        return await self._partial_close_locked(
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
        async with self._lock():
            return await self._partial_close_locked(horizon, close_pct, reason, price, decision)

    async def _partial_close_locked(
        self,
        horizon: str,
        close_pct: float,
        reason: str,
        price: float,
        decision: str = "EXIT",
    ) -> Optional[TradeAction]:
        current = self.positions.get(horizon)
        if current is None or current.is_flat():
            return None
        close_pct = max(0.0, min(1.0, float(close_pct)))
        if close_pct <= 0:
            return None
        if close_pct >= 0.999:
            return await self._local_close(horizon, decision, price, reason)

        intent_close = current.quantity * close_pct
        if intent_close <= 0:
            return None

        backup = HorizonPosition(**{k: v for k, v in current.to_dict().items()})
        # 用意图目标驱动同步，但确认仓只按实际成交回写
        current.quantity = max(0.0, backup.quantity - intent_close)
        current.remaining_pct = max(0.0, backup.remaining_pct * (1.0 - close_pct))
        if reason == "tp1":
            current.tp_levels_hit = max(current.tp_levels_hit, 1)
        elif reason == "tp2":
            current.tp_levels_hit = max(current.tp_levels_hit, 2)
        elif reason == "cs_decay":
            current.cs_decay_done = True

        if current.quantity <= QTY_EPS:
            self.positions[horizon] = backup
            return await self._local_close(horizon, decision, price, reason)

        self.bump_version()
        order = await self._sync_exchange_to_net(price)
        filled = float(order.cum_filled_qty or 0.0)
        if order.status == "SYNCED":
            filled = intent_close

        # 按交易所净仓反推本 horizon 确认剩余
        others = sum(_signed_qty(p) for h, p in self.positions.items() if h != horizon)
        # restore then apply fill
        try:
            exch = await self.client.get_position(self.symbol)
            actual = 0.0
            if exch.side == "LONG":
                actual = exch.quantity
            elif exch.side == "SHORT":
                actual = -exch.quantity
            confirmed_signed = actual - others
            if backup.side == "LONG":
                confirmed_remain = max(0.0, confirmed_signed)
            else:
                confirmed_remain = max(0.0, -confirmed_signed)
            filled = max(0.0, backup.quantity - confirmed_remain)
        except Exception:
            confirmed_remain = max(0.0, backup.quantity - filled)

        act = TradeAction(
            action="partial_close",
            horizon=horizon,
            side=backup.side,
            quantity=filled,
            price=order.avg_price or price,
            leverage=backup.leverage,
            decision=decision,
            reason=reason,
            order=order,
            trade_id=backup.trade_id,
        )

        if not order.ok and order.status not in ("FLAT", "SYNCED"):
            if order.order_state == OrderState.UNKNOWN.value or "unknown" in (order.error or ""):
                self.reconciliation_needed = True
                self._allow_new_entries = False
                # 未知：用交易所可见剩余
                restored = HorizonPosition(**{k: v for k, v in backup.to_dict().items()})
                restored.quantity = confirmed_remain
                restored.exit_incomplete = True
                restored.pending_exit_qty = max(0.0, intent_close - filled)
                self.positions[horizon] = restored
            else:
                self.positions[horizon] = backup
            logger.error("减仓同步失败/未知 %s: %s", horizon, order.error)
            self._persist()
            return act

        restored = HorizonPosition(**{k: v for k, v in backup.to_dict().items()})
        restored.quantity = confirmed_remain
        restored.remaining_pct = (
            confirmed_remain / restored.original_quantity
            if restored.original_quantity > 0 else 0.0
        )
        if reason == "tp1":
            restored.tp_levels_hit = max(restored.tp_levels_hit, 1)
        elif reason == "tp2":
            restored.tp_levels_hit = max(restored.tp_levels_hit, 2)
        elif reason == "cs_decay":
            restored.cs_decay_done = True
        if filled + QTY_EPS < intent_close:
            restored.exit_incomplete = True
            restored.pending_exit_qty = intent_close - filled
        else:
            restored.exit_incomplete = False
            restored.pending_exit_qty = 0.0
        if confirmed_remain <= QTY_EPS:
            self.positions[horizon] = None
        else:
            self.positions[horizon] = restored
        self.bump_version()
        self._persist()
        logger.info(
            "减仓 %s filled=%.6f remain=%.6f intent=%.6f reason=%s",
            horizon, filled, confirmed_remain, intent_close, reason,
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
        config_snapshot: Optional[dict] = None,
    ) -> Optional[TradeAction]:
        existing = self.positions.get(horizon)
        if existing and not existing.is_flat():
            logger.warning("position_mgr: %s 已有仓, 拒绝开仓", horizon)
            return None
        strategy_params = (config_snapshot or {}).get("parameters") or {}
        lev_key = "LEVERAGE_SHORT_TERM" if horizon == "short_term" else "LEVERAGE_LONG_TERM"
        lev = int(strategy_params.get(lev_key, self.leverage[horizon]))
        qty, risk_usdt, stop_distance = await self._calc_qty(
            horizon, price,
            cs=cs, min_confidence=min_confidence, atr=atr, atr_mean=atr_mean,
            require_atr=True,
            strategy_params=strategy_params,
        )
        if qty <= 0:
            logger.warning("position_mgr: qty=0, 跳过")
            return None

        # 合法化步长 — 账本不得保留截断前意图
        try:
            step = await self.client._lot_step(self.symbol)
            qty = self.client._qty_precision(qty, step)
        except Exception:
            qty = round(qty, 3)
        if qty <= 0:
            return None
        risk_usdt = min(qty * stop_distance, risk_usdt)

        entry_atr = float(atr or 0)
        if side == "LONG":
            sl_price = price - stop_distance
        else:
            sl_price = price + stop_distance

        intended = {
            h: _signed_qty(p) for h, p in self.positions.items()
        }
        intended[horizon] = qty if side == "LONG" else -qty
        before_net = self._local_net()
        others_net = sum(
            _signed_qty(p) for h, p in self.positions.items() if h != horizon
        )

        # 先按合法化目标记账，成交后再按确认量回写
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
            config_snapshot=dict(config_snapshot or {}),
        )
        self._record_internal_match(price, before_net, intended)

        order = await self._sync_exchange_to_net(price)

        # 按交易所实际净仓反推本 horizon 已确认量（守恒）
        confirmed_qty = 0.0
        try:
            exch = await self.client.get_position(self.symbol)
            actual = 0.0
            if exch.side == "LONG":
                actual = exch.quantity
            elif exch.side == "SHORT":
                actual = -exch.quantity
            confirmed_signed = actual - others_net
            if side == "LONG":
                confirmed_qty = max(0.0, confirmed_signed)
            else:
                confirmed_qty = max(0.0, -confirmed_signed)
        except Exception:
            confirmed_qty = float(order.cum_filled_qty or order.quantity or 0)

        if order.status == "SYNCED":
            # 纯内部匹配，无交易所成交 — 确认量=合法化意图
            confirmed_qty = qty

        act = TradeAction(
            action="reverse_open" if reason == "reverse" else "open",
            horizon=horizon,
            side=side,
            quantity=confirmed_qty,
            price=order.avg_price or price,
            leverage=lev,
            decision=decision,
            reason=reason,
            order=order,
        )

        if not order.ok and order.status not in ("SYNCED",):
            if order.order_state == "unknown" or "submitted_unknown" in (order.error or ""):
                self.reconciliation_needed = True
                self._allow_new_entries = False
                # 未决：若交易所已有仓则用确认量，否则清空意图仓
                if confirmed_qty > QTY_EPS:
                    self.positions[horizon].quantity = confirmed_qty
                    self.positions[horizon].original_quantity = confirmed_qty
                else:
                    self.positions[horizon] = None
                logger.error("开仓结果未知, 进入对账模式: %s", order.error)
            else:
                self.positions[horizon] = None
                logger.error("开仓同步失败 %s: %s", horizon, order.error)
            self._persist()
            return act

        if confirmed_qty <= QTY_EPS:
            self.positions[horizon] = None
            self._persist()
            return act

        pos = self.positions[horizon]
        if pos:
            pos.quantity = confirmed_qty
            pos.original_quantity = confirmed_qty
            pos.risk_usdt = confirmed_qty * stop_distance
            fill = order.avg_price or price
            if fill > 0:
                pos.entry_price = fill
                pos.peak_price = fill
                if side == "LONG":
                    pos.sl_price = fill - stop_distance
                else:
                    pos.sl_price = fill + stop_distance
            pos.order_id = order.order_id
        self._persist()
        logger.info(
            "开仓 %s %s qty=%.4f (confirmed) @ %.2f lev=%dx risk=$%.1f net=%.4f",
            horizon, side, confirmed_qty, act.price, lev,
            pos.risk_usdt if pos else risk_usdt, self._local_net(),
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
        backup = HorizonPosition(**{k: v for k, v in current.to_dict().items()})
        # 意图：本 horizon 归零，驱动净仓同步；确认后按成交回写残余
        self.positions[horizon] = None
        self.bump_version()
        order = await self._sync_exchange_to_net(price)

        filled = float(order.cum_filled_qty or 0.0)
        others = sum(_signed_qty(p) for h, p in self.positions.items() if h != horizon)
        residual = 0.0
        try:
            exch = await self.client.get_position(self.symbol)
            actual = 0.0
            if exch.side == "LONG":
                actual = exch.quantity
            elif exch.side == "SHORT":
                actual = -exch.quantity
            # 本 horizon 应占 actual - others；全平意图后 others 应等于 actual
            residual_signed = actual - others
            if side == "LONG":
                residual = max(0.0, residual_signed)
            else:
                residual = max(0.0, -residual_signed)
            filled = max(0.0, qty - residual)
        except Exception:
            if order.ok and order.status in ("FLAT", "SYNCED"):
                residual = 0.0
                filled = qty
            elif order.ok:
                residual = max(0.0, qty - filled)
            else:
                residual = qty
                filled = 0.0

        act = TradeAction(
            action="reverse_close" if reason == "reverse" else "close",
            horizon=horizon,
            side=side,
            quantity=filled,
            price=order.avg_price or price,
            leverage=lev,
            decision=decision,
            reason=reason,
            order=order,
            trade_id=trade_id,
        )

        if not order.ok and order.status not in ("FLAT", "SYNCED"):
            if (order.order_state or "") == "unknown" or "unknown" in (order.error or ""):
                self.reconciliation_needed = True
                self._allow_new_entries = False
            # 失败或未知：恢复残余（交易所可见）或备份
            if residual > QTY_EPS:
                restored = HorizonPosition(**{k: v for k, v in backup.to_dict().items()})
                restored.quantity = residual
                restored.exit_incomplete = True
                restored.pending_exit_qty = max(0.0, qty - filled)
                self.positions[horizon] = restored
            else:
                self.positions[horizon] = backup
            logger.error("平仓未完全确认 %s: %s residual=%.6f", horizon, order.error, residual)
            self._persist()
            return act

        if residual > QTY_EPS:
            # 部分全平：保留残余 + 未完成标记；保护必须继续覆盖
            restored = HorizonPosition(**{k: v for k, v in backup.to_dict().items()})
            restored.quantity = residual
            restored.exit_incomplete = True
            restored.pending_exit_qty = residual  # 仍需平掉的量
            restored.remaining_pct = (
                residual / restored.original_quantity if restored.original_quantity > 0 else 0.0
            )
            self.positions[horizon] = restored
            logger.warning(
                "全平部分成交 %s filled=%.6f residual=%.6f — 本地不归零",
                horizon, filled, residual,
            )
        else:
            self.positions[horizon] = None
        self.bump_version()
        self._persist()
        return act

    async def force_close(self, horizon: str) -> Optional[TradeAction]:
        try:
            price = await self.client.mark_price(self.symbol)
        except Exception:
            price = 0.0
        async with self._lock():
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
