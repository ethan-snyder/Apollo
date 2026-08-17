"""
Apollo — news aggregation and crypto signal terminal.

Usage:
    python main.py                  Apollo terminal (interactive shell)
    python main.py menu             legacy numbered menu
    python main.py run              start the collector daemon (Ctrl+C to stop)
    python main.py once             single collection pass across all sources
    python main.py signals          print current signals
    python main.py feeds            feed health check
    python main.py top [asset]      highest-impact recent articles
    python main.py stats            database statistics
    python main.py export [file]    dump articles to JSONL

Backtesting & paper trading:
    python main.py backfill [start] [end]     historical prices + news
    python main.py coverage                   what history is loaded
    python main.py backtest [asset] [start] [end]
    python main.py events [asset] [start] [end]   event study (CAR by category)
    python main.py validate [asset] [start] [end] lead-lag + null controls
    python main.py leadlag [asset] [start] [end]  lead-lag only, by lexicon half
    python main.py controls [start] [end]     fetch non-crypto control series
    python main.py backtests                  list saved backtest runs
    python main.py paper [duration]           paper trade (default 1h, $10k)
    python main.py papers                     list saved paper runs
    python main.py paperrun <id>              detail for one paper run
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
# Backtesting & paper trading
# ---------------------------------------------------------------------------

def do_backfill(start: str = "2025-01-01", end: str = "2026-01-01") -> None:
    import historical

    print(f"\n{BAR}\n  HISTORICAL BACKFILL  {start} .. {end}\n{BAR}\n")
    if not (config_has_alpaca()):
        print("  NOTE: ALPACA_API_KEY/SECRET not set in .env.")
        print("        Prices and Fear & Greed will backfill, but news will NOT.")
        print("        Alpaca is the only deep news archive wired up — without")
        print("        it a news-driven backtest has nothing to test.\n")

    res = historical.main(start, end)
    print(f"\n{BAR}\n  BACKFILL COMPLETE\n{BAR}\n")
    for label, n in (res.get("candles") or {}).items():
        print(f"  candles  {label:<18} {n:>8,}")
    if "fear_greed" in res:
        print(f"  fear&greed{'':<18} {res['fear_greed']:>7,} readings")
    if "news" in res:
        print(f"  news     {'new':<18} {res['news']['new']:>8,}")
        print(f"  news     {'duplicate':<18} {res['news']['duplicate']:>8,}")
    print()


def config_has_alpaca() -> bool:
    from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
    return bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)


def show_coverage() -> None:
    import historical

    cov = historical.coverage_report()
    store.init_db()
    print(f"\n{BAR}\n  HISTORICAL DATA COVERAGE\n{BAR}\n")
    for product, gran in cov.items():
        print(f"  {product}")
        for g, info in gran.items():
            if info["count"]:
                print(f"    {g:<4} {info['count']:>7,} candles   "
                      f"{info['start'][:10]} .. {info['end'][:10]}")
            else:
                print(f"    {g:<4} {'(none)':>7}")

    row = store.get_conn().execute(
        "SELECT COUNT(*) c, MIN(published_at) lo, MAX(published_at) hi"
        " FROM articles WHERE published_at IS NOT NULL").fetchone()
    print(f"\n  Articles: {row['c']:,}")
    if row["c"]:
        print(f"    {str(row['lo'])[:10]} .. {str(row['hi'])[:10]}")
    print()


def do_backtest(asset: str = "BTC", start: str = "2025-01-01",
                end: str = "2026-01-01") -> None:
    import backtest

    print(f"\n  Backtesting {asset.upper()}  {start} .. {end} ...\n")
    res = backtest.run(asset=asset, start_date=start, end_date=end)

    if "error" in res:
        print(f"  ERROR: {res['error']}\n")
        return

    m = res["metrics"]
    print(f"{BAR}\n  BACKTEST — {res['asset']} ({res['symbol']})  "
          f"{start} .. {end}\n{BAR}\n")
    print(f"  Bars                {m['bars_total']:,}")
    print(f"  Bars with news      {m['bars_with_news']:,} "
          f"({m['news_coverage_pct']}%)")
    print(f"  Articles loaded     {res['articles_loaded']:,}")

    if m.get("warning"):
        print(f"\n  WARNING: {m['warning']}\n")
        return

    print(f"\n  PREDICTIVE POWER BY HORIZON")
    print(f"  {'horizon':>8} {'n':>7} {'IC':>8} {'hit%':>7} "
          f"{'signal ret%':>12} {'t-stat':>8} {'L/S spread%':>12}")
    print("  " + "-" * 68)
    for h, d in m["horizons"].items():
        if d.get("note"):
            print(f"  {h:>8} {d['n']:>7}  {d['note']}")
            continue
        print(f"  {h:>8} {d['n']:>7} {d['information_coefficient']:>8.4f} "
              f"{(d['hit_rate_pct'] or 0):>7.2f} "
              f"{(d['signal_following_mean_pct'] or 0):>12.4f} "
              f"{(d['signal_following_tstat'] or 0):>8.3f} "
              f"{(d['long_short_decile_spread_pct'] or 0):>12.4f}")

    first = next(iter(m["horizons"].values()), {})
    if first.get("deciles"):
        print(f"\n  SIGNAL DECILES ({next(iter(m['horizons']))} forward return)")
        print(f"  {'decile':>7} {'n':>6} {'mean signal':>13} {'mean fwd ret%':>15}")
        print("  " + "-" * 45)
        for d in first["deciles"]:
            print(f"  {d['decile']:>7} {d['n']:>6} {d['mean_signal']:>13.4f} "
                  f"{d['mean_fwd_return_pct']:>15.4f}")

    if first.get("by_direction"):
        print(f"\n  BY SIGNAL DIRECTION")
        print(f"  {'direction':<18} {'n':>7} {'mean fwd ret%':>15} {'win%':>8}")
        print("  " + "-" * 52)
        for name, d in first["by_direction"].items():
            print(f"  {name:<18} {d['n']:>7} {d['mean_fwd_return_pct']:>15.4f} "
                  f"{d['win_rate_pct']:>8.2f}")

    s = res["strategy"]
    if "error" not in s:
        print(f"\n  STRATEGY SIMULATION (fees included)")
        print(f"    Starting cash       ${s['starting_cash']:>12,.2f}")
        print(f"    Ending equity       ${s['ending_equity']:>12,.2f}  "
              f"({s['return_pct']:+.2f}%)")
        print(f"    Buy & hold          ${s['buy_hold_equity']:>12,.2f}  "
              f"({s['buy_hold_return_pct']:+.2f}%)")
        print(f"    Excess vs B&H        {s['excess_vs_buy_hold_pct']:>+12.2f}%")
        print(f"    Trades               {s['trades']:>12,}")
        print(f"    Max drawdown         {s['max_drawdown_pct']:>+12.2f}%")
        if s.get("sharpe_annualized") is not None:
            print(f"    Sharpe (annualized)  {s['sharpe_annualized']:>12.3f}")

    print(f"\n  Saved as backtest run #{res.get('run_id')}\n")


def do_event_study(asset: str = "BTC", start: str = "2025-01-01",
                   end: str = "2026-01-01") -> None:
    import event_study

    print(f"\n  Event study — {asset.upper()}  {start} .. {end} ...\n")
    res = event_study.run_study(asset=asset, start_date=start, end_date=end)

    if "error" in res:
        print(f"  ERROR: {res['error']}\n")
        return

    w = res["window"]
    print(f"{BAR}\n  EVENT STUDY — {res['asset']} ({res['symbol']})  "
          f"{start} .. {end}\n{BAR}\n")
    print(f"  Event window        -{w['pre_bars']}h .. +{w['post_bars']}h")
    print(f"  Baseline            {w['estimation_bars']}h ending "
          f"{w['estimation_gap_bars']}h before each event")
    print(f"  Events detected     {res['events_detected']:,}")
    print(f"  After dedupe        {res['events_after_dedupe']:,}")
    print(f"  Measured            {res['events_measured']:,}")

    o = res["overall"]
    if o.get("signed_by_prior_mean_pct") is not None:
        print(f"\n  OVERALL (trading each event in its expected direction)")
        print(f"    Mean abnormal return  {o['signed_by_prior_mean_pct']:>+8.4f}%")
        print(f"    t-statistic           {o['signed_by_prior_tstat']:>8.3f}")
        print(f"    Events                {o['n_signed']:>8,}")

    print(f"\n  BY EVENT CATEGORY  (CAR = cumulative abnormal return, post-event)")
    print(f"  {'category':<24} {'n':>4} {'exp':>4} {'CAR%':>9} {'t':>7} "
          f"{'pos%':>6} {'prior✓':>7} {'lex✓':>6}")
    print("  " + "-" * 76)

    rows = [(k, v) for k, v in res["categories"].items() if not v.get("note")]
    rows.sort(key=lambda kv: -abs(kv[1]["tstat_car_post"]))
    for cat, d in rows:
        exp = {1: "bull", -1: "bear", 0: "—"}.get(d["expected_direction"], "?")
        prior = (f"{d['direction_agreement_pct']:.0f}%"
                 if d["direction_agreement_pct"] is not None else "  —")
        lex = (f"{d['lexicon_agreement_pct']:.0f}%"
               if d["lexicon_agreement_pct"] is not None else "  —")
        flag = " *" if abs(d["tstat_car_post"]) >= 2 else ""
        print(f"  {cat:<24} {d['n']:>4} {exp:>4} {d['mean_car_post_pct']:>+9.3f} "
              f"{d['tstat_car_post']:>7.2f} {d['pct_positive']:>5.0f}% "
              f"{prior:>7} {lex:>6}{flag}")

    skipped = [(k, v) for k, v in res["categories"].items() if v.get("note")]
    if skipped:
        print(f"\n  Too few events to report: "
              f"{', '.join(f'{k}({v[chr(110)]})' for k, v in skipped)}")

    print(f"\n  * = |t| >= 2 (conventionally significant)")

    if rows:
        top = rows[0]
        print(f"\n  STRONGEST CATEGORY — {top[0]}")
        print(f"    Mean abnormal return by hour after event:")
        aar = top[1]["aar_by_bar_pct"]
        pre_n = w["pre_bars"]
        line = "      "
        for i, v in enumerate(aar[:16]):
            tag = f"{i - pre_n:+d}h"
            line += f"{tag}:{v:+.3f}  "
            if (i + 1) % 4 == 0:
                print(line)
                line = "      "
        if line.strip():
            print(line)
        print(f"\n    Largest moves:")
        for ex in top[1]["examples"]:
            print(f"      {ex['car_post_pct']:>+8.3f}%  {ex['ts']}  "
                  f"{ex['title'][:60]}")

    print(f"\n  Saved as run #{res.get('run_id')}\n")


def _leadlag_curve(res: dict, indent: str = "    ",
                   scale: float | None = None) -> None:
    """
    One row per lag, with a bar so the shape of the curve is visible.

    `scale` is the |IC| that fills the bar. When several curves are printed
    together they must share one scale, or a curve whose peak is 0.04 draws
    the same full-width bar as one whose peak is 0.45 and the comparison the
    panels exist for becomes actively misleading.
    """
    scored = [l for l in res["lags"] if l.get("ic") is not None]
    if not scored:
        print(f"{indent}(insufficient data at every lag)")
        return
    peak = scale or max(abs(l["ic"]) for l in scored) or 1.0
    print(f"{indent}{'lag':>5} {'n':>7} {'IC':>9} {'t':>7}   "
          f"correlation (full bar = {peak:.3f})")
    print(f"{indent}" + "-" * 62)
    for l in res["lags"]:
        if l.get("ic") is None:
            print(f"{indent}{l['lag']:>+5} {l['n']:>7}   {l.get('note', '')}")
            continue
        cells = min(18, int(round(abs(l["ic"]) / peak * 18)))
        bar = ("#" if l["ic"] >= 0 else "-") * cells
        mark = "  <- peak" if l["lag"] == res["peak_lag"] else ""
        print(f"{indent}{l['lag']:>+5} {l['n']:>7} {l['ic']:>+9.4f} "
              f"{l['tstat']:>+7.2f}   {bar}{mark}")


def do_leadlag(asset: str = "BTC", start: str = "2025-01-01",
               end: str = "2026-01-01") -> None:
    import validation

    print(f"\n  Lead-lag — {asset.upper()}  {start} .. {end} ...\n")
    res = validation.lexicon_split(asset, start, end)
    if "error" in res:
        print(f"  ERROR: {res['error']}\n")
        return

    full = res["subsets"]["all"]
    print(f"{BAR}\n  LEAD-LAG — {full['asset']} ({full['symbol']})  "
          f"{start} .. {end}\n{BAR}\n")
    print(f"  Bars                {full['bars']:,}")
    print(f"  Bars with news      {full['bars_with_news']:,}")
    print(f"  Articles            {full['articles']:,}")
    print("\n  lag  0 = the move during the same hour as the headline")
    print("  lag +1 = the next hour (this is the backtest's fwd_1h)")
    print("  lag -1 = the hour before the headline existed")

    # One shared scale across the three panels — see _leadlag_curve.
    shared = max(abs(l["ic"]) for sub in res["subsets"].values()
                 for l in sub["lags"] if l.get("ic") is not None) or 1.0

    for name in ("all", "event", "price_action"):
        sub = res["subsets"][name]
        print(f"\n  {name.upper().replace('_', ' ')}  "
              f"— peak at lag {sub['peak_lag']:+d}  [{sub['verdict']}]")
        _leadlag_curve(sub, scale=shared)

    s = res["summary"]
    print(f"\n  SUMMARY")
    print(f"    event half         peak {s['event_peak_lag']:+d}  "
          f"IC@+1 {s['event_ic_at_lag_1']:+.4f}  {s['event_verdict']}")
    print(f"    price-action half  peak {s['price_action_peak_lag']:+d}  "
          f"IC@+1 {s['price_action_ic_at_lag_1']:+.4f}  "
          f"{s['price_action_verdict']}")
    print(f"\n  {full['reading']}\n")


def do_validate(asset: str = "BTC", start: str = "2025-01-01",
                end: str = "2026-01-01") -> None:
    import validation

    print(f"\n  Validating {asset.upper()}  {start} .. {end} — this runs a "
          f"bootstrap and\n  several hundred placebo studies, so give it a "
          f"minute.\n")
    res = validation.run_all(asset=asset, start_date=start, end_date=end)
    t = res["tests"]

    print(f"{BAR}\n  VALIDATION — {res['asset']}  {start} .. {end}\n{BAR}")

    # --- lead-lag -----------------------------------------------------------
    ll = t["lead_lag"]
    print(f"\n  1. LEAD-LAG  —  is the signal ahead of price, or behind it?")
    if "error" in ll:
        print(f"     ERROR: {ll['error']}")
    else:
        _leadlag_curve(ll, indent="     ")
        print(f"\n     peak lag {ll['peak_lag']:+d}   "
              f"IC@+1 {(ll['ic_at_lag_1'] or 0):+.4f}   "
              f"fwd/back mass ratio {ll['leadlag_ratio']}")
        print(f"     {ll['verdict']} — {ll['reading']}")

    ls = t["lexicon_split"]
    if "summary" in ls:
        s = ls["summary"]
        print(f"\n     by lexicon half:")
        print(f"       event         peak {s['event_peak_lag']:+d}  "
              f"IC@+1 {s['event_ic_at_lag_1']:+.4f}  {s['event_verdict']}")
        print(f"       price action  peak {s['price_action_peak_lag']:+d}  "
              f"IC@+1 {s['price_action_ic_at_lag_1']:+.4f}  "
              f"{s['price_action_verdict']}")

    # --- bootstrap ----------------------------------------------------------
    bs = t["block_bootstrap"]
    print(f"\n  2. BLOCK BOOTSTRAP  —  is the IC significant against an "
          f"honest null?")
    if "error" in bs:
        print(f"     ERROR: {bs['error']}")
    else:
        print(f"     observed IC          {bs['observed_ic']:>+9.5f}  "
              f"(n={bs['n']:,}, horizon {bs['horizon_h']}h)")
        print(f"     naive t-statistic    {bs['naive_tstat']:>+9.3f}  "
              f"<- assumes independence, which returns violate")
        print(f"     {'null':<20} {'mean':>9} {'sd':>9} {'pctile':>8} "
              f"{'p':>8} {'z':>8}")
        print("     " + "-" * 66)
        for d in (bs["block"], bs["iid"]):
            print(f"     {d['null']:<20} {d['null_mean']:>+9.5f} "
                  f"{d['null_sd']:>9.5f} {d['percentile']:>7.1f}% "
                  f"{d['p_value_two_sided']:>8.4f} "
                  f"{(d['z'] or 0):>+8.2f}")
        if bs["tstat_inflation"]:
            print(f"\n     the naive t-stat overstates significance by "
                  f"~{bs['tstat_inflation']}x")
        print(f"     {bs['verdict']} — {bs['reading']}")

    # --- time shift ---------------------------------------------------------
    ts = t["time_shift"]
    print(f"\n  3. TIME-SHIFT PLACEBO  —  does misaligning news and price "
          f"kill it?")
    if "error" in ts:
        print(f"     ERROR: {ts['error']}")
    else:
        print(f"     true IC (no shift)   {ts['true_ic']:>+9.5f}  "
              f"(n={ts['true_n']:,})")
        print(f"     {'shift':>8} {'n':>8} {'IC':>10} {'retained':>10}")
        print("     " + "-" * 40)
        for s in ts["shifts"]:
            ic = "     —" if s["ic"] is None else f"{s['ic']:>+10.5f}"
            ret = ("     —" if s["retained_vs_true"] is None
                   else f"{100 * s['retained_vs_true']:>9.1f}%")
            print(f"     {s['shift_h']:>+7}h {s['n']:>8} {ic} {ret}")
        print(f"     {ts['verdict']} — {ts['reading']}")

    # --- placebo events -----------------------------------------------------
    pe = t["placebo_events"]
    print(f"\n  4. PLACEBO EVENTS  —  what does the event study find in "
          f"random hours?")
    if "error" in pe:
        print(f"     ERROR: {pe['error']}")
    else:
        print(f"     real events {pe['real_events']:,}   "
              f"placebo studies {pe['draws']:,}")
        print(f"     {'measure':<18} {'real':>10} {'null mean':>11} "
              f"{'null sd':>9} {'pctile':>8} {'p':>8}")
        print("     " + "-" * 68)
        for label, d in (("mean CAR %", pe["mean_car"]),
                         ("signed by prior %", pe["signed_car"])):
            if d.get("value") is None:
                print(f"     {label:<18}       —")
                continue
            print(f"     {label:<18} {d['value']:>+10.4f} "
                  f"{d['null_mean']:>+11.4f} {d['null_sd']:>9.4f} "
                  f"{d['percentile']:>7.1f}% {d['p_value_two_sided']:>8.4f}")
        print(f"\n     false positive rate  {pe['false_positive_rate_pct']:.1f}%"
              f"  of random studies would have printed |t| >= 2")
        print(f"     {pe['verdict']} — {pe['reading']}")

    # --- wrong asset --------------------------------------------------------
    wa = t["wrong_asset"]
    print(f"\n  5. WRONG-ASSET CONTROL  —  does it 'predict' things it "
          f"shouldn't?")
    if "error" in wa:
        print(f"     ERROR: {wa['error']}")
    else:
        print(f"     native  {wa['native_symbol']:<10} "
              f"IC {wa['native_ic']:>+9.5f}  (n={wa['native_n']:,})")
        for cc in wa["controls"]:
            if not cc.get("available"):
                print(f"     control {cc['symbol']:<10} not loaded — "
                      f"{cc['hint']}")
                continue
            print(f"     control {cc['symbol']:<10} IC {cc['ic']:>+9.5f}  "
                  f"(n={cc['n']:,})  {100 * cc['ratio_vs_native']:.0f}% of "
                  f"native")
        print(f"     {wa['verdict']} — {wa['reading']}")

    # --- summary ------------------------------------------------------------
    print(f"\n{BAR}\n  VERDICTS\n{BAR}\n")
    for name, verdict in res["summary"].items():
        print(f"    {name:<20} {verdict}")
    print(f"\n  Saved as run #{res.get('run_id')}\n")


def do_controls(start: str = "2025-01-01", end: str = "2026-01-01") -> None:
    import validation

    print(f"\n{BAR}\n  CONTROL SERIES BACKFILL  {start} .. {end}\n{BAR}\n")
    print("  These are non-crypto price series used only as a negative")
    print("  control. They are never traded and never scored.\n")
    res = validation.backfill_control_series(start_date=start, end_date=end)
    if "error" in res:
        print(f"  ERROR: {res['error']}\n")
        return
    for sym, info in res.items():
        if "error" in info:
            print(f"  {sym:<8} FAILED — {info['error']}")
        else:
            print(f"  {sym:<8} {info['candles']:>7,} candles"
                  + (f"   ({info['note']})" if info.get("note") else ""))
    print()


def list_backtests() -> None:
    store.init_db()
    rows = store.list_backtests()
    print(f"\n{BAR}\n  SAVED BACKTEST RUNS\n{BAR}\n")
    if not rows:
        print("  None yet.\n")
        return
    for r in rows:
        print(f"  #{r['id']:<4} {r['run_at'][:16]}  {r['asset']:<6} "
              f"{r['start_date']} .. {r['end_date']}  {r['label'] or ''}")
    print()


def do_paper(duration: str = "1h", assets: str | None = None) -> None:
    import paper_trader

    print(f"\n{BAR}\n  PAPER TRADING — SIMULATED, NO REAL ORDERS\n{BAR}")
    print(f"\n  Starting cash : ${paper_trader.STARTING_CASH:,.2f}")
    print(f"  Duration      : {duration}")
    print(f"  Assets        : {assets or 'BTC,ETH,SOL'}")
    print(f"\n  Ctrl+C to stop early (the run is still saved).\n")

    kwargs = {"duration": duration}
    if assets:
        kwargs["assets"] = [a.strip().upper() for a in assets.split(",")]
    summary = paper_trader.main(**kwargs)

    if summary.get("status") == "stopped" and "ending_equity" not in summary:
        print("\n  Stopped before the first tick completed.\n")
        return

    print(f"\n{BAR}\n  RESULT — run #{summary.get('run_id')}\n{BAR}\n")
    print(f"  Starting cash   ${summary['starting_cash']:>12,.2f}")
    print(f"  Ending equity   ${summary['ending_equity']:>12,.2f}  "
          f"({summary['return_pct']:+.2f}%)")
    print(f"  Realized P&L    ${summary['realized_pnl']:>12,.2f}")
    print(f"  Fees paid       ${summary['fees_paid']:>12,.2f}")
    print(f"  Final cash      ${summary['final_cash']:>12,.2f}")
    print(f"  Ticks           {summary['ticks']:>13,}")
    if summary.get("final_positions"):
        print(f"\n  Final positions:")
        for a, p in summary["final_positions"].items():
            print(f"    {a:<6} {p['qty']:>14.8f} @ ${p['price']:>12,.2f}"
                  f"  = ${p['value']:>10,.2f}")
    print()


def list_papers() -> None:
    store.init_db()
    rows = store.list_paper_runs()
    print(f"\n{BAR}\n  SAVED PAPER TRADING RUNS\n{BAR}\n")
    if not rows:
        print("  None yet. Start one with: python main.py paper 1h\n")
        return
    print(f"  {'id':<5} {'started':<17} {'status':<10} {'start$':>10} "
          f"{'end$':>11} {'ret%':>8}")
    print("  " + "-" * 66)
    for r in rows:
        end_eq = r["ending_equity"]
        ret = (100 * (end_eq / r["starting_cash"] - 1)
               if end_eq and r["starting_cash"] else 0.0)
        print(f"  {r['id']:<5} {r['started_at'][:16]:<17} "
              f"{(r['status'] or ''):<10} {r['starting_cash']:>10,.0f} "
              f"{(end_eq or 0):>11,.2f} {ret:>+8.2f}")
    print()


def show_paper_run(run_id: str) -> None:
    store.init_db()
    try:
        data = store.get_paper_run(int(run_id))
    except (ValueError, TypeError):
        print("\n  Invalid run id.\n")
        return
    if not data:
        print(f"\n  No paper run #{run_id}.\n")
        return

    import json as _json
    r = data["run"]
    print(f"\n{BAR}\n  PAPER RUN #{r['id']} — {r['label']}\n{BAR}\n")
    print(f"  Status        {r['status']}")
    print(f"  Started       {r['started_at']}")
    print(f"  Ended         {r['ended_at'] or '(still running)'}")
    print(f"  Starting cash ${r['starting_cash']:,.2f}")
    if r["ending_equity"]:
        print(f"  Ending equity ${r['ending_equity']:,.2f}  "
              f"({100 * (r['ending_equity'] / r['starting_cash'] - 1):+.2f}%)")

    trades = data["trades"]
    print(f"\n  TRADES ({len(trades)})")
    if trades:
        print(f"  {'time':<17} {'side':<5} {'asset':<6} {'qty':>14} "
              f"{'price':>12} {'P&L':>10}")
        print("  " + "-" * 70)
        for t in trades[:60]:
            print(f"  {t['ts'][:16]:<17} {t['side']:<5} {t['symbol']:<6} "
                  f"{t['qty']:>14.8f} {t['price']:>12,.2f} "
                  f"{(t['realized_pnl'] or 0):>10,.2f}")
        if len(trades) > 60:
            print(f"  ... and {len(trades) - 60} more")

    eq = data["equity"]
    if eq:
        vals = [e["equity"] for e in eq]
        print(f"\n  EQUITY  min ${min(vals):,.2f}  max ${max(vals):,.2f}  "
              f"samples {len(vals):,}")
    print()


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

MENU = f"""
{BAR}
  APOLLO — NEWS AGGREGATION & CRYPTO SIGNAL TERMINAL
{BAR}
  COLLECTION
   1. Start collector daemon (continuous, all sources)
   2. Run one collection pass now
   3. View current signals (BTC / ETH / SOL / MACRO)
   4. View highest-impact articles
   5. Feed health check
   6. Database statistics
   7. SEC filings lookup by ticker
   8. Export articles to JSONL

  BACKTESTING
   9. Backfill historical data (prices + news)
  10. Show historical data coverage
  11. Run a backtest
  12. Run an event study
  13. List saved backtests / event studies

  PAPER TRADING (simulated — no real orders)
  14. Start a paper trading run
  15. List saved paper runs
  16. Inspect a paper run

  VALIDATION (is the signal real?)
  17. Lead-lag test (does the signal lead price, or follow it?)
  18. Full validation (lead-lag + four null controls)
  19. Fetch non-crypto control series (SPY / GLD)

   0. Exit
{'-' * 68}"""


def menu() -> None:
    while True:
        print(MENU)
        choice = input("  Select an option: ").strip()

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
            s = input("  Start date [2025-01-01]: ").strip() or "2025-01-01"
            e = input("  End date   [2026-01-01]: ").strip() or "2026-01-01"
            do_backfill(s, e)
        elif choice == "10":
            show_coverage()
        elif choice == "11":
            a = input("  Asset [BTC]: ").strip().upper() or "BTC"
            s = input("  Start date [2025-01-01]: ").strip() or "2025-01-01"
            e = input("  End date   [2026-01-01]: ").strip() or "2026-01-01"
            do_backtest(a, s, e)
        elif choice == "12":
            a = input("  Asset [BTC]: ").strip().upper() or "BTC"
            s = input("  Start date [2025-01-01]: ").strip() or "2025-01-01"
            e = input("  End date   [2026-01-01]: ").strip() or "2026-01-01"
            do_event_study(a, s, e)
        elif choice == "13":
            list_backtests()
        elif choice == "14":
            d = input("  Duration (e.g. 1h, 30m, 7d, 0=unbounded) [1h]: ").strip() or "1h"
            a = input("  Assets [BTC,ETH,SOL]: ").strip()
            do_paper(d, a or None)
        elif choice == "15":
            list_papers()
        elif choice == "16":
            rid = input("  Run id: ").strip()
            if rid:
                show_paper_run(rid)
        elif choice == "17":
            a = input("  Asset [BTC]: ").strip().upper() or "BTC"
            s = input("  Start date [2025-01-01]: ").strip() or "2025-01-01"
            e = input("  End date   [2026-01-01]: ").strip() or "2026-01-01"
            do_leadlag(a, s, e)
        elif choice == "18":
            a = input("  Asset [BTC]: ").strip().upper() or "BTC"
            s = input("  Start date [2025-01-01]: ").strip() or "2025-01-01"
            e = input("  End date   [2026-01-01]: ").strip() or "2026-01-01"
            do_validate(a, s, e)
        elif choice == "19":
            s = input("  Start date [2025-01-01]: ").strip() or "2025-01-01"
            e = input("  End date   [2026-01-01]: ").strip() or "2026-01-01"
            do_controls(s, e)
        elif choice == "0":
            logger.info("User exited the application.")
            print("  Goodbye.\n")
            break
        else:
            print("  Invalid selection.")


def cli() -> None:
    # No arguments -> the full terminal. The numbered menu is still reachable
    # via `python main.py menu` for anyone who prefers it.
    if len(sys.argv) < 2:
        import terminal
        terminal.main([])
        return

    cmd = sys.argv[1].lower()
    arg = sys.argv[2] if len(sys.argv) > 2 else None

    arg2 = sys.argv[3] if len(sys.argv) > 3 else None
    arg3 = sys.argv[4] if len(sys.argv) > 4 else None

    if cmd in ("term", "terminal", "shell"):
        import terminal
        terminal.main(sys.argv[2:])
    elif cmd == "menu":
        menu()
    elif cmd == "run":
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
    elif cmd == "backfill":
        do_backfill(arg or "2025-01-01", arg2 or "2026-01-01")
    elif cmd == "coverage":
        show_coverage()
    elif cmd == "backtest":
        do_backtest((arg or "BTC").upper(), arg2 or "2025-01-01",
                    arg3 or "2026-01-01")
    elif cmd == "events":
        do_event_study((arg or "BTC").upper(), arg2 or "2025-01-01",
                       arg3 or "2026-01-01")
    elif cmd == "validate":
        do_validate((arg or "BTC").upper(), arg2 or "2025-01-01",
                    arg3 or "2026-01-01")
    elif cmd in ("leadlag", "lead-lag"):
        do_leadlag((arg or "BTC").upper(), arg2 or "2025-01-01",
                   arg3 or "2026-01-01")
    elif cmd == "controls":
        do_controls(arg or "2025-01-01", arg2 or "2026-01-01")
    elif cmd == "backtests":
        list_backtests()
    elif cmd == "paper":
        do_paper(arg or "1h", arg2)
    elif cmd == "papers":
        list_papers()
    elif cmd == "paperrun":
        show_paper_run(arg or "")
    else:
        print(__doc__)


if __name__ == "__main__":
    cli()
