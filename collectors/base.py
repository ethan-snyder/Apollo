"""
Collector base classes and shared HTTP plumbing.

Every collector is an async task with its own cadence. The orchestrator in
daemon.py runs them all concurrently; a collector that throws is logged and
retried on its next tick rather than taking the process down.
"""
from __future__ import annotations

import asyncio
import logging
import random
import ssl
from abc import ABC, abstractmethod
from typing import Any, Callable

import aiohttp

from config import HTTP_TIMEOUT, MAX_RETRIES, USER_AGENT

try:
    import certifi
    # Some Windows Python installs ship without a populated system CA trust
    # store, which makes aiohttp's default SSL context fail with
    # ClientConnectorCertificateError against otherwise-valid sites (seen
    # against ecb.europa.eu in testing). Pin to certifi's bundle when
    # available; fall back to the interpreter default if certifi isn't
    # installed.
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None
from scoring import score_article
from store import Article, bulk_upsert

logger = logging.getLogger("COLLECTOR")

REGISTRY: dict[str, type["Collector"]] = {}


def register(name: str) -> Callable[[type["Collector"]], type["Collector"]]:
    def deco(cls: type["Collector"]) -> type["Collector"]:
        cls.name = name
        REGISTRY[name] = cls
        return cls
    return deco


class HttpClient:
    """Shared aiohttp session with retry, jittered backoff, and a concurrency cap."""

    def __init__(self, concurrency: int = 12, user_agent: str = USER_AGENT):
        self._sem = asyncio.Semaphore(concurrency)
        self._session: aiohttp.ClientSession | None = None
        self.user_agent = user_agent

    async def __aenter__(self) -> "HttpClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/rss+xml, application/atom+xml, application/xml,"
                          " application/json;q=0.9, text/xml;q=0.8, */*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            },
            connector=aiohttp.TCPConnector(limit=32, ttl_dns_cache=300,
                                           ssl=_SSL_CONTEXT),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    async def get(self, url: str, *, headers: dict | None = None,
                  retries: int = MAX_RETRIES) -> tuple[int, bytes, dict]:
        """
        Returns (status, body, response_headers).

        304 is a normal, expected outcome (conditional GET hit) and returns an
        empty body rather than raising.
        """
        assert self._session is not None, "HttpClient used outside its context manager"
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                async with self._sem:
                    async with self._session.get(url, headers=headers,
                                                 allow_redirects=True) as r:
                        body = await r.read() if r.status != 304 else b""
                        if r.status in (429, 500, 502, 503, 504) and attempt < retries - 1:
                            wait = (2 ** attempt) + random.random()
                            # Honour Retry-After when the server sends one.
                            ra = r.headers.get("Retry-After")
                            if ra and ra.isdigit():
                                wait = min(int(ra), 60)
                            logger.debug("%s -> %s, retrying in %.1fs", url, r.status, wait)
                            await asyncio.sleep(wait)
                            continue
                        return r.status, body, dict(r.headers)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    await asyncio.sleep((2 ** attempt) + random.random())
        raise last_exc or RuntimeError(f"GET failed: {url}")

    async def get_json(self, url: str, *, headers: dict | None = None) -> Any:
        h = {"Accept": "application/json", **(headers or {})}
        status, body, _ = await self.get(url, headers=h)
        if status != 200 or not body:
            raise RuntimeError(f"HTTP {status} for {url}")
        import json
        return json.loads(body)


class Collector(ABC):
    """
    Base collector.

    Subclasses implement `collect()`, returning Articles. Scoring, dedup, and
    persistence are handled here so every source gets identical treatment.
    """
    name: str = "collector"
    interval: int = 300          # seconds between runs
    enabled: bool = True

    def __init__(self, http: HttpClient):
        self.http = http
        self.log = logging.getLogger(self.name.upper())
        self.last_new = 0
        self.last_error: str | None = None
        self.runs = 0

    @abstractmethod
    async def collect(self) -> list[Article]:
        """Fetch from the source and return unscored Articles."""

    async def run_once(self) -> tuple[int, int]:
        """Collect -> score -> store. Returns (new, duplicate)."""
        self.runs += 1
        try:
            articles = await self.collect()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log.error("collect failed: %s", self.last_error)
            return 0, 0

        if not articles:
            return 0, 0

        for a in articles:
            try:
                score_article(a)
            except Exception as exc:
                self.log.error("scoring failed for %r: %s", a.title[:60], exc)

        # SQLite writes are blocking; keep them off the event loop.
        new, dup = await asyncio.to_thread(bulk_upsert, articles)
        self.last_new = new
        self.last_error = None
        if new:
            self.log.info("%d new / %d dupes", new, dup)
        return new, dup

    async def run_forever(self, stop: asyncio.Event) -> None:
        # Stagger startup so 60+ feeds don't all fire in the same second.
        await asyncio.sleep(random.uniform(0, min(8.0, self.interval / 4)))
        while not stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
