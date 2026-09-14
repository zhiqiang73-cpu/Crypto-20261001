"""Polymarket Gamma API 采集器 — 预测面概率定价.

工程近似:
  - CME FedWatch 无免费公开 API → 用 Polymarket Fed 利率决议市场概率代替
  - 市场匹配不到时返回 None, 绝不猜测哪个市场
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import (
    POLYMARKET_GAMMA_URL,
    POLYMARKET_POLL_SEC,
    POLYMARKET_PROB_HISTORY_SEC,
)
from models.snapshots import PolymarketSnapshot

logger = logging.getLogger(__name__)


def _parse_outcome_prices(raw: Any) -> Optional[List[float]]:
    """Gamma 可能返回 JSON 字符串或 list."""
    if raw is None:
        return None
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    out = []
    for x in raw:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            return None
    return out


def extract_yes_prob(market: dict) -> Optional[float]:
    """取 Yes / 第一个 outcome 的概率."""
    prices = _parse_outcome_prices(market.get("outcomePrices"))
    if not prices:
        return None
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        import json
        try:
            outcomes = json.loads(outcomes)
        except (json.JSONDecodeError, TypeError):
            outcomes = None
    if isinstance(outcomes, list):
        for i, name in enumerate(outcomes):
            if str(name).lower() in ("yes", "up", "higher"):
                if i < len(prices):
                    return prices[i]
    return prices[0]


def extract_btc_threshold(text: str) -> Optional[float]:
    """从标题/问题里抽 '$XX,XXX' 或 'XXXXX' 阈值."""
    if not text:
        return None
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*[kK]?", text)
    if not m:
        m = re.search(r"(?:above|over|reach|>)\s*\$?\s*([\d,]+)", text, re.I)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    if "k" in text[m.start(): m.end() + 1].lower() and val < 1000:
        val *= 1000
    return val


def _is_near_settled(prob: Optional[float], lo: float = 0.05, hi: float = 0.95) -> bool:
    """概率钉死在两端 → 实质已结算 / 无信息量."""
    if prob is None:
        return True
    return prob <= lo or prob >= hi


def select_btc_price_market(
    markets: List[dict],
    mark_price: Optional[float] = None,
) -> Optional[dict]:
    """挑手册口径的「本月 > $X」上行阈值市场.

    优先: above/over/reach + 阈值贴近现价 (ATM) + 未钉死.
    拒绝: between 区间盘、ETF/宏观盘、近结算盘.

    阈值窗口: mark×0.995 ~ mark×1.15 (允许略低于现价的近 ATM 盘;
    旧阈值 mark×1.005 会误杀 $78k / mark=$77.6k 这类最有信息量的合约).
    """
    cands: List[Tuple[float, dict]] = []
    for m in markets:
        if m.get("closed") is True or m.get("active") is False:
            continue
        q = m.get("question") or ""
        slug = m.get("slug") or ""
        low = (slug + " " + q).lower()
        if "bitcoin" not in low and "btc" not in low:
            continue
        if any(k in low for k in ("etf", "fed", "rate", "election", "president", "unban", "reserve", "company")):
            continue
        # 区间盘不是「> $X」方向押注
        if "between" in low or " range" in low:
            continue
        if not any(k in low for k in ("above", "over", "reach", "hit", "higher than", ">")):
            continue
        thr = extract_btc_threshold(q or slug)
        if thr is None:
            continue
        prob = extract_yes_prob(m)
        if _is_near_settled(prob):
            continue
        # 无现价时宁可不选, 避免暖机阶段误锁「已在价内」合约
        if mark_price is None:
            continue
        # 近 ATM 优先: 允许略低于现价, 拒绝远低于现价的「已成真」盘
        if thr <= mark_price * 0.995:
            continue
        if thr > mark_price * 1.15:
            continue
        vol = float(m.get("volume") or m.get("volumeNum") or 0)
        rel = (thr - mark_price) / mark_price
        # 优先 0%~5% 上方 (含略低 ATM); 再远扣分; 绝对值越小越好
        if -0.005 <= rel <= 0.05:
            band_pen = abs(rel) * mark_price * 0.1
        elif 0.05 < rel <= 0.15:
            band_pen = abs(rel - 0.03) * mark_price
        else:
            band_pen = abs(rel) * mark_price * 2.0
        # 越小越好
        score = band_pen - min(vol, 1e9) * 1e-12
        cands.append((score, m))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def select_fed_market(markets: List[dict]) -> Optional[dict]:
    """挑未近结算的 Fed/FOMC 利率市场. 匹配不到 → None.

    几乎钉死的合约 (如「9 月是否已降息」0.4%) 不当 FedWatch 代理 —
    那会把「未降息/已过期」误读成「几乎确定加息」。
    """
    scored: List[Tuple[float, dict]] = []
    for m in markets:
        if m.get("closed") is True or m.get("active") is False:
            continue
        text = ((m.get("slug") or "") + " " + (m.get("question") or "")).lower()
        if not any(k in text for k in ("fed", "fomc", "interest rate", "rate cut", "rate hike")):
            continue
        if "bitcoin" in text or "btc" in text:
            continue
        prob = extract_yes_prob(m)
        if _is_near_settled(prob):
            continue
        vol = float(m.get("volume") or m.get("volumeNum") or 0)
        # 偏好 cut 类问题 + 高成交; 钉死盘已剔除
        cut_bonus = -1e6 if any(k in text for k in ("cut", "lower", "ease")) else 0.0
        scored.append((cut_bonus - vol, m))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    return scored[0][1]


def infer_fed_cut_hike(market: dict) -> Tuple[Optional[float], Optional[float]]:
    """从 Fed 市场推断 (cut_prob, hike_prob).

    只在问题明确写 hike/raise 时才填 hike.
    cut 类问题: 只返回 cut, hike=None —— 「不降息」≠「加息」.
    """
    yes = extract_yes_prob(market)
    if yes is None:
        return None, None
    text = ((market.get("question") or "") + " " + (market.get("slug") or "")).lower()
    if any(k in text for k in ("hike", "raise", "increase rate")):
        return None, yes
    if any(k in text for k in ("cut", "lower", "decrease", "ease")):
        return yes, None
    # 含糊利率问题: 只当 cut 代理, 不发明 hike
    return yes, None


class PolymarketCollector:
    """轮询 Gamma API, 维护 1h 概率变化滚动窗."""

    def __init__(self, poll_sec: float = POLYMARKET_POLL_SEC) -> None:
        self.poll_sec = poll_sec
        self._session: Optional[Any] = None
        self.snapshot = PolymarketSnapshot()
        self._prob_hist: Deque[Tuple[float, float]] = deque(maxlen=120)
        self._running = False

    @property
    def last_success_ts(self) -> Optional[int]:
        return self.snapshot.last_success_ts

    def get_snapshot(self) -> PolymarketSnapshot:
        return self.snapshot

    async def _session_get(self) -> Any:
        if aiohttp is None:
            raise RuntimeError("aiohttp required")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _search_events(self, query: str) -> List[dict]:
        """Gamma public-search → events (各含 markets[])."""
        sess = await self._session_get()
        url = f"{POLYMARKET_GAMMA_URL}/public-search"
        async with sess.get(url, params={"q": query}) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"gamma search {resp.status}: {text[:200]}")
            data = await resp.json()
        if isinstance(data, dict):
            return data.get("events") or []
        return []

    @staticmethod
    def _flatten_markets(events: List[dict]) -> List[dict]:
        out: List[dict] = []
        for ev in events:
            if ev.get("closed") is True or ev.get("active") is False:
                continue
            for m in ev.get("markets") or []:
                # 继承 event 活跃状态
                if m.get("closed") is True:
                    continue
                out.append(m)
        return out

    async def _fetch_markets(self, query: str, limit: int = 50) -> List[dict]:
        """优先 public-search；失败再退回 /markets 列表（通常无搜索能力）."""
        try:
            events = await self._search_events(query)
            markets = self._flatten_markets(events)
            if markets:
                return markets
        except Exception as exc:
            logger.info("public-search failed (%s), fallback /markets", exc)
        sess = await self._session_get()
        url = f"{POLYMARKET_GAMMA_URL}/markets"
        params = {"limit": limit, "active": "true", "closed": "false"}
        async with sess.get(url, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"gamma {resp.status}: {text[:200]}")
            data = await resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("markets") or data.get("data") or []
        return []

    def _update_prob_change(self, prob: float, now: float, market: Optional[dict] = None) -> Optional[float]:
        # 优先用 Gamma 自带的 1h 价格变化
        if market:
            oh = market.get("oneHourPriceChange")
            if oh is not None:
                try:
                    return float(oh)
                except (TypeError, ValueError):
                    pass
        self._prob_hist.append((now, prob))
        cutoff = now - POLYMARKET_PROB_HISTORY_SEC
        old = None
        for ts, p in self._prob_hist:
            if ts <= cutoff:
                old = p
            else:
                break
        if old is None and len(self._prob_hist) >= 2:
            old = self._prob_hist[0][1]
            age = now - self._prob_hist[0][0]
            if age < 300:
                return 0.0
        if old is None:
            return 0.0
        return prob - old

    async def fetch_once(
        self, mark_price: Optional[float] = None
    ) -> PolymarketSnapshot:
        snap = PolymarketSnapshot()
        try:
            btc_markets = await self._fetch_markets("bitcoin above")
            if len(btc_markets) < 3:
                btc_markets = btc_markets + await self._fetch_markets("bitcoin price")

            btc_m = select_btc_price_market(btc_markets, mark_price)
            if btc_m:
                prob = extract_yes_prob(btc_m)
                snap.btc_prob = prob
                snap.btc_market_slug = btc_m.get("slug")
                snap.btc_threshold_usd = extract_btc_threshold(
                    btc_m.get("question") or ""
                )
                snap.btc_market_question = (btc_m.get("question") or "")[:160] or None
                if prob is not None:
                    snap.btc_prob_change_1h = self._update_prob_change(
                        prob, time.time(), btc_m
                    )

            fed_markets = await self._fetch_markets("fed rate cut")
            if len(fed_markets) < 3:
                fed_markets = fed_markets + await self._fetch_markets("fed rate")
            fed_m = select_fed_market(fed_markets)
            if fed_m:
                cut, hike = infer_fed_cut_hike(fed_m)
                snap.fed_cut_prob = cut
                snap.fed_hike_prob = hike
                snap.fed_market_slug = fed_m.get("slug")
                snap.fed_market_question = (fed_m.get("question") or "")[:160] or None

            snap.available = any(
                v is not None
                for v in (snap.btc_prob, snap.fed_cut_prob, snap.fed_hike_prob)
            )
            if snap.available:
                snap.last_success_ts = int(time.time() * 1000)
            else:
                snap.last_error = "no matching markets"
        except Exception as exc:
            logger.warning("polymarket fetch failed: %s", exc)
            snap.last_error = str(exc)
            if self.snapshot.available:
                snap = self.snapshot
                snap.last_error = str(exc)

        self.snapshot = snap
        return snap

    async def run(
        self,
        stop_event: Optional[asyncio.Event] = None,
        mark_price_provider=None,
    ) -> None:
        self._running = True
        stop = stop_event or asyncio.Event()
        while self._running and not stop.is_set():
            mp = mark_price_provider() if mark_price_provider else None
            await self.fetch_once(mark_price=mp)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_sec)
            except asyncio.TimeoutError:
                pass
        await self.close()

    def stop(self) -> None:
        self._running = False
