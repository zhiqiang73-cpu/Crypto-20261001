"""Binance USDⓈ-M Futures Testnet REST 客户端.

默认 base: https://testnet.binancefuture.com
签名: HMAC-SHA256(query_string, secret)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.review import BINANCE_TESTNET_DEFAULT_BASE, TRADING_SYMBOL
from config.secrets import get_secret, mask_secret
from trading.runtime_mode import validate_exchange_target
from trading.models import AccountBalance, ManagedOrder, OrderResult, OrderState, PositionInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 限价追价参数 (改动 B)
#
# 设计目标: 先被动挂单争取 maker 手续费, 未成交则逐档朝市价推进,
# 最后一档穿越盘口确保成交。绝不静默挂单不成交。
# ---------------------------------------------------------------------------
CHASE_INITIAL_OFFSET_BPS = 2.0     # 初始被动偏移 (万分之几), 且不小于 1 tick
CHASE_POLL_INTERVAL = 3.0          # 每档轮询间隔 (秒)
CHASE_REPRICE_INTERVAL = 5.0       # 每档等待多久后追价 (秒)
CHASE_MAX_STEPS = 6                # 最大追价档数
CHASE_ORDER_TIMEOUT = 120.0        # 整笔超时上限 (秒)

# 每档价格 = mark ± frac × offset。负值=被动(赚 maker), 正值=朝市价推进,
# 最后一档 2.0 倍偏移确保穿越盘口。
_CHASE_FRACS = (-1.0, -0.5, 0.0, 0.5, 1.0, 2.0)


def _new_client_order_id(prefix: str = "btc") -> str:
    """Binance clientOrderId ≤ 36 chars."""
    import os
    return f"{prefix}{int(time.time() * 1000) % 10_000_000_000_000}{os.getpid() % 1000:03d}"


class BinanceClientError(RuntimeError):
    pass


class BinanceAuthError(BinanceClientError):
    pass


class BinanceTestnetClient:
    """最小可用的异步 USDM Futures Testnet 客户端."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        symbol: str = TRADING_SYMBOL,
        timeout_sec: float = 20.0,
    ) -> None:
        if aiohttp is None:
            raise BinanceClientError("需要 aiohttp")
        self.api_key = (api_key or get_secret("binance_testnet_api_key") or "").strip()
        self.api_secret = (
            api_secret or get_secret("binance_testnet_api_secret") or ""
        ).strip()
        self.base_url = (
            base_url
            or get_secret("binance_testnet_base_url")
            or BINANCE_TESTNET_DEFAULT_BASE
        ).rstrip("/")
        validate_exchange_target(self.base_url)
        self.symbol = symbol
        self.timeout_sec = timeout_sec
        self._session: Optional[Any] = None
        self._exchange_info: Optional[Dict[str, Any]] = None
        self._time_offset_ms: int = 0
        self._time_synced: bool = False
        self._hedge_mode: Optional[bool] = None  # True=双向持仓

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_sec)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _sign(self, params: Dict[str, Any]) -> str:
        qs = urlencode(params, doseq=True)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def sync_time(self, force: bool = False) -> int:
        """校准本地时钟与交易所服务器的偏移."""
        if self._time_synced and not force:
            return self._time_offset_ms
        data = await self._request("GET", "/fapi/v1/time", signed=False, _skip_time_sync=True)
        server = int(data.get("serverTime") or 0)
        local = int(time.time() * 1000)
        self._time_offset_ms = server - local
        self._time_synced = True
        logger.info("binance time offset=%dms", self._time_offset_ms)
        return self._time_offset_ms

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        _skip_time_sync: bool = False,
        _retried: bool = False,
    ) -> Any:
        if signed and not self.configured:
            raise BinanceAuthError("未配置 Binance Testnet API key/secret")
        if signed and not _skip_time_sync and not self._time_synced:
            await self.sync_time()
        session = await self._ensure_session()
        params = dict(params or {})
        headers: Dict[str, str] = {}
        if signed:
            params["timestamp"] = self._timestamp()
            params.setdefault("recvWindow", 10000)
            params["signature"] = self._sign(params)
            headers["X-MBX-APIKEY"] = self.api_key
        elif self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        url = f"{self.base_url}{path}"
        try:
            async with session.request(
                method, url, params=params, headers=headers
            ) as resp:
                text = await resp.text()
                if resp.status == 401 or resp.status == 418:
                    raise BinanceAuthError(
                        f"鉴权失败 ({mask_secret(self.api_key)}): {text[:200]}"
                    )
                # 时钟偏差 → 强制重同步再试一次
                if (
                    signed
                    and not _retried
                    and resp.status >= 400
                    and ("-1021" in text or "Timestamp" in text)
                ):
                    self._time_synced = False
                    await self.sync_time(force=True)
                    return await self._request(
                        method, path, params={k: v for k, v in (params or {}).items()
                                              if k not in ("timestamp", "signature")},
                        signed=True,
                        _retried=True,
                    )
                if resp.status >= 400:
                    raise BinanceClientError(
                        f"Binance {resp.status} {path}: {text[:300]}"
                    )
                if not text:
                    return {}
                return __import__("json").loads(text)
        except aiohttp.ClientError as exc:
            raise BinanceClientError(f"连接 Binance 失败: {exc}") from exc

    # ------------------------------------------------------------------ public
    async def ping(self) -> bool:
        await self._request("GET", "/fapi/v1/ping")
        return True

    async def server_time(self) -> int:
        data = await self._request("GET", "/fapi/v1/time")
        return int(data.get("serverTime") or 0)

    async def exchange_info(self, force: bool = False) -> Dict[str, Any]:
        if self._exchange_info is None or force:
            self._exchange_info = await self._request("GET", "/fapi/v1/exchangeInfo")
        return self._exchange_info or {}

    async def mark_price(self, symbol: Optional[str] = None) -> float:
        data = await self._request(
            "GET", "/fapi/v1/premiumIndex", {"symbol": symbol or self.symbol}
        )
        return float(data.get("markPrice") or 0)

    async def set_leverage(self, leverage: int, symbol: Optional[str] = None) -> dict:
        return await self._request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol or self.symbol, "leverage": int(leverage)},
            signed=True,
        )

    async def set_margin_type_isolated(self, symbol: Optional[str] = None) -> None:
        """尽量切到逐仓; 已是 ISOLATED 时忽略错误."""
        try:
            await self._request(
                "POST",
                "/fapi/v1/marginType",
                {"symbol": symbol or self.symbol, "marginType": "ISOLATED"},
                signed=True,
            )
        except BinanceClientError as exc:
            if "No need to change" in str(exc) or "-4046" in str(exc):
                return
            logger.warning("set_margin_type: %s", exc)

    async def get_position_mode(self) -> bool:
        """返回是否双向持仓 (hedge)."""
        if self._hedge_mode is not None:
            return self._hedge_mode
        data = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        self._hedge_mode = bool(data.get("dualSidePosition"))
        return self._hedge_mode

    async def set_one_way_mode(self) -> None:
        """尽量切到单向持仓, 失败则保留现状并记录."""
        try:
            await self._request(
                "POST",
                "/fapi/v1/positionSide/dual",
                {"dualSidePosition": "false"},
                signed=True,
            )
            self._hedge_mode = False
        except BinanceClientError as exc:
            msg = str(exc)
            if "No need to change" in msg or "-4059" in msg:
                self._hedge_mode = False
                return
            # 有仓时无法切换 — 探测当前模式
            logger.warning("set_one_way_mode: %s", exc)
            try:
                await self.get_position_mode()
            except Exception:
                self._hedge_mode = True

    async def get_balance(self) -> AccountBalance:
        data = await self._request("GET", "/fapi/v2/balance", signed=True)
        bal = AccountBalance()
        for item in data or []:
            if item.get("asset") == "USDT":
                bal.asset = "USDT"
                bal.total_wallet_balance = float(item.get("balance") or 0)
                bal.available_balance = float(
                    item.get("availableBalance") or item.get("balance") or 0
                )
                bal.total_unrealized_pnl = float(
                    item.get("crossUnPnl") or 0
                )
                break
        return bal

    async def get_position(self, symbol: Optional[str] = None) -> PositionInfo:
        symbol = symbol or self.symbol
        data = await self._request(
            "GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True
        )
        info = PositionInfo(symbol=symbol, side="FLAT")
        net = 0.0
        entry_num = 0.0
        entry_den = 0.0
        upnl = 0.0
        mark = 0.0
        lev = 1
        for item in data or []:
            if item.get("symbol") != symbol:
                continue
            amt = float(item.get("positionAmt") or 0)
            if abs(amt) < 1e-12:
                continue
            net += amt
            upnl += float(item.get("unRealizedProfit") or 0)
            mark = float(item.get("markPrice") or mark)
            lev = int(float(item.get("leverage") or lev))
            ep = float(item.get("entryPrice") or 0)
            entry_num += ep * abs(amt)
            entry_den += abs(amt)
        if abs(net) < 1e-12:
            return info
        info.quantity = abs(net)
        info.side = "LONG" if net > 0 else "SHORT"
        info.entry_price = (entry_num / entry_den) if entry_den else 0.0
        info.unrealized_pnl = upnl
        info.leverage = lev
        info.mark_price = mark
        return info

    async def get_open_orders(self, symbol: Optional[str] = None) -> list:
        return await self._request(
            "GET",
            "/fapi/v1/openOrders",
            {"symbol": symbol or self.symbol},
            signed=True,
        )

    async def query_order(
        self,
        *,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> ManagedOrder:
        """按 clientOrderId 或 orderId 查询 — 超时后必须先查再决定重发."""
        symbol = symbol or self.symbol
        params: Dict[str, Any] = {"symbol": symbol}
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        elif order_id:
            params["orderId"] = order_id
        else:
            return ManagedOrder(
                client_order_id="",
                state=OrderState.REJECTED,
                error="need client_order_id or order_id",
                symbol=symbol,
            )
        try:
            raw = await self._request("GET", "/fapi/v1/order", params, signed=True)
        except BinanceClientError as exc:
            return ManagedOrder(
                client_order_id=client_order_id or "",
                exchange_order_id=str(order_id or ""),
                state=OrderState.UNKNOWN,
                error=str(exc),
                symbol=symbol,
            )
        return self._raw_to_managed(raw, client_order_id=client_order_id or "")

    def _map_binance_status(self, status: str, filled: float, qty: float) -> OrderState:
        s = (status or "").upper()
        if s == "FILLED":
            return OrderState.FILLED
        if s == "PARTIALLY_FILLED":
            return OrderState.PARTIALLY_FILLED
        if s in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"):
            return OrderState.CANCELED
        if s == "REJECTED":
            return OrderState.REJECTED
        if s in ("NEW", "PENDING_NEW"):
            if filled > 0:
                return OrderState.PARTIALLY_FILLED
            return OrderState.ACKNOWLEDGED
        if filled > 0 and qty > 0 and filled + 1e-12 >= qty:
            return OrderState.FILLED
        if filled > 0:
            return OrderState.PARTIALLY_FILLED
        return OrderState.UNKNOWN

    def _raw_to_managed(
        self, raw: Dict[str, Any], *, client_order_id: str = ""
    ) -> ManagedOrder:
        qty = float(raw.get("origQty") or raw.get("quantity") or 0)
        filled = float(raw.get("executedQty") or 0)
        status = str(raw.get("status") or "")
        cid = str(raw.get("clientOrderId") or client_order_id or "")
        return ManagedOrder(
            client_order_id=cid,
            state=self._map_binance_status(status, filled, qty),
            exchange_order_id=str(raw.get("orderId") or raw.get("algoId") or ""),
            algo_id=str(raw.get("algoId") or ""),
            symbol=str(raw.get("symbol") or self.symbol),
            side=str(raw.get("side") or ""),
            position_side=str(raw.get("positionSide") or ""),
            requested_qty=qty,
            submitted_qty=qty,
            quantity=qty,
            cum_filled_qty=filled,
            filled_qty=filled,
            last_fill_qty=filled,
            avg_price=float(raw.get("avgPrice") or 0),
            reduce_only=bool(raw.get("reduceOnly")),
            is_stop=str(raw.get("type") or "").upper().startswith("STOP"),
            is_algo=bool(raw.get("algoId") or raw.get("algoType")),
            stop_price=float(raw.get("stopPrice") or raw.get("triggerPrice") or 0),
            raw=raw if isinstance(raw, dict) else {},
        )

    async def cancel_order(
        self,
        *,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> ManagedOrder:
        symbol = symbol or self.symbol
        params: Dict[str, Any] = {"symbol": symbol}
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        elif order_id:
            params["orderId"] = order_id
        else:
            return ManagedOrder(
                client_order_id="", state=OrderState.REJECTED, error="need id", symbol=symbol
            )
        try:
            raw = await self._request("DELETE", "/fapi/v1/order", params, signed=True)
            mo = self._raw_to_managed(raw, client_order_id=client_order_id or "")
            if mo.state not in (OrderState.CANCELED, OrderState.FILLED):
                mo.state = OrderState.CANCELED
            return mo
        except BinanceClientError as exc:
            return ManagedOrder(
                client_order_id=client_order_id or "",
                exchange_order_id=str(order_id or ""),
                state=OrderState.UNKNOWN,
                error=str(exc),
                symbol=symbol,
            )

    async def cancel_all_stops(self, symbol: Optional[str] = None) -> int:
        """取消该合约所有 STOP/TAKE_PROFIT 类挂单. 返回取消数量."""
        symbol = symbol or self.symbol
        opens = await self.get_open_orders(symbol)
        n = 0
        for o in opens or []:
            typ = str(o.get("type") or "").upper()
            if "STOP" not in typ and "TAKE_PROFIT" not in typ:
                continue
            await self.cancel_order(order_id=str(o.get("orderId") or ""), symbol=symbol)
            n += 1
        return n

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
        """Algo 条件单 STOP_MARKET — POST /fapi/v1/algoOrder (官方已迁出 /order)."""
        symbol = symbol or self.symbol
        side_u = side.upper()
        order_side = "SELL" if side_u == "LONG" else "BUY"
        cid = client_order_id or _new_client_order_id("sl")
        # 触发价必须按 PRICE_FILTER.tickSize 对齐。
        # 硬编码 round(...,2) 会在 tick=0.10 以外的合约上触发
        # -4014 Price not increased by tick size，导致保护单静默失效。
        tick = await self.price_tick(symbol)
        trigger = self._price_precision(float(stop_price), tick)
        if trigger <= 0:
            return ManagedOrder(
                client_order_id=cid,
                state=OrderState.REJECTED,
                error=f"stop price invalid: {stop_price}",
                symbol=symbol,
                is_stop=True,
                is_algo=True,
            )
        params: Dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": order_side,
            "type": "STOP_MARKET",
            "triggerPrice": trigger,
            "workingType": "MARK_PRICE",
            "clientAlgoId": cid,
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            step = await self._lot_step(symbol)
            qty = self._qty_precision(quantity, step)
            if qty <= 0:
                return ManagedOrder(
                    client_order_id=cid,
                    state=OrderState.REJECTED,
                    error=f"quantity too small: {quantity}",
                    symbol=symbol,
                    is_stop=True,
                    is_algo=True,
                )
            params["quantity"] = qty
            params["reduceOnly"] = "true"
        try:
            hedge = await self.get_position_mode()
        except Exception:
            hedge = bool(self._hedge_mode)
        if hedge:
            params["positionSide"] = side_u
            params.pop("reduceOnly", None)
        try:
            raw = await self._request("POST", "/fapi/v1/algoOrder", params, signed=True)
            mo = self._raw_to_managed(raw, client_order_id=cid)
            mo.is_stop = True
            mo.is_algo = True
            mo.stop_price = float(stop_price)
            mo.position_side = side_u
            mo.algo_id = str(raw.get("algoId") or mo.exchange_order_id)
            mo.raw = dict(raw) if isinstance(raw, dict) else {}
            mo.raw.setdefault("algoType", "CONDITIONAL")
            if mo.state == OrderState.UNKNOWN and not mo.error:
                mo.state = OrderState.ACKNOWLEDGED
            return mo
        except BinanceClientError as exc:
            # 只有明确的参数/权限拒单才是 REJECTED；
            # 网络与超时类保持 UNKNOWN，交由调用方查询确认，避免误判保护单失败。
            text = str(exc)
            is_reject = any(
                code in text
                for code in ("-2015", "-1111", "-1102", "-4014", "-2021", "Invalid")
            )
            return ManagedOrder(
                client_order_id=cid,
                state=OrderState.REJECTED if is_reject else OrderState.UNKNOWN,
                error=text,
                symbol=symbol,
                is_stop=True,
                is_algo=True,
                stop_price=trigger,
                position_side=side_u,
            )

    async def get_open_algo_orders(self, symbol: Optional[str] = None) -> list:
        return await self._request(
            "GET",
            "/fapi/v1/openAlgoOrders",
            {"symbol": symbol or self.symbol},
            signed=True,
        )

    async def cancel_algo_order(
        self,
        *,
        client_algo_id: Optional[str] = None,
        algo_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> ManagedOrder:
        symbol = symbol or self.symbol
        params: Dict[str, Any] = {"symbol": symbol}
        if client_algo_id:
            params["clientAlgoId"] = client_algo_id
        elif algo_id:
            params["algoId"] = algo_id
        else:
            return ManagedOrder(client_order_id="", state=OrderState.REJECTED, error="need id", is_algo=True)
        try:
            raw = await self._request("DELETE", "/fapi/v1/algoOrder", params, signed=True)
            mo = self._raw_to_managed(raw, client_order_id=client_algo_id or "")
            mo.is_algo = True
            mo.is_stop = True
            mo.state = OrderState.CANCELED
            return mo
        except BinanceClientError as exc:
            return ManagedOrder(
                client_order_id=client_algo_id or "",
                state=OrderState.UNKNOWN,
                error=str(exc),
                is_algo=True,
                is_stop=True,
            )

    def _qty_precision(self, qty: float, step: float = 0.001) -> float:
        if step <= 0:
            return round(qty, 3)
        n = int(qty / step)
        return round(n * step, 8)

    def _price_precision(self, price: float, tick: float = 0.1) -> float:
        """把价格对齐到交易所 tick，避免 -4014 Price not increased by tick size."""
        if tick <= 0:
            return round(price, 8)
        return round(round(price / tick) * tick, 8)

    async def _lot_step(self, symbol: Optional[str] = None) -> float:
        info = await self.exchange_info()
        sym = symbol or self.symbol
        for s in info.get("symbols") or []:
            if s.get("symbol") != sym:
                continue
            for f in s.get("filters") or []:
                if f.get("filterType") == "LOT_SIZE":
                    return float(f.get("stepSize") or 0.001)
        return 0.001

    async def price_tick(self, symbol: Optional[str] = None) -> float:
        """读取 PRICE_FILTER.tickSize；失败时退回 0.1."""
        try:
            info = await self.exchange_info()
        except BinanceClientError:
            return 0.1
        sym = symbol or self.symbol
        for s in info.get("symbols") or []:
            if s.get("symbol") != sym:
                continue
            for f in s.get("filters") or []:
                if f.get("filterType") == "PRICE_FILTER":
                    try:
                        tick = float(f.get("tickSize") or 0.1)
                    except (TypeError, ValueError):
                        tick = 0.1
                    return tick if tick > 0 else 0.1
        return 0.1

    async def market_open(
        self,
        side: str,
        quantity: float,
        symbol: Optional[str] = None,
        reduce_only: bool = False,
        *,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """side: LONG / SHORT → BUY / SELL. 自动适配单向/双向持仓."""
        symbol = symbol or self.symbol
        step = await self._lot_step(symbol)
        qty = self._qty_precision(quantity, step)
        if qty <= 0:
            return OrderResult(ok=False, error=f"quantity too small: {quantity}")
        side_u = side.upper()
        order_side = "BUY" if side_u == "LONG" else "SELL"
        cid = client_order_id or _new_client_order_id("mkt")
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": order_side,
            "type": "MARKET",
            "quantity": qty,
            "newClientOrderId": cid,
        }
        hedge = False
        try:
            hedge = await self.get_position_mode()
        except Exception:
            hedge = bool(self._hedge_mode)
        if hedge:
            params["positionSide"] = side_u if not reduce_only else (
                "LONG" if side_u == "SHORT" else "SHORT"
            )
            if reduce_only:
                params["positionSide"] = "LONG" if order_side == "SELL" else "SHORT"
        else:
            if reduce_only:
                params["reduceOnly"] = "true"
        try:
            raw = await self._request("POST", "/fapi/v1/order", params, signed=True)
            avg = float(raw.get("avgPrice") or 0)
            qty_filled = float(raw.get("executedQty") or 0)
            status = str(raw.get("status") or "")
            # 市价单偶发返回 NEW + avg=0 → 用订单查询确认, 不用总仓位冒充成交量
            if avg <= 0 or qty_filled <= 0 or status.upper() in ("NEW", "PENDING_NEW"):
                await asyncio.sleep(0.2)
                mo = await self.query_order(client_order_id=cid, symbol=symbol)
                if mo.filled_qty > 0:
                    qty_filled = mo.filled_qty
                if mo.avg_price > 0:
                    avg = mo.avg_price
                if mo.state == OrderState.FILLED:
                    status = "FILLED"
                elif mo.state == OrderState.PARTIALLY_FILLED:
                    status = "PARTIALLY_FILLED"
            mapped = self._map_binance_status(status, qty_filled, qty)
            # NEW/查询后仍无成交 → 未决，不得用请求量冒充，不得 ok=True
            if mapped in (OrderState.UNKNOWN, OrderState.ACKNOWLEDGED, OrderState.SUBMITTED) and qty_filled <= 0:
                return OrderResult(
                    ok=False,
                    order_id=str(raw.get("orderId") or ""),
                    symbol=symbol,
                    side=order_side,
                    position_side=side_u,
                    quantity=0.0,
                    requested_qty=quantity,
                    submitted_qty=qty,
                    cum_filled_qty=0.0,
                    avg_price=0.0,
                    status=status or mapped.value,
                    raw=raw if isinstance(raw, dict) else {},
                    client_order_id=cid,
                    order_state=OrderState.UNKNOWN.value,
                    error="acknowledged_unfilled",
                )
            return OrderResult(
                ok=qty_filled > 0 and mapped in (OrderState.FILLED, OrderState.PARTIALLY_FILLED),
                order_id=str(raw.get("orderId") or ""),
                symbol=symbol,
                side=order_side,
                position_side=side_u,
                quantity=qty_filled,
                requested_qty=quantity,
                submitted_qty=qty,
                cum_filled_qty=qty_filled,
                last_fill_qty=qty_filled,
                avg_price=avg,
                status=status,
                raw=raw if isinstance(raw, dict) else {},
                client_order_id=cid,
                order_state=mapped.value,
            )
        except (BinanceClientError, asyncio.TimeoutError, OSError) as exc:
            # 超时/断线: 先查 clientOrderId；查询失败保持 UNKNOWN，不标 REJECTED
            mo = await self.query_order(client_order_id=cid, symbol=symbol)
            filled = mo.cum_filled_qty or mo.filled_qty
            if mo.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED) or filled > 0:
                return mo.to_order_result()
            # 持仓模式不匹配（-4061）→ 翻转模式后重试。
            # 必须放在"已提交未决"返回之前，否则该重试分支永远不可达。
            if "-4061" in str(exc) and not getattr(self, "_mode_flipped", False):
                self._mode_flipped = True  # type: ignore[attr-defined]
                self._hedge_mode = not bool(self._hedge_mode)
                return await self.market_open(
                    side, quantity, symbol, reduce_only, client_order_id=cid
                )
            if mo.state in (OrderState.ACKNOWLEDGED, OrderState.SUBMITTED, OrderState.UNKNOWN) or mo.error:
                return OrderResult(
                    ok=False,
                    error=f"submitted_unknown: {exc}",
                    side=order_side,
                    symbol=symbol,
                    client_order_id=cid,
                    order_state=OrderState.UNKNOWN.value,
                    order_id=mo.exchange_order_id,
                    requested_qty=quantity,
                    submitted_qty=qty,
                    cum_filled_qty=0.0,
                    quantity=0.0,
                )
            # 仅明确拒单（鉴权/参数）才 REJECTED；网络类保持 UNKNOWN
            is_reject = isinstance(exc, BinanceClientError) and (
                "-2015" in str(exc) or "-1111" in str(exc) or "Invalid" in str(exc)
            )
            return OrderResult(
                ok=False,
                error=str(exc),
                side=order_side,
                symbol=symbol,
                client_order_id=cid,
                order_state=(OrderState.REJECTED if is_reject else OrderState.UNKNOWN).value,
                requested_qty=quantity,
                submitted_qty=qty,
                cum_filled_qty=0.0,
                quantity=0.0,
            )

    async def place_limit_order(
        self,
        side: str,
        quantity: float,
        price: float,
        symbol: Optional[str] = None,
        *,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
        fill_timeout_sec: float = 6.0,
        poll_interval_sec: float = 0.5,
        cancel_if_unfilled: bool = False,
    ) -> OrderResult:
        """限价单：提交后轮询确认成交，绝不把 NEW 当作失败。

        关键规则（修复历史误判）:
          * 刚提交返回 NEW/PENDING_NEW 是正常的，必须轮询而不是直接取消
          * 网络异常时先查询订单；只有确认零成交才允许取消
          * 部分成交后即使被取消，也按实际成交量返回
          * 只有明确拒单（-2013 / -2015 / -1111）才标记 REJECTED
        """
        symbol = symbol or self.symbol
        step = await self._lot_step(symbol)
        qty = self._qty_precision(quantity, step)
        if qty <= 0:
            return OrderResult(
                ok=False, error=f"quantity too small: {quantity}", symbol=symbol
            )
        tick = await self.price_tick(symbol)
        limit_price = self._price_precision(price, tick)
        if limit_price <= 0:
            return OrderResult(
                ok=False, error=f"price invalid: {price}", symbol=symbol
            )
        side_u = side.upper()
        order_side = "BUY" if side_u == "LONG" else "SELL"
        cid = client_order_id or _new_client_order_id("lmt")
        tif = (time_in_force or "GTC").upper()
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "timeInForce": tif,
            "quantity": qty,
            "price": limit_price,
            "newClientOrderId": cid,
        }
        hedge = False
        try:
            hedge = await self.get_position_mode()
        except Exception:
            hedge = bool(self._hedge_mode)
        if hedge:
            params["positionSide"] = (
                ("LONG" if order_side == "SELL" else "SHORT")
                if reduce_only
                else ("LONG" if order_side == "BUY" else "SHORT")
            )
        elif reduce_only:
            params["reduceOnly"] = "true"

        submit_error = ""
        submit_raw: Dict[str, Any] = {}
        try:
            submit_raw = await self._request(
                "POST", "/fapi/v1/order", params, signed=True
            )
        except (BinanceClientError, asyncio.TimeoutError, OSError) as exc:
            submit_error = str(exc)
            if "-4061" in submit_error and not getattr(self, "_mode_flipped", False):
                self._mode_flipped = True  # type: ignore[attr-defined]
                self._hedge_mode = not bool(self._hedge_mode)
                return await self.place_limit_order(
                    side, quantity, price, symbol,
                    time_in_force=time_in_force, reduce_only=reduce_only,
                    client_order_id=cid, fill_timeout_sec=fill_timeout_sec,
                    poll_interval_sec=poll_interval_sec,
                    cancel_if_unfilled=cancel_if_unfilled,
                )
            if "-2013" in submit_error or "Order does not exist" in submit_error:
                return OrderResult(
                    ok=False, error=submit_error, symbol=symbol, side=order_side,
                    client_order_id=cid, order_state=OrderState.REJECTED.value,
                    requested_qty=quantity, submitted_qty=qty,
                    cum_filled_qty=0.0, quantity=0.0,
                )
            # 其余网络类错误：继续走查询路径，不判定失败

        entry = (
            self._raw_to_managed(submit_raw, client_order_id=cid)
            if submit_raw
            else ManagedOrder(
                client_order_id=cid, state=OrderState.UNKNOWN,
                symbol=symbol, side=order_side, raw={},
            )
        )
        if entry.state == OrderState.FILLED:
            return entry.to_order_result()

        deadline = time.time() + max(0.0, fill_timeout_sec)
        last = entry
        while time.time() < deadline:
            await asyncio.sleep(poll_interval_sec)
            last = await self.query_order(client_order_id=cid, symbol=symbol)
            if last.state == OrderState.FILLED:
                return last.to_order_result()
            if last.state in (OrderState.CANCELED, OrderState.REJECTED):
                break
            if last.state == OrderState.UNKNOWN and last.error and not submit_error:
                break  # 查询本身失败：保持 UNKNOWN，不冒充失败

        filled = float(last.cum_filled_qty or last.filled_qty or 0)
        if filled > 0:
            return last.to_order_result()

        if cancel_if_unfilled:
            canceled = await self.cancel_order(client_order_id=cid, symbol=symbol)
            canceled.cum_filled_qty = float(canceled.cum_filled_qty or 0)
            canceled.filled_qty = canceled.cum_filled_qty
            if canceled.state == OrderState.UNKNOWN and not canceled.error:
                canceled.state = OrderState.CANCELED
            return canceled.to_order_result()

        return OrderResult(
            ok=False,
            order_id=last.exchange_order_id or entry.exchange_order_id,
            symbol=symbol,
            side=order_side,
            position_side=side_u,
            quantity=0.0,
            requested_qty=quantity,
            submitted_qty=qty,
            cum_filled_qty=0.0,
            avg_price=0.0,
            status=last.state.value,
            client_order_id=cid,
            order_state=last.state.value,
            error=(
                f"submitted_unknown: {submit_error}"
                if submit_error
                else "unfilled_after_timeout"
            ),
            raw=last.raw if isinstance(last.raw, dict) else {},
        )

    async def place_limit_chase(
        self,
        side: str,
        quantity: float,
        symbol: Optional[str] = None,
        *,
        reduce_only: bool = False,
        mark_price: Optional[float] = None,
        max_steps: Optional[int] = None,
    ) -> OrderResult:
        """被动限价挂单 + 追价循环: 争取 maker 手续费, 同时保证成交.

        价格阶梯 (做多为例, offset = 初始被动偏移, 不小于 1 tick):
            档0  mark - offset      被动, 等价格下来 (maker)
            档1  mark - offset/2
            档2  mark               盘口
            档3  mark + offset/2
            档4  mark + offset
            档5  mark + 2*offset    穿越盘口, 确保成交 (taker)

        做空为对称反向。绝不静默挂单不成交: 追完所有档仍未成交则明确返回失败。

        竞态处理: 每档撤单后必须复核订单状态 —— 撤单与成交可能同时发生,
        撤单返回 -2011 (订单不存在/已成交) 同样按成交复核, 避免漏记成交。
        """
        symbol = symbol or self.symbol
        lot_step = await self._lot_step(symbol)
        qty = self._qty_precision(quantity, lot_step)
        if qty <= 0:
            return OrderResult(
                ok=False, error=f"quantity too small: {quantity}", symbol=symbol
            )
        tick = await self.price_tick(symbol)
        if mark_price is None or mark_price <= 0:
            mark_price = await self.mark_price(symbol)
        if not mark_price or mark_price <= 0:
            return OrderResult(
                ok=False, error="mark price unavailable", symbol=symbol
            )

        offset = max(tick, mark_price * CHASE_INITIAL_OFFSET_BPS / 10000.0)
        is_long = side.upper() == "LONG"
        steps = max(1, min(int(max_steps or CHASE_MAX_STEPS), len(_CHASE_FRACS)))
        deadline = time.time() + CHASE_ORDER_TIMEOUT
        attempts: List[Dict[str, Any]] = []

        for idx in range(steps):
            if time.time() >= deadline:
                break
            frac = _CHASE_FRACS[idx]
            signed = frac if is_long else -frac
            px = self._price_precision(mark_price + signed * offset, tick)
            cid = _new_client_order_id(f"ch{idx}")
            res = await self.place_limit_order(
                side,
                qty,
                px,
                symbol,
                reduce_only=reduce_only,
                client_order_id=cid,
                fill_timeout_sec=CHASE_REPRICE_INTERVAL,
                poll_interval_sec=CHASE_POLL_INTERVAL,
                cancel_if_unfilled=True,
            )
            filled = float(res.cum_filled_qty or 0)
            if filled > 0:
                attempts.append({"step": idx, "price": px, "filled": filled})
                return self._with_chase_meta(res, attempts, offset)

            # 撤单后复核: 撤单与成交可能竞态
            final = await self.query_order(client_order_id=cid, symbol=symbol)
            final_filled = float(final.cum_filled_qty or final.filled_qty or 0)
            if final.state == OrderState.FILLED or final_filled > 0:
                attempts.append({"step": idx, "price": px, "filled": final_filled})
                return self._with_chase_meta(
                    final.to_order_result(), attempts, offset
                )
            attempts.append({"step": idx, "price": px, "filled": 0.0})

        return OrderResult(
            ok=False,
            symbol=symbol,
            side="BUY" if is_long else "SELL",
            position_side=side.upper(),
            quantity=0.0,
            requested_qty=quantity,
            submitted_qty=qty,
            cum_filled_qty=0.0,
            avg_price=0.0,
            status=OrderState.CANCELED.value,
            client_order_id="",
            order_state=OrderState.CANCELED.value,
            error=f"chase_exhausted: {steps} 档追价后仍未成交",
            raw={"chase": {"attempts": attempts, "offset": offset, "steps": steps}},
        )

    @staticmethod
    def _with_chase_meta(
        res: OrderResult, attempts: List[Dict[str, Any]], offset: float
    ) -> OrderResult:
        """把追价过程写入 raw.chase, 供日志统计 maker/taker 与档数."""
        meta = {
            "attempts": attempts,
            "steps_used": len(attempts),
            "offset": offset,
            "final_step": attempts[-1]["step"] if attempts else -1,
            # 档 0 即成交 = 价格主动来找我们 = maker; 否则基本是 taker
            "likely_maker": bool(attempts) and attempts[-1]["step"] == 0,
        }
        raw = res.raw if isinstance(res.raw, dict) else {}
        res.raw = {**raw, "chase": meta}
        return res

    async def all_orders(
        self, symbol: Optional[str] = None, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """历史委托 (GET /fapi/v1/allOrders) —— 币安侧真实记录, 非本地账本."""
        symbol = symbol or self.symbol
        params: Dict[str, Any] = {
            "symbol": symbol,
            "limit": max(1, min(int(limit), 1000)),
        }
        raw = await self._request(
            "GET", "/fapi/v1/allOrders", params, signed=True
        )
        return raw if isinstance(raw, list) else []

    async def user_trades(
        self, symbol: Optional[str] = None, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """历史成交 (GET /fapi/v1/userTrades) —— 含 realizedPnl 与 commission.

        这是「赚了多少」的唯一可信来源: 逐笔已实现盈亏与手续费都取自交易所,
        不由本地账本推算。
        """
        symbol = symbol or self.symbol
        params: Dict[str, Any] = {
            "symbol": symbol,
            "limit": max(1, min(int(limit), 1000)),
        }
        raw = await self._request(
            "GET", "/fapi/v1/userTrades", params, signed=True
        )
        return raw if isinstance(raw, list) else []

    async def market_close(self, symbol: Optional[str] = None) -> OrderResult:
        """平掉当前全部仓位 (单向净仓或双向两边)."""
        symbol = symbol or self.symbol
        hedge = False
        try:
            hedge = await self.get_position_mode()
        except Exception:
            pass

        if hedge:
            # 查两边仓位
            data = await self._request(
                "GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True
            )
            last = OrderResult(ok=True, status="FLAT", error="already flat")
            for item in data or []:
                if item.get("symbol") != symbol:
                    continue
                amt = float(item.get("positionAmt") or 0)
                if abs(amt) < 1e-12:
                    continue
                ps = str(item.get("positionSide") or ("LONG" if amt > 0 else "SHORT"))
                order_side = "SELL" if amt > 0 else "BUY"
                step = await self._lot_step(symbol)
                qty = self._qty_precision(abs(amt), step)
                params = {
                    "symbol": symbol,
                    "side": order_side,
                    "type": "MARKET",
                    "quantity": qty,
                    "positionSide": ps,
                }
                try:
                    raw = await self._request("POST", "/fapi/v1/order", params, signed=True)
                    last = OrderResult(
                        ok=True,
                        order_id=str(raw.get("orderId") or ""),
                        symbol=symbol,
                        side=order_side,
                        position_side=ps,
                        quantity=float(raw.get("executedQty") or qty),
                        avg_price=float(raw.get("avgPrice") or 0),
                        status=str(raw.get("status") or ""),
                        raw=raw if isinstance(raw, dict) else {},
                    )
                except BinanceClientError as exc:
                    return OrderResult(ok=False, error=str(exc))
            return last

        pos = await self.get_position(symbol)
        if pos.side == "FLAT" or pos.quantity <= 0:
            return OrderResult(ok=True, status="FLAT", error="already flat")
        return await self.market_open(
            side=("SHORT" if pos.side == "LONG" else "LONG"),
            quantity=pos.quantity,
            symbol=symbol,
            reduce_only=True,
        )

    async def verify(self) -> Dict[str, Any]:
        """连通性自检."""
        t0 = time.time()
        await self.ping()
        try:
            bal = await self.get_balance()
        except BinanceAuthError as exc:
            hint = str(exc)
            if "-2015" in hint or "Invalid API-key" in hint:
                raise BinanceAuthError(
                    "币安拒绝此密钥 (-2015)。请确认："
                    "1) 密钥来自 testnet.binancefuture.com（不是 binance.com 正式站）；"
                    "2) 已勾选「读取」与「合约交易」权限；"
                    "3) 未开 IP 白名单，或已把本机出口 IP 加进白名单；"
                    "4) 不要把 Predict.fun / 其他平台的 HMAC 密钥填到这里。"
                ) from exc
            raise
        mark = await self.mark_price()
        return {
            "ok": True,
            "base_url": self.base_url,
            "key_masked": mask_secret(self.api_key),
            "latency_ms": int((time.time() - t0) * 1000),
            "available_balance": bal.available_balance,
            "mark_price": mark,
        }
