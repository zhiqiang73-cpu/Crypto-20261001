"""免费消息面采集 — 短期突发 + 长期宏观 双路径.

短期: CryptoPanic 主源 + RSS 交叉验证 / 政府源补充
长期: ETF / DXY / 减半 / 监管 RSS / FRED 注入字段
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from collectors.cryptopanic import (
    aggregate_posts,
    source_credibility,
)
from config.mapping import (
    BTC_LAST_HALVING_DATE,
    BTC_NEXT_HALVING_DATE,
    FARSIDE_BTC_URL,
    NEWS_POLL_SEC,
    REGULATION_BEAR_KEYWORDS,
    REGULATION_BULL_KEYWORDS,
    REGULATION_RSS_URLS,
    STOOQ_DXY_URL,
    YAHOO_DXY_URL,
)
from models.snapshots import CryptoPanicSnapshot, NewsSnapshot

logger = logging.getLogger(__name__)


def parse_farside_html(html: str) -> Optional[Dict[str, Any]]:
    """从 Farside BTC ETF 页面抽最近一日净流入与近 5 日合计."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.I | re.S)
    daily_candidates: List[float] = []
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.I | re.S)
        cleaned = []
        for c in cells:
            text = re.sub(r"<[^>]+>", "", c).strip().replace(",", "")
            text = text.replace("\xa0", "").replace("$", "")
            if text in ("", "-", "–", "—"):
                cleaned.append(None)
                continue
            neg = False
            if text.startswith("(") and text.endswith(")"):
                neg = True
                text = text[1:-1]
            try:
                val = float(text)
                if neg:
                    val = -val
                cleaned.append(val)
            except ValueError:
                cleaned.append(None)
        if cleaned and cleaned[-1] is not None and abs(cleaned[-1]) < 50_000:
            daily_candidates.append(cleaned[-1])

    if not daily_candidates:
        return None

    latest_m = daily_candidates[0]
    week = daily_candidates[:5]
    weekly_m = sum(week)
    consecutive = 0
    if all(x > 0 for x in week) and len(week) >= 3:
        consecutive = 1

    return {
        "daily_net_usd": latest_m * 1_000_000,
        "weekly_net_usd": weekly_m * 1_000_000,
        "consecutive_inflow_weeks": consecutive,
    }


def parse_stooq_csv(text: str) -> Optional[float]:
    reader = csv.DictReader(io.StringIO(text))
    closes: List[float] = []
    for row in reader:
        try:
            closes.append(float(row.get("Close") or row.get("close") or 0))
        except (TypeError, ValueError):
            continue
    if len(closes) < 6:
        return None
    window = closes[-6:]
    if window[0] == 0:
        return None
    return (window[-1] / window[0]) - 1.0


def parse_yahoo_chart(payload: dict) -> Optional[float]:
    try:
        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
    except (KeyError, IndexError, TypeError):
        return None
    if len(closes) < 6:
        return None
    window = closes[-6:]
    if window[0] == 0:
        return None
    return (window[-1] / window[0]) - 1.0


def halving_months(today: Optional[date] = None) -> Tuple[float, float]:
    today = today or date.today()
    last = datetime.strptime(BTC_LAST_HALVING_DATE, "%Y-%m-%d").date()
    nxt = datetime.strptime(BTC_NEXT_HALVING_DATE, "%Y-%m-%d").date()
    since = (today - last).days / 30.4375
    until = (nxt - today).days / 30.4375
    return since, until


def score_regulation_headlines(texts: List[str]) -> Tuple[Optional[float], str]:
    if not texts:
        return None, "none"
    bull = 0
    bear = 0
    for t in texts:
        low = t.lower()
        for kw in REGULATION_BULL_KEYWORDS:
            if kw in low:
                bull += 1
        for kw in REGULATION_BEAR_KEYWORDS:
            if kw in low:
                bear += 1
    if bull == 0 and bear == 0:
        return 0.0, "low"
    net = bull - bear
    score = max(-80.0, min(80.0, net * 25.0))
    return score, "low"


def parse_rss_items(xml_text: str, source_hint: str = "") -> List[dict]:
    """解析 RSS → CryptoPanic 风格 posts 列表."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    domain = ""
    try:
        domain = urlparse(source_hint).netloc if source_hint else ""
    except Exception:
        domain = ""

    items: List[dict] = []
    for item in root.iter():
        tag = item.tag.lower()
        if not (tag.endswith("item") or tag.endswith("entry")):
            continue
        title = None
        pub = None
        link = None
        for child in list(item):
            ct = child.tag.lower()
            if ct.endswith("title") and child.text:
                title = child.text.strip()
            elif ct.endswith("pubdate") or ct.endswith("published") or ct.endswith("updated"):
                if child.text:
                    pub = child.text.strip()
            elif ct.endswith("link"):
                link = child.get("href") or (child.text or "").strip() or link
        if not title:
            continue
        published_at = None
        if pub:
            try:
                published_at = parsedate_to_datetime(pub)
            except Exception:
                try:
                    published_at = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                except Exception:
                    published_at = None
        if published_at is None:
            published_at = datetime.now(timezone.utc)
        elif published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)

        item_domain = domain
        if link:
            try:
                item_domain = urlparse(link).netloc or domain
            except Exception:
                pass
        items.append({
            "title": title,
            "published_at": published_at.isoformat(),
            "source": {"domain": item_domain},
            "votes": {},
        })
    return items[:40]


def parse_rss_titles(xml_text: str) -> List[str]:
    return [p["title"] for p in parse_rss_items(xml_text) if p.get("title")]


class FreeNewsCollector:
    def __init__(self, poll_sec: float = NEWS_POLL_SEC) -> None:
        self.poll_sec = poll_sec
        self._session: Optional[Any] = None
        self.snapshot = NewsSnapshot()
        self._running = False
        self._cryptopanic = None  # lazy
        self._rss_cache: List[dict] = []
        self._rss_cache_ts: float = 0.0
        self._rss_cache_ttl: float = 180.0
        self._prev_black_swan: Optional[float] = None

    @property
    def last_success_ts(self) -> Optional[int]:
        return self.snapshot.last_success_ts

    def get_snapshot(self) -> NewsSnapshot:
        return self.snapshot

    def _get_cryptopanic(self):
        if self._cryptopanic is None:
            from collectors.cryptopanic import CryptoPanicCollector
            self._cryptopanic = CryptoPanicCollector(session=None)
        return self._cryptopanic

    async def _session_get(self) -> Any:
        if aiohttp is None:
            raise RuntimeError("aiohttp required")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": "btc-four-face/1.0"},
            )
        return self._session

    async def close(self) -> None:
        if self._cryptopanic is not None:
            await self._cryptopanic.close()
        if self._session and not self._session.closed:
            await self._session.close()

    async def _fetch_text(self, url: str) -> str:
        sess = await self._session_get()
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"{url} → {resp.status}")
            return await resp.text()

    async def _fetch_json(self, url: str) -> dict:
        sess = await self._session_get()
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"{url} → {resp.status}")
            return await resp.json()

    async def fetch_dxy(self) -> Tuple[Optional[float], Optional[str]]:
        try:
            text = await self._fetch_text(STOOQ_DXY_URL)
            ch = parse_stooq_csv(text)
            if ch is not None:
                return ch, "stooq"
        except Exception as exc:
            logger.info("stooq DXY failed: %s", exc)
        try:
            payload = await self._fetch_json(YAHOO_DXY_URL)
            ch = parse_yahoo_chart(payload)
            if ch is not None:
                return ch, "yahoo"
        except Exception as exc:
            logger.info("yahoo DXY failed: %s", exc)
        return None, None

    async def fetch_etf(self) -> Optional[Dict[str, Any]]:
        try:
            html = await self._fetch_text(FARSIDE_BTC_URL)
            return parse_farside_html(html)
        except Exception as exc:
            logger.warning("farside fetch failed: %s", exc)
            return None

    async def fetch_rss_posts(self) -> List[dict]:
        now = time.time()
        if self._rss_cache and (now - self._rss_cache_ts) < self._rss_cache_ttl:
            return list(self._rss_cache)
        posts: List[dict] = []
        for url in REGULATION_RSS_URLS:
            try:
                xml = await self._fetch_text(url)
                posts.extend(parse_rss_items(xml, source_hint=url))
            except Exception as exc:
                logger.info("rss %s failed: %s", url, exc)
        self._rss_cache = posts
        self._rss_cache_ts = now
        return list(posts)

    async def fetch_regulation(self) -> Tuple[Optional[float], str]:
        posts = await self.fetch_rss_posts()
        media_titles = []
        all_titles = []
        for p in posts:
            t = p.get("title")
            if not t:
                continue
            all_titles.append(t)
            domain = str((p.get("source") or {}).get("domain") or "")
            if any(d in domain for d in ("coindesk", "cointelegraph", "theblock", "decrypt")):
                media_titles.append(t)
        return score_regulation_headlines(media_titles or all_titles)

    async def fetch_breaking_bundle(
        self,
        cryptopanic_snap: Optional[CryptoPanicSnapshot] = None,
    ) -> Dict[str, Any]:
        """短期突发: CryptoPanic + RSS 合并聚合."""
        cp = cryptopanic_snap
        if cp is None:
            try:
                collector = self._get_cryptopanic()
                # 复用本 collector 的 session 会更稳, 但 CryptoPanic 自有 session
                cp = await collector.fetch_once()
            except Exception as exc:
                logger.warning("cryptopanic in news failed: %s", exc)
                cp = CryptoPanicSnapshot()

        rss_posts = await self.fetch_rss_posts()
        # 把 RSS 再聚合一次, 与 CryptoPanic 取较大绝对值 / 平均
        rss_agg = (
            aggregate_posts(rss_posts, prev_black_swan=self._prev_black_swan)
            if rss_posts
            else {}
        )

        def _merge(a: Optional[float], b: Optional[float]) -> Optional[float]:
            if a is None:
                return b
            if b is None:
                return a
            # 同向取均值, 反向取绝对值更大者
            if a * b >= 0:
                return (a + b) / 2.0
            return a if abs(a) >= abs(b) else b

        breaking = _merge(cp.breaking_sentiment, rss_agg.get("breaking_sentiment"))
        regulatory = _merge(
            cp.regulatory_event_score, rss_agg.get("regulatory_event_score")
        )
        macro = _merge(cp.macro_surprise_score, rss_agg.get("macro_surprise_score"))
        black = _merge(cp.black_swan_score, rss_agg.get("black_swan_score"))
        if black is not None:
            self._prev_black_swan = black

        headlines = list(cp.recent_headlines or [])
        for h in (rss_agg.get("recent_headlines") or [])[:6]:
            if h not in headlines:
                headlines.append(h)

        sev = cp.event_severity or "none"
        rss_sev = rss_agg.get("event_severity") or "none"
        rank = {"none": 0, "normal": 1, "important": 2, "extreme": 3}
        if rank.get(rss_sev, 0) > rank.get(sev, 0):
            sev = rss_sev

        return {
            "breaking_sentiment": breaking,
            "regulatory_event_score": regulatory,
            "macro_surprise_score": macro,
            "black_swan_score": black,
            "event_severity": sev,
            "recent_headlines": headlines[:15],
            "cp_available": bool(cp.available),
        }

    async def fetch_long_term(
        self,
        whale_institutional_score: Optional[float] = None,
        usdt_net_mint_24h: Optional[float] = None,
    ) -> NewsSnapshot:
        """长期宏观路径: ETF / DXY / 减半 / 监管趋势."""
        snap = NewsSnapshot()
        missing: List[str] = []

        etf = await self.fetch_etf()
        if etf:
            snap.etf_daily_net_usd = etf["daily_net_usd"]
            snap.etf_weekly_net_usd = etf["weekly_net_usd"]
            snap.etf_consecutive_inflow_weeks = etf["consecutive_inflow_weeks"]
        else:
            missing.append("etf_flows")

        dxy, src = await self.fetch_dxy()
        snap.dxy_change_5d = dxy
        snap.dxy_source = src
        if dxy is None:
            missing.append("monetary_policy")

        since, until = halving_months()
        snap.months_since_halving = since
        snap.months_to_halving = until

        reg_score, conf = await self.fetch_regulation()
        snap.regulation_score = reg_score
        snap.regulation_confidence = conf
        if reg_score is None:
            missing.append("regulations")

        snap.institutional_score = whale_institutional_score
        if whale_institutional_score is None:
            missing.append("institutional_gov")

        snap.usdt_net_mint_24h = usdt_net_mint_24h
        if usdt_net_mint_24h is None:
            missing.append("usdt_dynamics")

        # macro_data / black_swan 由 FRED / 突发注入
        missing.append("macro_data")
        missing.append("black_swan")
        snap.missing_fields = missing

        snap.available = any(
            v is not None
            for v in (
                snap.etf_daily_net_usd,
                snap.dxy_change_5d,
                snap.months_since_halving,
                snap.regulation_score,
                snap.institutional_score,
                snap.usdt_net_mint_24h,
            )
        )
        if snap.available:
            snap.last_success_ts = int(time.time() * 1000)
        else:
            snap.last_error = "all long-term news sources empty"
        return snap

    async def fetch_short_term(
        self,
        whale_institutional_score: Optional[float] = None,
        cryptopanic_snap: Optional[CryptoPanicSnapshot] = None,
    ) -> NewsSnapshot:
        """短期突发路径: CryptoPanic + RSS + whale."""
        snap = NewsSnapshot()
        missing: List[str] = []

        bundle = await self.fetch_breaking_bundle(cryptopanic_snap)
        snap.breaking_sentiment = bundle["breaking_sentiment"]
        snap.regulatory_event_score = bundle["regulatory_event_score"]
        snap.macro_surprise_score = bundle["macro_surprise_score"]
        snap.black_swan_score = bundle["black_swan_score"]
        snap.event_severity = bundle["event_severity"]
        snap.recent_headlines = bundle["recent_headlines"]
        snap.black_swan_event = bundle["event_severity"] == "extreme"

        if snap.breaking_sentiment is None:
            missing.append("breaking_crypto")
        if snap.regulatory_event_score is None:
            missing.append("regulatory_event")
        if snap.macro_surprise_score is None:
            missing.append("macro_surprise")
        if snap.black_swan_score is None:
            missing.append("black_swan")

        snap.institutional_score = whale_institutional_score
        if whale_institutional_score is None:
            missing.append("whale_institutional")

        # 减半日历仍填, 但短期权重不含
        since, until = halving_months()
        snap.months_since_halving = since
        snap.months_to_halving = until

        snap.missing_fields = missing
        snap.available = any(
            v is not None
            for v in (
                snap.breaking_sentiment,
                snap.regulatory_event_score,
                snap.macro_surprise_score,
                snap.black_swan_score,
                snap.institutional_score,
            )
        ) or bool(snap.recent_headlines)
        if snap.available:
            snap.last_success_ts = int(time.time() * 1000)
        else:
            snap.last_error = "all short-term news sources empty"
        return snap

    async def fetch_once(
        self,
        whale_institutional_score: Optional[float] = None,
        usdt_net_mint_24h: Optional[float] = None,
        cryptopanic_snap: Optional[CryptoPanicSnapshot] = None,
        prefer_short: bool = True,
    ) -> NewsSnapshot:
        """默认拉短期突发 + 长期宏观字段合并, 供双口径 mapper 使用."""
        long_snap = await self.fetch_long_term(
            whale_institutional_score=whale_institutional_score,
            usdt_net_mint_24h=usdt_net_mint_24h,
        )
        short_snap = await self.fetch_short_term(
            whale_institutional_score=whale_institutional_score,
            cryptopanic_snap=cryptopanic_snap,
        )

        # 合并: 长期字段 + 短期突发字段
        merged = long_snap
        merged.breaking_sentiment = short_snap.breaking_sentiment
        merged.regulatory_event_score = short_snap.regulatory_event_score
        merged.macro_surprise_score = short_snap.macro_surprise_score
        merged.black_swan_score = short_snap.black_swan_score
        merged.event_severity = short_snap.event_severity
        merged.recent_headlines = short_snap.recent_headlines
        merged.black_swan_event = short_snap.black_swan_event
        if short_snap.institutional_score is not None:
            merged.institutional_score = short_snap.institutional_score

        # missing 取并集后由 mapper 按口径过滤
        miss = set(long_snap.missing_fields) | set(short_snap.missing_fields)
        # 短期字段若有值则从 missing 去掉对应长期别名
        if merged.breaking_sentiment is not None:
            miss.discard("breaking_crypto")
        if merged.regulatory_event_score is not None:
            miss.discard("regulatory_event")
            # 长期 regulations 仍可能缺, 用突发监管分兜底
            if merged.regulation_score is None:
                merged.regulation_score = merged.regulatory_event_score
                merged.regulation_confidence = "low"
                miss.discard("regulations")
        if merged.macro_surprise_score is not None:
            miss.discard("macro_surprise")
        if merged.black_swan_score is not None:
            miss.discard("black_swan")
        if merged.institutional_score is not None:
            miss.discard("institutional_gov")
            miss.discard("whale_institutional")

        merged.missing_fields = sorted(miss)
        merged.available = long_snap.available or short_snap.available
        if merged.available:
            merged.last_success_ts = int(time.time() * 1000)
            merged.last_error = None
        else:
            merged.last_error = short_snap.last_error or long_snap.last_error

        # 记录源可信度触达 (调试用, 不强制)
        if prefer_short and short_snap.recent_headlines:
            logger.debug(
                "news short headlines=%d sev=%s",
                len(short_snap.recent_headlines),
                short_snap.event_severity,
            )

        self.snapshot = merged
        return merged

    async def run(
        self,
        stop_event: Optional[asyncio.Event] = None,
        onchain_provider=None,
        cryptopanic_provider=None,
    ) -> None:
        self._running = True
        stop = stop_event or asyncio.Event()
        while self._running and not stop.is_set():
            inst = None
            usdt = None
            cp = None
            if onchain_provider:
                oc = onchain_provider()
                if oc is not None:
                    inst = getattr(oc, "whale_net_flow_btc", None)
                    mint = getattr(oc, "usdt_mint_24h", None) or 0
                    burn = getattr(oc, "usdt_burn_24h", None) or 0
                    if mint or burn:
                        usdt = mint - burn
            if cryptopanic_provider:
                try:
                    cp = cryptopanic_provider()
                except Exception:
                    cp = None
            await self.fetch_once(
                whale_institutional_score=inst,
                usdt_net_mint_24h=usdt,
                cryptopanic_snap=cp,
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_sec)
            except asyncio.TimeoutError:
                pass
        await self.close()

    def stop(self) -> None:
        self._running = False
