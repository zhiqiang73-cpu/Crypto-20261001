"""假交易所 — 足够真实以捕捉工程错误，不为实现放水."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from trading.models import AccountBalance, ManagedOrder, OrderResult, OrderState, PositionInfo


@dataclass
class FakeOrderBook:
    orders: Dict[str, ManagedOrder] = field(default_factory=dict)
    next_id: int = 1


class FakeBinanceClient:
    """接口对齐真实客户端关键子集 + Algo 条件单."""

    def __init__(
        self,
        *,
        equity: float = 5000.0,
        mark: float = 100.0,
        lot_step: float = 0.001,
        fail_next: int = 0,
        partial_fill_pct: float = 1.0,
        drop_response: bool = False,
        latency_ms: float = 0.0,
    ) -> None:
        self.symbol = "BTCUSDT"
        self.configured = True
        self._equity = equity
        self._available = equity
        self._mark = mark
        self._lot_step_size = lot_step
        self._net_qty = 0.0
        self._entry = 0.0
        self._book = FakeOrderBook()
        self._fail_next = fail_next
        self._partial_fill_pct = partial_fill_pct
        self._drop_response = drop_response
        self._latency_ms = latency_ms
        self._hedge_mode = False
        self._new_then_unknown = False
        self._query_fail_once = False
        self.placed: List[ManagedOrder] = []
        self.market_orders: List[dict] = []
        self._reject_legacy_stop = True  # 旧 STOP_MARKET 走 /order 应失败

    @property
    def equity(self) -> float:
        return float(self._equity)

    async def close(self) -> None:
        return None

    async def set_one_way_mode(self) -> None:
        self._hedge_mode = False

    async def set_margin_type_isolated(self, symbol: Optional[str] = None) -> None:
        return None

    async def set_leverage(self, leverage: int, symbol: Optional[str] = None) -> dict:
        return {"leverage": leverage}

    async def get_position_mode(self) -> bool:
        return self._hedge_mode

    async def mark_price(self, symbol: Optional[str] = None) -> float:
        return self._mark

    def set_mark(self, mark: float) -> None:
        self._mark = float(mark)

    async def get_balance(self) -> AccountBalance:
        return AccountBalance(
            total_wallet_balance=self._equity,
            available_balance=self._available,
            total_unrealized_pnl=0.0,
        )

    async def get_position(self, symbol: Optional[str] = None) -> PositionInfo:
        if abs(self._net_qty) < 1e-12:
            return PositionInfo(symbol=self.symbol, side="FLAT")
        return PositionInfo(
            symbol=self.symbol,
            side="LONG" if self._net_qty > 0 else "SHORT",
            quantity=abs(self._net_qty),
            entry_price=self._entry,
            mark_price=self._mark,
            leverage=2,
        )

    def _qty_precision(self, qty: float, step: float = 0.001) -> float:
        if step <= 0:
            return round(qty, 3)
        n = int(qty / step + 1e-12)
        return round(n * step, 8)

    async def _lot_step(self, symbol: Optional[str] = None) -> float:
        return self._lot_step_size

    async def market_open(
        self,
        side: str,
        quantity: float,
        symbol: Optional[str] = None,
        reduce_only: bool = False,
        *,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        if self._latency_ms:
            await asyncio.sleep(self._latency_ms / 1000.0)
        cid = client_order_id or f"fake{int(time.time()*1000)}"
        requested = float(quantity)
        qty = self._qty_precision(quantity, self._lot_step_size)

        if self._new_then_unknown:
            # 模拟: 交易所可能已受理，但查询未知 — 本地不得当成功
            self._new_then_unknown = False
            # 实际未成交（或未确认）
            mo = ManagedOrder(
                client_order_id=cid,
                state=OrderState.UNKNOWN,
                exchange_order_id="",
                symbol=self.symbol,
                side="BUY" if side.upper() == "LONG" else "SELL",
                position_side=side.upper(),
                requested_qty=requested,
                submitted_qty=qty,
                quantity=qty,
                cum_filled_qty=0.0,
                filled_qty=0.0,
                error="query_unknown_after_new",
            )
            self._book.orders[cid] = mo
            return OrderResult(
                ok=False,
                error="new_then_unknown",
                client_order_id=cid,
                order_state=OrderState.UNKNOWN.value,
                requested_qty=requested,
                submitted_qty=qty,
                cum_filled_qty=0.0,
                quantity=0.0,
                side="BUY" if side.upper() == "LONG" else "SELL",
                symbol=self.symbol,
            )

        if self._fail_next > 0:
            self._fail_next -= 1
            return OrderResult(
                ok=False,
                error="simulated_reject",
                client_order_id=cid,
                order_state=OrderState.REJECTED.value,
                requested_qty=requested,
                submitted_qty=qty,
                cum_filled_qty=0.0,
                quantity=0.0,
            )

        if qty <= 0:
            return OrderResult(ok=False, error="zero qty after step", client_order_id=cid,
                               requested_qty=requested, submitted_qty=0.0, quantity=0.0,
                               order_state=OrderState.REJECTED.value)

        fill = self._qty_precision(qty * self._partial_fill_pct, self._lot_step_size)
        if fill <= 0:
            return OrderResult(ok=False, error="zero fill", client_order_id=cid,
                               requested_qty=requested, submitted_qty=qty, quantity=0.0,
                               order_state=OrderState.REJECTED.value)

        side_u = side.upper()
        signed = fill if side_u == "LONG" else -fill
        if reduce_only:
            if self._net_qty * signed > 0:
                return OrderResult(ok=False, error="reduce_only_wrong_side", client_order_id=cid,
                                   order_state=OrderState.REJECTED.value, quantity=0.0)
            max_reduce = abs(self._net_qty)
            if fill > max_reduce + 1e-12:
                fill = self._qty_precision(max_reduce, self._lot_step_size)
                signed = fill if side_u == "LONG" else -fill

        old = self._net_qty
        self._net_qty = round(self._net_qty + signed, 12)
        if abs(self._net_qty) < 1e-12:
            self._net_qty = 0.0
            self._entry = 0.0
        elif old == 0 or (old * self._net_qty < 0):
            self._entry = self._mark

        oid = str(self._book.next_id)
        self._book.next_id += 1
        state = OrderState.FILLED if fill + 1e-12 >= qty else OrderState.PARTIALLY_FILLED
        mo = ManagedOrder(
            client_order_id=cid,
            state=state,
            exchange_order_id=oid,
            symbol=self.symbol,
            side="BUY" if side_u == "LONG" else "SELL",
            position_side=side_u,
            requested_qty=requested,
            submitted_qty=qty,
            quantity=qty,
            cum_filled_qty=fill,
            filled_qty=fill,
            last_fill_qty=fill,
            avg_price=self._mark,
            reduce_only=reduce_only,
        )
        self._book.orders[cid] = mo
        self.placed.append(mo)
        self.market_orders.append({"side": side_u, "qty": fill, "reduce_only": reduce_only})

        if self._drop_response:
            self._drop_response = False
            return OrderResult(
                ok=False,
                error="response_lost",
                client_order_id=cid,
                order_state=OrderState.UNKNOWN.value,
                order_id=oid,
                requested_qty=requested,
                submitted_qty=qty,
                cum_filled_qty=0.0,  # 本地未知
                quantity=0.0,
            )
        return mo.to_order_result()

    async def user_trades(self, symbol: Optional[str] = None, **kwargs) -> list:
        """官方 userTrades 形状的成交明细（含手续费）。"""
        out = []
        for mo in self.placed:
            if mo.is_stop or mo.is_algo:
                continue
            if (mo.cum_filled_qty or mo.filled_qty or 0) <= 0:
                continue
            tid = f"tr_{mo.exchange_order_id}"
            out.append({
                "symbol": self.symbol,
                "id": int(mo.exchange_order_id) if str(mo.exchange_order_id).isdigit() else hash(tid) % 10**9,
                "orderId": mo.exchange_order_id,
                "clientOrderId": mo.client_order_id,
                "price": str(mo.avg_price or self._mark),
                "qty": str(mo.cum_filled_qty or mo.filled_qty),
                "commission": str(round(0.0004 * (mo.cum_filled_qty or 0) * (mo.avg_price or self._mark), 6)),
                "commissionAsset": "USDT",
                "realizedPnl": "0",
                "time": int(time.time() * 1000),
                "maker": False,
            })
        return out

    async def market_close(self, symbol: Optional[str] = None) -> OrderResult:
        if abs(self._net_qty) < 1e-12:
            return OrderResult(ok=True, status="FLAT", error="already flat", quantity=0.0)
        side = "SHORT" if self._net_qty > 0 else "LONG"
        return await self.market_open(side, abs(self._net_qty), reduce_only=True)

    async def query_order(
        self,
        *,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> ManagedOrder:
        if self._query_fail_once:
            self._query_fail_once = False
            return ManagedOrder(
                client_order_id=client_order_id or "",
                state=OrderState.UNKNOWN,
                error="query_failed",
            )
        if client_order_id and client_order_id in self._book.orders:
            return self._book.orders[client_order_id]
        for mo in self._book.orders.values():
            if order_id and mo.exchange_order_id == str(order_id):
                return mo
        return ManagedOrder(
            client_order_id=client_order_id or "",
            state=OrderState.UNKNOWN,
            error="not_found",
        )

    async def get_open_orders(self, symbol: Optional[str] = None) -> list:
        out = []
        for mo in self._book.orders.values():
            if mo.state not in (OrderState.ACKNOWLEDGED, OrderState.SUBMITTED):
                continue
            if mo.is_stop or mo.is_algo:
                out.append({
                    "orderId": mo.exchange_order_id,
                    "algoId": mo.algo_id or mo.exchange_order_id,
                    "clientOrderId": mo.client_order_id,
                    "clientAlgoId": mo.client_order_id,
                    "type": "STOP_MARKET",
                    "algoType": "CONDITIONAL",
                    "stopPrice": mo.stop_price,
                    "triggerPrice": mo.stop_price,
                    "side": mo.side,
                    "positionSide": mo.position_side,
                    "origQty": mo.submitted_qty or mo.quantity,
                })
        return out

    async def get_open_algo_orders(self, symbol: Optional[str] = None) -> list:
        return await self.get_open_orders(symbol)

    async def place_stop_market(
        self,
        side: str,
        quantity: float,
        stop_price: float,
        symbol: Optional[str] = None,
        *,
        client_order_id: Optional[str] = None,
        close_position: bool = False,
    ) -> ManagedOrder:
        """Algo 条件单路径 — 标记 is_algo=True."""
        cid = client_order_id or f"sl{int(time.time()*1000)}"
        if self._fail_next > 0:
            self._fail_next -= 1
            return ManagedOrder(
                client_order_id=cid,
                state=OrderState.REJECTED,
                error="stop_rejected",
                is_stop=True,
                is_algo=True,
                stop_price=stop_price,
            )
        oid = str(self._book.next_id)
        self._book.next_id += 1
        qty = self._qty_precision(quantity, self._lot_step_size) if not close_position else 0.0
        mo = ManagedOrder(
            client_order_id=cid,
            state=OrderState.ACKNOWLEDGED,
            exchange_order_id=oid,
            algo_id=oid,
            symbol=self.symbol,
            side="SELL" if side.upper() == "LONG" else "BUY",
            position_side=side.upper(),
            requested_qty=quantity,
            submitted_qty=qty,
            quantity=qty,
            is_stop=True,
            is_algo=True,
            stop_price=float(stop_price),
            raw={"algoType": "CONDITIONAL", "triggerPrice": float(stop_price),
                 "clientAlgoId": cid, "algoId": oid},
        )
        self._book.orders[cid] = mo
        self.placed.append(mo)
        return mo

    async def cancel_order(
        self,
        *,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> ManagedOrder:
        mo = await self.query_order(client_order_id=client_order_id, order_id=order_id)
        if mo.state != OrderState.UNKNOWN:
            mo.state = OrderState.CANCELED
        return mo

    async def cancel_algo_order(
        self,
        *,
        client_algo_id: Optional[str] = None,
        algo_id: Optional[str] = None,
    ) -> ManagedOrder:
        return await self.cancel_order(client_order_id=client_algo_id, order_id=algo_id)

    async def cancel_all_stops(self, symbol: Optional[str] = None) -> int:
        n = 0
        for mo in list(self._book.orders.values()):
            if mo.is_stop and mo.state == OrderState.ACKNOWLEDGED:
                mo.state = OrderState.CANCELED
                n += 1
        return n

    def trigger_stop(self, mark: float) -> Optional[ManagedOrder]:
        """触发保护：只减掉该保护覆盖的净仓方向数量，不盲目清零再假装."""
        self._mark = mark
        for mo in list(self._book.orders.values()):
            if not mo.is_stop or mo.state != OrderState.ACKNOWLEDGED:
                continue
            hit = False
            if mo.position_side == "LONG" and mark <= mo.stop_price:
                hit = True
            if mo.position_side == "SHORT" and mark >= mo.stop_price:
                hit = True
            if not hit:
                continue
            # 平掉同向净仓（或 closePosition 全平该方向）
            close_qty = mo.submitted_qty or mo.quantity or abs(self._net_qty)
            if mo.position_side == "LONG" and self._net_qty > 0:
                close_qty = min(close_qty, self._net_qty) if close_qty > 0 else self._net_qty
                self._net_qty = round(self._net_qty - close_qty, 12)
            elif mo.position_side == "SHORT" and self._net_qty < 0:
                close_qty = min(close_qty, abs(self._net_qty)) if close_qty > 0 else abs(self._net_qty)
                self._net_qty = round(self._net_qty + close_qty, 12)
            else:
                # closePosition 语义：全平
                close_qty = abs(self._net_qty)
                self._net_qty = 0.0
            if abs(self._net_qty) < 1e-12:
                self._net_qty = 0.0
                self._entry = 0.0
            mo.state = OrderState.FILLED
            mo.cum_filled_qty = close_qty
            mo.filled_qty = close_qty
            mo.last_fill_qty = close_qty
            mo.avg_price = mark
            return mo
        return None
