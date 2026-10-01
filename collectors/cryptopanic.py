"""CryptoPanic 突发新闻聚合 — 短期消息面主源.

免费层: 公开 posts; 有 auth_token 可解锁 votes/filter.
源可信度 + 严重度 + 时效衰减在此完成聚合.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.mapping import (
    BREAKING_BEAR_KEYWORDS,
    BREAKING_BULL_KEYWORDS,
    BREAKING_EXTREME_KEYWORDS,
    BREAKING_IMPORTANT_KEYWORDS,
    CRYPTOPANIC_POLL_SEC,
    CRYPTOPANIC_URL,
    LOW_SOURCE_THRESHOLD,
    MACRO_SURPRISE_KEYWORDS,
    MULTI_SOURCE_BOOST,
    NEWS_AGE_DECAY,
    REGULATORY_KEYWORDS,
    SINGLE_LOW_SOURCE_DAMPEN,
    SOURCE_CREDIBILITY,
    SOURCE_CREDIBILITY_DEFAULT,
)
from models.snapshots import CryptoPanicSnapshot

logger = logging.getLogger(__name__)


def source_credibility(domain: Optional[str]) -> float:
    if not domain:
        return SOURCE_CREDIBILITY_DEFAULT
    d = domain.lower().strip()
    best = SOURCE_CREDIBILITY_DEFAULT
    for needle, w in SOURCE_CREDIBILITY.items():
        if needle in d:
            best = max(best, w)
    return best


def age_decay(age_hours: float) -> float:
    for max_h, mult in NEWS_AGE_DECAY:
        if age_hours <= max_h:
            return mult
    return 0.0


def severity_multiplier(title: str, important_votes: int) -> Tuple[float, str]:
    """标题 + 投票 → (乘数, 严重度档).

    extreme: 硬核灾难关键词 (hack/war/collapse...) 或 important≥10
    important: 监管/宏观关键词 或 important≥2
    normal: 其余
    """
    low = title.lower()
    hard_extreme = (
        "hack", "exploit", "ban", "war", "invasion", "collapse",
        "depeg", "insolvency", "bankruptcy", "arrest",
    )
    if important_votes >= 10 or any(k in low for k in hard_extreme):
        return 2.0, "extreme"
    if any(k in low for k in BREAKING_EXTREME_KEYWORDS):
        # 广义极端词 (emergency 等) 但无硬核灾难 → 仍标 extreme, 需多帖确认
        return 2.0, "extreme"
    if important_votes >= 2 or any(k in low for k in BREAKING_IMPORTANT_KEYWORDS):
        return 1.5, "important"
    return 1.0, "normal"


def title_sentiment(title: str) -> float:
    """粗关键词情绪 → [-1, +1]."""
    low = title.lower()
    bull = sum(1 for k in BREAKING_BULL_KEYWORDS if k in low)
    bear = sum(1 for k in BREAKING_BEAR_KEYWORDS if k in low)
    if bull == 0 and bear == 0:
        return 0.0
    raw = (bull - bear) / max(bull + bear, 1)
    return max(-1.0, min(1.0, raw))


def votes_sentiment(votes: dict) -> float:
    pos = float(votes.get("positive") or 0)
    neg = float(votes.get("negative") or 0)
    if pos + neg <= 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / (pos + neg)))


def classify_bucket(title: str) -> str:
    low = title.lower()
    if any(k in low for k in REGULATORY_KEYWORDS):
        return "regulatory"
    if any(k in low for k in MACRO_SURPRISE_KEYWORDS):
        return "macro"
    return "breaking"


def _normalize_title_key(title: str) -> str:
    t = re.sub(r"[^a-z0-9\s]", "", title.lower())
    words = [w for w in t.split() if len(w) > 3][:6]
    return " ".join(words)


def parse_published_at(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    s = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def aggregate_posts(
    posts: List[dict],
    now: Optional[datetime] = None,
    prev_black_swan: Optional[float] = None,
    black_swan_ema_alpha: float = 0.35,
    min_extreme_posts: int = 3,
) -> Dict[str, Any]:
    """聚合 posts → breaking/regulatory/macro/black_swan 分值.

    黑天鹅规则 (V2):
    - 至少 min_extreme_posts 篇 extreme 簇才激活
    - 只聚合 extreme 帖自身评分, 不把所有桶最大值拉进来
    - 与前值 EMA 混合, 避免批次间符号翻转
    """
    now = now or datetime.now(timezone.utc)
    buckets: Dict[str, List[float]] = {
        "breaking": [],
        "regulatory": [],
        "macro": [],
    }
    extreme_scores: List[float] = []
    headlines: List[str] = []
    max_sev = "none"
    sev_rank = {"none": 0, "normal": 1, "important": 2, "extreme": 3}
    clusters: Dict[str, Dict[str, Any]] = {}

    for p in posts:
        title = str(p.get("title") or "").strip()
        if not title:
            continue
        src = p.get("source") or {}
        domain = src.get("domain") or ""
        if not domain and src.get("url"):
            try:
                domain = urlparse(str(src["url"])).netloc
            except Exception:
                domain = ""
        cred = source_credibility(domain)
        votes = p.get("votes") or {}
        if not isinstance(votes, dict):
            votes = {}
        important = int(votes.get("important") or 0)
        sev_mult, sev = severity_multiplier(title, important)
        if sev_rank.get(sev, 0) > sev_rank.get(max_sev, 0):
            max_sev = sev

        pub = parse_published_at(p.get("published_at"))
        if pub is None:
            continue
        age_h = max(0.0, (now - pub).total_seconds() / 3600.0)
        decay = age_decay(age_h)
        if decay <= 0:
            continue

        sent = title_sentiment(title)
        vs = votes_sentiment(votes)
        if sent == 0.0 and vs != 0.0:
            sent = vs
        elif sent != 0.0 and vs != 0.0:
            sent = 0.6 * sent + 0.4 * vs

        score = sent * 80.0 * sev_mult * cred * decay
        key = _normalize_title_key(title)
        if key not in clusters:
            clusters[key] = {
                "scores": [],
                "domains": set(),
                "bucket": classify_bucket(title),
                "title": title,
                "cred_max": cred,
                "sev": sev,
                "extreme_posts": 0,
            }
        clusters[key]["scores"].append(score)
        clusters[key]["domains"].add(domain or "unknown")
        clusters[key]["cred_max"] = max(clusters[key]["cred_max"], cred)
        if sev_rank.get(sev, 0) > sev_rank.get(clusters[key].get("sev", "none"), 0):
            clusters[key]["sev"] = sev
        if sev == "extreme":
            clusters[key]["extreme_posts"] = int(clusters[key].get("extreme_posts", 0)) + 1
        headlines.append(f"[{domain or '?'}] {title}")

    extreme_post_count = 0
    for cl in clusters.values():
        n_src = len({d for d in cl["domains"] if d and d != "unknown"})
        avg = sum(cl["scores"]) / len(cl["scores"])
        if n_src >= 2:
            avg *= MULTI_SOURCE_BOOST
        elif n_src <= 1 and cl["cred_max"] < LOW_SOURCE_THRESHOLD:
            avg *= SINGLE_LOW_SOURCE_DAMPEN
        avg = max(-100.0, min(100.0, avg))
        buckets[cl["bucket"]].append(avg)
        if cl.get("sev") == "extreme":
            extreme_scores.append(avg)
            extreme_post_count += int(cl.get("extreme_posts") or len(cl["scores"]))

    def _avg(xs: List[float]) -> Optional[float]:
        if not xs:
            return None
        return max(-100.0, min(100.0, sum(xs) / len(xs)))

    black = None
    # 至少 N 篇 extreme 帖才激活; 只取 extreme 簇自身均值
    if extreme_post_count >= min_extreme_posts and extreme_scores:
        raw_black = sum(extreme_scores) / len(extreme_scores)
        black = max(-100.0, min(100.0, raw_black))
        max_sev = "extreme"
    elif extreme_post_count > 0 and max_sev == "extreme":
        # 有 extreme 但不足确认门槛 → 降为 important, 不写 black_swan
        max_sev = "important"

    if black is not None and prev_black_swan is not None:
        a = max(0.0, min(1.0, black_swan_ema_alpha))
        black = a * black + (1.0 - a) * float(prev_black_swan)
        black = max(-100.0, min(100.0, black))

    return {
        "breaking_sentiment": _avg(buckets["breaking"]),
        "regulatory_event_score": _avg(buckets["regulatory"]),
        "macro_surprise_score": _avg(buckets["macro"]),
        "black_swan_score": black,
        "event_severity": max_sev,
        "extreme_cluster_count": extreme_post_count,
        "recent_headlines": headlines[:12],
        "post_count": len(posts),
    }


def parse_cryptopanic_payload(payload: dict) -> List[dict]:
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if isinstance(results, list):
        return results
    data = payload.get("data")
    if isinstance(data, list):
        return data
    return []


class CryptoPanicCollector:
    def __init__(
        self,
        api_key: Optional[str] = None,
        poll_sec: float = CRYPTOPANIC_POLL_SEC,
        session: Optional[Any] = None,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("CRYPTOPANIC_API_KEY")
            or ""
        )
        if not self.api_key:
            try:
                from config.secrets import get_secret
                self.api_key = get_secret("cryptopanic_api_key") or ""
            except Exception:
                self.api_key = ""
        self.poll_sec = poll_sec
        self._external_session = session
        self._session: Optional[Any] = None
        self._session_lock = None
        self._snapshot = CryptoPanicSnapshot()
        self._running = False

    def get_snapshot(self) -> CryptoPanicSnapshot:
        s = self._snapshot
        return CryptoPanicSnapshot(
            breaking_sentiment=s.breaking_sentiment,
            regulatory_event_score=s.regulatory_event_score,
            macro_surprise_score=s.macro_surprise_score,
            black_swan_score=s.black_swan_score,
            event_severity=s.event_severity,
            recent_headlines=list(s.recent_headlines),
            post_count=s.post_count,
            available=s.available,
            last_success_ts=s.last_success_ts,
            last_error=s.last_error,
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
                self._session = aiohttp.ClientSession(
                    headers={"User-Agent": "btc-four-face/1.0"},
                )
            return self._session

    async def fetch_once(self) -> CryptoPanicSnapshot:
        snap = CryptoPanicSnapshot()
        if not self.api_key:
            # CryptoPanic 现已要求 auth_token; 无 key 时不空打, 交给 RSS 兜底.
            # 仍刷新时间戳, 避免 staleness 永久 null (RSS 路径在 free_news).
            snap.last_error = (
                "CRYPTOPANIC_API_KEY missing — free key at "
                "https://cryptopanic.com/developers/api/ (RSS fallback active)"
            )
            snap.last_success_ts = int(time.time() * 1000)
            snap.available = False
            if self._snapshot.available:
                # 保留旧有效快照, 只更新 error 提示
                self._snapshot.last_error = snap.last_error
                return self.get_snapshot()
            self._snapshot = snap
            return self.get_snapshot()

        session = await self._ensure_session()
        params: Dict[str, str] = {
            "auth_token": self.api_key,
            "currencies": "BTC",
            "kind": "news",
        }
        try:
            async with session.get(
                CRYPTOPANIC_URL, params=params, timeout=20
            ) as resp:
                if resp.status == 403:
                    text = await resp.text()
                    snap.last_error = (
                        f"HTTP 403 (check API key): {text[:80]}"
                    )
                    logger.warning("cryptopanic %s", snap.last_error)
                    # 403 时降频: 拉长下次轮询
                    self.poll_sec = max(self.poll_sec, 600.0)
                    if self._snapshot.available:
                        return self.get_snapshot()
                    self._snapshot = snap
                    return self.get_snapshot()
                if resp.status >= 400:
                    text = await resp.text()
                    snap.last_error = f"HTTP {resp.status}: {text[:120]}"
                    logger.warning("cryptopanic %s", snap.last_error)
                    if self._snapshot.available:
                        return self.get_snapshot()
                    self._snapshot = snap
                    return self.get_snapshot()
                payload = await resp.json()
            posts = parse_cryptopanic_payload(payload)
            agg = aggregate_posts(
                posts, prev_black_swan=self._snapshot.black_swan_score
            )
            snap.breaking_sentiment = agg["breaking_sentiment"]
            snap.regulatory_event_score = agg["regulatory_event_score"]
            snap.macro_surprise_score = agg["macro_surprise_score"]
            snap.black_swan_score = agg["black_swan_score"]
            snap.event_severity = agg["event_severity"]
            snap.recent_headlines = agg["recent_headlines"]
            snap.post_count = agg["post_count"]
            if any(
                v is not None
                for v in (
                    snap.breaking_sentiment,
                    snap.regulatory_event_score,
                    snap.macro_surprise_score,
                    snap.black_swan_score,
                )
            ) or snap.post_count > 0:
                if snap.breaking_sentiment is None and snap.post_count > 0:
                    snap.breaking_sentiment = 0.0
                snap.available = True
                snap.last_success_ts = int(time.time() * 1000)
                self.poll_sec = CRYPTOPANIC_POLL_SEC
            else:
                snap.last_error = "empty posts"
        except Exception as exc:
            snap.last_error = str(exc)
            logger.warning("cryptopanic error: %s", exc)
            if self._snapshot.available:
                return self.get_snapshot()

        if snap.available:
            self._snapshot = snap
        elif snap.last_error:
            self._snapshot.last_error = snap.last_error
        return self.get_snapshot()

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        self._running = True
        stop = stop_event or asyncio.Event()
        while self._running and not stop.is_set():
            try:
                await self.fetch_once()
            except Exception as exc:
                logger.warning("cryptopanic poll failed: %s", exc)
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
