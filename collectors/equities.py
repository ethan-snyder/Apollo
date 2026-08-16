"""
Equities-side collectors: Alpaca news websocket + Yahoo Finance per-ticker news.

The Alpaca stream is the only true push source in the system — everything else
polls. It carries Benzinga headlines with sub-second latency, which for
crypto-adjacent equities (COIN, MSTR, miners) often beats the crypto press.
"""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, POLL_INTERVALS, WATCH_TICKERS
from store import Article
from .base import Collector, register


@register("alpaca_ws")
class AlpacaStreamCollector(Collector):
    """
    Alpaca news websocket.

    alpaca-py's `NewsDataStream.run()` owns its own event loop, so it runs on a
    daemon thread and hands articles back through a queue. The collector's
    `collect()` simply drains whatever arrived since the last tick, which keeps
    all scoring and persistence on the main loop with everything else.

    Auto-reconnect is handled by alpaca-py, but the thread is also restarted
    here if it dies outright.
    """
    interval = 5   # drain cadence; the socket itself is real-time

    def __init__(self, http):
        super().__init__(http)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=5000)
        self._thread: threading.Thread | None = None
        self._stream = None
        self.enabled = bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)
        if not self.enabled:
            self.log.warning(
                "ALPACA_API_KEY/ALPACA_SECRET_KEY not set — websocket disabled")

    def _start_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def runner() -> None:
            try:
                from alpaca.data.live import NewsDataStream
            except ImportError:
                self.log.error("alpaca-py not installed; websocket disabled")
                self.enabled = False
                return

            async def handler(news) -> None:
                try:
                    self._queue.put_nowait(news)
                except queue.Full:
                    self.log.warning("queue full, dropping %s",
                                     getattr(news, "headline", "")[:60])

            while self.enabled:
                try:
                    self._stream = NewsDataStream(ALPACA_API_KEY, ALPACA_SECRET_KEY)
                    self._stream.subscribe_news(handler, "*")
                    self.log.info("websocket connected, subscribed to all news")
                    self._stream.run()          # blocks until disconnect
                except Exception as exc:
                    self.log.error("stream error, reconnecting in 15s: %s", exc)
                import time
                time.sleep(15)

        self._thread = threading.Thread(target=runner, daemon=True,
                                        name="alpaca-news-stream")
        self._thread.start()

    async def collect(self) -> list[Article]:
        if not self.enabled:
            return []
        self._start_thread()

        out: list[Article] = []
        while True:
            try:
                n = self._queue.get_nowait()
            except queue.Empty:
                break

            symbols = list(getattr(n, "symbols", []) or [])
            # Benzinga is the dominant Alpaca provider and is reliable enough
            # to sit near the top of the weighting.
            out.append(Article(
                title=getattr(n, "headline", "") or "",
                url=getattr(n, "url", None),
                summary=getattr(n, "summary", None),
                body=(getattr(n, "content", None) or None),
                author=getattr(n, "author", None),
                source="alpaca_news",
                source_category="equities",
                source_weight=1.2,
                published_at=getattr(n, "created_at", None)
                             or getattr(n, "updated_at", None),
                raw={"symbols": symbols,
                     "news_id": getattr(n, "id", None),
                     "source": getattr(n, "source", None)},
            ))
        return out


@register("yfinance")
class YahooFinanceCollector(Collector):
    """
    Per-ticker Yahoo Finance news for the crypto-correlated watchlist.

    yfinance changed its news payload shape (fields moved under `content`),
    which silently broke the previous implementation — both shapes are handled
    here, and price context is written to `market_snapshots`.
    """
    interval = POLL_INTERVALS["yfinance"]

    def __init__(self, http, tickers: list[str] | None = None):
        super().__init__(http)
        self.tickers = tickers or WATCH_TICKERS

    @staticmethod
    def _extract(item: dict, ticker: str) -> Article | None:
        content = item.get("content") or {}

        title = item.get("title") or content.get("title") or ""
        if not title:
            return None

        url = (item.get("link")
               or (content.get("canonicalUrl") or {}).get("url")
               or (content.get("clickThroughUrl") or {}).get("url"))

        summary = item.get("summary") or content.get("summary") or \
            content.get("description") or ""

        published = (item.get("providerPublishTime")
                     or content.get("pubDate")
                     or content.get("displayTime"))

        publisher = (item.get("publisher")
                     or (content.get("provider") or {}).get("displayName")
                     or "Yahoo Finance")

        return Article(
            title=title,
            url=url,
            summary=summary,
            author=publisher,
            source="yfinance",
            source_category="equities",
            source_weight=0.9,
            published_at=published,
            raw={"ticker": ticker, "publisher": publisher,
                 "related": item.get("relatedTickers")},
        )

    def _fetch_sync(self) -> list[Article]:
        try:
            import yfinance as yf
        except ImportError:
            self.log.error("yfinance not installed")
            return []
        from store import record_snapshot

        out: list[Article] = []
        for sym in self.tickers:
            try:
                t = yf.Ticker(sym)
                for item in (t.news or [])[:15]:
                    a = self._extract(item, sym)
                    if a:
                        out.append(a)

                hist = t.history(period="2d", interval="1d")
                if not hist.empty:
                    close = float(hist["Close"].iloc[-1])
                    record_snapshot("yfinance", "close", close, sym, None)
                    record_snapshot("yfinance", "volume",
                                    float(hist["Volume"].iloc[-1]), sym, None)
                    if len(hist) > 1:
                        prev = float(hist["Close"].iloc[-2])
                        if prev:
                            record_snapshot("yfinance", "change_1d_pct",
                                            (close - prev) / prev * 100, sym, None)
            except Exception as exc:
                self.log.warning("%s failed: %s", sym, exc)
        return out

    async def collect(self) -> list[Article]:
        return await asyncio.to_thread(self._fetch_sync)
