"""
RSS/Atom firehose.

One `RssCollector` fans out across every feed in config.FEEDS whose poll
interval has elapsed. Conditional GET (ETag / If-Modified-Since) means an
unchanged feed costs a 304 and no parsing, so polling 60+ feeds every couple of
minutes stays cheap.

Feeds that fail repeatedly are backed off exponentially rather than hammered,
and their state is visible via `apollo feeds`.
"""
from __future__ import annotations

import asyncio
import calendar
import html
import re
import time
from datetime import datetime, timezone

import feedparser

from config import FEEDS, Feed
from store import Article, get_feed_state, save_feed_state
from .base import Collector, register

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_html(text: str | None, limit: int = 4000) -> str:
    if not text:
        return ""
    t = _TAG_RE.sub(" ", text)
    t = html.unescape(t)
    return _WS_RE.sub(" ", t).strip()[:limit]


def _struct_to_iso(st) -> str | None:
    """
    feedparser normalizes *_parsed to a UTC struct_time.

    Use calendar.timegm, not time.mktime — mktime interprets the struct as
    local time, which silently shifts every timestamp by the machine's UTC
    offset and corrupts recency scoring.
    """
    if not st:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(st), timezone.utc).isoformat(
            timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def parse_feed(feed: Feed, body: bytes) -> list[Article]:
    parsed = feedparser.parse(body)
    out: list[Article] = []

    for e in parsed.entries:
        title = clean_html(e.get("title"), 500)
        if not title:
            continue

        summary = clean_html(
            e.get("summary") or e.get("description") or "", 2000)
        body_text = ""
        if e.get("content"):
            body_text = clean_html(
                " ".join(c.get("value", "") for c in e["content"]), 6000)

        published = (
            _struct_to_iso(e.get("published_parsed"))
            or _struct_to_iso(e.get("updated_parsed"))
            or e.get("published")
            or e.get("updated")
        )

        author = e.get("author") or ""
        if not author and e.get("authors"):
            author = ", ".join(a.get("name", "") for a in e["authors"]).strip(", ")

        categories = [t.get("term", "") for t in (e.get("tags") or []) if t.get("term")]

        out.append(Article(
            title=title,
            url=e.get("link") or e.get("id"),
            summary=summary,
            body=body_text or None,
            author=author or None,
            source=feed.key,
            source_category=feed.category,
            source_weight=feed.weight,
            published_at=published,
            raw={
                "feed": feed.key,
                "categories": categories,
                "guid": e.get("id"),
                "_feed_tags": feed.tags,
            },
        ))
    return out


@register("rss")
class RssCollector(Collector):
    """Polls every configured feed on its own cadence from a single task."""

    interval = 30          # scheduler tick; individual feeds use feed.poll_seconds

    def __init__(self, http, feeds: list[Feed] | None = None):
        super().__init__(http)
        self.feeds = feeds if feeds is not None else FEEDS
        self._next_due: dict[str, float] = {f.key: 0.0 for f in self.feeds}

    def _due(self) -> list[Feed]:
        now = time.monotonic()
        return [f for f in self.feeds if self._next_due.get(f.key, 0.0) <= now]

    async def _fetch_one(self, feed: Feed) -> list[Article]:
        state = await asyncio.to_thread(get_feed_state, feed.key)
        headers: dict[str, str] = {}
        if state:
            if state["etag"]:
                headers["If-None-Match"] = state["etag"]
            if state["last_modified"]:
                headers["If-Modified-Since"] = state["last_modified"]

        backoff = 1.0
        try:
            status, body, resp_headers = await self.http.get(feed.url, headers=headers)
        except Exception as exc:
            streak = (state["error_streak"] if state else 0) + 1
            backoff = min(2 ** min(streak, 5), 30)
            await asyncio.to_thread(
                save_feed_state, feed.key, feed.url, status=f"error: {type(exc).__name__}")
            self.log.warning("%s unreachable (%s), backing off %.0fx",
                             feed.key, type(exc).__name__, backoff)
            self._next_due[feed.key] = time.monotonic() + feed.poll_seconds * backoff
            return []

        if status == 304:
            await asyncio.to_thread(save_feed_state, feed.key, feed.url,
                                    etag=resp_headers.get("ETag"), status="ok")
            self._next_due[feed.key] = time.monotonic() + feed.poll_seconds
            return []

        if status != 200 or not body:
            streak = (state["error_streak"] if state else 0) + 1
            backoff = min(2 ** min(streak, 5), 30)
            await asyncio.to_thread(save_feed_state, feed.key, feed.url,
                                    status=f"http {status}")
            self.log.warning("%s returned HTTP %s", feed.key, status)
            self._next_due[feed.key] = time.monotonic() + feed.poll_seconds * backoff
            return []

        articles = await asyncio.to_thread(parse_feed, feed, body)
        await asyncio.to_thread(
            save_feed_state, feed.key, feed.url,
            etag=resp_headers.get("ETag"),
            last_modified=resp_headers.get("Last-Modified"),
            status="ok", items_seen=len(articles),
        )
        self._next_due[feed.key] = time.monotonic() + feed.poll_seconds
        return articles

    async def collect(self) -> list[Article]:
        due = self._due()
        if not due:
            return []
        results = await asyncio.gather(
            *(self._fetch_one(f) for f in due), return_exceptions=True)
        out: list[Article] = []
        for feed, res in zip(due, results):
            if isinstance(res, Exception):
                self.log.error("%s raised %s", feed.key, res)
            else:
                out.extend(res)
        return out


async def check_feed_health(http, feeds: list[Feed] | None = None) -> list[dict]:
    """One-shot diagnostic: hit every feed, report status and item count."""
    feeds = feeds if feeds is not None else FEEDS

    async def probe(f: Feed) -> dict:
        try:
            status, body, _ = await http.get(f.url, retries=1)
            n = len(feedparser.parse(body).entries) if body else 0
            return {"key": f.key, "status": status, "items": n,
                    "ok": status in (200, 304) and (n > 0 or status == 304),
                    "url": f.url}
        except Exception as exc:
            return {"key": f.key, "status": type(exc).__name__, "items": 0,
                    "ok": False, "url": f.url}

    return list(await asyncio.gather(*(probe(f) for f in feeds)))
