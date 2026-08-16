"""
GDELT Doc 2.0 collector.

GDELT indexes worldwide news in near-real-time across 65+ languages and is
free with no key. Its value here is breadth: it surfaces regional outlets and
non-English coverage that never appears in a US-centric RSS list, which is
exactly where geopolitical shocks show up first.

Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import quote

from config import GDELT_QUERIES, POLL_INTERVALS
from store import Article
from .base import Collector, register

BASE = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT's index is enormous and includes a lot of low-quality aggregators.
# Restricting to English + a recent window keeps the noise manageable.
TIMESPAN = "60min"
MAX_RECORDS = 75

# Domains that are pure scrape-and-republish; their copies add no information
# and would otherwise inflate corroboration counts.
DOMAIN_BLOCKLIST = {
    "menafn.com", "marketscreener.com", "streetinsider.com", "finanzen.net",
    "investing.com", "msn.com", "news.google.com", "biztoc.com",
}


def _parse_seendate(s: str | None) -> str | None:
    """GDELT uses 'YYYYMMDDTHHMMSSZ'."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


@register("gdelt")
class GdeltCollector(Collector):
    interval = POLL_INTERVALS["gdelt"]

    async def _query(self, q: str) -> list[Article]:
        url = (f"{BASE}?query={quote(q + ' sourcelang:english')}"
               f"&mode=artlist&format=json&maxrecords={MAX_RECORDS}"
               f"&timespan={TIMESPAN}&sort=datedesc")
        status, body, _ = await self.http.get(url)
        if status != 200 or not body:
            return []
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            # GDELT occasionally returns an HTML error page with a 200.
            self.log.debug("non-JSON response for query %r", q)
            return []

        out: list[Article] = []
        for item in data.get("articles", []):
            domain = (item.get("domain") or "").lower()
            if domain in DOMAIN_BLOCKLIST:
                continue
            title = (item.get("title") or "").strip()
            if not title:
                continue
            out.append(Article(
                title=title,
                url=item.get("url"),
                source=f"gdelt:{domain}" if domain else "gdelt",
                source_category="geopolitical",
                # GDELT is an index, not a newsroom — discount it relative to
                # feeds we've explicitly vetted.
                source_weight=0.7,
                published_at=_parse_seendate(item.get("seendate")),
                raw={"gdelt_query": q, "domain": domain,
                     "country": item.get("sourcecountry"),
                     "language": item.get("language")},
            ))
        return out

    async def collect(self) -> list[Article]:
        results = await asyncio.gather(
            *(self._query(q) for q in GDELT_QUERIES), return_exceptions=True)
        out: list[Article] = []
        for q, res in zip(GDELT_QUERIES, results):
            if isinstance(res, Exception):
                self.log.warning("query %r failed: %s", q, res)
            else:
                out.extend(res)
        return out
