"""
Apollo — backtest engine.

Replays stored articles chronologically against historical price candles and
measures whether the signal had any predictive power.

LOOK-AHEAD PROTECTION (the thing that makes or breaks a backtest):

  1. Impact is RECOMPUTED at each simulated timestamp, never read from the
     `articles.impact` column. That stored value was computed at ingest time
     with `recency_factor` measured against the wall clock of ingestion — using
     it here would leak information about when we happened to scrape, not when
     the news broke. `_impact_at()` below recomputes decay relative to the
     simulated evaluation time instead.

  2. Only articles with `published_at <= t` are visible at time t.

  3. Forward returns are measured from the candle CLOSE at t to the close at
     t+h. Signals computed from news published up to t are therefore acted on
     at a price that was still available after that news existed.

  4. Articles with no `published_at` are excluded entirely rather than assumed
     fresh — an unknown timestamp cannot be safely placed in time.

Metrics are computed in pure Python (no scipy/numpy) so this has no extra
dependencies beyond what the collectors already need.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import datetime, timedelta, timezone

from config import ASSETS
from scoring import aggregate_signal
from store import get_candles, get_conn, init_db, save_backtest

logger = logging.getLogger("BACKTEST")

DEFAULT_HORIZONS = (1, 4, 24)          # hours ahead to measure
DEFAULT_WINDOW_MIN = 60                # trailing news window feeding a signal
RECENCY_HALF_LIFE_MIN = 180.0
FEE_BPS = 10.0                         # 0.10% per side, ~retail taker fee


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _impact_at(row: dict, now: datetime) -> float:
    """
    Recompute an article's impact as of `now`.

    Mirrors scoring.score_article's impact formula, but with recency measured
    against the simulated clock. See the look-ahead note in the module docstring.
    """
    published = row["_published_dt"]
    age_min = (now - published).total_seconds() / 60.0
    if age_min < 0:
        return 0.0                      # not yet published — invisible
    recency = 2 ** (-age_min / RECENCY_HALF_LIFE_MIN)
    magnitude = 1.35 if row["_high_impact"] else 1.0
    return (row["_sentiment"] * row["_relevance"] * row["_source_weight"]
            * recency * magnitude)


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation, average ranks for ties. Returns 0.0 if degenerate."""
    n = len(xs)
    if n < 3:
        return 0.0

    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx and dy else 0.0


def _tstat(vals: list[float]) -> float:
    """One-sample t against zero mean."""
    n = len(vals)
    if n < 2:
        return 0.0
    sd = statistics.pstdev(vals)
    if sd == 0:
        return 0.0
    return (sum(vals) / n) / (sd / math.sqrt(n))


def _max_drawdown(equity: list[float]) -> float:
    peak, mdd = equity[0] if equity else 1.0, 0.0
    for v in equity:
        peak = max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_articles(asset: str, start: datetime, end: datetime) -> list[dict]:
    """
    Pull scored articles for one asset, pre-parsing everything the replay loop
    needs so the hot loop does no JSON or date parsing.
    """
    rows = get_conn().execute(
        """SELECT id, title, source, source_weight, published_at, sentiment,
                  assets, score_detail
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
            d["_published_dt"] = _dt(d["published_at"])
        except (ValueError, TypeError):
            continue                    # unparseable timestamp — drop it

        detail = {}
        if d.get("score_detail"):
            try:
                detail = json.loads(d["score_detail"])
            except json.JSONDecodeError:
                detail = {}

        rel = (detail.get("asset_relevance") or {}).get(asset)
        if rel is None:
            continue                    # not actually scored for this asset
        d["_relevance"] = float(rel)
        d["_sentiment"] = float(d.get("sentiment") or 0.0)
        d["_source_weight"] = float(d.get("source_weight") or 1.0)
        d["_high_impact"] = bool(detail.get("high_impact_event"))
        out.append(d)
    return out


def load_prices(symbol: str, start: datetime, end: datetime) -> list[dict]:
    rows = get_candles(symbol, 3600, start.isoformat(), end.isoformat())
    return [{"ts": _dt(r["ts"]), "close": float(r["close"]),
             "open": float(r["open"]), "volume": float(r["volume"] or 0)}
            for r in rows if r["close"] is not None]


# ---------------------------------------------------------------------------
# Core replay
# ---------------------------------------------------------------------------

def replay(asset: str, articles: list[dict], prices: list[dict],
           window_min: int = DEFAULT_WINDOW_MIN,
           horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> list[dict]:
    """
    Walk the price series bar by bar, building the signal from only-visible
    news and attaching forward returns.
    """
    if not prices:
        return []

    closes = [p["close"] for p in prices]
    obs: list[dict] = []
    window = timedelta(minutes=window_min)

    # Two-pointer sweep over time-ordered articles: lo/hi bound the trailing
    # window, so total work is O(articles + bars) rather than O(bars × articles).
    lo = hi = 0
    n_art = len(articles)

    for i, bar in enumerate(prices):
        t = bar["ts"]
        w_start = t - window

        while hi < n_art and articles[hi]["_published_dt"] <= t:
            hi += 1
        while lo < hi and articles[lo]["_published_dt"] < w_start:
            lo += 1

        visible = articles[lo:hi]
        if visible:
            scored = [{"impact": _impact_at(a, t), "sentiment": a["_sentiment"],
                       "title": a["title"], "source": a["source"]}
                      for a in visible]
            agg = aggregate_signal(scored, window_min)
        else:
            agg = aggregate_signal([], window_min)

        rec = {
            "ts": t.isoformat(),
            "close": bar["close"],
            "article_count": agg["article_count"],
            "net_impact": agg["net_impact"],
            "mean_sentiment": agg["mean_sentiment"],
            "direction": agg["direction"],
            "confidence": agg["confidence"],
        }
        for h in horizons:
            j = i + h
            rec[f"fwd_{h}h"] = ((closes[j] - closes[i]) / closes[i]
                                if j < len(closes) and closes[i] else None)
        obs.append(rec)

    return obs


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def evaluate(obs: list[dict], horizons: tuple[int, ...] = DEFAULT_HORIZONS,
             min_abs_impact: float = 0.05) -> dict:
    """
    Turn replay observations into predictive-power statistics.

    Every metric is computed only over bars where a signal actually existed
    (article_count > 0), because scoring a flat "no news" bar as a correct
    NEUTRAL call would inflate accuracy meaninglessly.
    """
    active = [o for o in obs if o["article_count"] > 0]
    results: dict = {
        "bars_total": len(obs),
        "bars_with_news": len(active),
        "news_coverage_pct": round(100.0 * len(active) / len(obs), 2) if obs else 0.0,
        "horizons": {},
    }
    if not active:
        results["warning"] = ("No bars had any news. Backfill news before "
                              "interpreting this run.")
        return results

    for h in horizons:
        key = f"fwd_{h}h"
        pairs = [(o["net_impact"], o[key]) for o in active if o.get(key) is not None]
        if len(pairs) < 10:
            results["horizons"][f"{h}h"] = {"n": len(pairs),
                                            "note": "insufficient data"}
            continue

        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]

        # Directional accuracy, restricted to bars with a real signal.
        directional = [(x, y) for x, y in pairs if abs(x) >= min_abs_impact]
        hits = sum(1 for x, y in directional if (x > 0) == (y > 0))
        hit_rate = hits / len(directional) if directional else None

        # Deciles of signal strength vs. mean forward return. A monotone
        # gradient here is the strongest evidence the signal carries anything.
        ranked = sorted(pairs, key=lambda p: p[0])
        dec_size = max(1, len(ranked) // 10)
        deciles = []
        for d in range(10):
            chunk = ranked[d * dec_size:(d + 1) * dec_size] if d < 9 else ranked[9 * dec_size:]
            if chunk:
                deciles.append({
                    "decile": d + 1,
                    "n": len(chunk),
                    "mean_signal": round(sum(c[0] for c in chunk) / len(chunk), 4),
                    "mean_fwd_return_pct": round(
                        100 * sum(c[1] for c in chunk) / len(chunk), 4),
                })

        by_direction: dict[str, dict] = {}
        for d in ("STRONG_BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONG_BEARISH"):
            sub = [o[key] for o in active
                   if o["direction"] == d and o.get(key) is not None]
            if sub:
                by_direction[d] = {
                    "n": len(sub),
                    "mean_fwd_return_pct": round(100 * sum(sub) / len(sub), 4),
                    "win_rate_pct": round(
                        100 * sum(1 for v in sub if v > 0) / len(sub), 2),
                }

        long_short = None
        if len(deciles) == 10:
            long_short = round(deciles[-1]["mean_fwd_return_pct"]
                               - deciles[0]["mean_fwd_return_pct"], 4)

        # Return series from naively following the signal's sign.
        signal_returns = [y if x > 0 else -y for x, y in directional]

        results["horizons"][f"{h}h"] = {
            "n": len(pairs),
            "n_directional": len(directional),
            "information_coefficient": round(_spearman(xs, ys), 4),
            "hit_rate_pct": round(100 * hit_rate, 2) if hit_rate is not None else None,
            "mean_fwd_return_all_pct": round(100 * sum(ys) / len(ys), 4),
            "signal_following_mean_pct": round(
                100 * sum(signal_returns) / len(signal_returns), 4)
            if signal_returns else None,
            "signal_following_tstat": round(_tstat(signal_returns), 3)
            if signal_returns else None,
            "long_short_decile_spread_pct": long_short,
            "deciles": deciles,
            "by_direction": by_direction,
        }

    return results


def simulate_strategy(obs: list[dict], *, threshold: float = 0.3,
                      min_confidence: float = 0.2, allow_short: bool = False,
                      fee_bps: float = FEE_BPS,
                      starting_cash: float = 10_000.0) -> dict:
    """
    Bar-by-bar long/flat (optionally long/short) simulation with fees.

    Deliberately simple: target position is a function of the current signal
    only. The point is to measure whether the signal is worth anything, not to
    optimize an execution strategy on top of it.
    """
    cash = starting_cash
    position = 0.0            # units of the asset
    equity_curve: list[float] = []
    timestamps: list[str] = []
    trades = 0
    fee_rate = fee_bps / 10_000.0

    for o in obs:
        price = o["close"]
        if not price:
            continue

        if o["confidence"] >= min_confidence and o["net_impact"] >= threshold:
            target = 1.0
        elif o["confidence"] >= min_confidence and o["net_impact"] <= -threshold:
            target = -1.0 if allow_short else 0.0
        else:
            target = 0.0

        equity = cash + position * price
        desired_units = (target * equity) / price
        delta = desired_units - position

        # Only trade on a meaningful change, so tiny signal jitter doesn't
        # churn the book and bleed fees.
        if abs(delta * price) > equity * 0.05:
            cash -= delta * price
            cash -= abs(delta * price) * fee_rate
            position = desired_units
            trades += 1

        equity_curve.append(cash + position * price)
        timestamps.append(o["ts"])

    if not equity_curve:
        return {"error": "no bars to simulate"}

    final = equity_curve[-1]
    first_price = next((o["close"] for o in obs if o["close"]), None)
    last_price = next((o["close"] for o in reversed(obs) if o["close"]), None)
    buy_hold = (starting_cash * last_price / first_price
                if first_price and last_price else starting_cash)

    rets = [equity_curve[i] / equity_curve[i - 1] - 1
            for i in range(1, len(equity_curve)) if equity_curve[i - 1]]
    sharpe = None
    if len(rets) > 2 and statistics.pstdev(rets):
        # Hourly bars -> annualization factor sqrt(24*365).
        sharpe = round((statistics.mean(rets) / statistics.pstdev(rets))
                       * math.sqrt(24 * 365), 3)

    return {
        "starting_cash": starting_cash,
        "ending_equity": round(final, 2),
        "return_pct": round(100 * (final / starting_cash - 1), 3),
        "buy_hold_equity": round(buy_hold, 2),
        "buy_hold_return_pct": round(100 * (buy_hold / starting_cash - 1), 3),
        "excess_vs_buy_hold_pct": round(100 * (final - buy_hold) / starting_cash, 3),
        "trades": trades,
        "max_drawdown_pct": round(100 * _max_drawdown(equity_curve), 3),
        "sharpe_annualized": sharpe,
        "params": {"threshold": threshold, "min_confidence": min_confidence,
                   "allow_short": allow_short, "fee_bps": fee_bps},
        "_equity_curve": equity_curve,
        "_timestamps": timestamps,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(asset: str = "BTC", start_date: str = "2025-01-01",
        end_date: str = "2026-01-01", window_min: int = DEFAULT_WINDOW_MIN,
        horizons: tuple[int, ...] = DEFAULT_HORIZONS,
        label: str = "", save: bool = True, **strategy_kwargs) -> dict:
    init_db()
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

    articles = load_articles(asset, start, end)
    logger.info("%s: %d bars, %d articles", asset, len(prices), len(articles))

    obs = replay(asset, articles, prices, window_min, horizons)
    metrics = evaluate(obs, horizons)
    strat = simulate_strategy(obs, **strategy_kwargs)

    # Equity curve is bulky; keep it out of the stored JSON blob.
    strat_stored = {k: v for k, v in strat.items() if not k.startswith("_")}

    results = {
        "asset": asset,
        "symbol": symbol,
        "start": start_date,
        "end": end_date,
        "window_min": window_min,
        "articles_loaded": len(articles),
        "metrics": metrics,
        "strategy": strat_stored,
    }

    if save:
        run_id = save_backtest(
            label or f"{asset} {start_date}..{end_date}", asset,
            start_date, end_date,
            {"window_min": window_min, "horizons": list(horizons),
             **strategy_kwargs},
            results)
        results["run_id"] = run_id

    results["_observations"] = obs
    results["_strategy_full"] = strat
    return results
