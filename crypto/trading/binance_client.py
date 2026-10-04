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

PASSIVE_REPRICE_SEC = 5.0      # 被 post-only 拒单后的最短重挂间隔
PASSIVE_WINDOW_SEC = 180.0     # 被动窗口总时长 (秒), 耗尽后走兜底
# 2026-10-04 改造: 盘口不变时**不再**撤单重挂, 而是让挂单一直躺在队里。
#
# 交易所按「价格优先、时间优先」撮合, 撤单重挂 = 放弃排队位置、排回队尾。
# 实测 2026-10-04 17:00 那单: 目标 0.0980 BTC, 在 180 秒里撤挂 28 次
# (每 6 秒一次), 只有 6% 的量拿到 maker, 其余 94% 落到 taker 兜底;
# 而 15:45 那单只重挂 6 次就整笔 maker 成交。差别就在挂单有没有时间攒排队位置。
#
# 现在: 价格一动立刻撤单跟盘口, 价格不动就一直挂着 —— 既不失去跟随性,
# 也不再每 6 秒白扔一次排队位置。
PASSIVE_REST_POLL_SEC = 3.0    # 驻留期间的轮询间隔 (订单状态 + 盘口)
PASSIVE_MAX_REPRICE = 60       # 最多重挂次数 (防止窗口内空转)
PASSIVE_MAX_PAD = 5            # 被 post-only 拒单后最多退几个 tick
PASSIVE_CROSS_TICKS = 5        # 兜底穿盘口时超出盘口几个 tick (=滑点上限)

# 窗口耗尽后的行为:
#   "cross"   = 穿盘口限价单成交 (仍是限价单, 但按 taker 收费, 与市价等价)
#   "abandon" = 放弃本单, 不成交 (信号作废, 但手续费一分不付)
PASSIVE_ON_TIMEOUT = "cross"


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
        self._position_risk_cache: Optional[List[Dict[str, Any]]] = None
        self._position_risk_cache_ms: int = 0
        self._balance_cache: Optional[AccountBalance] = None
        self._balance_cache_ms: int = 0
        self._account_cache_ttl_ms: int = 5_000
        self._rate_limit_until: float = 0.0
        self._rate_limit_backoff: float = 5.0

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
        wait = self._rate_limit_until - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
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
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        delay = max(float(retry_after or 0), self._rate_limit_backoff)
                    except (TypeError, ValueError):
                        delay = self._rate_limit_backoff
                    self._rate_limit_until = time.monotonic() + min(delay, 60.0)
                    self._rate_limit_backoff = min(self._rate_limit_backoff * 2.0, 60.0)
                    raise BinanceClientError(
                        f"Binance 429 {path}: 请求被限流，退避 {delay:.1f}s"
                    )
                if resp.status >= 400:
                    raise BinanceClientError(
                        f"Binance {resp.status} {path}: {text[:300]}"
                    )
                if not text:
                    return {}
                self._rate_limit_backoff = 5.0
                return __import__("json").loads(text)
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:
            # 2026-10-04: aiohttp 的 ClientTimeout 抛的是 asyncio.TimeoutError，
            # 它**不是** ClientError，此前直接逃逸出 _request。追价一个 tick 要发
            # 上百次请求，任一次超时就会冒泡到 step()，把整个 tick（含另一标的）打断。
            raise BinanceClientError(
                f"连接 Binance 失败: {type(exc).__name__}: {exc}"
            ) from exc

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
        now_ms = int(time.time() * 1000)
        if (self._balance_cache is not None
                and now_ms - self._balance_cache_ms < self._account_cache_ttl_ms):
            return self._balance_cache
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
        # 2026-10-04: 本系统跑 10× **逐仓**，而 /fapi/v2/balance 的 crossUnPnl 只统计
        # 全仓未实现盈亏 —— 逐仓账户上恒为 0。于是「未实现盈亏」在面板与权益口径里
        # 一直是 0，持仓浮亏完全不可见（当晚 ETH 浮亏 -33 时读数仍是 0；权益也因此
        # 少算了浮盈浮亏，直接影响回撤闸门与按权益的仓位定量）。
        #
        # ⚠ 字段名是币安的 **unRealizedProfit**（大写 R，历史拼法）——不是
        #   unrealizedProfit。写错只会静默拿到 0，与整个 bug 的表现一模一样。
        #   两种拼法都接受，避免再踩。
        if not bal.total_unrealized_pnl:
            try:
                risk = await self.get_position_risk_snapshot()
                bal.total_unrealized_pnl = sum(
                    float(r.get("unRealizedProfit")
                          or r.get("unrealizedProfit") or 0)
                    for r in (risk or [])
                )
            except Exception as exc:  # noqa: BLE001  取不到就保留原值
                logger.warning("读取逐仓未实现盈亏失败: %s", exc)
        self._balance_cache = bal
        self._balance_cache_ms = now_ms
        return bal

    async def get_position_risk_snapshot(self, force: bool = False) -> List[Dict[str, Any]]:
        """一次读取全标的逐仓风险快照，供 BTC/ETH 共用。"""
        now_ms = int(time.time() * 1000)
        if (not force and self._position_risk_cache is not None
                and now_ms - self._position_risk_cache_ms < self._account_cache_ttl_ms):
            return self._position_risk_cache
        data = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        self._position_risk_cache = list(data or [])
        self._position_risk_cache_ms = now_ms
        return self._position_risk_cache

    async def get_position(self, symbol: Optional[str] = None) -> PositionInfo:
        symbol = symbol or self.symbol
        data = await self.get_position_risk_snapshot()
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
        # 均价回填（2026-10-04 修复）。
        # 币安只在订单**完全成交**时才填 avgPrice；部分成交（含被撤单的被动
        # 挂单）avgPrice 恒为 0。此前直接取 avgPrice，导致台账里出现
        # 「filled=0.0048 / avg_price=0.00」——成交量和成交价对不上，滑点与
        # 成本都没法算。成交额字段是可靠的：USDⓈ-M 用 cumQuote，现货用
        # cummulativeQuoteQty，两者都取。
        avg_price = float(raw.get("avgPrice") or 0)
        if avg_price <= 0 and filled > 0:
            quote = float(raw.get("cumQuote") or raw.get("cummulativeQuoteQty") or 0)
            if quote > 0:
                avg_price = quote / filled
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
            avg_price=avg_price,
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
        order_type: str = "STOP_MARKET",
    ) -> ManagedOrder:
        """Algo 条件单 — POST /fapi/v1/algoOrder (官方已迁出 /order).

        order_type: STOP_MARKET（止损）或 TAKE_PROFIT_MARKET（止盈）。
            两者的方向相同（多仓都是 SELL），**只有 orderType 能区分** ——
            这也是查询侧必须按 orderType 过滤的原因。
        """
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
            "type": order_type,
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

    async def min_notional(self, symbol: Optional[str] = None) -> float:
        """读取 MIN_NOTIONAL.notional；失败时退回 0（表示不做该约束）。

        2026-10-04：追价改为「成交多少、继续追多少」之后，剩余量会越追越小。
        小于交易所最小名义金额的单子必然被拒，继续挂在窗口里只是空转，
        所以提前判掉、直接按已成交量收工。
        """
        try:
            info = await self.exchange_info()
        except BinanceClientError:
            return 0.0
        sym = symbol or self.symbol
        for s in info.get("symbols") or []:
            if s.get("symbol") != sym:
                continue
            for f in s.get("filters") or []:
                if f.get("filterType") == "MIN_NOTIONAL":
                    try:
                        return float(f.get("notional") or 0)
                    except (TypeError, ValueError):
                        return 0.0
        return 0.0

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
        post_only: bool = False,
        price_watch: Optional[Any] = None,
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

        price_watch: 可选的异步回调, 每个轮询周期调用一次; 返回真值即中止等待,
            由调用方决定撤单还是重挂。追价用它实现「盘口一变就撤单重挂、
            盘口不动就一直挂着攒排队位置」。
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
            if "-4061" in submit_error and not getattr(self, "_mode_flipped", False):
                self._mode_flipped = True  # type: ignore[attr-defined]
                self._hedge_mode = not bool(self._hedge_mode)
                return await self.place_limit_order(
                    side, quantity, price, symbol,
                    time_in_force=time_in_force, reduce_only=reduce_only,
                    client_order_id=cid, fill_timeout_sec=fill_timeout_sec,
                    poll_interval_sec=poll_interval_sec,
                    cancel_if_unfilled=cancel_if_unfilled,
                    post_only=post_only,
                )
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
            if price_watch is not None:
                try:
                    if await price_watch():
                        break
                except Exception as exc:  # noqa: BLE001 监控故障不得影响订单
                    logger.debug("price_watch 失败(已忽略): %s", exc)

        filled = float(last.cum_filled_qty or last.filled_qty or 0)

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
            cum = float(canceled.cum_filled_qty or 0) or filled
            canceled.cum_filled_qty = cum
            canceled.filled_qty = cum
            if canceled.state == OrderState.UNKNOWN and not canceled.error:
                canceled.state = OrderState.CANCELED
            result = canceled.to_order_result()
            if cum > 0:
                # 有真实成交就是「成功的一部分」，与改动前
                # （部分成交直接返回、ok=True）保持一致的语义；
                # order_state 仍如实记为 CANCELED，不粉饰订单真实状态。
                result.ok = True
            return result

        if filled > 0:
            return last.to_order_result()

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

    async def place_resting_limit_order(
        self,
        side: str,
        quantity: float,
        price: float,
        symbol: Optional[str] = None,
        *,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
        time_in_force: str = "GTC",
    ) -> OrderResult:
        """挂一张**留在盘口**的限价单，提交成功即返回。

        与 place_limit_order 的本质差别：后者是「追价成交」语义 —— 超时未成交
        会返回 ok=False（unfilled_after_timeout），因为它假设调用方要的是成交。
        而挂单止盈要的恰恰是**挂着不成交**，用它会得到假失败，进而重复下单。

        2026-10-04：止盈腿从 TAKE_PROFIT_MARKET 改为 LIMIT + reduceOnly，目的
        是吃 maker 费率（2bp）而不是 taker（4bp），同时避免市价成交的滑点。
        多头的止盈价在市场上方，所以一张挂在那里的 SELL LIMIT 天然是 maker，
        **不需要触发机制**。

        与 place_limit_order 相同的安全规则：
          * 刚提交返回 NEW/PENDING_NEW 是正常的，必须查一次确认
          * 提交异常时先按 clientOrderId 回查，确认没下出去才报失败
          * 只有明确拒单才标记 REJECTED
        **绝不撤单** —— 留在盘口就是目的。
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
        # side 是持仓方向，不是买卖方向：止盈必须平仓。
        # 多仓 SELL、空仓 BUY；写反会变成给多仓加仓，触发 -2022。
        order_side = "SELL" if side_u == "LONG" else "BUY"
        cid = client_order_id or _new_client_order_id("tp")
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "quantity": qty,
            "price": limit_price,
            "timeInForce": (time_in_force or "GTC").upper(),
            "newClientOrderId": cid,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        hedge = False
        try:
            hedge = await self.get_position_mode()
        except Exception:
            hedge = bool(self._hedge_mode)
        if hedge:
            # 双向持仓模式下 reduceOnly 不被接受，用 positionSide 表达「只减仓」
            params["positionSide"] = (
                "LONG" if order_side == "SELL" else "SHORT"
            )
            params.pop("reduceOnly", None)
        submit_raw = None
        submit_error = ""
        try:
            submit_raw = await self._request(
                "POST", "/fapi/v1/order", params, signed=True
            )
        except BinanceClientError as exc:
            submit_error = str(exc)
            if "-1116" in submit_error or "-1102" in submit_error \
                    or "-4014" in submit_error:
                return OrderResult(
                    ok=False, error=submit_error, symbol=symbol,
                    side=order_side, client_order_id=cid,
                    order_state=OrderState.REJECTED.value,
                    requested_qty=quantity, submitted_qty=qty,
                    cum_filled_qty=0.0, quantity=0.0,
                )
            # 其余网络类错误：走回查，不判定失败
        entry = (
            self._raw_to_managed(submit_raw, client_order_id=cid)
            if submit_raw
            else ManagedOrder(
                client_order_id=cid, state=OrderState.UNKNOWN,
                symbol=symbol, side=order_side, raw={},
            )
        )
        # 查一次确认挂单真的在盘口。挂单成功时状态是 NEW / PARTIALLY_FILLED。
        if entry.state == OrderState.UNKNOWN:
            try:
                entry = await self.query_order(client_order_id=cid, symbol=symbol)
            except Exception:  # noqa: BLE001 回查失败不得冒充失败
                pass
        # 注意：OrderState 没有 NEW —— 币安的 NEW 映射为 ACKNOWLEDGED。
        # 「挂单成功」= ACKNOWLEDGED（在盘口待成交）/ PARTIALLY_FILLED / FILLED
        if entry.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED,
                           OrderState.FILLED):
            res = entry.to_order_result()
            res.ok = True
            return res
        if entry.state == OrderState.REJECTED:
            return entry.to_order_result()
        return OrderResult(
            ok=False,
            order_id=entry.exchange_order_id or "",
            symbol=symbol, side=order_side, position_side=side_u,
            quantity=0.0, requested_qty=quantity, submitted_qty=qty,
            cum_filled_qty=0.0, avg_price=0.0,
            status=entry.state.value, client_order_id=cid,
            order_state=entry.state.value,
            error=(f"submitted_unknown: {submit_error}" if submit_error
                   else "resting_order_unconfirmed"),
            raw=entry.raw if isinstance(entry.raw, dict) else {},
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
        on_step: Optional[Any] = None,
    ) -> OrderResult:
        """下单入口 —— **契约: 永不抛异常**, 失败一律返回 ok=False 的 OrderResult.

        2026-10-04: 追价内部要发上百次 HTTP, 任何一次超时/网络抖动此前都以异常
        形式逃逸, 冒泡到运行器 step() 把整个 tick 打断(另一标的也不再处理、状态
        不落盘)。现在统一兜底, 由调用方按"下单未成功"处理。

        on_step: 可选的 async 回调 ``on_step(bid, ask) -> bool``, 在**每次挂单
        之前**调用。返回真值即中止追价(返回 ok=False), 供调用方插入安全检查点
        —— 追价最长 180 秒, 期间主循环被占住, 此前没有任何机会评估止损。
        回调抛异常只记警告, 不影响下单。
        """
        symbol = symbol or self.symbol
        try:
            return await self._place_limit_chase_impl(
                side, quantity, symbol,
                reduce_only=reduce_only, mark_price=mark_price,
                max_steps=max_steps, window_sec=window_sec,
                tag=tag, force_cross=force_cross, on_step=on_step,
            )
        except Exception as exc:  # noqa: BLE001  契约要求兜住一切非取消异常
            logger.error("place_limit_chase 失败(已降级为失败结果): %s: %s",
                         type(exc).__name__, exc)
            return OrderResult(
                ok=False, symbol=symbol,
                side="BUY" if side.upper() == "LONG" else "SELL",
                position_side=side.upper(),
                quantity=0.0, requested_qty=float(quantity or 0),
                cum_filled_qty=0.0, avg_price=0.0,
                order_state=OrderState.UNKNOWN.value,
                error=f"chase_error: {type(exc).__name__}: {exc}",
            )

    async def _place_limit_chase_impl(
        self,
        side: str,
        quantity: float,
        symbol: str,
        *,
        reduce_only: bool = False,
        mark_price: Optional[float] = None,
        max_steps: Optional[int] = None,
        window_sec: Optional[float] = None,
        tag: str = "ps",
        force_cross: bool = False,
        on_step: Optional[Any] = None,
    ) -> OrderResult:
        """被动限价挂单 (post-only) + 跟盘口重挂, 超时兜底.

        订单类型: 被动阶段一律 post-only (type=LIMIT + timeInForce=GTX)。
        该模式下交易所会直接拒掉任何会立即成交的单 (-5022), 所以只要成交,
        手续费必定是 maker。这不是约定, 是规则。

        挂价 (基于真实盘口, 不是 mark price):
            做多: min(best_bid - pad*tick, best_ask - tick)   —— 绝不碰 best_ask
            做空: max(best_ask + pad*tick, best_bid + tick)   —— 绝不碰 best_bid

        跟随逻辑: **盘口目标价一变就撤单重挂，不变就一直挂着**。
            市场动了 → 挂价跟着走 → 一路贴住最优价
            市场没动 → 挂单原地驻留 → 攒排队位置，而不是每 6 秒白排一次队尾
            （2026-10-04 实测：无脑每 6 秒重挂 28 次，maker 成交率只剩 6%）

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

        # 2026-10-04 修复「部分成交即结束」（P0）：
        #
        # 此前只要被动挂单成交了哪怕 0.0001，就立刻 return —— 剩下的量既不继续
        # 追、也不走兜底。实测 13:46 那笔目标 0.2010 BTC 只成交 0.0048（2.4%），
        # 被动尝试仅 1 次就收工，仓位凭空少掉 97.6%，风险暴露从计划的 1% 权益
        # 掉到 0.024%。做对了也赚不到钱。
        #
        # 现在改成「成交多少、剩余多少继续追」：每次成交后把剩余量减掉，重新
        # 挂下一档；直到全部成交、窗口耗尽、或剩余量小到交易所必然拒单为止。
        # 窗口耗尽仍未补齐时，兜底只对**剩余量**下单，不重复整笔。
        remaining = qty
        maker_filled = 0.0
        taker_filled = 0.0
        # 安全检查点是否要求中止（在轮询回调里被置位，主循环据此收工）
        abort: Dict[str, bool] = {"stop": False}
        filled_quote = 0.0        # 累计成交额，用于算加权均价
        min_notional = await self.min_notional(symbol)
        last_res: Optional[OrderResult] = None

        def below_min(q: float, price: float) -> bool:
            """剩余量是否已经小到交易所必然拒单（再挂只是空转）。"""
            if q <= 0:
                return True
            if min_notional > 0 and price > 0 and q * price < min_notional:
                return True
            return q < lot_step

        def fill_px(r: Optional[OrderResult], fallback: float) -> float:
            p = float(getattr(r, "avg_price", 0) or 0)
            return p if p > 0 else fallback

        def finalize(ok: bool, *, error: str = "",
                     state: str = "") -> OrderResult:
            """把多次成交合并成一个结果：数量累加、均价按成交额加权。"""
            total = maker_filled + taker_filled
            base = last_res if isinstance(last_res, OrderResult) else OrderResult(
                ok=ok, symbol=symbol,
                side="BUY" if is_long else "SELL",
                position_side=side.upper(),
            )
            base.ok = ok
            base.cum_filled_qty = total
            base.quantity = total
            base.avg_price = (filled_quote / total) if total > 0 else 0.0
            base.requested_qty = quantity
            base.submitted_qty = qty
            if error:
                base.error = error
            if state:
                base.status = state
                base.order_state = state
            return base

        while (remaining > 0 and time.time() < deadline
               and len(attempts) < PASSIVE_MAX_REPRICE):
            book = await self.book_ticker(symbol)
            bid, ask = book["bid"], book["ask"]
            if bid <= 0 or ask <= 0:
                return OrderResult(
                    ok=False, error="book ticker unavailable", symbol=symbol
                )
            if on_step is not None:
                # 安全检查点: 追价最长 180 秒, 这期间主循环被占住, 此前完全
                # 没有机会评估灾难止损。回调返回真值即中止本次追价(此时尚未
                # 挂出任何单, 不会有遗留挂单), 让主循环去处理止损。
                try:
                    if await on_step(bid, ask):
                        return OrderResult(
                            ok=False, symbol=symbol,
                            side="BUY" if is_long else "SELL",
                            position_side=side.upper(),
                            quantity=0.0, requested_qty=quantity,
                            submitted_qty=qty, cum_filled_qty=0.0,
                            avg_price=0.0, client_order_id="",
                            order_state=OrderState.CANCELED.value,
                            error="chase_aborted: 安全检查点要求中止追价",
                            raw={"chase": {"attempts": attempts,
                                           "likely_maker": False}},
                        )
                except Exception as exc:  # noqa: BLE001 检查点故障不得影响下单
                    logger.warning("chase on_step 回调失败(已忽略): %s", exc)
            if is_long:
                px = min(bid - pad * tick, ask - tick)
            else:
                px = max(ask + pad * tick, bid + tick)
            px = self._price_precision(px, tick)

            if below_min(remaining, px):
                # 剩余量已不可能再成交：按已成交量收工，不再空转。
                break

            idx = len(attempts)
            cid = _new_client_order_id(f"{tag}{idx}")

            async def _watch(_px: float = px, _pad: int = pad) -> bool:
                """每个轮询周期跑一次：安全检查点 + 盘口变化检测。

                返回真值 = 「该撤单了」。两种情况：
                  1. 安全检查点要求中止追价（on_step 返回真值）；
                  2. 盘口目标价已经变了，挂单价格不再贴盘口。
                两者都必须放在轮询里：挂单驻留期间主循环被占住，这里是唯一
                能及时发现灾难止损的机会，也是唯一能跟上盘口的地方。
                """
                book = await self.book_ticker(symbol)
                nb, na = book["bid"], book["ask"]
                if on_step is not None:
                    try:
                        if await on_step(nb, na):
                            abort["stop"] = True
                            return True
                    except Exception as exc:  # noqa: BLE001 检查点故障不得影响下单
                        logger.warning("chase on_step 回调失败(已忽略): %s", exc)
                if nb <= 0 or na <= 0:
                    return False
                npx = self._price_precision(
                    (min(nb - _pad * tick, na - tick) if is_long
                     else max(na + _pad * tick, nb + tick)), tick
                )
                return abs(npx - _px) >= tick / 2

            # 盘口不动就挂到窗口结束（价格一变由 _watch 提前叫停）；
            # 刚被 post-only 拒过则只等一小会儿再试，避免在同一价位空转。
            wait_sec = (
                PASSIVE_REPRICE_SEC if pad > 0
                else max(PASSIVE_REPRICE_SEC, deadline - time.time())
            )
            res = await self.place_limit_order(
                side,
                remaining,
                px,
                symbol,
                reduce_only=reduce_only,
                client_order_id=cid,
                fill_timeout_sec=wait_sec,
                poll_interval_sec=PASSIVE_REST_POLL_SEC,
                cancel_if_unfilled=True,
                post_only=True,
                price_watch=_watch,
            )
            last_res = res
            got = float(res.cum_filled_qty or 0)
            if got <= 0:
                # 撤单后复核: 撤单与成交可能竞态
                final = await self.query_order(client_order_id=cid, symbol=symbol)
                final_filled = float(final.cum_filled_qty or final.filled_qty or 0)
                if final.state == OrderState.FILLED or final_filled > 0:
                    last_res = final.to_order_result()
                    got = final_filled
            if got > 0:
                # post-only 成交必为 maker（GTX 会直接拒掉会立即成交的单）。
                maker_filled += got
                filled_quote += got * fill_px(last_res, px)
                remaining = self._qty_precision(remaining - got, lot_step)
                attempts.append({"step": idx, "price": px, "filled": got,
                                 "bid": bid, "ask": ask, "maker": True})
                pad = 0
                if abort["stop"]:
                    break
                continue

            if abort["stop"]:
                # 安全检查点要求中止：已经拿到的被动成交不能被抹掉，
                # 没有成交则明确返回中止，让主循环去处理止损。
                if maker_filled + taker_filled > 0:
                    break
                return OrderResult(
                    ok=False, symbol=symbol,
                    side="BUY" if is_long else "SELL",
                    position_side=side.upper(),
                    quantity=0.0, requested_qty=quantity,
                    submitted_qty=qty, cum_filled_qty=0.0,
                    avg_price=0.0, client_order_id="",
                    order_state=OrderState.CANCELED.value,
                    error="chase_aborted: 安全检查点要求中止追价",
                    raw={"chase": {"attempts": attempts,
                                   "likely_maker": False}},
                )

            if res.error and "POST_ONLY_REJECT" in res.error:
                # 盘口在读取与提交之间动了, 价格已经站到对手方: 让开一档
                pad = min(pad + 1, PASSIVE_MAX_PAD)
                attempts.append({"step": idx, "price": px, "filled": 0.0,
                                 "bid": bid, "ask": ask, "reject": "post_only"})
                continue
            pad = 0
            attempts.append({"step": idx, "price": px, "filled": 0.0,
                             "bid": bid, "ask": ask})

        # 被动阶段已经把整笔吃满 —— 最好结果，不付任何 taker 费。
        if remaining <= 0 and (maker_filled + taker_filled) > 0:
            return self._finish_passive(
                finalize(True, state=OrderState.FILLED.value),
                attempts, maker=True, reduce_only=reduce_only,
            )

        # ---- 兜底 --------------------------------------------------------
        if PASSIVE_ON_TIMEOUT == "abandon":
            if maker_filled > 0:
                # 已经拿到的 maker 成交不能被「放弃」抹掉。
                return self._finish_passive(
                    finalize(True, state=OrderState.CANCELED.value),
                    attempts, maker=True, reduce_only=reduce_only,
                )
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
        # 兜底只对**剩余量**下单；被动阶段已经成交的部分不重复买。
        cross_qty = remaining if remaining > 0 else 0.0
        res = None
        if cross_qty > 0 and not below_min(cross_qty, cross_px):
            cid = _new_client_order_id(f"{tag}x")
            res = await self.place_limit_order(
                side, cross_qty, cross_px, symbol,
                # IOC 仍然是 LIMIT：立即成交可成交部分，余量由交易所取消。
                # 不留下 GTC 挂单，避免运行器认为失败后该单又延迟成交。
                time_in_force="IOC",
                reduce_only=reduce_only, client_order_id=cid,
                fill_timeout_sec=10.0, poll_interval_sec=CHASE_POLL_INTERVAL,
                cancel_if_unfilled=False,
            )
            last_res = res
            got = float(res.cum_filled_qty or 0)
            if got > 0:
                taker_filled += got
                filled_quote += got * fill_px(res, cross_px)
                remaining = self._qty_precision(remaining - got, lot_step)
                attempts.append({"step": len(attempts), "price": cross_px,
                                 "filled": got, "bid": bid, "ask": ask,
                                 "maker": False, "cross": True})
        total_filled = maker_filled + taker_filled
        mixed_maker = maker_filled > 0 and taker_filled <= 0
        if total_filled > 0:
            # 被动部分成交 + 兜底补齐（或兜底没补齐）：都按「成功的一部分」返回，
            # 绝不因为兜底没成交就丢掉已经拿到的被动成交量。
            tail = ""
            if res is not None and remaining > 0:
                tail = (f"partial_then_exhausted: 被动 {maker_filled:.6f} + "
                        f"IOC 未补齐剩余 {remaining:.6f}; "
                        f"{res.error or res.order_state}")
            elif remaining > 0:
                tail = (f"partial_below_min: 被动 {maker_filled:.6f}, "
                        f"剩余 {remaining:.6f} 低于最小下单量")
            return self._finish_passive(
                finalize(True, state=OrderState.CANCELED.value, error=tail),
                attempts, maker=mixed_maker, reduce_only=reduce_only,
            )
        # 保留交易所实际订单状态/ID；网络未知不能伪称已经撤销。
        base = res if isinstance(res, OrderResult) else OrderResult(
            ok=False, symbol=symbol,
            side="BUY" if is_long else "SELL", position_side=side.upper(),
        )
        base.ok = False
        base.requested_qty = quantity
        base.submitted_qty = qty
        base.error = (f"passive_exhausted: 被动 {len(attempts)} 次 + IOC限价兜底未确认成交; "
                      f"{getattr(base, 'error', '') or getattr(base, 'order_state', '')}")
        # 复用同一套 chase meta 键名；2026-10-03 手写 {"attempts","likely_maker"} 导致
        # 日志把 steps/passive 读成 0（「taker 被动0次」），真实原因被挡住。
        return self._with_passive_meta(base, attempts, maker=False)

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
            # 2026-10-04: 一笔追价可能「被动成交一部分 + 兜底再成交一部分」，
            # 单一 likely_maker 布尔量说不清成本结构，所以把两部分成交量分开记。
            # 面板/日志可以据此算真实加权费率（maker 2bps / taker 4bps）。
            "maker_filled": sum(
                float(a.get("filled") or 0.0) for a in attempts if a.get("maker")
            ),
            "taker_filled": sum(
                float(a.get("filled") or 0.0) for a in attempts
                if a.get("filled") and not a.get("maker")
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
        end_time: Optional[int] = None,
        strict: bool = False,
    ) -> List[Dict[str, Any]]:
        """历史成交 (GET /fapi/v1/userTrades) —— 含 realizedPnl 与 commission.

        这是「赚了多少」的唯一可信来源: 逐笔已实现盈亏与手续费都取自交易所,
        不由本地账本推算。

        start_time / end_time 为毫秒时间戳。**币安硬性要求两者跨度不超过 7 天**，
        超出会直接报错；需要更长区间时必须自己按 ≤7 天分窗多次调用。

        strict=False（默认）保持历史行为：接口报错时返回空列表。这在回放/面板
        里会把「查询失败」伪装成「没有成交」，所以做对账时必须传 strict=True，
        让错误显式抛出来。
        """
        symbol = symbol or self.symbol
        params: Dict[str, Any] = {
            "symbol": symbol,
            "limit": max(1, min(int(limit), 1000)),
        }
        if start_time:
            params["startTime"] = int(start_time)
        if end_time:
            params["endTime"] = int(end_time)
        raw = await self._request(
            "GET", "/fapi/v1/userTrades", params, signed=True
        )
        if isinstance(raw, list):
            return raw
        if strict:
            raise BinanceClientError(
                f"userTrades 返回非列表（可能超出 7 天跨度限制）: {str(raw)[:200]}"
            )
        return []

    async def income(
        self,
        symbol: Optional[str] = None,
        *,
        income_type: Optional[str] = None,
        limit: int = 100,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        strict: bool = False,
    ) -> List[Dict[str, Any]]:
        """账户收支明细 (GET /fapi/v1/income) —— 只读查询.

        资金费 (incomeType=FUNDING_FEE) 只有这里能拿到: userTrades 的
        commission 不含资金费, 所以成本必须分两处取, 不能混成一个数。

        与 user_trades 同样受 7 天跨度限制；strict=True 时错误显式抛出。
        """
        symbol = symbol or self.symbol
        params: Dict[str, Any] = {
            "symbol": symbol,
            "limit": max(1, min(int(limit), 1000)),
        }
        if income_type:
            params["incomeType"] = income_type
        if start_time:
            params["startTime"] = int(start_time)
        if end_time:
            params["endTime"] = int(end_time)
        raw = await self._request(
            "GET", "/fapi/v1/income", params, signed=True
        )
        if not isinstance(raw, list) and strict:
            raise BinanceClientError(
                f"income 返回非列表（可能超出 7 天跨度限制）: {str(raw)[:200]}"
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
