"""
Optional keyed collectors.

Every collector here is a no-op unless its API key is present in .env, so the
system runs fully keyless out of the box. Each has a genuinely free tier:

  CryptoPanic  — crypto news aggregator with community bull/bear votes, which
                 is a useful independent check on our lexicon scoring.
                 Free key: https://cryptopanic.com/developers/api/
  Finnhub      — company + general market news, 60 calls/min free.
                 Free key: https://finnhub.io/register
  NewsAPI.org  — broad publisher coverage, 100 req/day free (dev tier).
                 Free key: https://newsapi.org/register
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

from config import (CRYPTOPANIC_TOKEN, FINNHUB_KEY, NEWSAPI_KEY,
                    POLL_INTERVALS, WATCH_TICKERS)
from store import Article
from .base import Collector, register


@register("cryptopanic")
class CryptoPanicCollector(Collector):
    interval = POLL_INTERVALS["cryptopanic"]

    def __init__(self, http):
        super().__init__(http)
        self.enabled = bool(CRYPTOPANIC_TOKEN)

    async def collect(self) -> list[Article]:
        if not self.enabled:
            return []
        url = ("https://cryptopanic.com/api/v1/posts/"
               f"?auth_token={CRYPTOPANIC_TOKEN}&public=true&kind=news")
        data = await self.http.get_json(url)

        out: list[Article] = []
        for p in (data or {}).get("results", []):
            votes = p.get("votes") or {}
            # CryptoPanic's own community vote is a free sentiment prior; keep
            # it in raw so it can be compared against our lexicon score.
            currencies = [c.get("code") for c in (p.get("currencies") or [])]
            out.append(Article(
                title=p.get("title", ""),
                url=p.get("url") or (p.get("source") or {}).get("domain"),
                source="cryptopanic",
                source_category="crypto",
                source_weight=0.9,
                published_at=p.get("published_at") or p.get("created_at"),
                raw={"votes": votes, "currencies": currencies,
                     "domain": (p.get("source") or {}).get("domain"),
                     "_feed_tags": [c for c in currencies
                                    if c in ("BTC", "ETH", "SOL")]},
            ))
        return out


@register("finnhub")
class FinnhubCollector(Collector):
    interval = POLL_INTERVALS["finnhub"]

    def __init__(self, http):
        super().__init__(http)
        self.enabled = bool(FINNHUB_KEY)

    async def _general(self, category: str) -> list[Article]:
        url = (f"https://finnhub.io/api/v1/news?category={category}"
               f"&token={FINNHUB_KEY}")
        try:
            data = await self.http.get_json(url)
        except Exception as exc:
            self.log.warning("%s news failed: %s", category, exc)
            return []
        return [
            Article(
                title=d.get("headline", ""),
                url=d.get("url"),
                summary=d.get("summary"),
                source=f"finnhub:{d.get('source', category)}",
                source_category="crypto" if category == "crypto" else "macro",
                source_weight=1.0,
                published_at=d.get("datetime"),
                raw={"category": category, "related": d.get("related")},
            )
            for d in (data or []) if d.get("headline")
        ]

    async def collect(self) -> list[Article]:
        if not self.enabled:
            return []
        results = await asyncio.gather(
            self._general("crypto"), self._general("general"),
            self._general("forex"), return_exceptions=True)
        out: list[Article] = []
        for r in results:
            if not isinstance(r, Exception):
                out.extend(r)
        return out


@register("newsapi")
class NewsApiCollector(Collector):
    """
    NewsAPI's free tier is only 100 requests/day, so this polls infrequently
    and uses one broad query rather than several narrow ones.
    """
    interval = 3600

    QUERY = ('(bitcoin OR ethereum OR solana OR cryptocurrency OR "federal reserve" '
             'OR "interest rates") AND (market OR price OR regulation OR policy)')

    def __init__(self, http):
        super().__init__(http)
        self.enabled = bool(NEWSAPI_KEY)

    async def collect(self) -> list[Article]:
        if not self.enabled:
            return []
        url = (f"https://newsapi.org/v2/everything?q={quote(self.QUERY)}"
               f"&language=en&sortBy=publishedAt&pageSize=100&apiKey={NEWSAPI_KEY}")
        data = await self.http.get_json(url)
        if (data or {}).get("status") != "ok":
            self.log.warning("newsapi error: %s", (data or {}).get("message"))
            return []
        return [
            Article(
                title=a.get("title", ""),
                url=a.get("url"),
                summary=a.get("description"),
                body=a.get("content"),
                author=a.get("author"),
                source=f"newsapi:{(a.get('source') or {}).get('name', 'unknown')}",
                source_category="macro",
                source_weight=0.85,
                published_at=a.get("publishedAt"),
                raw={"source": (a.get("source") or {}).get("name")},
            )
            for a in (data or {}).get("articles", []) if a.get("title")
        ]
