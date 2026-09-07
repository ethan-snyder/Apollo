"""
Apollo — validation harness.

The backtest answers "did the signal correlate with forward returns?" This
module answers the two questions that have to be settled before that number
means anything:

  1. LEAD-LAG — is the signal ahead of the price, or behind it? A news signal
     built partly from phrases like "surges", "plunges", "sell-off" and "worst
     week" can score a healthy IC purely because those headlines are written
     *after* the move. Reverse causality looks identical to alpha in every
     aggregate statistic the backtest reports.

  2. NULLS — how often would this machinery produce a result this good from
     data that contains no signal at all? Four controls, each designed to fail
     in a different way than a naive sentiment shuffle:

       block_bootstrap      preserves return autocorrelation, so the null is
                            not artificially tight and the t-stat is not
                            artificially significant
       time_shift_placebo   misaligns news and price in time; real news signal
                            must die, a slow-moving regime variable will not
                            notice
       placebo_events       feeds random timestamps through the event-study
                            machinery and measures how often it manufactures
                            a |t| >= 2 from nothing
       wrong_asset_control  runs the crypto news signal against a non-crypto
                            price series; a genuinely BTC-specific signal
                            should not work on SPY

Everything here reuses the estimators in `backtest.py` and `event_study.py`
rather than reimplementing them. That is deliberate: a validation harness that
computes its own IC differently would be testing a different quantity than the
one being validated, and any disagreement between them would be unfalsifiable.

Pure Python — no numpy/scipy — matching the rest of the project. The bootstrap
is the only expensive path and defaults to a size that finishes in seconds.
"""
from __future__ import annotations

import json
import logging
import math
import random
import statistics
from datetime import datetime, timedelta, timezone

import backtest
import event_study
import scoring
from backtest import DEFAULT_WINDOW_MIN, RECENCY_HALF_LIFE_MIN
# Reuse the exact estimators the backtest reports, so a number produced here is
# directly comparable to a number produced there. Re-deriving them would make
# any disagreement impossible to attribute.
from backtest import _spearman, _tstat, load_prices, replay
from config import ASSETS
from store import get_candles, get_conn, init_db, save_backtest, upsert_candles

logger = logging.getLogger("VALIDATION")

DEFAULT_MAX_LAG = 6            # hours either side of contemporaneous
DEFAULT_BLOCK_HOURS = 24       # bootstrap block length
DEFAULT_ITERATIONS = 1000
DEFAULT_SHIFTS = (-72, -48, -24, 24, 48, 72)
DEFAULT_CONTROLS = ("SPY", "GLD")

# Above this share of the true IC surviving a time shift, the signal is not
# reacting to news — it is tracking something that moves on a multi-day scale.
SURVIVAL_ALARM = 0.5


# ---------------------------------------------------------------------------
# Shared loading
# ---------------------------------------------------------------------------

def load_articles(asset: str, start: datetime, end: datetime, *,
                  subset: str = "all", recompute: bool | None = None
                  ) -> list[dict]:
    """
    Articles for one asset, pre-parsed for the replay loop.

    `subset` selects which half of the lexicon scores sentiment — see
    `scoring.score_sentiment`. `recompute` controls where sentiment comes from:

      False  the stored `articles.sentiment` column, which is what
             `backtest.load_articles` uses. Only valid for subset="all".
      True   re-scored from the article text at call time.

    The default recomputes for every subset except "all", so that a plain
    `lead_lag()` is computed from exactly the same inputs as `main.py backtest`.
    `lexicon_split()` overrides this to True for all three subsets, because the
    halves only sum back to the whole if all three are scored by the same code
    at the same moment — if the lexicon has been edited since ingest, stored
    and recomputed sentiment disagree and the split stops being a partition.
    """
    if subset not in scoring.LEXICON_SUBSETS:
        raise ValueError(f"unknown subset {subset!r}; "
                         f"expected one of {scoring.LEXICON_SUBSETS}")
    if recompute is None:
        recompute = subset != "all"
    if not recompute and subset != "all":
        raise ValueError("recompute=False is only meaningful for subset='all'; "
                         "the stored sentiment column is full-lexicon")

    if not recompute:
        return backtest.load_articles(asset, start, end)

    rows = get_conn().execute(
        """SELECT id, title, summary, body, source, source_weight,
                  published_at, sentiment, score_detail
           FROM articles
           WHERE assets LIKE ? AND published_at IS NOT NULL
             AND published_at >= ? AND published_at <= ?
           ORDER BY published_at""",
        (f'%"{asset}"%', start.isoformat(), end.isoformat()),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["_published_dt"] = backtest._dt(d["published_at"])
        except (ValueError, TypeError):
            continue

        detail = {}
        if d.get("score_detail"):
            try:
                detail = json.loads(d["score_detail"])
            except json.JSONDecodeError:
                detail = {}

        rel = (detail.get("asset_relevance") or {}).get(asset)
        if rel is None:
            continue

        # Mirror score_article's text assembly exactly, or the recomputed
        # sentiment is scored over a different document than the stored one.
        text = "\n".join(p for p in (d["title"], d["summary"] or "",
                                     (d["body"] or "")[:4000]) if p)
        sentiment, _ = scoring.score_sentiment(text, subset)

        d["_relevance"] = float(rel)
        d["_sentiment"] = sentiment
        d["_source_weight"] = float(d.get("source_weight") or 1.0)
        d["_high_impact"] = bool(detail.get("high_impact_event"))
        out.append(d)
    return out


def _series(obs: list[dict]) -> tuple[list[float], list[float], list[bool]]:
    """
    Split replay observations into aligned bar-indexed series.

    Returns (signal, return_into_bar, has_news) where `return_into_bar[i]` is
    the return realized over the hour ENDING at bar i's close — the move a
    headline published during that hour would be describing. Index 0 is 0.0
    because there is no prior close to measure from.
    """
    closes = [o["close"] for o in obs]
    sig = [o["net_impact"] for o in obs]
    has_news = [o["article_count"] > 0 for o in obs]
    into = [0.0]
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        into.append((closes[i] - prev) / prev if prev else 0.0)
    return sig, into, has_news


def _ic_tstat(ic: float, n: int) -> float:
    """t-statistic for a rank correlation. Undefined at |ic| == 1."""
    if n < 3 or abs(ic) >= 1.0:
        return 0.0
    return ic * math.sqrt((n - 2) / (1 - ic * ic))


def _observations(asset: str, start_date: str, end_date: str, *,
                  window_min: int, subset: str, recompute: bool | None,
                  horizons: tuple[int, ...] = (1,)) -> tuple[list[dict], dict]:
    """Replay one asset over its own pair. Returns (obs, meta) or ([], error)."""
    asset = asset.upper()
    symbol = ASSETS.get(asset, {}).get("pair")
    if not symbol:
        return [], {"error": f"{asset} has no tradable pair configured"}

    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)

    prices = load_prices(symbol, start, end)
    if not prices:
        return [], {"error": f"No {symbol} candles in range. Run: "
                             f"python main.py backfill {start_date} {end_date}"}

    articles = load_articles(asset, start, end, subset=subset,
                             recompute=recompute)
    if not articles:
        return [], {"error": "No scored articles for this asset in range. "
                             "Backfill news before validating."}

    obs = replay(asset, articles, prices, window_min, horizons)
    return obs, {"symbol": symbol, "bars": len(prices),
                 "articles": len(articles)}


# ---------------------------------------------------------------------------
# 1. Lead-lag
# ---------------------------------------------------------------------------

def lead_lag(asset: str = "BTC", start_date: str = "2025-01-01",
             end_date: str = "2026-01-01", *, window_min: int = DEFAULT_WINDOW_MIN,
             max_lag: int = DEFAULT_MAX_LAG, subset: str = "all",
             recompute: bool | None = None) -> dict:
    """
    Cross-correlate the signal against returns at leads and lags.

    SIGN CONVENTION (the whole test hinges on it):

        lag  0   the signal at bar i vs the return realized DURING bar i.
                 Contemporaneous. The headline and the move happened in the
                 same hour.
        lag +1   the signal at bar i vs the return realized over the NEXT
                 hour. This is exactly `fwd_1h` in the backtest, and the
                 smallest lag at which the signal can be said to predict.
        lag -1   the signal at bar i vs the return realized in the hour
                 BEFORE it. Price moved before the news existed.

    A genuine news signal peaks at lag >= +1 and decays as the lag grows.

    A signal that peaks at lag 0 is contemporaneous: the headline is being
    published alongside the move, and by the time it is ingested, scored and
    acted on, the move is gone.

    A signal that peaks at negative lag is REACTIVE. The price moved first and
    the article describes it. This produces a perfectly respectable-looking IC
    in the backtest (because hourly crypto returns are autocorrelated, so the
    move that already happened partly predicts the next one) while containing
    no forward information of its own.

    Only bars with news are correlated. Including the thousands of empty bars
    would drag every lag toward zero uniformly and flatten the shape of the
    curve, which is the only thing this test is trying to read.
    """
    obs, meta = _observations(asset, start_date, end_date, window_min=window_min,
                              subset=subset, recompute=recompute)
    if not obs:
        return meta

    sig, into, has_news = _series(obs)
    n_bars = len(sig)

    lags: list[dict] = []
    for lag in range(-max_lag, max_lag + 1):
        pairs = [(sig[i], into[i + lag]) for i in range(n_bars)
                 if has_news[i] and 0 <= i + lag < n_bars]
        if len(pairs) < 30:
            lags.append({"lag": lag, "n": len(pairs), "ic": None,
                         "tstat": None, "note": "insufficient data"})
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        ic = _spearman(xs, ys)
        lags.append({"lag": lag, "n": len(pairs), "ic": round(ic, 5),
                     "tstat": round(_ic_tstat(ic, len(pairs)), 3)})

    scored = [l for l in lags if l["ic"] is not None]
    if not scored:
        return {"error": "Not enough overlapping bars to cross-correlate."}

    peak = max(scored, key=lambda l: abs(l["ic"]))
    forward = sum(abs(l["ic"]) for l in scored if l["lag"] >= 1)
    backward = sum(abs(l["ic"]) for l in scored if l["lag"] <= -1)
    n_fwd = sum(1 for l in scored if l["lag"] >= 1)
    n_bwd = sum(1 for l in scored if l["lag"] <= -1)

    if peak["lag"] >= 1:
        verdict = "PREDICTIVE"
        reading = ("The signal leads price. This is the shape a real news "
                   "signal has.")
    elif peak["lag"] == 0:
        verdict = "CONTEMPORANEOUS"
        reading = ("The signal moves with price, not ahead of it. Headlines "
                   "are landing alongside the move; there is little or no "
                   "actionable lead.")
    else:
        verdict = "REACTIVE"
        reading = ("Price leads the signal — the news is describing a move "
                   "that already happened. Any forward IC is coming from "
                   "return autocorrelation, not from the news.")

    return {
        "asset": asset.upper(),
        "symbol": meta["symbol"],
        "start": start_date,
        "end": end_date,
        "window_min": window_min,
        "subset": subset,
        "bars": meta["bars"],
        "articles": meta["articles"],
        "bars_with_news": sum(has_news),
        "lags": lags,
        "peak_lag": peak["lag"],
        "peak_ic": peak["ic"],
        "ic_at_lag_1": next((l["ic"] for l in scored if l["lag"] == 1), None),
        "forward_mass": round(forward / n_fwd, 5) if n_fwd else None,
        "backward_mass": round(backward / n_bwd, 5) if n_bwd else None,
        "leadlag_ratio": (round(forward / backward, 3)
                          if backward and n_fwd and n_bwd else None),
        "verdict": verdict,
        "reading": reading,
    }


def lexicon_split(asset: str = "BTC", start_date: str = "2025-01-01",
                  end_date: str = "2026-01-01", **kwargs) -> dict:
    """
    Run the lead-lag test separately over each half of the lexicon.

    This is the payoff of the event / price-action split in `scoring.py`. The
    aggregate lead-lag curve mixes two populations with opposite causal
    structure, and mixing them can hide either one:

      * Event phrases that genuinely lead get diluted by descriptive phrases
        that lag, making a real signal look weaker than it is.
      * Descriptive phrases that lag can, on their own, produce a positive
        forward IC via return autocorrelation, making a signal with no
        forward information look real.

    All three subsets are force-recomputed from article text so that the two
    halves partition the whole. See `load_articles`.

    What to do with the result:

      event predictive, price-action reactive   Expected and healthy. Consider
                                                dropping or down-weighting the
                                                price-action half — it is
                                                adding variance, not signal.
      both reactive                             The signal has no forward
                                                information. Stop tuning
                                                thresholds; fix the lexicon.
      price-action predictive, event flat       Momentum, not news. Cheaper to
                                                get from the price series
                                                directly, with no scraper.
    """
    kwargs.pop("subset", None)
    kwargs.pop("recompute", None)
    out: dict = {"asset": asset.upper(), "start": start_date, "end": end_date,
                 "subsets": {}}
    for sub in scoring.LEXICON_SUBSETS:
        res = lead_lag(asset, start_date, end_date, subset=sub,
                       recompute=True, **kwargs)
        if "error" in res:
            return res
        out["subsets"][sub] = res

    ev, pa = out["subsets"]["event"], out["subsets"]["price_action"]
    out["summary"] = {
        "event_peak_lag": ev["peak_lag"], "event_verdict": ev["verdict"],
        "price_action_peak_lag": pa["peak_lag"],
        "price_action_verdict": pa["verdict"],
        "event_ic_at_lag_1": ev["ic_at_lag_1"],
        "price_action_ic_at_lag_1": pa["ic_at_lag_1"],
    }
    return out


# ---------------------------------------------------------------------------
# 2a. Block bootstrap
# ---------------------------------------------------------------------------

def _block_permutation(values: list[float], block: int,
                       rng: random.Random) -> list[float]:
    """Cut the series into contiguous blocks and reassemble in random order."""
    blocks = [values[i:i + block] for i in range(0, len(values), block)]
    rng.shuffle(blocks)
    out: list[float] = []
    for b in blocks:
        out.extend(b)
    return out[:len(values)]


def block_bootstrap(asset: str = "BTC", start_date: str = "2025-01-01",
                    end_date: str = "2026-01-01", *,
                    window_min: int = DEFAULT_WINDOW_MIN, horizon: int = 1,
                    block_hours: int = DEFAULT_BLOCK_HOURS,
                    n_iter: int = DEFAULT_ITERATIONS, seed: int = 7,
                    subset: str = "all", recompute: bool | None = None) -> dict:
    """
    Empirical significance for the IC, with autocorrelation preserved.

    WHY THE REPORTED t-STAT IS OPTIMISTIC

    A t-statistic assumes independent observations. Hourly crypto returns are
    neither independent nor thin-tailed: volatility clusters, so a run of large
    moves arrives together, and a signal that happens to be elevated during one
    such run gets credit for many "independent" correct calls that are really
    one call. The standard error is therefore too small and |t| too large,
    often by a factor of two or more.

    A plain i.i.d. shuffle of the returns does not fix this — it destroys the
    autocorrelation, which makes the null distribution *tighter* than reality
    and the observed IC look even more significant than the naive t-stat said.
    That is why a naive shuffle is the weakest possible control here, and why
    passing one means very little.

    A moving-block bootstrap resamples contiguous blocks of returns, so each
    draw keeps realistic volatility clustering and serial correlation inside
    the block while destroying any relationship to the signal. The resulting
    null is the honest one. Both nulls are reported side by side, because the
    gap between them is itself the diagnostic: a large gap means this dataset's
    autocorrelation is doing real work in the headline number.

    `block_hours` should comfortably exceed the horizon being tested; the
    default 24 covers a full daily cycle, which is where crypto's strongest
    intraday seasonality lives.
    """
    obs, meta = _observations(asset, start_date, end_date, window_min=window_min,
                              subset=subset, recompute=recompute,
                              horizons=(horizon,))
    if not obs:
        return meta

    # Drop the tail where the forward return does not exist, so the permuted
    # series has no holes to fall into.
    key = f"fwd_{horizon}h"
    trimmed = [o for o in obs if o.get(key) is not None]
    if len(trimmed) < 100:
        return {"error": "Fewer than 100 bars with a forward return — "
                         "bootstrap would be meaningless."}

    sig = [o["net_impact"] for o in trimmed]
    fwd = [o[key] for o in trimmed]
    active = [i for i, o in enumerate(trimmed) if o["article_count"] > 0]
    if len(active) < 50:
        return {"error": f"Only {len(active)} bars carry news — "
                         "not enough to bootstrap."}

    xs = [sig[i] for i in active]
    observed = _spearman(xs, [fwd[i] for i in active])

    rng = random.Random(seed)
    block_null: list[float] = []
    iid_null: list[float] = []
    for _ in range(n_iter):
        permuted = _block_permutation(fwd, block_hours, rng)
        block_null.append(_spearman(xs, [permuted[i] for i in active]))
        shuffled = fwd[:]
        rng.shuffle(shuffled)
        iid_null.append(_spearman(xs, [shuffled[i] for i in active]))

    def summarize(null: list[float], name: str) -> dict:
        mean = statistics.mean(null)
        sd = statistics.pstdev(null)
        pct = 100.0 * sum(1 for v in null if v < observed) / len(null)
        p_two = (sum(1 for v in null if abs(v - mean) >= abs(observed - mean))
                 + 1) / (len(null) + 1)
        return {
            "null": name,
            "null_mean": round(mean, 5),
            "null_sd": round(sd, 5),
            "percentile": round(pct, 2),
            "p_value_two_sided": round(p_two, 4),
            "z": round((observed - mean) / sd, 3) if sd else None,
        }

    block_stats = summarize(block_null, f"moving block ({block_hours}h)")
    iid_stats = summarize(iid_null, "i.i.d. shuffle")

    naive_t = _ic_tstat(observed, len(active))
    z = block_stats["z"]
    inflation = round(abs(naive_t) / abs(z), 2) if z else None

    if block_stats["p_value_two_sided"] > 0.10:
        verdict = "NOT SIGNIFICANT"
        reading = ("The observed IC sits comfortably inside the null. "
                   "Whatever the t-stat says, this is noise.")
    elif block_stats["p_value_two_sided"] > 0.05:
        verdict = "MARGINAL"
        reading = "Borderline against an honest null. Do not size on this yet."
    else:
        verdict = "SIGNIFICANT"
        reading = ("Survives a null that preserves autocorrelation, which is "
                   "a materially harder test than the t-stat.")

    return {
        "asset": asset.upper(),
        "symbol": meta["symbol"],
        "start": start_date,
        "end": end_date,
        "horizon_h": horizon,
        "window_min": window_min,
        "subset": subset,
        "n": len(active),
        "block_hours": block_hours,
        "iterations": n_iter,
        "seed": seed,
        "observed_ic": round(observed, 5),
        "naive_tstat": round(naive_t, 3),
        "block": block_stats,
        "iid": iid_stats,
        "tstat_inflation": inflation,
        "verdict": verdict,
        "reading": reading,
    }


# ---------------------------------------------------------------------------
# 2b. Time-shift placebo
# ---------------------------------------------------------------------------

def time_shift_placebo(asset: str = "BTC", start_date: str = "2025-01-01",
                       end_date: str = "2026-01-01", *,
                       shifts: tuple[int, ...] = DEFAULT_SHIFTS,
                       window_min: int = DEFAULT_WINDOW_MIN, horizon: int = 1,
                       subset: str = "all",
                       recompute: bool | None = None) -> dict:
    """
    Re-run the IC with every article's timestamp displaced in time.

    The logic: if the signal works because specific news moved a specific
    price at a specific hour, then breaking that alignment must destroy it.
    Displace every article by two days and the IC should collapse to roughly
    zero.

    If it does NOT collapse, the signal was never about the news. It is
    tracking something that varies slowly enough that a 48-hour displacement
    barely changes it — news *volume* rising through a bull market, a seasonal
    pattern in publication times, a regime variable that both the article flow
    and the price respond to. Those can all produce a real, reproducible,
    completely untradeable IC.

    Negative shifts (news moved earlier) are tested alongside positive ones,
    because the two fail differently: a signal that survives a negative shift
    specifically is consistent with information leaking into price before
    publication.

    Articles are loaded over a padded range so that a shifted article still has
    somewhere to land, and the shift=0 baseline uses that same padded set, so
    the comparison is not confounded by a different article population.
    """
    asset = asset.upper()
    symbol = ASSETS.get(asset, {}).get("pair")
    if not symbol:
        return {"error": f"{asset} has no tradable pair configured"}

    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)

    prices = load_prices(symbol, start, end)
    if not prices:
        return {"error": f"No {symbol} candles in range. Run: "
                         f"python main.py backfill {start_date} {end_date}"}

    pad = timedelta(hours=max(abs(s) for s in shifts) + 24)
    articles = load_articles(asset, start - pad, end + pad, subset=subset,
                             recompute=recompute)
    if not articles:
        return {"error": "No scored articles for this asset in range."}

    key = f"fwd_{horizon}h"

    def ic_for(shift_h: int) -> tuple[float | None, int]:
        if shift_h:
            delta = timedelta(hours=shift_h)
            # A uniform shift preserves ordering, so the list stays sorted by
            # `_published_dt` and replay's two-pointer sweep stays valid.
            shifted = [{**a, "_published_dt": a["_published_dt"] + delta}
                       for a in articles]
        else:
            shifted = articles
        obs = replay(asset, shifted, prices, window_min, (horizon,))
        pairs = [(o["net_impact"], o[key]) for o in obs
                 if o["article_count"] > 0 and o.get(key) is not None]
        if len(pairs) < 30:
            return None, len(pairs)
        return _spearman([p[0] for p in pairs], [p[1] for p in pairs]), len(pairs)

    true_ic, true_n = ic_for(0)
    if true_ic is None:
        return {"error": "Too few news-bearing bars to measure a baseline IC."}

    results: list[dict] = []
    for s in sorted(shifts):
        ic, n = ic_for(s)
        retained = (round(ic / true_ic, 3)
                    if ic is not None and true_ic else None)
        results.append({"shift_h": s, "n": n,
                        "ic": round(ic, 5) if ic is not None else None,
                        "retained_vs_true": retained})

    survivals = [abs(r["retained_vs_true"]) for r in results
                 if r["retained_vs_true"] is not None]
    worst = max(survivals) if survivals else None

    if worst is None:
        verdict, reading = "INCONCLUSIVE", "No shift produced enough bars."
    elif worst >= SURVIVAL_ALARM:
        verdict = "LEAKING"
        reading = (f"{worst:.0%} of the IC survives a displacement of up to "
                   f"{max(abs(s) for s in shifts)}h. The signal is not "
                   "reacting to individual news; it is tracking a slow "
                   "variable. Treat the headline IC as unearned.")
    elif worst >= 0.25:
        verdict = "PARTIAL"
        reading = ("Some IC survives misalignment. A slow component is mixed "
                   "into the signal — worth isolating before trusting the "
                   "aggregate number.")
    else:
        verdict = "CLEAN"
        reading = ("The IC collapses when news and price are misaligned, "
                   "which is what a genuine event-driven signal does.")

    return {
        "asset": asset,
        "symbol": symbol,
        "start": start_date,
        "end": end_date,
        "horizon_h": horizon,
        "window_min": window_min,
        "subset": subset,
        "articles": len(articles),
        "true_ic": round(true_ic, 5),
        "true_n": true_n,
        "shifts": results,
        "max_survival": round(worst, 3) if worst is not None else None,
        "verdict": verdict,
        "reading": reading,
    }


# ---------------------------------------------------------------------------
# 2c. Placebo events
# ---------------------------------------------------------------------------

def placebo_events(asset: str = "BTC", start_date: str = "2025-01-01",
                   end_date: str = "2026-01-01", *,
                   pre: int = event_study.PRE_WINDOW,
                   post: int = event_study.POST_WINDOW,
                   n_draws: int = 500, seed: int = 11,
                   min_relevance: float = 0.5,
                   min_corroboration: int = 1,
                   dedupe_hours: float = 6.0) -> dict:
    """
    Feed random timestamps through the event-study machinery.

    An event study is a powerful tool and a generous one: give it a set of
    timestamps and it will hand back a mean CAR and a t-statistic whether or
    not anything happened at those times. Crypto's fat tails mean a handful of
    randomly chosen hours can easily average to a CAR that clears |t| >= 2.

    This draws `n_draws` synthetic studies, each using the same number of
    events as the real one and the same mix of expected directions, but with
    timestamps sampled uniformly from the valid bar range. That produces:

      * a null distribution for mean CAR, and the real study's percentile
        within it;
      * the same for the signed-by-prior return, which is the number the
        event study actually reports as its bottom line;
      * a FALSE POSITIVE RATE — the share of purely random studies that would
        have been reported as significant. If that rate is 15%, then the `*`
        markers in the event-study output mean much less than they appear to,
        and any single starred category should be treated as unproven.

    Directions are drawn as a permutation of the real study's own direction
    multiset rather than resampled, so the signed null has an identical
    bull/bear mix to the study it is being compared against.
    """
    init_db()
    asset = asset.upper()
    symbol = ASSETS.get(asset, {}).get("pair")
    if not symbol:
        return {"error": f"{asset} has no tradable pair configured"}

    real = event_study.run_study(
        asset, start_date, end_date, pre=pre, post=post,
        min_relevance=min_relevance, min_corroboration=min_corroboration,
        dedupe_hours=dedupe_hours, save=False)
    if "error" in real:
        return real

    n_events = real["events_measured"]
    if n_events < event_study.MIN_EVENTS:
        return {"error": f"Only {n_events} real events measured — too few to "
                         "place against a null."}

    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
    pad = timedelta(hours=event_study.ESTIMATION_BARS
                    + event_study.ESTIMATION_GAP + 10)
    candles = get_candles(symbol, 3600, (start - pad).isoformat(),
                          end.isoformat())
    closes = [float(c["close"]) for c in candles]
    rets = event_study._returns(closes)

    # compute_car needs a full estimation window behind the event and a full
    # post window ahead of it; anything outside that range would return None.
    lo = event_study.ESTIMATION_BARS + event_study.ESTIMATION_GAP + pre
    hi = len(rets) - post - 1
    if hi - lo < n_events * 2:
        return {"error": "Not enough candle history to draw placebo events. "
                         "Backfill a wider date range."}

    # The real study's direction mix, taken from the taxonomy rather than
    # re-detecting events (categories the study skipped for low n still
    # contribute their count here, exactly as they do to events_measured).
    pool: list[int] = []
    for cat, info in real["categories"].items():
        direction = event_study.EVENT_TYPES.get(cat, (None, 0))[1]
        pool.extend([direction] * info["n"])
    pool = pool[:n_events] or [0] * n_events

    rng = random.Random(seed)
    mean_cars: list[float] = []
    signed_cars: list[float] = []
    significant = 0

    for _ in range(n_draws):
        cars: list[float] = []
        for _ in range(n_events):
            idx = rng.randint(lo, hi)
            car = event_study.compute_car(
                rets, idx, event_study.ESTIMATION_BARS,
                event_study.ESTIMATION_GAP, pre, post)
            if car is not None:
                cars.append(car["car_post"])
        if len(cars) < 3:
            continue
        dirs = pool[:len(cars)]
        rng.shuffle(dirs)
        mean_cars.append(100 * statistics.mean(cars))
        signed_cars.append(100 * statistics.mean(
            [c * d for c, d in zip(cars, dirs)]))
        if abs(_tstat(cars)) >= 2.0:
            significant += 1

    if not mean_cars:
        return {"error": "Every placebo draw failed to produce usable CARs."}

    real_mean = real["overall"]["mean_car_post_pct"]
    real_signed = real["overall"]["signed_by_prior_mean_pct"]

    def place(value: float | None, null: list[float]) -> dict:
        if value is None:
            return {"value": None}
        mean = statistics.mean(null)
        sd = statistics.pstdev(null)
        return {
            "value": value,
            "null_mean": round(mean, 4),
            "null_sd": round(sd, 4),
            "percentile": round(100.0 * sum(1 for v in null if v < value)
                                / len(null), 2),
            "p_value_two_sided": round(
                (sum(1 for v in null if abs(v - mean) >= abs(value - mean)) + 1)
                / (len(null) + 1), 4),
            "z": round((value - mean) / sd, 3) if sd else None,
        }

    fpr = 100.0 * significant / len(mean_cars)
    signed_p = place(real_signed, signed_cars).get("p_value_two_sided")

    if signed_p is None:
        verdict, reading = "INCONCLUSIVE", "No signed result to place."
    elif signed_p > 0.10:
        verdict = "NOT SIGNIFICANT"
        reading = ("Random timestamps produce results as good as the real "
                   "events. The taxonomy is not identifying anything the "
                   "price cares about.")
    elif fpr >= 10.0:
        verdict = "SIGNIFICANT (fragile)"
        reading = (f"The real study beats the null, but {fpr:.0f}% of random "
                   "studies would have been reported as significant too. "
                   "Individual starred categories are not trustworthy on "
                   "their own.")
    else:
        verdict = "SIGNIFICANT"
        reading = ("The real events clearly beat random timestamps, and the "
                   "machinery's own false-positive rate is low.")

    return {
        "asset": asset,
        "symbol": symbol,
        "start": start_date,
        "end": end_date,
        "window": {"pre_bars": pre, "post_bars": post},
        "real_events": n_events,
        "draws": len(mean_cars),
        "seed": seed,
        "mean_car": place(real_mean, mean_cars),
        "signed_car": place(real_signed, signed_cars),
        "false_positive_rate_pct": round(fpr, 2),
        "verdict": verdict,
        "reading": reading,
    }


# ---------------------------------------------------------------------------
# 2d. Wrong-asset control
# ---------------------------------------------------------------------------

def backfill_control_series(symbols: tuple[str, ...] = DEFAULT_CONTROLS,
                            start_date: str = "2025-01-01",
                            end_date: str = "2026-01-01") -> dict:
    """
    Pull hourly candles for non-crypto control series into `ohlcv`.

    Uses yfinance (already a dependency) because Coinbase only serves crypto
    pairs. Note yfinance caps *hourly* history at roughly 730 days, so a
    control range far in the past will come back empty — that is a data limit,
    not a bug, and the wrong-asset test simply reports the symbol as
    unavailable.

    Control series are stored in the same `ohlcv` table as the crypto pairs.
    They are never traded and never scored; they exist only to be a price
    series that the news signal has no business predicting.
    """
    init_db()
    try:
        import yfinance as yf
    except ImportError:
        return {"error": "yfinance is not installed. "
                         "pip install -r requirements.txt"}

    out: dict = {}
    for sym in symbols:
        try:
            df = yf.download(sym, start=start_date, end=end_date, interval="1h",
                             auto_adjust=False, progress=False,
                             actions=False, threads=False)
        except Exception as exc:
            out[sym] = {"error": str(exc)}
            continue

        if df is None or df.empty:
            out[sym] = {"candles": 0,
                        "note": "no data returned — hourly history from "
                                "yfinance only reaches back ~730 days"}
            continue

        # Recent yfinance versions return MultiIndex columns even for one
        # ticker; flatten so the column lookups below work either way.
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)

        rows = []
        for ts, r in df.iterrows():
            try:
                dt = ts.to_pydatetime()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                rows.append((dt.astimezone(timezone.utc)
                             .isoformat(timespec="seconds"),
                             float(r["Open"]), float(r["High"]),
                             float(r["Low"]), float(r["Close"]),
                             float(r.get("Volume", 0) or 0)))
            except (KeyError, TypeError, ValueError):
                continue
        out[sym] = {"candles": upsert_candles(sym, 3600, rows)}
        logger.info("control series %s -> %d candles", sym, out[sym]["candles"])
    return out


def wrong_asset_control(asset: str = "BTC", start_date: str = "2025-01-01",
                        end_date: str = "2026-01-01", *,
                        control_symbols: tuple[str, ...] = DEFAULT_CONTROLS,
                        window_min: int = DEFAULT_WINDOW_MIN, horizon: int = 1,
                        subset: str = "all",
                        recompute: bool | None = None) -> dict:
    """
    Run the crypto news signal against price series it should not predict.

    A BTC news signal that predicts SPY as well as it predicts BTC is not a
    BTC news signal. It is a risk-appetite proxy: the same macro conditions
    that make crypto headlines skew bullish also lift equities, so the
    correlation is real, reproducible, and adds nothing that a cheaper
    risk-on/risk-off indicator would not already give.

    Some leakage is expected and correct — the lexicon contains genuine macro
    phrases (rate cuts, CPI, tariffs) that move every risk asset, and those
    SHOULD show up on SPY. The question is one of degree, which is why the
    ratio of control IC to native IC is the headline number rather than the
    control IC alone.

    Control bars do not align with crypto bars — equity markets close, crypto
    does not — and that is fine. The replay evaluates the signal at whatever
    bar timestamps it is given, so the control simply asks "what was the news
    signal at each SPY bar, and did it predict the next SPY bar?"
    """
    asset = asset.upper()
    native_symbol = ASSETS.get(asset, {}).get("pair")
    if not native_symbol:
        return {"error": f"{asset} has no tradable pair configured"}

    start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)

    articles = load_articles(asset, start, end, subset=subset,
                             recompute=recompute)
    if not articles:
        return {"error": "No scored articles for this asset in range."}

    key = f"fwd_{horizon}h"

    def ic_on(symbol: str) -> tuple[float | None, int]:
        prices = load_prices(symbol, start, end)
        if not prices:
            return None, 0
        obs = replay(asset, articles, prices, window_min, (horizon,))
        pairs = [(o["net_impact"], o[key]) for o in obs
                 if o["article_count"] > 0 and o.get(key) is not None]
        if len(pairs) < 30:
            return None, len(pairs)
        return _spearman([p[0] for p in pairs], [p[1] for p in pairs]), len(pairs)

    native_ic, native_n = ic_on(native_symbol)
    if native_ic is None:
        return {"error": f"No usable {native_symbol} bars in range. Run: "
                         f"python main.py backfill {start_date} {end_date}"}

    controls: list[dict] = []
    for sym in control_symbols:
        ic, n = ic_on(sym)
        if ic is None:
            controls.append({
                "symbol": sym, "available": False, "n": n,
                "hint": f"python main.py controls {start_date} {end_date}",
            })
            continue
        controls.append({
            "symbol": sym, "available": True, "n": n, "ic": round(ic, 5),
            "tstat": round(_ic_tstat(ic, n), 3),
            "ratio_vs_native": (round(abs(ic) / abs(native_ic), 3)
                                if native_ic else None),
        })

    ratios = [c["ratio_vs_native"] for c in controls
              if c.get("ratio_vs_native") is not None]
    worst = max(ratios) if ratios else None

    if worst is None:
        verdict = "UNAVAILABLE"
        reading = ("No control candles loaded. Run `python main.py controls` "
                   "to fetch them, then re-run.")
    elif worst >= 0.75:
        verdict = "GENERIC"
        reading = ("The signal predicts a non-crypto asset nearly as well as "
                   "its own. This is risk-appetite beta, not crypto news.")
    elif worst >= 0.4:
        verdict = "PARTLY GENERIC"
        reading = ("A substantial part of the signal is market-wide. Expect "
                   "the crypto-specific edge to be smaller than the headline "
                   "IC implies.")
    else:
        verdict = "SPECIFIC"
        reading = ("The signal is much weaker on control assets, which is "
                   "what an asset-specific news signal should look like.")

    return {
        "asset": asset,
        "native_symbol": native_symbol,
        "start": start_date,
        "end": end_date,
        "horizon_h": horizon,
        "window_min": window_min,
        "subset": subset,
        "articles": len(articles),
        "native_ic": round(native_ic, 5),
        "native_n": native_n,
        "controls": controls,
        "max_ratio": worst,
        "verdict": verdict,
        "reading": reading,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_all(asset: str = "BTC", start_date: str = "2025-01-01",
            end_date: str = "2026-01-01", *,
            window_min: int = DEFAULT_WINDOW_MIN, horizon: int = 1,
            max_lag: int = DEFAULT_MAX_LAG,
            block_hours: int = DEFAULT_BLOCK_HOURS,
            n_iter: int = DEFAULT_ITERATIONS,
            shifts: tuple[int, ...] = DEFAULT_SHIFTS,
            n_draws: int = 500,
            control_symbols: tuple[str, ...] = DEFAULT_CONTROLS,
            seed: int = 7, save: bool = True, label: str = "") -> dict:
    """
    Run every validation in order and collect the verdicts.

    Ordering is deliberate: lead-lag runs first because if the signal turns out
    to be reactive, the significance of its forward IC is beside the point —
    a reactive signal can be highly significant and still worthless.

    Any individual test may return an `error` key (usually missing data). The
    others still run; the summary reports what was reachable.
    """
    init_db()
    asset = asset.upper()
    results: dict = {
        "asset": asset, "start": start_date, "end": end_date,
        "params": {"window_min": window_min, "horizon": horizon,
                   "max_lag": max_lag, "block_hours": block_hours,
                   "iterations": n_iter, "shifts": list(shifts),
                   "placebo_draws": n_draws,
                   "control_symbols": list(control_symbols), "seed": seed},
        "tests": {},
    }

    logger.info("lead-lag ...")
    results["tests"]["lead_lag"] = lead_lag(
        asset, start_date, end_date, window_min=window_min, max_lag=max_lag)

    logger.info("lexicon split ...")
    results["tests"]["lexicon_split"] = lexicon_split(
        asset, start_date, end_date, window_min=window_min, max_lag=max_lag)

    logger.info("block bootstrap ...")
    results["tests"]["block_bootstrap"] = block_bootstrap(
        asset, start_date, end_date, window_min=window_min, horizon=horizon,
        block_hours=block_hours, n_iter=n_iter, seed=seed)

    logger.info("time-shift placebo ...")
    results["tests"]["time_shift"] = time_shift_placebo(
        asset, start_date, end_date, shifts=shifts, window_min=window_min,
        horizon=horizon)

    logger.info("placebo events ...")
    results["tests"]["placebo_events"] = placebo_events(
        asset, start_date, end_date, n_draws=n_draws, seed=seed)

    logger.info("wrong-asset control ...")
    results["tests"]["wrong_asset"] = wrong_asset_control(
        asset, start_date, end_date, control_symbols=control_symbols,
        window_min=window_min, horizon=horizon)

    results["summary"] = {
        name: (res.get("verdict") or "ERROR")
        for name, res in results["tests"].items()
        if isinstance(res, dict) and "subsets" not in res
    }
    ls = results["tests"]["lexicon_split"]
    if "summary" in ls:
        results["summary"]["lexicon_split"] = (
            f"event={ls['summary']['event_verdict']} / "
            f"price_action={ls['summary']['price_action_verdict']}")

    if save:
        results["run_id"] = save_backtest(
            label or f"validation {asset} {start_date}..{end_date}",
            asset, start_date, end_date,
            {"type": "validation", **results["params"]}, results)
    return results
