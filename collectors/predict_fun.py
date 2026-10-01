"""Predict.fun CRYPTO_UP_DOWN 采集器 — 币安预测市场底层.

主网需 PREDICT_FUN_API_KEY (x-api-key)。
活盘特征:
  * search=Bitcoin + marketVariant=CRYPTO_UP_DOWN + status=OPEN
  * 盘口 bestBid/bestAsk 可能是 {price,size} 对象
  * 窗口以小时盘 / 日盘为主 (priceFeedProvider=BINANCE)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import (
    PREDICT_FUN_MAINNET_URL,
    PREDICT_FUN_POLL_SEC,
    PREDICT_FUN_TESTNET_URL,
)
from models.snapshots import PredictFunSnapshot

logger = logging.getLogger(__name__)

LIVE_STATUSES = frozenset({
    "OPEN", "REGISTERED", "PRICE_PROPOSED", "ACTIVE", "TRADING",
})


def _num(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, dict):
        x = x.get("price")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _outcome_mid(outcome: dict) -> Optional[float]:
    """bestBid/bestAsk 中价; 支持裸浮点或 {price,size}."""
    bid = _num(outcome.get("bestBid"))
    ask = _num(outcome.get("bestAsk"))
    try:
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        if ask is not None:
            return ask
        if bid is not None:
            return bid
    except (TypeError, ValueError):
        return None
    status = str(outcome.get("status") or "").upper()
    if status == "WON":
        return 1.0
    if status == "LOST":
        return 0.0
    return None


def extract_up_prob(market: dict) -> Optional[float]:
    for o in market.get("outcomes") or []:
        name = str(o.get("name") or "").lower()
        if name in ("up", "yes", "higher"):
            mid = _outcome_mid(o)
            if mid is not None:
                return max(0.0, min(1.0, mid))
    return None


def spread_width(market: dict) -> Optional[float]:
    """Up 盘口价差, 越小越有信息量."""
    for o in market.get("outcomes") or []:
        if str(o.get("name") or "").lower() not in ("up", "yes", "higher"):
            continue
        bid = _num(o.get("bestBid"))
        ask = _num(o.get("bestAsk"))
        if bid is not None and ask is not None:
            return max(0.0, ask - bid)
    return None


def is_btc_market(market: dict) -> bool:
    text = " ".join(
        [
            str(market.get("title") or ""),
            str(market.get("question") or ""),
            str(market.get("categorySlug") or ""),
        ]
    ).upper()
    return "BTC" in text or "BITCOIN" in text


def is_live_market(market: dict) -> bool:
    st = str(market.get("status") or "").upper()
    tr = str(market.get("tradingStatus") or "").upper()
    if st in ("RESOLVED", "SETTLED", "CLOSED", "CANCELLED"):
        return False
    return st in LIVE_STATUSES or tr in LIVE_STATUSES or tr == "OPEN"


def infer_window(market: dict) -> Optional[str]:
    """5m / 15m / 1h / 1d. 只用 title/question/slug, 不用 description (含结算时刻干扰)."""
    text = " ".join(
        [
            str(market.get("title") or ""),
            str(market.get("question") or ""),
            str(market.get("categorySlug") or ""),
        ]
    ).lower()
    slug = str(market.get("categorySlug") or "").lower()

    if re.search(r"15[\s\-]?min|15m\b|15[\s\-]?minute|15-minute", text):
        return "15m"
    if re.search(r"5[\s\-]?min|5m\b|5[\s\-]?minute|5-minute", text):
        return "5m"
    # 日盘优先于「含 am/pm 的小时盘」判定之前: slug/title 明确 on-Month
    if "up-or-down-on-" in slug or re.search(
        r"up or down on\s+(january|february|march|april|may|june|july|"
        r"august|september|october|november|december)\b",
        text,
    ):
        return "1d"
    if re.search(r"\b\d{1,2}(am|pm)\b", text) or re.search(
        r"\d{1,2}:\d{2}", text
    ):
        return "1h"
    if re.search(r"1[\s\-]?hour|60[\s\-]?min|1h\b", text):
        return "1h"
    if re.search(
        r"\bon\s+(january|february|march|april|may|june|july|"
        r"august|september|october|november|december)\b",
        text,
    ):
        return "1d"
    if re.search(r"\d{1,2}(am|pm)-et", slug):
        return "1h"
    return "1h"  # CRYPTO_UP_DOWN 默认短窗


def _informative(prob: Optional[float]) -> bool:
    return prob is not None and 0.05 < prob < 0.95


def _has_started(market: dict) -> bool:
    st = str(market.get("status") or "").upper()
    vd = market.get("variantData") or {}
    return bool(vd.get("startPrice")) or st == "PRICE_PROPOSED"


def _odds_moved(prob: float) -> bool:
    """相对 0.5 先验已挪动, 说明有真实交易信息."""
    return abs(prob - 0.5) >= 0.03


def _rank_market(market: dict, prob: float) -> Tuple:
    """排序: 有信息量 > 已开盘且赔率已动 > 靠近当前(非极端结算) > 窄价差 > 新 id.

    优先 PRICE_PROPOSED / 已挪动赔率的活盘; 在活盘中偏好尚未钉死的读数.
    """
    spr = spread_width(market)
    info = 1 if _informative(prob) else 0
    started = 1 if _has_started(market) else 0
    moved = 1 if _odds_moved(prob) else 0
    # 钉死度: |p-0| 或 |p-1| 太近则降权; 用「离端点距离」
    edge_dist = min(prob, 1.0 - prob)  # 越大越好 (0.5→0.5, 0.99→0.01)
    tight = 0 if spr is None else -spr
    try:
        mid = int(market.get("id") or 0)
    except (TypeError, ValueError):
        mid = 0
    return (info, started + moved, edge_dist, tight, mid)


def select_best_by_window(
    markets: List[dict],
) -> Dict[str, Tuple[dict, float]]:
    buckets: Dict[str, List[Tuple[dict, float]]] = {
        "5m": [], "15m": [], "1h": [], "1d": [],
    }
    for m in markets:
        if not is_btc_market(m) or not is_live_market(m):
            continue
        window = infer_window(m)
        if window not in buckets:
            continue
        prob = extract_up_prob(m)
        if prob is None:
            continue
        buckets[window].append((m, prob))

    out: Dict[str, Tuple[dict, float]] = {}
    for w, items in buckets.items():
        if not items:
            continue
        items.sort(key=lambda x: _rank_market(x[0], x[1]), reverse=True)
        out[w] = items[0]
    return out


def consensus_up_prob(by_w: Dict[str, Tuple[dict, float]]) -> Tuple[Optional[float], Optional[str]]:
    """多窗口共识: 优先有信息量的 5m→15m→1h→1d; 多个有信息量则加权."""
    order = ["5m", "15m", "1h", "1d"]
    weights = {"5m": 0.35, "15m": 0.30, "1h": 0.25, "1d": 0.10}
    usable = []
    for w in order:
        if w not in by_w:
            continue
        _, p = by_w[w]
        if _informative(p):
            usable.append((w, p, weights[w]))
    if not usable:
        # 退而取任意最新窗口
        for w in order:
            if w in by_w:
                return by_w[w][1], w
        return None, None
    if len(usable) == 1:
        return usable[0][1], usable[0][0]
    tw = sum(x[2] for x in usable)
    blended = sum(p * w for _, p, w in usable) / tw
    # active_window 取权重最大的那个
    best_w = max(usable, key=lambda x: x[2])[0]
    return blended, best_w


class PredictFunCollector:
    """Predict.fun / 币安预测市场 CRYPTO_UP_DOWN 采集."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        use_mainnet: Optional[bool] = None,
        poll_sec: float = PREDICT_FUN_POLL_SEC,
        session: Optional[Any] = None,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("PREDICT_FUN_API_KEY")
            or ""
        )
        if not self.api_key:
            try:
                from config.secrets import get_secret
                self.api_key = get_secret("predict_fun_api_key") or ""
            except Exception:
                self.api_key = ""
        if use_mainnet is None:
            use_mainnet = bool(self.api_key)
        self.use_mainnet = use_mainnet
        self.base_url = (
            PREDICT_FUN_MAINNET_URL if use_mainnet else PREDICT_FUN_TESTNET_URL
        )
        self.poll_sec = poll_sec
        self._external_session = session
        self._session: Optional[Any] = None
        self._session_lock = None
        self._snapshot = PredictFunSnapshot(
            source="mainnet" if use_mainnet else "testnet"
        )
        self._running = False

    @property
    def snapshot(self) -> PredictFunSnapshot:
        return self._snapshot

    def get_snapshot(self) -> PredictFunSnapshot:
        s = self._snapshot
        return PredictFunSnapshot(
            btc_up_prob_5m=s.btc_up_prob_5m,
            btc_up_prob_15m=s.btc_up_prob_15m,
            btc_up_prob_1h=s.btc_up_prob_1h,
            btc_up_prob_1d=s.btc_up_prob_1d,
            btc_up_prob=s.btc_up_prob,
            active_window=s.active_window,
            market_question=s.market_question,
            market_id=s.market_id,
            start_price=s.start_price,
            price_feed_provider=s.price_feed_provider,
            available=s.available,
            last_success_ts=s.last_success_ts,
            last_error=s.last_error,
            source=s.source,
        )

    async def _ensure_session(self) -> Any:
        if self._external_session is not None:
            return self._external_session
        if aiohttp is None:
            raise RuntimeError("aiohttp required")
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    async def _get_json(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        session = await self._ensure_session()
        url = f"{self.base_url.rstrip('/')}{path}"
        headers: Dict[str, str] = {}
        if self.api_key and self.use_mainnet:
            headers["x-api-key"] = self.api_key
        try:
            async with session.get(
                url, params=params or {}, headers=headers, timeout=20
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    self._snapshot.last_error = f"HTTP {resp.status}: {text[:160]}"
                    logger.warning("predict_fun %s", self._snapshot.last_error)
                    return None
                return await resp.json()
        except Exception as exc:
            self._snapshot.last_error = str(exc)
            logger.warning("predict_fun error: %s", exc)
            return None

    async def _fetch_markets(self) -> List[dict]:
        """多策略拉取, 合并去重."""
        seen = set()
        out: List[dict] = []
        queries = [
            {"first": "50", "marketVariant": "CRYPTO_UP_DOWN",
             "status": "OPEN", "search": "Bitcoin"},
            {"first": "50", "marketVariant": "CRYPTO_UP_DOWN",
             "status": "OPEN", "search": "BTC"},
            {"first": "50", "marketVariant": "CRYPTO_UP_DOWN", "status": "OPEN"},
        ]
        if not self.use_mainnet:
            queries.append({"first": "50", "marketVariant": "CRYPTO_UP_DOWN"})

        for params in queries:
            payload = await self._get_json("/v1/markets", params)
            if not isinstance(payload, dict):
                continue
            for m in payload.get("data") or []:
                mid = m.get("id") or m.get("conditionId") or id(m)
                if mid in seen:
                    continue
                seen.add(mid)
                out.append(m)
            # 主网搜到 Bitcoin 活盘就够用
            if self.use_mainnet and any(
                is_btc_market(m) and is_live_market(m) for m in out
            ):
                break
        return out

    async def fetch_once(self) -> PredictFunSnapshot:
        markets = await self._fetch_markets()
        by_w = select_best_by_window(markets)
        snap = PredictFunSnapshot(source="mainnet" if self.use_mainnet else "testnet")

        if "5m" in by_w:
            snap.btc_up_prob_5m = by_w["5m"][1]
        if "15m" in by_w:
            snap.btc_up_prob_15m = by_w["15m"][1]
        if "1h" in by_w:
            snap.btc_up_prob_1h = by_w["1h"][1]
        if "1d" in by_w:
            snap.btc_up_prob_1d = by_w["1d"][1]

        blended, active_w = consensus_up_prob(by_w)
        snap.btc_up_prob = blended
        snap.active_window = active_w

        if active_w and active_w in by_w:
            m, _ = by_w[active_w]
            snap.market_question = m.get("title") or m.get("question")
            try:
                snap.market_id = int(m.get("id"))
            except (TypeError, ValueError):
                snap.market_id = None
            vd = m.get("variantData") or {}
            snap.price_feed_provider = str(
                vd.get("priceFeedProvider") or ""
            ) or None
            try:
                sp = vd.get("startPrice")
                snap.start_price = float(sp) if sp is not None else None
            except (TypeError, ValueError):
                snap.start_price = None

        if snap.btc_up_prob is not None and _informative(snap.btc_up_prob):
            snap.available = True
            snap.last_success_ts = int(time.time() * 1000)
            snap.last_error = None
        elif snap.btc_up_prob is not None:
            snap.available = False
            snap.last_success_ts = int(time.time() * 1000)
            snap.last_error = (
                f"settled-like/wide-spread prob={snap.btc_up_prob:.2f} "
                f"window={snap.active_window}"
            )
        else:
            snap.available = False
            n_btc = sum(1 for m in markets if is_btc_market(m))
            n_live = sum(
                1 for m in markets if is_btc_market(m) and is_live_market(m)
            )
            snap.last_error = (
                self._snapshot.last_error
                or f"no live BTC up/down (markets={len(markets)} btc={n_btc} live={n_live})"
            )

        self._snapshot = snap
        return self.get_snapshot()

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        self._running = True
        stop = stop_event or asyncio.Event()
        while self._running and not stop.is_set():
            try:
                await self.fetch_once()
            except Exception as exc:
                logger.warning("predict_fun poll failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_sec)
            except asyncio.TimeoutError:
                pass
        self._running = False

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        self._running = False
        if self._external_session is None and self._session and not self._session.closed:
            await self._session.close()
            self._session = None
