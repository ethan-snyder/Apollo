"""
Apollo — backtest & paper trading test suite (offline, no network).

The highest-value checks here are the look-ahead ones. A backtest that
accidentally sees the future produces beautiful, meaningless results, and the
failure is silent — so these are tested explicitly with planted data:

  * an article published AFTER a bar must contribute nothing at that bar
  * the same article must score LOWER at a later bar than a nearer one (decay
    measured against simulated time, not the wall clock)
  * a known-good signal must produce a positive IC, and an inverted one a
    negative IC, so the metric's sign convention is pinned down

    python test_backtest.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_TMP_DB = os.path.join(tempfile.mkdtemp(), "apollo_bt_test.db")
os.environ["APOLLO_DB"] = _TMP_DB

import store                                          # noqa: E402
import backtest                                       # noqa: E402
from paper_trader import Portfolio, parse_duration, PaperTrader  # noqa: E402

PASS, FAIL = 0, 0
BASE = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  pass  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"  -> {detail}" if detail else ""))


def art(hours: float, sentiment: float, relevance: float = 1.0,
        weight: float = 1.0, high: bool = False) -> dict:
    """Build a pre-parsed article row like load_articles() would return."""
    return {
        "id": int(hours * 100), "title": f"art@{hours}h", "source": "t",
        "_published_dt": BASE + timedelta(hours=hours),
        "_sentiment": sentiment, "_relevance": relevance,
        "_source_weight": weight, "_high_impact": high,
    }


def bars(n: int, prices: list[float] | None = None) -> list[dict]:
    return [{"ts": BASE + timedelta(hours=i),
             "close": (prices[i] if prices else 100.0 + i),
             "open": 100.0, "volume": 1.0}
            for i in range(n)]


# ---------------------------------------------------------------------------
print("\nDuration parsing")
for s, expected in [("1h", 3600), ("30m", 1800), ("7d", 604800),
                    ("2w", 1209600), ("45s", 45), ("0", 0), (None, 0),
                    ("forever", 0), ("90", 90)]:
    check(f"{s!r} -> {expected}", parse_duration(s) == expected,
          str(parse_duration(s)))

# ---------------------------------------------------------------------------
print("\nLook-ahead protection")

now = BASE + timedelta(hours=5)
future = art(hours=10, sentiment=1.0)
check("future article contributes zero impact",
      backtest._impact_at(future, now) == 0.0,
      str(backtest._impact_at(future, now)))

past = art(hours=4, sentiment=1.0)
check("past article contributes non-zero impact",
      backtest._impact_at(past, now) > 0)

near = backtest._impact_at(art(hours=4.5, sentiment=1.0), now)
far = backtest._impact_at(art(hours=1.0, sentiment=1.0), now)
check("older article decays below newer one", far < near, f"{far} vs {near}")

# Decay must track SIMULATED time, not wall-clock. Same article, two eval
# times: the later evaluation must score it lower.
a = art(hours=1, sentiment=1.0)
e1 = backtest._impact_at(a, BASE + timedelta(hours=2))
e2 = backtest._impact_at(a, BASE + timedelta(hours=8))
check("decay measured against simulated clock", e2 < e1, f"{e1} -> {e2}")

# Half-life sanity: exactly 3h later should be ~half.
h0 = backtest._impact_at(art(0, 1.0), BASE)
h3 = backtest._impact_at(art(0, 1.0), BASE + timedelta(hours=3))
check("3h half-life halves the impact", abs(h3 / h0 - 0.5) < 0.01,
      f"ratio={h3 / h0:.4f}")

check("high-impact flag raises magnitude",
      backtest._impact_at(art(0, 1.0, high=True), BASE)
      > backtest._impact_at(art(0, 1.0, high=False), BASE))

# replay() must never include an article published after the bar.
planted = [art(hours=0.5, sentiment=1.0), art(hours=50, sentiment=-1.0)]
obs = backtest.replay("BTC", planted, bars(10), window_min=600)
early = obs[1]
check("replay excludes not-yet-published news",
      early["net_impact"] > 0,
      f"net={early['net_impact']} (the -1.0 article at h=50 leaked in)")
check("replay counts only visible articles at each bar",
      all(o["article_count"] <= 1 for o in obs[:10]),
      str([o["article_count"] for o in obs[:10]]))

# ---------------------------------------------------------------------------
print("\nStatistical helpers")

check("spearman perfect positive == 1",
      abs(backtest._spearman([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) - 1.0) < 1e-9)
check("spearman perfect negative == -1",
      abs(backtest._spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) + 1.0) < 1e-9)
check("spearman is monotone-invariant (not linear-only)",
      abs(backtest._spearman([1, 2, 3, 4, 5], [1, 4, 9, 16, 25]) - 1.0) < 1e-9)
check("spearman handles ties without crashing",
      -1.0 <= backtest._spearman([1, 1, 1, 2, 2], [3, 3, 1, 2, 2]) <= 1.0)
check("spearman degenerate input returns 0",
      backtest._spearman([1, 2], [1, 2]) == 0.0)

check("tstat of zero-mean sample is ~0",
      abs(backtest._tstat([-1.0, 1.0, -1.0, 1.0])) < 1e-9)
check("tstat of consistent positive sample is positive",
      backtest._tstat([0.1, 0.12, 0.09, 0.11]) > 3)
check("tstat of constant sample returns 0 (no variance)",
      backtest._tstat([0.5, 0.5, 0.5]) == 0.0)

check("max drawdown of rising curve is 0",
      backtest._max_drawdown([1, 2, 3, 4]) == 0.0)
check("max drawdown of -50% dip is -0.5",
      abs(backtest._max_drawdown([100, 50, 80]) + 0.5) < 1e-9)

# ---------------------------------------------------------------------------
print("\nIC sign convention (planted signal)")

# Build a series where positive signal genuinely precedes a price rise.
n = 200
prices, arts = [], []
price = 100.0
for i in range(n):
    up = (i // 10) % 2 == 0
    prices.append(price)
    price *= 1.01 if up else 0.99
    # News published just before bar i, pointing the right way.
    arts.append(art(hours=i - 0.1, sentiment=1.0 if up else -1.0))

obs = backtest.replay("BTC", arts, bars(n, prices), window_min=60)
res = backtest.evaluate(obs, horizons=(1,))
ic = res["horizons"]["1h"]["information_coefficient"]
hit = res["horizons"]["1h"]["hit_rate_pct"]
check("planted predictive signal yields positive IC", ic > 0.3, f"IC={ic}")
check("planted predictive signal beats 50% hit rate", (hit or 0) > 60,
      f"hit={hit}")

# Invert the news and the IC must flip sign — pins the convention down.
inv = [art(hours=a["_published_dt"].hour + a["_published_dt"].minute / 60 - 0.0,
           sentiment=-a["_sentiment"]) for a in arts]
inv = [dict(a, _sentiment=-a["_sentiment"]) for a in arts]
obs_i = backtest.replay("BTC", inv, bars(n, prices), window_min=60)
ic_i = backtest.evaluate(obs_i, horizons=(1,))["horizons"]["1h"][
    "information_coefficient"]
check("inverted signal yields negative IC", ic_i < 0, f"IC={ic_i}")

# ---------------------------------------------------------------------------
print("\nEvaluate / edge cases")

empty = backtest.evaluate([], horizons=(1,))
check("evaluate([]) is safe", empty["bars_total"] == 0)

no_news = backtest.evaluate(
    [{"ts": "x", "close": 1.0, "article_count": 0, "net_impact": 0.0,
      "mean_sentiment": 0.0, "direction": "NEUTRAL", "confidence": 0.0,
      "fwd_1h": 0.01}], horizons=(1,))
check("all-quiet input produces an explicit warning",
      "warning" in no_news, str(no_news.keys()))

# ---------------------------------------------------------------------------
print("\nStrategy simulation")

flat_obs = [{"ts": f"t{i}", "close": 100.0, "article_count": 1,
             "net_impact": 0.0, "confidence": 0.0, "direction": "NEUTRAL"}
            for i in range(50)]
s = backtest.simulate_strategy(flat_obs)
check("no-signal simulation preserves capital exactly",
      abs(s["ending_equity"] - 10_000.0) < 1e-6, str(s["ending_equity"]))
check("no-signal simulation places no trades", s["trades"] == 0)

rising = [{"ts": f"t{i}", "close": 100.0 + i, "article_count": 5,
           "net_impact": 1.0, "confidence": 0.9, "direction": "BULLISH"}
          for i in range(50)]
s2 = backtest.simulate_strategy(rising)
check("always-long in a rising market gains", s2["return_pct"] > 0,
      str(s2["return_pct"]))
check("fees make strategy trail pure buy&hold",
      s2["ending_equity"] <= s2["buy_hold_equity"] + 1e-6,
      f"{s2['ending_equity']} vs {s2['buy_hold_equity']}")
check("drawdown is <= 0 by definition", s2["max_drawdown_pct"] <= 0)

falling = [{"ts": f"t{i}", "close": 200.0 - i, "article_count": 5,
            "net_impact": -1.0, "confidence": 0.9, "direction": "BEARISH"}
           for i in range(50)]
s3 = backtest.simulate_strategy(falling, allow_short=False)
check("bearish signal + no-short stays flat (no losses)",
      abs(s3["ending_equity"] - 10_000.0) < 1e-6, str(s3["ending_equity"]))
s4 = backtest.simulate_strategy(falling, allow_short=True)
check("bearish signal + short enabled profits in a downtrend",
      s4["ending_equity"] > 10_000.0, str(s4["ending_equity"]))

# ---------------------------------------------------------------------------
print("\nPortfolio accounting")

p = Portfolio(10_000.0)
check("starts flat", p.equity({}) == 10_000.0)

p.apply_fill("BTC", 0.1, 50_000.0, 0.001)     # buy 0.1 @ 50k = $5,000 + $5 fee
check("cash decreases by notional + fee",
      abs(p.cash - (10_000 - 5_000 - 5)) < 1e-6, str(p.cash))
check("position recorded", abs(p.positions["BTC"] - 0.1) < 1e-12)
check("equity = cash + mark-to-market",
      abs(p.equity({"BTC": 50_000.0}) - (p.cash + 5_000)) < 1e-6)
check("unrealized gain raises equity",
      p.equity({"BTC": 60_000.0}) > p.equity({"BTC": 50_000.0}))
check("cost basis recorded at entry", abs(p.cost_basis["BTC"] - 50_000) < 1e-6)

# Sell half at a profit -> realized P&L on the closed portion only.
_, realized = p.apply_fill("BTC", -0.05, 60_000.0, 0.001)
check("realized P&L booked on the closed portion",
      abs(realized - 0.05 * 10_000) < 1e-6, str(realized))
check("remaining position is correct",
      abs(p.positions["BTC"] - 0.05) < 1e-12, str(p.positions.get("BTC")))

# Close out entirely -> position and basis removed.
p.apply_fill("BTC", -0.05, 60_000.0, 0.001)
check("flat position is removed from the book", "BTC" not in p.positions)
check("cost basis cleared on full close", "BTC" not in p.cost_basis)
check("fees accumulated across all fills", p.fees_paid > 0)

# Averaging up should move basis between the two prices.
p2 = Portfolio(100_000.0)
p2.apply_fill("ETH", 1.0, 2_000.0, 0.0)
p2.apply_fill("ETH", 1.0, 4_000.0, 0.0)
check("averaging up rolls cost basis to the mean",
      abs(p2.cost_basis["ETH"] - 3_000.0) < 1e-6, str(p2.cost_basis["ETH"]))

# ---------------------------------------------------------------------------
print("\nPosition sizing")

t = PaperTrader(duration="1h")
check("low confidence -> no position",
      t.target_fraction({"confidence": 0.05, "net_impact": 2.0}) == 0.0)
check("below threshold -> no position",
      t.target_fraction({"confidence": 0.9, "net_impact": 0.1}) == 0.0)
strong = t.target_fraction({"confidence": 0.9, "net_impact": 2.0})
weak = t.target_fraction({"confidence": 0.9, "net_impact": 0.5})
check("stronger signal -> larger position", strong > weak, f"{strong} vs {weak}")
check("allocation capped at max_allocation", strong <= t.max_allocation + 1e-9,
      str(strong))
check("bearish + no-short -> flat",
      t.target_fraction({"confidence": 0.9, "net_impact": -2.0}) == 0.0)
ts = PaperTrader(duration="1h", allow_short=True)
check("bearish + short enabled -> negative target",
      ts.target_fraction({"confidence": 0.9, "net_impact": -2.0}) < 0)

# ---------------------------------------------------------------------------
print("\nPersistence")

store.init_db()

store.upsert_candles("BTC-USD", 3600, [
    ("2025-06-01T00:00:00+00:00", 100, 110, 95, 105, 12.5),
    ("2025-06-01T01:00:00+00:00", 105, 115, 100, 110, 9.0),
])
cov = store.candle_coverage("BTC-USD", 3600)
check("candles persist", cov["count"] == 2, str(cov))

store.upsert_candles("BTC-USD", 3600, [
    ("2025-06-01T00:00:00+00:00", 100, 110, 95, 999, 12.5)])
c = store.get_candles("BTC-USD", 3600)
check("candle re-insert replaces rather than duplicates",
      len(c) == 2 and c[0]["close"] == 999, f"n={len(c)}")

bt_id = store.save_backtest("t", "BTC", "2025-01-01", "2026-01-01",
                            {"w": 60}, {"metrics": {}})
check("backtest run persists", bt_id > 0)
check("backtest listed", len(store.list_backtests()) == 1)
check("backtest round-trips params",
      json.loads(store.get_backtest(bt_id)["params"])["w"] == 60)

rid = store.create_paper_run("t", 10_000.0, {"assets": ["BTC"]})
store.record_trade(rid, store.utcnow(), "BTC", "BUY", 0.1, 50_000.0, 5.0,
                   "test", "BULLISH", 1.0, 0.8, 4_995.0, 0.1, 0.0)
store.record_equity(rid, store.utcnow(), 10_000.0, 4_995.0, {"BTC": {"qty": 0.1}})
store.finish_paper_run(rid, "completed", 10_500.0, {"return_pct": 5.0})

data = store.get_paper_run(rid)
check("paper run persists", data["run"]["status"] == "completed")
check("trades persist", len(data["trades"]) == 1)
check("equity samples persist", len(data["equity"]) == 1)
check("ending equity recorded", data["run"]["ending_equity"] == 10_500.0)
check("paper run listed", len(store.list_paper_runs()) == 1)

# ---------------------------------------------------------------------------
print(f"\n{'=' * 50}\n  {PASS} passed, {FAIL} failed\n{'=' * 50}\n")
sys.exit(1 if FAIL else 0)
