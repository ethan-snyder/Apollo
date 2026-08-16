"""
Apollo — news aggregation and crypto signal terminal.

Usage:
    python main.py                  interactive menu
    python main.py run              start the collector daemon (Ctrl+C to stop)
    python main.py once             single collection pass across all sources
    python main.py signals          print current signals
    python main.py feeds            feed health check
    python main.py top [asset]      highest-impact recent articles
    python main.py stats            database statistics
    python main.py export [file]    dump articles to JSONL
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime

from config import ASSETS, CRYPTO_ASSETS, LOG_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
# Third-party libraries are noisy at INFO.
for noisy in ("yfinance", "peewee", "urllib3", "asyncio", "aiohttp", "websockets"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("MAIN")

import daemon                                    # noqa: E402
import store                                     # noqa: E402
from collectors.base import HttpClient           # noqa: E402
from collectors.rss import check_feed_health     # noqa: E402
from collectors.government import fetch_company_filings  # noqa: E402
from scoring import score_article                # noqa: E402

BAR = "=" * 68


def _arrow(direction: str) -> str:
    return {
        "STRONG_BULLISH": "^^", "BULLISH": "^ ",
        "NEUTRAL": "= ", "BEARISH": "v ", "STRONG_BEARISH": "vv",
    }.get(direction, "? ")


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def show_signals() -> None:
    store.init_db()
    print(f"\n{BAR}\n  CURRENT SIGNALS\n{BAR}")

    rows = store.get_conn().execute(
        """SELECT s.* FROM signals s
           JOIN (SELECT asset, window_min, MAX(computed_at) mx
                 FROM signals GROUP BY asset, window_min) l
             ON s.asset = l.asset AND s.window_min = l.window_min
            AND s.computed_at = l.mx
           ORDER BY s.asset, s.window_min"""
    ).fetchall()

    if not rows:
        print("\n  No signals computed yet. Run 'python main.py once' first.\n")
        return

    by_asset: dict[str, list] = {}
    for r in rows:
        by_asset.setdefault(r["asset"], []).append(r)

    for asset, sigs in by_asset.items():
        pair = ASSETS.get(asset, {}).get("pair") or "market-wide"
        print(f"\n  {asset}  ({pair})")
        print(f"  {'window':>8} {'dir':<16} {'net impact':>11} {'conf':>6} {'articles':>9}")
        print("  " + "-" * 56)
        for s in sigs:
            w = s["window_min"]
            label = f"{w}m" if w < 60 else f"{w // 60}h"
            print(f"  {label:>8} {_arrow(s['direction'])} {s['direction']:<13}"
                  f" {s['net_impact']:>+11.3f} {s['confidence']:>6.2f}"
                  f" {s['article_count']:>9}")

        hour = next((s for s in sigs if s["window_min"] == 60), None)
        if hour and hour["detail"]:
            import json
            drivers = json.loads(hour["detail"]).get("top_drivers", [])
            if drivers:
                print("    top drivers (1h):")
                for d in drivers[:3]:
                    print(f"      {d['impact']:+.2f}  {d['title'][:62]}  [{d['source']}]")
    print()


def show_top(asset: str | None = None, limit: int = 20) -> None:
    store.init_db()
    rows = store.recent_articles(limit=limit, asset=asset, minutes=1440,
                                 min_abs_impact=0.05)
    label = asset or "ALL ASSETS"
    print(f"\n{BAR}\n  HIGHEST-IMPACT ARTICLES (24h) — {label}\n{BAR}\n")
    if not rows:
        print("  Nothing scored yet in this window.\n")
        return
    for r in sorted(rows, key=lambda x: -abs(x["impact"] or 0)):
        import json
        assets = ", ".join(json.loads(r["assets"] or "[]")) or "-"
        when = (r["published_at"] or "")[:16].replace("T", " ")
        print(f"  {r['impact']:+.3f}  [{assets:<12}] {when}  {r['source']}")
        print(f"          {r['title'][:88]}")
        if r["corroboration"] > 1:
            print(f"          corroborated by {r['corroboration']} outlets")
        print()


def show_stats() -> None:
    store.init_db()
    s = store.stats()
    print(f"\n{BAR}\n  DATABASE STATISTICS\n{BAR}")
    print(f"\n  Total articles : {s['total_articles']:,}")
    print(f"  Last 24 hours  : {s['last_24h']:,}")
    print(f"  Last hour      : {s['last_hour']:,}")

    print("\n  By asset:")
    for asset, n in s["by_asset"].items():
        print(f"    {asset:<8} {n:>7,}")

    print("\n  Top sources:")
    for row in s["top_sources"]:
        print(f"    {row['source']:<28} {row['c']:>7,}")

    broken = [f for f in s["feeds"] if (f["error_streak"] or 0) >= 3]
    if broken:
        print(f"\n  Feeds erroring (streak >= 3): {len(broken)}")
        for f in broken[:15]:
            print(f"    {f['key']:<24} {f['last_status']} (x{f['error_streak']})")

    for metric, asset in (("price_usd", "BTC"), ("price_usd", "ETH"),
                          ("price_usd", "SOL"), ("fear_greed", None)):
        snap = store.latest_snapshot(metric, asset)
        if snap:
            tag = f"{asset} {metric}" if asset else metric
            print(f"\n  {tag:<22} {snap['value']:,.2f}   ({snap['taken_at'][:16]})")
    print()


def check_feeds() -> None:
    from config import FEEDS

    async def run() -> list[dict]:
        async with HttpClient(concurrency=16) as http:
            return await check_feed_health(http)

    print(f"\n{BAR}\n  FEED HEALTH CHECK ({len(FEEDS)} feeds)\n{BAR}\n")
    results = asyncio.run(run())
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]

    for r in sorted(ok, key=lambda x: -x["items"]):
        print(f"  ok    {r['key']:<24} {r['items']:>4} items")
    if bad:
        print()
        for r in bad:
            print(f"  FAIL  {r['key']:<24} {r['status']}")
            print(f"        {r['url']}")
    print(f"\n  {len(ok)}/{len(results)} feeds healthy\n")


def lookup_ticker(ticker: str) -> None:
    async def run() -> list:
        async with HttpClient() as http:
            return await fetch_company_filings(http, ticker)

    store.init_db()
    articles = asyncio.run(run())
    if not articles:
        print(f"\n  No SEC filings found for {ticker}.\n")
        return
    for a in articles:
        score_article(a)
    new, dup = store.bulk_upsert(articles)
    print(f"\n{BAR}\n  SEC FILINGS — {ticker}  ({new} new, {dup} already stored)\n{BAR}\n")
    for a in articles:
        print(f"  {a.published_at[:10]}  {a.title[:80]}")
        print(f"              {a.url}")
    print()


def collect_once() -> None:
    print("\n  Running one collection pass across all sources...\n")
    results = asyncio.run(daemon.run_once())
    print(f"\n{BAR}\n  COLLECTION RESULTS\n{BAR}\n")
    total_new = total_dup = 0
    for name, (new, dup) in sorted(results.items(), key=lambda kv: -kv[1][0]):
        total_new += new
        total_dup += dup
        print(f"  {name:<20} {new:>5} new  {dup:>5} duplicate")
    print(f"\n  TOTAL: {total_new} new, {total_dup} duplicates suppressed\n")


def export(path: str = "apollo_export.jsonl") -> None:
    store.init_db()
    n = store.export_jsonl(path)
    print(f"\n  Exported {n:,} articles to {path}\n")


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

MENU = f"""
{BAR}
  APOLLO — NEWS AGGREGATION & CRYPTO SIGNAL TERMINAL
{BAR}
  1. Start collector daemon (continuous, all sources)
  2. Run one collection pass now
  3. View current signals (BTC / ETH / SOL / MACRO)
  4. View highest-impact articles
  5. Feed health check
  6. Database statistics
  7. SEC filings lookup by ticker
  8. Export articles to JSONL
  9. Exit
{'-' * 68}"""


def menu() -> None:
    while True:
        print(MENU)
        choice = input("  Select an option (1-9): ").strip()

        if choice == "1":
            logger.info("User started the collector daemon.")
            print("\n  Starting daemon — Ctrl+C to stop and return to the menu.\n")
            try:
                daemon.main()
            except KeyboardInterrupt:
                print("\n  Daemon stopped.")
        elif choice == "2":
            collect_once()
        elif choice == "3":
            show_signals()
        elif choice == "4":
            a = input("  Asset (BTC/ETH/SOL/MACRO, blank for all): ").strip().upper()
            show_top(a if a in ASSETS else None)
        elif choice == "5":
            check_feeds()
        elif choice == "6":
            show_stats()
        elif choice == "7":
            t = input("  Ticker (e.g. COIN): ").strip().upper()
            if t:
                lookup_ticker(t)
        elif choice == "8":
            p = input("  Output path [apollo_export.jsonl]: ").strip()
            export(p or "apollo_export.jsonl")
        elif choice == "9":
            logger.info("User exited the application.")
            print("  Goodbye.\n")
            break
        else:
            print("  Invalid selection.")


def cli() -> None:
    if len(sys.argv) < 2:
        menu()
        return

    cmd = sys.argv[1].lower()
    arg = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "run":
        only = arg.split(",") if arg else None
        daemon.main(only=only)
    elif cmd == "once":
        collect_once()
    elif cmd == "signals":
        show_signals()
    elif cmd == "feeds":
        check_feeds()
    elif cmd == "top":
        show_top(arg.upper() if arg else None)
    elif cmd == "stats":
        show_stats()
    elif cmd == "sec":
        lookup_ticker((arg or "").upper())
    elif cmd == "export":
        export(arg or "apollo_export.jsonl")
    else:
        print(__doc__)


if __name__ == "__main__":
    cli()
