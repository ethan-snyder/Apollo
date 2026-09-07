"""
Apollo — event study framework.

WHY THIS EXISTS (and why it beats continuous IC):

Continuous IC scores every hourly bar. But most bars contain no real news, so
the handful of bars that actually matter get buried under thousands of
near-zero observations. IC ends up measuring "does ambient news chatter track
price," which is mostly noise, rather than "does a real event move price,"
which is the question.

An event study inverts that. It isolates discrete, identifiable events, then
measures the *abnormal* return in a window around each one — abnormal meaning
"beyond what the asset would have done anyway given its own drift and
volatility." Because it only looks where something happened, it reaches
statistical significance with far fewer observations, and it answers a much
more actionable question: WHICH KINDS of news does the lexicon read correctly?

Method (standard finance event-study methodology):

  1. Detect events and assign each to a category (ETF decision, hack,
     enforcement, monetary policy, ...).
  2. For each event at time t0, fit a market model over an ESTIMATION WINDOW
     ending strictly before t0 (default: 168 bars, ending 6 bars prior). The
     gap prevents pre-event drift from contaminating the baseline.
  3. Abnormal return AR = actual return − expected return from that baseline.
  4. Cumulative abnormal return CAR = sum of ARs across the event window.
  5. Average across events (AAR/CAAR), then t-test against zero.

The estimation window ALWAYS ends before the event, so no post-event
information can leak into the baseline. This is the event-study analogue of
the look-ahead protections in backtest.py.
"""
from __future__ import annotations

import json
import logging
import math
import re
import statistics
from datetime import datetime, timedelta, timezone

from config import ASSETS
from store import get_candles, get_conn, init_db, save_backtest

logger = logging.getLogger("EVENTSTUDY")

# Window geometry, in hourly bars.
ESTIMATION_BARS = 168        # 7 days of hourly data for the baseline
ESTIMATION_GAP = 6           # buffer between baseline and event
PRE_WINDOW = 3               # bars before t0 included in the event window
POST_WINDOW = 24             # bars after t0 included in the event window
MIN_EVENTS = 5               # below this, per-category stats aren't reported


# ---------------------------------------------------------------------------
# Event taxonomy
# ---------------------------------------------------------------------------
# Each category is (regex, expected_direction). expected_direction encodes the
# prior a human trader would hold: +1 bullish, -1 bearish, 0 ambiguous.
# Comparing realized CAR against this prior is the real payload of this
# module — it shows where the lexicon's assumptions are right, wrong, or
# already priced in.

EVENT_TYPES: dict[str, tuple[str, int]] = {
    # NOTE on word order: headlines put the verb before OR after the noun with
    # roughly equal frequency ("SEC approves bitcoin ETF" vs "Bitcoin ETF
    # approved by SEC"). Patterns below match BOTH orders — a one-directional
    # pattern silently drops about half of real events, which is invisible in
    # the output and quietly biases every downstream statistic.
    "etf_approval": (
        r"(?:etf|exchange[- ]traded fund).{0,40}(?:approv|green ?light|"
        r"launch|begins? trading)"
        r"|(?:approv\w*|green ?light\w*|greenlit|launch\w*)"
        r".{0,40}(?:etf|exchange[- ]traded fund)", +1),
    "etf_rejection_delay": (
        r"(?:etf|exchange[- ]traded fund).{0,40}(?:reject|denie|delay|"
        r"postpone|push(?:es|ed)? back)"
        r"|(?:reject\w*|denie[sd]|denial|delay\w*|postpone\w*)"
        r".{0,60}(?:etf|exchange[- ]traded fund)", -1),
    "etf_flows": (
        r"etf.{0,30}(?:inflow|outflow)|(?:inflow|outflow).{0,30}etf", 0),
    "enforcement": (
        r"\b(?:sec|cftc|doj|justice department|regulator)\b.{0,50}"
        r"(?:sues?|sued|charges?|charged|indict|investigat|subpoena|"
        r"enforcement action|crackdown|fine[sd]?|penalt)"
        r"|(?:sued|charged|indicted|investigated|fined)\s+by\s+"
        r"(?:the\s+)?(?:sec|cftc|doj|justice department|regulators?)", -1),
    "enforcement_relief": (
        r"(?:drop(?:s|ped)?|dismiss(?:es|ed)?|settle[sd]?|withdraw[sn]?)"
        r".{0,30}(?:case|charges|lawsuit|suit|probe|investigation)", +1),
    "security_incident": (
        r"\b(?:hack(?:ed|er)?|exploit(?:ed)?|breach|stolen|drained|"
        r"rug ?pull|51% attack)\b", -1),
    "insolvency": (
        r"\b(?:bankrupt(?:cy)?|insolven(?:t|cy)|chapter 11|halts? withdrawal|"
        r"collapse[sd]?|contagion)\b", -1),
    "depeg": (r"\bdepeg|loses? (?:its )?peg\b", -1),
    "monetary_easing": (
        r"(?:rate cut|cuts? (?:interest )?rates?|dovish|quantitative easing|"
        r"stimulus)", +1),
    "monetary_tightening": (
        r"(?:rate hike|rais(?:e|es|ed) (?:interest )?rates?|hawkish|"
        r"quantitative tightening)", -1),
    "inflation_data": (r"\b(?:cpi|inflation|pce|ppi)\b.{0,40}(?:data|report|"
                       r"reading|print|rose|fell|cooled|climbed)", 0),
    "fomc": (r"\b(?:fomc|federal open market committee|fed (?:meeting|decision))\b", 0),
    "institutional_adoption": (
        r"(?:blackrock|fidelity|vanguard|jpmorgan|goldman|morgan stanley|"
        r"citi|pension|sovereign wealth|endowment).{0,50}"
        r"(?:buy|bought|invest|allocat|launch|offer|approv|adopt)", +1),
    "treasury_purchase": (
        r"(?:adds?|buys?|acquires?|purchas\w+).{0,40}"
        r"(?:bitcoin|btc|ether|eth|solana|sol)\b", +1),
    "regulatory_clarity": (
        r"(?:regulatory clarity|(?:bill|act) (?:passes|passed|advances|clears)|"
        r"legaliz|bank charter|licen[sc]e (?:granted|approved))", +1),
    "ban_restriction": (
        r"\bban(?:s|ned|ning)?\b.{0,30}(?:crypto|bitcoin|mining|trading)|"
        r"(?:tighter|stricter) (?:regulation|rules)", -1),
    "network_upgrade": (
        r"(?:upgrade|hard fork|mainnet).{0,30}(?:launch|go(?:es)? live|"
        r"activat|complet)", +1),
    "network_outage": (r"(?:outage|downtime|network hal(?:t|ted)|"
                       r"chain (?:hal|stall))", -1),
    "geopolitical_shock": (
        r"(?:military|air) strike|invasion|missile|declares? war|"
        r"(?:new|fresh|higher) tariffs?|trade war", -1),
    "geopolitical_relief": (
        r"ceasefire|peace (?:deal|agreement)|de-?escalat|"
        r"tariffs? (?:lifted|removed|paused)|sanctions? (?:lifted|eased)", +1),
}

_EVENT_C = {k: (re.compile(p, re.I), d) for k, (p, d) in EVENT_TYPES.items()}


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def classify(text: str) -> list[tuple[str, int]]:
    """Return every (category, expected_direction) this text matches."""
    return [(name, direction) for name, (rx, direction) in _EVENT_C.items()
            if rx.search(text)]


def detect_events(asset: str, start: datetime, end: datetime,
                  min_relevance: float = 0.5,
                  min_corroboration: int = 1) -> list[dict]:
    """
    Find candidate events in stored articles.

    `min_corroboration` filters to stories multiple outlets picked up, which is
    a decent proxy for "actually mattered" and cuts a lot of single-blog noise.
    """
    rows = get_conn().execute(
        """SELECT id, title, summary, source, published_at, sentiment,
                  corroboration, score_detail
           FROM articles
           WHERE assets LIKE ? AND published_at IS NOT NULL
             AND published_at >= ? AND published_at <= ?
             AND corroboration >= ?
           ORDER BY published_at""",
        (f'%"{asset}"%', start.isoformat(), end.isoformat(), min_corroboration),
    ).fetchall()

    events: list[dict] = []
    for r in rows:
        detail = {}
        if r["score_detail"]:
            try:
                detail = json.loads(r["score_detail"])
            except json.JSONDecodeError:
                pass
        rel = (detail.get("asset_relevance") or {}).get(asset, 0.0)
        if rel < min_relevance:
            continue

        text = f"{r['title']}\n{r['summary'] or ''}"
        cats = classify(text)
        if not cats:
            continue

        try:
            ts = _dt(r["published_at"])
        except (ValueError, TypeError):
            continue

        for name, expected in cats:
            events.append({
                "article_id": r["id"],
                "ts": ts,
                "category": name,
                "expected_direction": expected,
                "title": r["title"],
                "source": r["source"],
                "sentiment": float(r["sentiment"] or 0.0),
                "relevance": rel,
                "corroboration": r["corroboration"],
            })
    return events


# ---------------------------------------------------------------------------
# Abnormal return machinery
# ---------------------------------------------------------------------------

def _returns(closes: list[float]) -> list[float]:
    return [(closes[i] / closes[i - 1] - 1) if closes[i - 1] else 0.0
            for i in range(1, len(closes))]


def compute_car(returns: list[float], event_idx: int,
                estimation_bars: int = ESTIMATION_BARS,
                gap: int = ESTIMATION_GAP,
                pre: int = PRE_WINDOW,
                post: int = POST_WINDOW) -> dict | None:
    """
    Abnormal returns around one event.

    Baseline is a constant-mean model fitted on the estimation window, which
    ends `gap` bars BEFORE the event so pre-event drift can't contaminate it.
    A constant-mean model is used rather than a full market model because
    crypto majors lack a clean, independent market index — BTC effectively
    *is* the index, so regressing BTC on a BTC-inclusive benchmark would be
    circular.

    Returns None when there isn't enough surrounding history.
    """
    est_end = event_idx - gap
    est_start = est_end - estimation_bars
    win_start = event_idx - pre
    win_end = event_idx + post

    if est_start < 0 or win_end >= len(returns) or est_end <= est_start:
        return None

    baseline = returns[est_start:est_end]
    if len(baseline) < 20:
        return None

    mu = statistics.mean(baseline)
    sd = statistics.pstdev(baseline)
    # NOTE: a zero-variance baseline does NOT invalidate CAR — CAR is just
    # actual minus expected, and the expectation is still well defined. Only
    # the *standardized* SCAR needs volatility, so that is reported as None
    # instead of discarding the whole event. (Real data essentially never has
    # sd == 0; synthetic/illiquid series do, and silently dropping those
    # events would bias any study built on them.)

    window = returns[win_start:win_end + 1]
    ars = [r - mu for r in window]
    car = sum(ars)

    # Post-event-only CAR isolates reaction from any pre-event drift/leak.
    post_ars = ars[pre:]
    car_post = sum(post_ars)

    return {
        "car": car,
        "car_post": car_post,
        "car_pre": sum(ars[:pre]),
        "ars": ars,
        "baseline_mean": mu,
        "baseline_sd": sd,
        # Standardized CAR — scales by baseline volatility so a 2% move in a
        # calm regime isn't treated the same as 2% in a violent one.
        "scar": car / (sd * math.sqrt(len(window))) if sd else None,
    }


def _tstat(vals: list[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    sd = statistics.pstdev(vals)
    if sd == 0:
        return 0.0
    return (sum(vals) / n) / (sd / math.sqrt(n))


def _bar_index(bar_times: list[datetime], ts: datetime) -> int | None:
    """
    First bar at or after `ts` — the earliest bar a trader could have acted on.

    Uses binary search; returns None if the event falls outside the series.
    """
    lo, hi = 0, len(bar_times)
    while lo < hi:
        mid = (lo + hi) // 2
        if bar_times[mid] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(bar_times) else None


# ---------------------------------------------------------------------------
# Study
# ---------------------------------------------------------------------------

def run_study(asset: str = "BTC", start_date: str = "2025-01-01",
              end_date: str = "2026-01-01", *, pre: int = PRE_WINDOW,
              post: int = POST_WINDOW, min_relevance: float = 0.5,
              min_corroboration: int = 1, dedupe_hours: float = 6.0,
              save: bool = True, label: str = "") -> dict:
    init_db()
    asset = asset.upper()
    symbol = ASSETS.get(asset, {}).get("pair")
    if not symbol:
        return {"error": f"{asset} has no tradable pair configured"}

    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)

    # Pull extra history before `start` so early events still have a baseline.
    pad = timedelta(hours=ESTIMATION_BARS + ESTIMATION_GAP + 10)
    candles = get_candles(symbol, 3600, (start - pad).isoformat(), end.isoformat())
    if len(candles) < ESTIMATION_BARS + post + 10:
        return {"error": f"Not enough {symbol} candles. Run: "
                         f"python main.py backfill {start_date} {end_date}"}

    bar_times = [_dt(c["ts"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    rets = _returns(closes)          # rets[i] is the return INTO bar i+1

    events = detect_events(asset, start, end, min_relevance, min_corroboration)
    if not events:
        return {"error": "No events detected. Backfill news first, or relax "
                         "min_relevance / min_corroboration."}

    # Collapse repeats of the same category within `dedupe_hours` so one story
    # covered by twelve outlets counts once, not twelve times.
    events.sort(key=lambda e: e["ts"])
    deduped: list[dict] = []
    last_seen: dict[str, datetime] = {}
    for e in events:
        prev = last_seen.get(e["category"])
        if prev and (e["ts"] - prev).total_seconds() < dedupe_hours * 3600:
            continue
        last_seen[e["category"]] = e["ts"]
        deduped.append(e)

    by_cat: dict[str, list[dict]] = {}
    measured = 0
    for e in deduped:
        idx = _bar_index(bar_times, e["ts"])
        if idx is None or idx >= len(rets):
            continue
        car = compute_car(rets, idx, ESTIMATION_BARS, ESTIMATION_GAP, pre, post)
        if car is None:
            continue
        e = {**e, **car}
        by_cat.setdefault(e["category"], []).append(e)
        measured += 1

    categories: dict[str, dict] = {}
    for cat, evs in sorted(by_cat.items()):
        if len(evs) < MIN_EVENTS:
            categories[cat] = {"n": len(evs), "note": "too few events"}
            continue

        cars = [e["car"] for e in evs]
        cars_post = [e["car_post"] for e in evs]
        scars = [e["scar"] for e in evs if e["scar"] is not None]
        expected = evs[0]["expected_direction"]

        # Does realized move agree with the prior encoded in the taxonomy?
        if expected:
            agree = sum(1 for c in cars_post if (c > 0) == (expected > 0))
            agreement = 100.0 * agree / len(cars_post)
        else:
            agreement = None

        # Does the lexicon's own sentiment agree with the realized move?
        lex = [(e["sentiment"], e["car_post"]) for e in evs if e["sentiment"]]
        lex_agree = (100.0 * sum(1 for s, c in lex if (s > 0) == (c > 0)) / len(lex)
                     if lex else None)

        # Mean AR per bar, for eyeballing how fast the move materializes.
        maxlen = max(len(e["ars"]) for e in evs)
        aar = [round(100 * statistics.mean(
                   [e["ars"][i] for e in evs if i < len(e["ars"])]), 4)
               for i in range(maxlen)]

        categories[cat] = {
            "n": len(evs),
            "expected_direction": expected,
            "mean_car_pct": round(100 * statistics.mean(cars), 4),
            "mean_car_post_pct": round(100 * statistics.mean(cars_post), 4),
            "mean_car_pre_pct": round(
                100 * statistics.mean([e["car_pre"] for e in evs]), 4),
            "median_car_post_pct": round(100 * statistics.median(cars_post), 4),
            "tstat_car_post": round(_tstat(cars_post), 3),
            "mean_scar": round(statistics.mean(scars), 4) if scars else None,
            "tstat_scar": round(_tstat(scars), 3) if scars else None,
            "pct_positive": round(
                100 * sum(1 for c in cars_post if c > 0) / len(cars_post), 2),
            "direction_agreement_pct": (round(agreement, 2)
                                        if agreement is not None else None),
            "lexicon_agreement_pct": (round(lex_agree, 2)
                                      if lex_agree is not None else None),
            "aar_by_bar_pct": aar,
            "examples": [{"ts": e["ts"].isoformat()[:16], "title": e["title"][:90],
                          "car_post_pct": round(100 * e["car_post"], 3)}
                         for e in sorted(evs, key=lambda x: -abs(x["car_post"]))[:3]],
        }

    all_post = [e["car_post"] for evs in by_cat.values() for e in evs]
    # Signed by prior: does trading the taxonomy's expectation make money?
    signed = [e["car_post"] * e["expected_direction"]
              for evs in by_cat.values() for e in evs
              if e["expected_direction"]]

    results = {
        "asset": asset,
        "symbol": symbol,
        "start": start_date,
        "end": end_date,
        "window": {"pre_bars": pre, "post_bars": post,
                   "estimation_bars": ESTIMATION_BARS,
                   "estimation_gap_bars": ESTIMATION_GAP},
        "events_detected": len(events),
        "events_after_dedupe": len(deduped),
        "events_measured": measured,
        "overall": {
            "mean_car_post_pct": round(100 * statistics.mean(all_post), 4)
            if all_post else None,
            "signed_by_prior_mean_pct": round(100 * statistics.mean(signed), 4)
            if signed else None,
            "signed_by_prior_tstat": round(_tstat(signed), 3) if signed else None,
            "n_signed": len(signed),
        },
        "categories": categories,
    }

    if save:
        results["run_id"] = save_backtest(
            label or f"eventstudy {asset} {start_date}..{end_date}",
            asset, start_date, end_date,
            {"type": "event_study", "pre": pre, "post": post,
             "min_relevance": min_relevance,
             "min_corroboration": min_corroboration},
            results)
    return results
