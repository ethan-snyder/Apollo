"""
Apollo — historical data backfill for backtesting.

Live collectors only ever see "now". Backtesting needs the past, which comes
from different endpoints entirely:

  Coinbase Exchange  OHLCV candles, keyless, full history      -> ohlcv
  Alpaca News API    Benzinga archive back to 2015, keyed      -> articles
  Alternative.me     Fear & Greed full history, keyless        -> market_snapshots

IMPORTANT — the news constraint:
RSS feeds serve only current items, so there is no way to reconstruct 2025 news
from the live feed list. Alpaca's news API is the only source in this project
with a deep archive, and it requires ALPACA_API_KEY / ALPACA_SECRET_KEY. Without
those keys you can still backfill prices and Fear & Greed, but a news-driven
backtest will have nothing to test. See README for the full caveat list.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, HTTP_CONCURRENCY
from collectors.base import HttpClient
from scoring import score_article
from store import (Article, bulk_upsert, candle_coverage, init_db,
                   record_snapshot, upsert_candles)

logger = logging.getLogger("HISTORICAL")

COINBASE = "https://api.exchange.coinbase.com"
ALPACA_NEWS = "https://data.alpaca.markets/v1beta1/news"
FNG_ALL = "https://api.alternative.me/fng/?limit=0&format=json"

# Coinbase product ids map 1:1 to the pairs this project targets.
SYMBOLS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}

# Coinbase caps each candle request at 300 rows.
CB_MAX_CANDLES = 300


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_day(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

async def backfill_candles(http: HttpClient, symbol: str, start: datetime,
                           end: datetime, granularity: int = 3600) -> int:
    """
    Page through Coinbase candles for one product.

    Coinbase returns rows as [time, low, high, open, close, volume] — note the
    non-obvious ordering; low/high come before open/close.
    """
    total = 0
    step = timedelta(seconds=granularity * CB_MAX_CANDLES)
    cursor = start

    while cursor < end:
        chunk_end = min(cursor + step, end)
        url = (f"{COINBASE}/products/{symbol}/candles"
               f"?granularity={granularity}&start={_iso(cursor)}&end={_iso(chunk_end)}")
        try:
            status, body, _ = await http.get(url)
            if status != 200 or not body:
                logger.warning("%s %s -> HTTP %s", symbol, _iso(cursor), status)
                cursor = chunk_end
                continue
            raw = json.loads(body)
        except Exception as exc:
            logger.error("%s %s failed: %s", symbol, _iso(cursor), exc)
            cursor = chunk_end
            continue

        rows = []
        for c in raw:
            if len(c) < 6:
                continue
            ts, low, high, opn, close, vol = c[0], c[1], c[2], c[3], c[4], c[5]
            rows.append((
                datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"),
                opn, high, low, close, vol,
            ))

        total += await asyncio.to_thread(upsert_candles, symbol, granularity, rows)
        cursor = chunk_end
        # Coinbase public rate limit is ~10 req/s; stay well under it.
        await asyncio.sleep(0.25)

    return total


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

async def backfill_alpaca_news(http: HttpClient, start: datetime, end: datetime,
                               symbols: list[str] | None = None,
                               page_limit: int = 50) -> tuple[int, int]:
    """
    Page through Alpaca's Benzinga news archive.

    Returns (new, duplicate). Alpaca paginates with an opaque `page_token`;
    `page_limit` caps how many pages we'll pull per call so a bad date range
    can't run away.
    """
    if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
        logger.error("ALPACA_API_KEY/SECRET not set — cannot backfill news. "
                     "This is the only deep news archive available; prices and "
                     "Fear & Greed will still backfill.")
        return 0, 0

    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "accept": "application/json",
    }
    sym_param = f"&symbols={','.join(symbols)}" if symbols else ""

    total_new = total_dup = 0
    token: str | None = None
    pages = 0

    while pages < page_limit:
        url = (f"{ALPACA_NEWS}?start={_iso(start)}&end={_iso(end)}"
               f"&limit=50&sort=asc&include_content=true{sym_param}")
        if token:
            url += f"&page_token={token}"

        try:
            status, body, _ = await http.get(url, headers=headers)
            if status == 403:
                logger.error("Alpaca returned 403 — check API keys / news "
                             "entitlement on your account.")
                break
            if status != 200 or not body:
                logger.warning("Alpaca news -> HTTP %s", status)
                break
            data = json.loads(body)
        except Exception as exc:
            logger.error("Alpaca news request failed: %s", exc)
            break

        items = data.get("news", []) or []
        if not items:
            break

        articles = []
        for n in items:
            syms = n.get("symbols") or []
            articles.append(Article(
                title=n.get("headline", "") or "",
                url=n.get("url"),
                summary=n.get("summary"),
                body=n.get("content"),
                author=n.get("author"),
                source="alpaca_hist",
                source_category="equities",
                source_weight=1.2,
                published_at=n.get("created_at") or n.get("updated_at"),
                raw={"symbols": syms, "news_id": n.get("id"),
                     "source": n.get("source"), "_backfill": True},
            ))

        for a in articles:
            score_article(a)
        new, dup = await asyncio.to_thread(bulk_upsert, articles)
        total_new += new
        total_dup += dup

        token = data.get("next_page_token")
        pages += 1
        if not token:
            break
        await asyncio.sleep(0.2)

    logger.info("Alpaca news backfill: %d new, %d duplicate (%d pages)",
                total_new, total_dup, pages)
    return total_new, total_dup


# ---------------------------------------------------------------------------
# Fear & Greed history
# ---------------------------------------------------------------------------

async def backfill_fear_greed(http: HttpClient, start: datetime,
                              end: datetime) -> int:
    """
    Alternative.me serves the full index history with limit=0 — one reading
    per day going back to 2018.
    """
    try:
        data = await http.get_json(FNG_ALL)
    except Exception as exc:
        logger.error("Fear & Greed backfill failed: %s", exc)
        return 0

    n = 0
    for entry in (data or {}).get("data", []):
        try:
            ts = datetime.fromtimestamp(int(entry["timestamp"]), timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        if not (start <= ts <= end):
            continue
        # record_snapshot stamps taken_at with now(), so write the historical
        # timestamp directly instead.
        from store import tx, utcnow  # local import to keep module deps flat
        with tx() as conn:
            conn.execute(
                "INSERT INTO market_snapshots (taken_at, source, metric, asset,"
                " value, detail) VALUES (?,?,?,?,?,?)",
                (ts.isoformat(timespec="seconds"), "alternative.me",
                 "fear_greed", None, float(entry.get("value", 0)),
                 json.dumps({"classification": entry.get("value_classification"),
                             "_backfill": True})),
            )
        n += 1
    logger.info("Fear & Greed backfill: %d daily readings", n)
    return n


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_backfill(start_date: str, end_date: str, *, prices: bool = True,
                       news: bool = True, fng: bool = True,
                       granularities: tuple[int, ...] = (3600, 86400)) -> dict:
    await asyncio.to_thread(init_db)
    start, end = _parse_day(start_date), _parse_day(end_date)
    out: dict = {"start": start_date, "end": end_date}

    async with HttpClient(concurrency=HTTP_CONCURRENCY) as http:
        if prices:
            out["candles"] = {}
            for asset, product in SYMBOLS.items():
                for g in granularities:
                    label = f"{product}@{g}s"
                    logger.info("Backfilling candles %s ...", label)
                    n = await backfill_candles(http, product, start, end, g)
                    out["candles"][label] = n
                    logger.info("  %s -> %d candles", label, n)

        if fng:
            out["fear_greed"] = await backfill_fear_greed(http, start, end)

        if news:
            logger.info("Backfilling news (this is the slow part) ...")
            # Crypto tickers as Alpaca knows them, plus the crypto-proxy
            # equities whose news moves the same assets.
            syms = ["BTCUSD", "ETHUSD", "SOLUSD", "COIN", "MSTR", "MARA",
                    "RIOT", "CLSK", "HOOD"]
            new, dup = await backfill_alpaca_news(
                http, start, end, symbols=syms, page_limit=2000)
            out["news"] = {"new": new, "duplicate": dup}

    return out


def coverage_report() -> dict:
    init_db()
    out = {}
    for asset, product in SYMBOLS.items():
        out[product] = {
            "1h": candle_coverage(product, 3600),
            "1d": candle_coverage(product, 86400),
        }
    return out


def main(start_date: str = "2025-01-01", end_date: str = "2026-01-01",
         **kwargs) -> dict:
    return asyncio.run(run_backfill(start_date, end_date, **kwargs))
