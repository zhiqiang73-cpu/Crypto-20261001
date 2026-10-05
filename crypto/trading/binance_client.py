"""Binance USDⓈ-M Futures Testnet REST 客户端.

默认 base: https://testnet.binancefuture.com
签名: HMAC-SHA256(query_string, secret)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional
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
# 限价成交参数 (改动 C)
#
# 硬性要求 (2026-10-02 用户指令): 开仓和平仓一律走限价单, 不允许市价。
#
# 交易所规则决定了两个必须先讲清的事实, 整个设计围着它们转:
#   (1) 穿盘口的限价单会立即成交并按 taker 收费 —— 和市价单同价。
#       所以"走限价"本身不省手续费, "挂成 maker"才省。
#   (2) post-only 是唯一能硬保证 maker 的订单类型 (币安期货写法:
#       type=LIMIT + timeInForce=GTX): 若该单会立即成交, 交易所直接拒单
#       (-5022), 永远不会变成 taker。
#
# 因此本实现: 被动阶段一律用 post-only 贴盘口挂单, 成交即必为 maker
# (0.02%/边)。只有窗口耗尽后的兜底那一单才允许穿盘口 (= taker)。
# ---------------------------------------------------------------------------
CHASE_POLL_INTERVAL = 3.0          # 订单状态轮询间隔 (秒)

PASSIVE_REPRICE_SEC = 5.0      # 每个挂单挂多久, 未成交则撤单跟盘口重挂
PASSIVE_WINDOW_SEC = 180.0     # 被动窗口总时长 (秒), 耗尽后走兜底
PASSIVE_MAX_REPRICE = 60       # 最多重挂次数 (防止窗口内空转)
PASSIVE_MAX_PAD = 5            # 被 post-only 拒单后最多退几个 tick
PASSIVE_CROSS_TICKS = 5        # 兜底穿盘口时超出盘口几个 tick (=滑点上限)

# 窗口耗尽后的行为:
#   "cross"   = 穿盘口限价单成交 (仍是限价单, 但按 taker 收费, 与市价等价)
#   "abandon" = 放弃本单, 不成交 (信号作废, 但手续费一分不付)
PASSIVE_ON_TIMEOUT = "cross"


def _new_client_order_id(prefix: str = "btc") -> str:
    """跨进程/同毫秒唯一；为档号预留空间，长度不超过 36。"""
    return f"{prefix[:12]}{uuid.uuid4().hex[:20]}"


def _definite_order_reject(error: str) -> bool:
    """仅交易所明确拒单才能结束委托意图；查询 -2013 仍须单独判未决。"""
    return any(code in error for code in (
        "-2019", "-5022", "-4061", "-2015", "-1111", "-1102",
        "-4014", "-2022", "-4164", "-2013",
    ))


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

    async def book_ticker(self, symbol: Optional[str] = None) -> Dict[str, float]:
        """盘口最优买卖价 (公开接口, 无需签名).

        被动挂单必须基于盘口而不是 mark price: mark 是标记价, 与真实最优
        买卖价差一个点差, 用它算出来的"被动价"可能已经穿过盘口变成 taker。
        """
        data = await self._request(
            "GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol or self.symbol}
        )
        return {"bid": float(data.get("bidPrice") or 0),
                "ask": float(data.get("askPrice") or 0)}

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
                # 0 是有效可用余额，不能通过 `or` 回退到钱包总额。
                available = item.get("availableBalance")
                bal.available_balance = float(available if available is not None else 0)
                bal.total_unrealized_pnl = float(
                    item.get("crossUnPnl") or 0
                )
                break
        return bal

    async def get_account_balance_snapshot(self) -> AccountBalance:
        """账户级总浮盈含逐仓，供扩仓预算；字段缺失时不得回退至钱包余额。"""
        data = await self._request("GET", "/fapi/v2/account", signed=True)
        required = ("totalWalletBalance", "availableBalance", "totalUnrealizedProfit")
        if not isinstance(data, dict) or any(data.get(key) is None for key in required):
            raise BinanceClientError("账户余额快照字段不完整，拒绝扩仓")
        return AccountBalance(total_wallet_balance=float(data["totalWalletBalance"]),
                              available_balance=float(data["availableBalance"]),
                              total_unrealized_pnl=float(data["totalUnrealizedProfit"]))

    async def get_position_risks(self) -> list:
        """只读账户全部合约持仓；组合敞口不能只统计 BTC/ETH。"""
        data = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        if not isinstance(data, list):
            raise BinanceClientError("positionRisk 返回格式异常，拒绝扩仓")
        return data

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
        notional = 0.0
        margin = 0.0
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
            # 交易所自己的口径, 直接透传给面板:
            # notional = positionRisk.notional (名义价值, 按标记价, 与网页端一致)
            # margin   = positionRisk.isolatedMargin (逐仓实际占用保证金)
            notional += float(item.get("notional") or 0)
            margin += float(
                item.get("isolatedMargin")
                or item.get("positionInitialMargin")
                or 0
            )
        if abs(net) < 1e-12:
            return info
        info.quantity = abs(net)
        info.side = "LONG" if net > 0 else "SHORT"
        info.entry_price = (entry_num / entry_den) if entry_den else 0.0
        info.unrealized_pnl = upnl
        info.leverage = lev
        info.mark_price = mark
        info.notional = notional if abs(notional) > 0 else net * mark
        info.margin = margin if margin > 0 else abs(info.notional) / max(lev, 1)
        return info

    async def get_position_settings(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """读取 BTCUSDT 的交易所逐仓/杠杆设置（即使空仓也能返回真实设置）。

        `get_position()` 在空仓时为了简洁返回默认 `leverage=1`，因此不能拿它
        验证策略的「10x 逐仓」要求。本方法直接读取 positionRisk 的原始设置，
        仅做 GET、没有任何账户修改副作用。
        """
        symbol = symbol or self.symbol
        data = await self._request(
            "GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True
        )
        for item in data or []:
            if item.get("symbol") != symbol:
                continue
            isolated_raw = item.get("isolated", False)
            isolated = (isolated_raw is True or str(isolated_raw).lower() == "true")
            return {
                "symbol": symbol,
                "leverage": int(float(item.get("leverage") or 0)),
                "isolated": isolated,
                "margin_type": "ISOLATED" if isolated else "CROSSED",
            }
        return {"symbol": symbol, "leverage": 0, "isolated": False,
                "margin_type": "UNKNOWN"}

    async def get_open_orders(self, symbol: Optional[str] = None) -> list:
        return await self._request(
            "GET",
            "/fapi/v1/openOrders",
            {"symbol": symbol or self.symbol},
            signed=True,
        )

    async def get_all_open_orders(self) -> list:
        """不带 symbol 的全账户挂单；扩仓时不能遗漏其他标的待成交敞口。"""
        data = await self._request("GET", "/fapi/v1/openOrders", signed=True)
        if not isinstance(data, list):
            raise BinanceClientError("全账户挂单快照异常，拒绝扩仓")
        return data

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
            # DELETE 的 HTTP 200 不证明撤单已处于终态。
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
        # 5.260 - 5.259 在二进制浮点可能略小于 0.001；只容忍远小于
        # 交易所步长的表示误差，真实不足一步的数量仍向下取整为 0。
        n = math.floor(qty / step + 1e-10)
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
            # 明确拒单不应被查询 -2013 覆盖，更不能因 -4061 擅自翻转模式重发。
            if isinstance(exc, BinanceClientError) and _definite_order_reject(str(exc)):
                return OrderResult(ok=False, error=str(exc), side=order_side,
                                   symbol=symbol, client_order_id=cid,
                                   order_state=OrderState.REJECTED.value,
                                   requested_qty=quantity, submitted_qty=qty)
            # 超时/断线: 只查同一个 CID；失败保持 UNKNOWN。
            try:
                mo = await self.query_order(client_order_id=cid, symbol=symbol)
            except (BinanceClientError, asyncio.TimeoutError, OSError) as query_exc:
                mo = ManagedOrder(client_order_id=cid, state=OrderState.UNKNOWN,
                                  symbol=symbol, error=str(query_exc))
            filled = mo.cum_filled_qty or mo.filled_qty
            if mo.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED) or filled > 0:
                return mo.to_order_result()
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
            # 网络类始终 UNKNOWN；上方只把交易所明确拒单分类为 REJECTED。
            return OrderResult(
                ok=False,
                error=str(exc),
                side=order_side,
                symbol=symbol,
                client_order_id=cid,
                order_state=OrderState.UNKNOWN.value,
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
        post_only: bool = False,
    ) -> OrderResult:
        """限价单：提交后轮询确认成交，绝不把 NEW 当作失败。

        关键规则（修复历史误判）:
          * 刚提交返回 NEW/PENDING_NEW 是正常的，必须轮询而不是直接取消
          * 网络异常时先查询订单；只有确认零成交才允许取消
          * 部分成交后即使被取消，也按实际成交量返回
          * 只有明确拒单（-2013 / -2015 / -1111）才标记 REJECTED

        post_only=True 时改用 GTX (币安期货的 post-only TIF): 若该单会立即
        成交, 交易所直接拒单 (-5022), 调用方应把价格让开重挂。
        这是唯一能从交易所层面硬保证成交即 maker 的方式。
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
        # post-only 在币安期货上不是订单类型, 而是 TIF:
        #   现货 type=LIMIT_MAKER; 期货 type=LIMIT + timeInForce=GTX
        #   (GTX = Good-Till-Crossing: 只做 maker, 会立即成交则交易所拒单)
        # 用成 LIMIT_MAKER 会得到 -1116 Invalid orderType —— 2026-10-02 实测。
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "quantity": qty,
            "price": limit_price,
            "timeInForce": "GTX" if post_only else tif,
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
            if post_only and (
                # 期货实测拒单码: -5022 "could not be executed as maker"
                # 现货/旧版文案是 "would immediately match", 两个都认。
                "-5022" in submit_error
                or "could not be executed as maker" in submit_error
                or "would immediately match" in submit_error
                or "IMMEDIATELY_MATCH" in submit_error
            ):
                # 挂单价格已经穿过盘口: 不是错误, 是"让开一档"的信号
                return OrderResult(
                    ok=False, error=f"POST_ONLY_REJECT: {submit_error[:120]}",
                    symbol=symbol, side=order_side, position_side=side_u,
                    client_order_id=cid, order_state=OrderState.REJECTED.value,
                    requested_qty=quantity, submitted_qty=0.0,
                    cum_filled_qty=0.0, quantity=0.0,
                )
            if _definite_order_reject(submit_error):
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
        known_filled = float(entry.cum_filled_qty or 0)
        known_avg = float(entry.avg_price or 0)
        while time.time() < deadline:
            await asyncio.sleep(poll_interval_sec)
            last = await self.query_order(client_order_id=cid, symbol=symbol)
            known_filled = max(known_filled, float(last.cum_filled_qty or last.filled_qty or 0))
            if last.avg_price > 0:
                known_avg = last.avg_price
            if last.state == OrderState.FILLED:
                return last.to_order_result()
            if last.state in (OrderState.CANCELED, OrderState.REJECTED):
                break
            if last.state == OrderState.UNKNOWN and last.error and not submit_error:
                break  # 查询本身失败：保持 UNKNOWN，不冒充失败

        filled = known_filled

        # 只要不是「完全成交」，就必须把剩余挂单撤掉。
        #
        # 2026-10-02 21:46 事故根因（务必保留这段说明）:
        # 这里原本是 `if filled > 0: return last.to_order_result()` —— 只要
        # 有一点部分成交就直接返回、**不撤单**，未成交的剩余量继续以 GTC
        # post-only 挂在盘口。运行器以为这张单已经结束，下一步的净仓同步
        # 又下了一张补差单，两张单在同一秒全部成交：
        #   目标 5.070 → 实际 0.130（部分成交）→ 补差单 4.940
        #   → 原单剩余 4.940 也成交 → 持仓 10.010（正好翻倍）
        # 一分钟后被迫反向卖出 4.939 纠正，白付一次买卖价差与手续费。
        if cancel_if_unfilled and last.state not in (
            OrderState.REJECTED,
        ):
            canceled = await self.cancel_order(client_order_id=cid, symbol=symbol)
            # 撤单回执可能不带成交量（-2011 等），此时用轮询到的值兜底，
            # 绝不把已经成交的部分当成 0。
            cum = max(float(canceled.cum_filled_qty or 0), filled)
            canceled.cum_filled_qty = cum
            canceled.filled_qty = cum
            if not canceled.avg_price:
                canceled.avg_price = known_avg
            result = canceled.to_order_result()
            if cum > 0 and canceled.state in (OrderState.CANCELED, OrderState.FILLED):
                # 有真实成交就是「成功的一部分」，与改动前
                # （部分成交直接返回、ok=True）保持一致的语义；
                # order_state 仍如实记为 CANCELED，不粉饰订单真实状态。
                result.ok = True
            return result

        if filled > 0:
            last.cum_filled_qty = last.filled_qty = filled
            if not last.avg_price:
                last.avg_price = known_avg
            result = last.to_order_result()
            if last.state == OrderState.UNKNOWN:
                result.ok = False
            return result

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
        window_sec: Optional[float] = None,
        tag: str = "ps",
        force_cross: bool = False,
        client_order_base: Optional[str] = None,
        before_submit: Optional[Callable[[str], None]] = None,
        before_order: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> OrderResult:
        """被动限价挂单 (post-only) + 跟盘口重挂, 超时兜底.

        订单类型: 被动阶段一律 post-only (type=LIMIT + timeInForce=GTX)。
        该模式下交易所会直接拒掉任何会立即成交的单 (-5022), 所以只要成交,
        手续费必定是 maker。这不是约定, 是规则。

        挂价 (基于真实盘口, 不是 mark price):
            做多: min(best_bid - pad*tick, best_ask - tick)   —— 绝不碰 best_ask
            做空: max(best_ask + pad*tick, best_bid + tick)   —— 绝不碰 best_bid

        跟随逻辑: 每 PASSIVE_REPRICE_SEC 秒撤单重读盘口重挂。
            市场朝我有利走 → 挂价跟着走 → 一路贴住最优价
            市场朝我不利走 → 挂价也跟着走 → 始终留在盘口第一档等对手方

        兜底: 窗口 PASSIVE_WINDOW_SEC 用尽仍未成交, 按 PASSIVE_ON_TIMEOUT 处理:
            "cross"   → 穿盘口限价单成交 (taker, 与市价等价, 但有滑点上限)
            "abandon" → 放弃本单, 明确返回失败

        竞态处理: 每档撤单后必须复核订单状态 —— 撤单与成交可能同时发生,
        撤单返回 -2011 (订单不存在/已成交) 同样按成交复核, 避免漏记成交。

        tag: clientOrderId 前缀, 用于事后区分下单来源 (见 review/order_sources.py)。
            策略运行器用 "kdj", 面板冒烟测试用 "smk", 用户在面板主动平仓用 "usr"。
            没有它, 账户里「策略单」和「功能测试单」就无法可靠分开 —— 2026-10-02
            的 10:22 事故正是这样发生的。
        force_cross: 仅限灾难止损。跳过 post-only 等待，直接提交带滑点上限的
            穿盘口 LIMIT 单。仍然**不是 MARKET 单**；普通开仓、反手、用户平仓
            一律保持 180 秒 post-only 追价逻辑。
        """
        symbol = symbol or self.symbol
        lot_step = await self._lot_step(symbol)
        qty = self._qty_precision(quantity, lot_step)
        if qty <= 0:
            return OrderResult(
                ok=False, error=f"quantity too small: {quantity}", symbol=symbol
            )
        # 追价会多次重挂，提示音只在本轮委托开始时响一次。
        from utils.alert_sound import play_alert
        play_alert("order")
        tick = await self.price_tick(symbol)
        is_long = side.upper() == "LONG"
        deadline = time.time() if force_cross else time.time() + (
            window_sec or PASSIVE_WINDOW_SEC
        )
        attempts: List[Dict[str, Any]] = []
        pad = 0
        base = client_order_base or _new_client_order_id(tag)
        if len(base) > 32:
            raise ValueError("追价订单基准 clientOrderId 过长")

        while time.time() < deadline and len(attempts) < PASSIVE_MAX_REPRICE:
            book = await self.book_ticker(symbol)
            bid, ask = book["bid"], book["ask"]
            if bid <= 0 or ask <= 0:
                return OrderResult(
                    ok=False, error="book ticker unavailable", symbol=symbol
                )
            if is_long:
                px = min(bid - pad * tick, ask - tick)
            else:
                px = max(ask + pad * tick, bid + tick)
            px = self._price_precision(px, tick)

            idx = len(attempts)
            if before_order:
                await before_order(book)  # 盘口/权益/账户仓位随追价变化，逐单复核
            cid = f"{base}p{idx}"
            if before_submit:
                before_submit(cid)  # 先持久化订单身份，再发送 POST
            res = await self.place_limit_order(
                side,
                qty,
                px,
                symbol,
                reduce_only=reduce_only,
                client_order_id=cid,
                fill_timeout_sec=PASSIVE_REPRICE_SEC,
                poll_interval_sec=CHASE_POLL_INTERVAL,
                cancel_if_unfilled=True,
                post_only=True,
            )
            filled = float(res.cum_filled_qty or 0)
            # 未决（即使部分成交）或明确风控拒单绝不继续下一档/IOC。
            if res.order_state not in (OrderState.FILLED.value,
                                       OrderState.CANCELED.value,
                                       OrderState.REJECTED.value):
                return self._with_passive_meta(res, attempts, maker=True)
            if res.order_state == OrderState.REJECTED.value and "POST_ONLY_REJECT" not in res.error:
                return self._with_passive_meta(res, attempts, maker=True)
            if filled > 0:
                attempts.append({"step": idx, "price": px, "filled": filled,
                                 "bid": bid, "ask": ask, "maker": True})
                return self._finish_passive(res, attempts, maker=True,
                                            reduce_only=reduce_only)

            if "POST_ONLY_REJECT" in res.error:
                # 明确的交易所 -5022 根本没有建立订单；只允许此路径退档重试。
                pad = min(pad + 1, PASSIVE_MAX_PAD)
                attempts.append({"step": idx, "price": px, "filled": 0.0,
                                 "bid": bid, "ask": ask, "reject": "post_only"})
                continue

            # 撤单后复核: 撤单与成交可能竞态
            final = await self.query_order(client_order_id=cid, symbol=symbol)
            final_filled = float(final.cum_filled_qty or final.filled_qty or 0)
            if final.state in (OrderState.UNKNOWN, OrderState.ACKNOWLEDGED,
                               OrderState.PARTIALLY_FILLED):
                unresolved = final.to_order_result()
                unresolved.ok = False
                unresolved.order_state = OrderState.UNKNOWN.value
                unresolved.error = f"post_cancel_unresolved: {final.error or final.state.value}"
                return self._with_passive_meta(unresolved, attempts, maker=True)
            if final.state == OrderState.FILLED or final_filled > 0:
                attempts.append({"step": idx, "price": px, "filled": final_filled,
                                 "bid": bid, "ask": ask, "maker": True})
                return self._finish_passive(
                    final.to_order_result(), attempts, maker=True,
                    reduce_only=reduce_only,
                )

            pad = 0
            attempts.append({"step": idx, "price": px, "filled": 0.0,
                             "bid": bid, "ask": ask})

        # ---- 兜底 --------------------------------------------------------
        if PASSIVE_ON_TIMEOUT == "abandon":
            return OrderResult(
                ok=False, symbol=symbol,
                side="BUY" if is_long else "SELL", position_side=side.upper(),
                quantity=0.0, requested_qty=quantity, submitted_qty=qty,
                cum_filled_qty=0.0, avg_price=0.0,
                status=OrderState.CANCELED.value, client_order_id="",
                order_state=OrderState.CANCELED.value,
                error=f"passive_abandoned: {len(attempts)} 次被动挂单未成交, "
                      f"按配置放弃 (未付任何手续费)",
                raw={"chase": {"attempts": attempts, "likely_maker": False}},
            )

        book = await self.book_ticker(symbol)
        bid, ask = book["bid"], book["ask"]
        cross_px = self._price_precision(
            (ask + PASSIVE_CROSS_TICKS * tick) if is_long
            else (bid - PASSIVE_CROSS_TICKS * tick), tick
        )
        if before_order:
            await before_order(book)
        cid = f"{base}x"
        if before_submit:
            before_submit(cid)
        res = await self.place_limit_order(
            side, qty, cross_px, symbol,
            # IOC 仍然是 LIMIT：立即成交可成交部分，余量由交易所取消。
            # 不留下 GTC 挂单，避免运行器认为失败后该单又延迟成交。
            time_in_force="IOC",
            reduce_only=reduce_only, client_order_id=cid,
            fill_timeout_sec=10.0, poll_interval_sec=CHASE_POLL_INTERVAL,
            cancel_if_unfilled=False,
        )
        filled = float(res.cum_filled_qty or 0)
        if filled > 0:
            res.ok = res.order_state in (OrderState.FILLED.value, OrderState.CANCELED.value)
            # 未知撤单/查询不等于已完成，即使之前查询见到部分成交。
            attempts.append({"step": len(attempts), "price": cross_px,
                             "filled": filled, "bid": bid, "ask": ask,
                             "maker": False, "cross": True})
            if res.ok:
                return self._finish_passive(res, attempts, maker=False,
                                            reduce_only=reduce_only)
            return self._with_passive_meta(res, attempts, maker=False)
        # 保留交易所实际订单状态/ID；网络未知不能伪称已经撤销。
        res.error = (f"passive_exhausted: 被动 {len(attempts)} 次 + IOC限价兜底未确认成交; "
                     f"{res.error or res.order_state}")
        # 复用同一套 chase meta 键名；2026-10-03 手写 {"attempts","likely_maker"} 导致
        # 日志把 steps/passive 读成 0（「taker 被动0次」），真实原因被挡住。
        return self._with_passive_meta(res, attempts, maker=False)

    @staticmethod
    def _finish_passive(
        res: OrderResult, attempts: List[Dict[str, Any]], *, maker: bool,
        reduce_only: bool,
    ) -> OrderResult:
        out = BinanceTestnetClient._with_passive_meta(res, attempts, maker=maker)
        if (not reduce_only) and float(out.cum_filled_qty or 0) > 0:
            from utils.alert_sound import play_alert
            play_alert("open")
        return out

    @staticmethod
    def _with_passive_meta(
        res: OrderResult, attempts: List[Dict[str, Any]], *, maker: bool
    ) -> OrderResult:
        """把挂单过程写入 raw.chase, 供日志统计 maker/taker 与档数.

        maker 不再是推断: 被动阶段走 post-only, 成交必为 maker;
        兜底阶段穿盘口, 必为 taker。
        """
        meta = {
            "attempts": attempts,
            "steps_used": len(attempts),
            "final_step": attempts[-1]["step"] if attempts else -1,
            "likely_maker": bool(maker),
            "passive_attempts": sum(
                1 for a in attempts if a.get("maker") or a.get("reject")
            ),
        }
        raw = res.raw if isinstance(res.raw, dict) else {}
        res.raw = {**raw, "chase": meta}
        return res

    async def all_orders(
        self,
        symbol: Optional[str] = None,
        *,
        limit: int = 50,
        start_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """历史委托 (GET /fapi/v1/allOrders) —— 币安侧真实记录, 非本地账本.

        start_time 为毫秒时间戳, 用于只取统计起点之后的委托。
        """
        symbol = symbol or self.symbol
        params: Dict[str, Any] = {
            "symbol": symbol,
            "limit": max(1, min(int(limit), 1000)),
        }
        if start_time:
            params["startTime"] = int(start_time)
        raw = await self._request(
            "GET", "/fapi/v1/allOrders", params, signed=True
        )
        return raw if isinstance(raw, list) else []

    async def user_trades(
        self,
        symbol: Optional[str] = None,
        *,
        limit: int = 50,
        start_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """历史成交 (GET /fapi/v1/userTrades) —— 含 realizedPnl 与 commission.

        这是「赚了多少」的唯一可信来源: 逐笔已实现盈亏与手续费都取自交易所,
        不由本地账本推算。

        start_time 为毫秒时间戳, 用于只取统计起点之后的成交。
        """
        symbol = symbol or self.symbol
        params: Dict[str, Any] = {
            "symbol": symbol,
            "limit": max(1, min(int(limit), 1000)),
        }
        if start_time:
            params["startTime"] = int(start_time)
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
