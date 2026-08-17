"""
Apollo — persistence layer.

SQLite with WAL, content-hash dedup, and query helpers for downstream signal
work. Everything a collector produces goes through `Article` -> `upsert_article`.

Dedup strategy (two layers):
  1. url_hash   — canonicalized URL (tracking params stripped, scheme/host
                  normalized). Catches the same story re-syndicated at the
                  same URL by multiple feeds.
  2. title_hash — aggressively normalized headline (lowercased, punctuation and
                  stopwords stripped, whitespace collapsed). Catches wire copy
                  republished by a dozen outlets under different URLs.

An article is a duplicate if *either* hash already exists. When that happens we
record the additional sighting in `article_sources` instead of a new row, so
"how many outlets picked this up" becomes a usable corroboration signal.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from config import DB_PATH

logger = logging.getLogger("STORE")

_LOCAL = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT,
    url_hash        TEXT UNIQUE,
    title_hash      TEXT,
    title           TEXT NOT NULL,
    summary         TEXT,
    body            TEXT,
    author          TEXT,
    source          TEXT NOT NULL,        -- feed/collector key
    source_category TEXT,                 -- crypto|macro|equities|regulator|geopolitical
    source_weight   REAL DEFAULT 1.0,
    published_at    TEXT,                 -- ISO8601 UTC
    ingested_at     TEXT NOT NULL,
    -- scoring output
    assets          TEXT,                 -- JSON list, e.g. ["BTC","MACRO"]
    tickers         TEXT,                 -- JSON list of equity tickers
    sentiment       REAL,                 -- -1.0 (bearish) .. +1.0 (bullish)
    relevance       REAL,                 -- 0.0 .. 1.0
    impact          REAL,                 -- signed, weighted: sentiment * relevance * weight
    score_detail    TEXT,                 -- JSON: matched terms, per-asset breakdown
    corroboration   INTEGER DEFAULT 1,    -- distinct outlets carrying this story
    raw             TEXT                  -- original payload JSON
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_ingested  ON articles(ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source    ON articles(source);
CREATE INDEX IF NOT EXISTS idx_articles_title_h   ON articles(title_hash);
CREATE INDEX IF NOT EXISTS idx_articles_impact    ON articles(impact);

-- Every outlet sighting of a story, including duplicates of an existing row.
CREATE TABLE IF NOT EXISTS article_sources (
    article_id  INTEGER NOT NULL,
    source      TEXT NOT NULL,
    url         TEXT,
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (article_id, source),
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
);

-- Conditional-GET state so we re-download as little as possible.
CREATE TABLE IF NOT EXISTS feed_state (
    key           TEXT PRIMARY KEY,
    url           TEXT,
    etag          TEXT,
    last_modified TEXT,
    last_polled   TEXT,
    last_success  TEXT,
    last_status   TEXT,
    error_streak  INTEGER DEFAULT 0,
    items_seen    INTEGER DEFAULT 0,
    new_items     INTEGER DEFAULT 0
);

-- Point-in-time market context (price/dominance/fear-greed/TVL) so a headline
-- can later be joined against what the market was doing when it landed.
CREATE TABLE IF NOT EXISTS market_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at  TEXT NOT NULL,
    source    TEXT NOT NULL,
    metric    TEXT NOT NULL,
    asset     TEXT,
    value     REAL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_taken ON market_snapshots(taken_at DESC);
CREATE INDEX IF NOT EXISTS idx_snap_metric ON market_snapshots(metric, asset);

-- Rolled-up signal per asset per time bucket, produced by the signal engine.
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at   TEXT NOT NULL,
    asset         TEXT NOT NULL,
    window_min    INTEGER NOT NULL,
    article_count INTEGER,
    net_impact    REAL,
    mean_sentiment REAL,
    direction     TEXT,
    confidence    REAL,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_asset ON signals(asset, computed_at DESC);

-- ---------------------------------------------------------------------------
-- Backtesting and paper trading
-- ---------------------------------------------------------------------------

-- Historical OHLCV candles. granularity is in seconds (3600 = 1h, 86400 = 1d).
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol      TEXT NOT NULL,
    granularity INTEGER NOT NULL,
    ts          TEXT NOT NULL,        -- ISO8601 UTC, candle OPEN time
    open        REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, granularity, ts)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup ON ohlcv(symbol, granularity, ts);

-- One row per backtest execution, with full params and results as JSON so
-- runs stay comparable after the code changes underneath them.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,
    label       TEXT,
    asset       TEXT,
    start_date  TEXT,
    end_date    TEXT,
    params      TEXT,
    results     TEXT
);
CREATE INDEX IF NOT EXISTS idx_backtest_run_at ON backtest_runs(run_at DESC);

-- A paper trading session.
CREATE TABLE IF NOT EXISTS paper_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    status        TEXT,               -- running | completed | stopped | error
    starting_cash REAL NOT NULL,
    ending_equity REAL,
    params        TEXT,
    summary       TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_runs_started ON paper_runs(started_at DESC);

-- Every simulated fill.
CREATE TABLE IF NOT EXISTS paper_trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL,
    ts               TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,   -- BUY | SELL
    qty              REAL,
    price            REAL,
    notional         REAL,
    fee              REAL,
    reason           TEXT,
    signal_direction TEXT,
    net_impact       REAL,
    confidence       REAL,
    cash_after       REAL,
    position_after   REAL,
    realized_pnl     REAL,
    FOREIGN KEY (run_id) REFERENCES paper_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_run ON paper_trades(run_id, ts);

-- Equity curve samples, one per evaluation tick.
CREATE TABLE IF NOT EXISTS paper_equity (
    run_id    INTEGER NOT NULL,
    ts        TEXT NOT NULL,
    equity    REAL,
    cash      REAL,
    positions TEXT,                   -- JSON {symbol: {qty, price, value}}
    PRIMARY KEY (run_id, ts),
    FOREIGN KEY (run_id) REFERENCES paper_runs(id) ON DELETE CASCADE
);
"""

_TRACKING_PARAMS = re.compile(
    r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|source|cmpid|ncid|__twitter|"
    r"guccounter|guce_|oc$|smid|partner|sh$|taid|itm_|ito$|at_)",
    re.I,
)

_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "as", "at", "by", "from", "is", "are", "was", "were", "be", "been", "it",
    "its", "this", "that", "these", "those", "s", "will", "has", "have", "had",
    "after", "over", "amid", "says", "say", "said", "new", "up", "down",
}


# ---------------------------------------------------------------------------
# Normalization / hashing
# ---------------------------------------------------------------------------

def canonical_url(url: str | None) -> str:
    """Strip tracking params, fragments, trailing slashes, www., and scheme case."""
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        host = (p.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        # Google News wraps the real article; the query string is the only
        # stable identity we get, so keep it rather than stripping to nothing.
        query = [
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
            if not _TRACKING_PARAMS.match(k)
        ]
        path = p.path.rstrip("/") or "/"
        return urlunparse(("https", host, path, "", urlencode(sorted(query)), ""))
    except Exception:
        return url.strip()


def normalize_title(title: str) -> str:
    """Aggressive headline normalization for near-duplicate detection."""
    t = title.lower()
    t = re.sub(r"[‘’“”]", "'", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    words = [w for w in t.split() if w and w not in _STOPWORDS]
    # Numbers are dropped last so "bitcoin falls to 63000" and
    # "bitcoin falls to 63,000" collapse to the same key.
    return " ".join(words)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(value: Any) -> str | None:
    """Coerce assorted timestamp shapes into ISO8601 UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, (int, float)):
        # Heuristic: values past year 2286 in seconds are actually millis.
        v = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(v, timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(s)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Article model
# ---------------------------------------------------------------------------

@dataclass
class Article:
    title: str
    source: str
    url: str | None = None
    summary: str | None = None
    body: str | None = None
    author: str | None = None
    source_category: str | None = None
    source_weight: float = 1.0
    published_at: str | None = None
    raw: dict = field(default_factory=dict)
    # Filled in by scoring.py before insert.
    assets: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    sentiment: float = 0.0
    relevance: float = 0.0
    impact: float = 0.0
    score_detail: dict = field(default_factory=dict)

    def __post_init__(self):
        self.title = (self.title or "").strip()
        self.published_at = to_iso(self.published_at)

    @property
    def url_hash(self) -> str:
        cu = canonical_url(self.url)
        # Untitled+URL-less items would all collide, so fall back to the title.
        return _sha(cu) if cu else _sha(f"{self.source}|{self.title}")

    @property
    def title_hash(self) -> str:
        return _sha(normalize_title(self.title))


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per thread; asyncio collectors all share the main thread."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = _connect()
        _LOCAL.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    logger.info("Database ready at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def upsert_article(a: Article) -> tuple[int | None, bool]:
    """
    Insert an article, or record a new sighting if it's a duplicate.

    Returns (article_id, is_new).
    """
    if not a.title:
        return None, False

    uh, th = a.url_hash, a.title_hash
    now = utcnow()

    with tx() as conn:
        row = conn.execute(
            "SELECT id FROM articles WHERE url_hash = ? OR title_hash = ? LIMIT 1",
            (uh, th),
        ).fetchone()

        if row:
            aid = row["id"]
            cur = conn.execute(
                "INSERT OR IGNORE INTO article_sources (article_id, source, url, seen_at)"
                " VALUES (?, ?, ?, ?)",
                (aid, a.source, a.url, now),
            )
            if cur.rowcount:
                # A genuinely new outlet picked this up — corroboration rises.
                conn.execute(
                    "UPDATE articles SET corroboration = corroboration + 1 WHERE id = ?",
                    (aid,),
                )
            return aid, False

        cur = conn.execute(
            """INSERT INTO articles
               (url, url_hash, title_hash, title, summary, body, author, source,
                source_category, source_weight, published_at, ingested_at,
                assets, tickers, sentiment, relevance, impact, score_detail,
                corroboration, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                a.url, uh, th, a.title, a.summary, a.body, a.author, a.source,
                a.source_category, a.source_weight, a.published_at or now, now,
                json.dumps(a.assets), json.dumps(a.tickers), a.sentiment,
                a.relevance, a.impact, json.dumps(a.score_detail), 1,
                json.dumps(a.raw, default=str)[:200_000],
            ),
        )
        aid = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO article_sources (article_id, source, url, seen_at)"
            " VALUES (?, ?, ?, ?)",
            (aid, a.source, a.url, now),
        )
        return aid, True


def bulk_upsert(articles: Iterable[Article]) -> tuple[int, int]:
    """Returns (new_count, duplicate_count)."""
    new = dup = 0
    for a in articles:
        try:
            _, is_new = upsert_article(a)
            new += is_new
            dup += not is_new
        except Exception as exc:
            logger.error("Failed to store article %r from %s: %s", a.title[:80], a.source, exc)
    return new, dup


def record_snapshot(source: str, metric: str, value: float | None,
                    asset: str | None = None, detail: dict | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO market_snapshots (taken_at, source, metric, asset, value, detail)"
            " VALUES (?,?,?,?,?,?)",
            (utcnow(), source, metric, asset, value,
             json.dumps(detail, default=str) if detail else None),
        )


def record_signal(asset: str, window_min: int, article_count: int, net_impact: float,
                  mean_sentiment: float, direction: str, confidence: float,
                  detail: dict | None = None) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO signals (computed_at, asset, window_min, article_count,
               net_impact, mean_sentiment, direction, confidence, detail)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (utcnow(), asset, window_min, article_count, net_impact, mean_sentiment,
             direction, confidence, json.dumps(detail, default=str) if detail else None),
        )


# ---------------------------------------------------------------------------
# Feed state (conditional GET + health)
# ---------------------------------------------------------------------------

def get_feed_state(key: str) -> sqlite3.Row | None:
    return get_conn().execute("SELECT * FROM feed_state WHERE key = ?", (key,)).fetchone()


def save_feed_state(key: str, url: str, etag: str | None = None,
                    last_modified: str | None = None, status: str = "ok",
                    items_seen: int = 0, new_items: int = 0) -> None:
    now = utcnow()
    ok = status == "ok"
    with tx() as conn:
        conn.execute(
            """INSERT INTO feed_state
                 (key, url, etag, last_modified, last_polled, last_success,
                  last_status, error_streak, items_seen, new_items)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 url=excluded.url,
                 etag=COALESCE(excluded.etag, feed_state.etag),
                 last_modified=COALESCE(excluded.last_modified, feed_state.last_modified),
                 last_polled=excluded.last_polled,
                 last_success=CASE WHEN ? THEN excluded.last_polled ELSE feed_state.last_success END,
                 last_status=excluded.last_status,
                 error_streak=CASE WHEN ? THEN 0 ELSE feed_state.error_streak + 1 END,
                 items_seen=feed_state.items_seen + excluded.items_seen,
                 new_items=feed_state.new_items + excluded.new_items""",
            (key, url, etag, last_modified, now, now if ok else None, status,
             0 if ok else 1, items_seen, new_items, ok, ok),
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def recent_articles(limit: int = 50, asset: str | None = None,
                    minutes: int | None = None, min_abs_impact: float = 0.0,
                    source_category: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM articles WHERE 1=1"
    params: list[Any] = []
    if asset:
        sql += " AND assets LIKE ?"
        params.append(f'%"{asset}"%')
    if minutes:
        sql += " AND published_at >= datetime('now', ?)"
        params.append(f"-{int(minutes)} minutes")
    if min_abs_impact:
        sql += " AND ABS(impact) >= ?"
        params.append(min_abs_impact)
    if source_category:
        sql += " AND source_category = ?"
        params.append(source_category)
    sql += " ORDER BY published_at DESC LIMIT ?"
    params.append(limit)
    return get_conn().execute(sql, params).fetchall()


def articles_for_signal(asset: str, minutes: int) -> list[sqlite3.Row]:
    return get_conn().execute(
        """SELECT * FROM articles
           WHERE assets LIKE ? AND published_at >= datetime('now', ?)
           ORDER BY published_at DESC""",
        (f'%"{asset}"%', f"-{int(minutes)} minutes"),
    ).fetchall()


def latest_signals(limit: int = 20) -> list[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM signals ORDER BY computed_at DESC LIMIT ?", (limit,)
    ).fetchall()


def latest_snapshot(metric: str, asset: str | None = None) -> sqlite3.Row | None:
    sql = "SELECT * FROM market_snapshots WHERE metric = ?"
    params: list[Any] = [metric]
    if asset:
        sql += " AND asset = ?"
        params.append(asset)
    sql += " ORDER BY taken_at DESC LIMIT 1"
    return get_conn().execute(sql, params).fetchone()


def stats() -> dict:
    conn = get_conn()
    out: dict[str, Any] = {}
    out["total_articles"] = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
    out["last_24h"] = conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE ingested_at >= datetime('now','-1 day')"
    ).fetchone()["c"]
    out["last_hour"] = conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE ingested_at >= datetime('now','-1 hour')"
    ).fetchone()["c"]
    from config import ASSETS
    out["by_asset"] = {}
    for asset in ASSETS:
        out["by_asset"][asset] = conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE assets LIKE ?", (f'%"{asset}"%',)
        ).fetchone()["c"]
    out["top_sources"] = [
        dict(r) for r in conn.execute(
            "SELECT source, COUNT(*) c FROM articles GROUP BY source ORDER BY c DESC LIMIT 12"
        ).fetchall()
    ]
    out["feeds"] = [
        dict(r) for r in conn.execute(
            "SELECT key, last_status, error_streak, items_seen, new_items, last_success"
            " FROM feed_state ORDER BY error_streak DESC, new_items DESC"
        ).fetchall()
    ]
    return out


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------

def upsert_candles(symbol: str, granularity: int, rows: Iterable[tuple]) -> int:
    """
    rows: (ts_iso, open, high, low, close, volume).

    INSERT OR REPLACE so re-running a backfill over the same range is safe and
    idempotent (exchanges occasionally revise recent candles).
    """
    rows = list(rows)
    if not rows:
        return 0
    with tx() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO ohlcv (symbol, granularity, ts, open, high,"
            " low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
            [(symbol, granularity, *r) for r in rows],
        )
    return len(rows)


def get_candles(symbol: str, granularity: int, start: str | None = None,
                end: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM ohlcv WHERE symbol = ? AND granularity = ?"
    params: list[Any] = [symbol, granularity]
    if start:
        sql += " AND ts >= ?"
        params.append(start)
    if end:
        sql += " AND ts <= ?"
        params.append(end)
    sql += " ORDER BY ts"
    return get_conn().execute(sql, params).fetchall()


def candle_coverage(symbol: str, granularity: int) -> dict:
    row = get_conn().execute(
        "SELECT COUNT(*) n, MIN(ts) lo, MAX(ts) hi FROM ohlcv"
        " WHERE symbol = ? AND granularity = ?", (symbol, granularity)
    ).fetchone()
    return {"count": row["n"], "start": row["lo"], "end": row["hi"]}


# ---------------------------------------------------------------------------
# Backtest / paper run persistence
# ---------------------------------------------------------------------------

def save_backtest(label: str, asset: str, start_date: str, end_date: str,
                  params: dict, results: dict) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO backtest_runs (run_at, label, asset, start_date,"
            " end_date, params, results) VALUES (?,?,?,?,?,?,?)",
            (utcnow(), label, asset, start_date, end_date,
             json.dumps(params, default=str), json.dumps(results, default=str)),
        )
        return cur.lastrowid


def list_backtests(limit: int = 20) -> list[sqlite3.Row]:
    return get_conn().execute(
        "SELECT id, run_at, label, asset, start_date, end_date FROM backtest_runs"
        " ORDER BY run_at DESC LIMIT ?", (limit,)
    ).fetchall()


def get_backtest(run_id: int) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()


def create_paper_run(label: str, starting_cash: float, params: dict) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO paper_runs (label, started_at, status, starting_cash,"
            " params) VALUES (?,?,?,?,?)",
            (label, utcnow(), "running", starting_cash,
             json.dumps(params, default=str)),
        )
        return cur.lastrowid


def finish_paper_run(run_id: int, status: str, ending_equity: float,
                     summary: dict) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE paper_runs SET ended_at = ?, status = ?, ending_equity = ?,"
            " summary = ? WHERE id = ?",
            (utcnow(), status, ending_equity,
             json.dumps(summary, default=str), run_id),
        )


def record_trade(run_id: int, ts: str, symbol: str, side: str, qty: float,
                 price: float, fee: float, reason: str, signal_direction: str,
                 net_impact: float, confidence: float, cash_after: float,
                 position_after: float, realized_pnl: float = 0.0) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO paper_trades (run_id, ts, symbol, side, qty, price,
               notional, fee, reason, signal_direction, net_impact, confidence,
               cash_after, position_after, realized_pnl)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, ts, symbol, side, qty, price, qty * price, fee, reason,
             signal_direction, net_impact, confidence, cash_after,
             position_after, realized_pnl),
        )


def record_equity(run_id: int, ts: str, equity: float, cash: float,
                  positions: dict) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO paper_equity (run_id, ts, equity, cash,"
            " positions) VALUES (?,?,?,?,?)",
            (run_id, ts, equity, cash, json.dumps(positions, default=str)),
        )


def list_paper_runs(limit: int = 20) -> list[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM paper_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()


def get_paper_run(run_id: int) -> dict:
    conn = get_conn()
    run = conn.execute("SELECT * FROM paper_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        return {}
    return {
        "run": dict(run),
        "trades": [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades WHERE run_id = ? ORDER BY ts", (run_id,))],
        "equity": [dict(r) for r in conn.execute(
            "SELECT * FROM paper_equity WHERE run_id = ? ORDER BY ts", (run_id,))],
    }


def export_jsonl(path: str, minutes: int | None = None) -> int:
    """Dump articles to JSONL for offline analysis / model training."""
    sql = "SELECT * FROM articles"
    params: list[Any] = []
    if minutes:
        sql += " WHERE ingested_at >= datetime('now', ?)"
        params.append(f"-{int(minutes)} minutes")
    sql += " ORDER BY published_at DESC"
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in get_conn().execute(sql, params):
            d = dict(row)
            for k in ("assets", "tickers", "score_detail", "raw"):
                if d.get(k):
                    try:
                        d[k] = json.loads(d[k])
                    except (json.JSONDecodeError, TypeError):
                        pass
            f.write(json.dumps(d, default=str) + "\n")
            n += 1
    return n
