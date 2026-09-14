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
from trading.models import AccountBalance, OrderResult, PositionInfo

logger = logging.getLogger(__name__)


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

    def _qty_precision(self, qty: float, step: float = 0.001) -> float:
        if step <= 0:
            return round(qty, 3)
        n = int(qty / step)
        return round(n * step, 8)

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

    async def market_open(
        self,
        side: str,
        quantity: float,
        symbol: Optional[str] = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        """side: LONG / SHORT → BUY / SELL. 自动适配单向/双向持仓."""
        symbol = symbol or self.symbol
        step = await self._lot_step(symbol)
        qty = self._qty_precision(quantity, step)
        if qty <= 0:
            return OrderResult(ok=False, error=f"quantity too small: {quantity}")
        side_u = side.upper()
        order_side = "BUY" if side_u == "LONG" else "SELL"
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": order_side,
            "type": "MARKET",
            "quantity": qty,
        }
        hedge = False
        try:
            hedge = await self.get_position_mode()
        except Exception:
            hedge = bool(self._hedge_mode)
        if hedge:
            # 双向模式: 开仓用同向 positionSide; 平仓用持仓方向 + reduce
            params["positionSide"] = side_u if not reduce_only else (
                "LONG" if side_u == "SHORT" else "SHORT"
            )
            # 双向模式下 reduceOnly 与 positionSide 组合: 平多 = SELL + LONG
            if reduce_only:
                params["positionSide"] = "LONG" if order_side == "SELL" else "SHORT"
        else:
            if reduce_only:
                params["reduceOnly"] = "true"
        try:
            raw = await self._request("POST", "/fapi/v1/order", params, signed=True)
            avg = float(raw.get("avgPrice") or 0)
            qty_filled = float(raw.get("executedQty") or 0)
            # 市价单偶发返回 NEW + avg=0, 用仓位入口价兜底
            if avg <= 0 or qty_filled <= 0:
                await asyncio.sleep(0.15)
                pos = await self.get_position(symbol)
                if pos.entry_price > 0:
                    avg = pos.entry_price
                if qty_filled <= 0 and pos.quantity > 0:
                    qty_filled = pos.quantity
            return OrderResult(
                ok=True,
                order_id=str(raw.get("orderId") or ""),
                symbol=symbol,
                side=order_side,
                position_side=side_u,
                quantity=qty_filled or qty,
                avg_price=avg,
                status=str(raw.get("status") or ""),
                raw=raw if isinstance(raw, dict) else {},
            )
        except BinanceClientError as exc:
            # 若因持仓模式不匹配, 翻转探测再试一次
            if "-4061" in str(exc) and not getattr(self, "_mode_flipped", False):
                self._mode_flipped = True  # type: ignore[attr-defined]
                self._hedge_mode = not bool(self._hedge_mode)
                return await self.market_open(side, quantity, symbol, reduce_only)
            return OrderResult(ok=False, error=str(exc), side=order_side, symbol=symbol)

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
