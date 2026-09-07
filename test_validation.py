"""
Apollo — validation harness test suite (offline, no network).

Every test in here plants data with a KNOWN causal structure and asserts the
harness recovers it. That matters more than usual for this module: a
validation tool that quietly reports the wrong verdict is worse than no
validation tool, because it converts an unknown into a false certainty.

The planted structures are:

  predictive      news precedes the move   -> lead-lag must peak at lag +1
  contemporaneous news lands with the move -> lead-lag must peak at lag  0
  reactive        news follows the move    -> lead-lag must peak at lag -1

and, for the nulls:

  real signal   -> bootstrap significant, time shift destroys it
  pure noise    -> bootstrap not significant, placebo events find nothing
  slow drift    -> time shift does NOT destroy it (the failure it exists to
                   catch), even though the naive IC looks fine
  cloned series -> wrong-asset control reports GENERIC

    python test_validation.py
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_TMP_DB = os.path.join(tempfile.mkdtemp(), "apollo_val_test.db")
os.environ["APOLLO_DB"] = _TMP_DB

import scoring                                          # noqa: E402
import store                                            # noqa: E402
import validation                                       # noqa: E402

PASS, FAIL = 0, 0
BASE = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
N_BARS = 1600


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  pass  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"  -> {detail}" if detail else ""))


def iso(hours: float) -> str:
    return (BASE + timedelta(hours=hours)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Synthetic world
# ---------------------------------------------------------------------------

def seed_candles(symbol: str, closes: list[float]) -> None:
    store.upsert_candles(symbol, 3600, [
        (iso(i), c, c * 1.001, c * 0.999, c, 1.0)
        for i, c in enumerate(closes)
    ])


_next_id = [0]


def seed_article(asset: str, hours: float, sentiment: float, *,
                 title: str = "planted", relevance: float = 1.0,
                 weight: float = 1.0, corroboration: int = 2) -> None:
    """Insert a pre-scored article directly, bypassing the collectors."""
    _next_id[0] += 1
    with store.tx() as conn:
        conn.execute(
            """INSERT INTO articles
               (url, url_hash, title_hash, title, summary, body, source,
                source_category, source_weight, published_at, ingested_at,
                assets, tickers, sentiment, relevance, impact, score_detail,
                corroboration, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"http://t/{_next_id[0]}", f"u{_next_id[0]}", f"t{_next_id[0]}",
             title, "", "", "test", "crypto", weight, iso(hours), iso(hours),
             json.dumps([asset]), "[]", sentiment, relevance,
             sentiment * relevance * weight,
             json.dumps({"asset_relevance": {asset: relevance},
                         "matches": [], "recency": 1.0,
                         "high_impact_event": False,
                         "source_weight": weight}),
             corroboration, "{}"),
        )


def wipe_articles() -> None:
    with store.tx() as conn:
        conn.execute("DELETE FROM articles")


# A gently trending random walk, so returns are neither constant nor degenerate.
rng = random.Random(20250817)
STEPS = [rng.gauss(0.0002, 0.006) for _ in range(N_BARS)]
CLOSES = [100.0]
for s in STEPS:
    CLOSES.append(CLOSES[-1] * (1 + s))
CLOSES = CLOSES[:N_BARS]

# ret_into[i] is the return realized over the hour ENDING at bar i.
RET_INTO = [0.0] + [(CLOSES[i] - CLOSES[i - 1]) / CLOSES[i - 1]
                    for i in range(1, N_BARS)]

store.init_db()
seed_candles("BTC-USD", CLOSES)

RANGE = ("2025-01-01", "2025-02-15")     # comfortably inside the seeded bars


# ---------------------------------------------------------------------------
print("\nLexicon split integrity")

check("all == event + price_action (bullish)",
      len(scoring.BULLISH) == len(scoring.BULLISH_EVENT)
      + len(scoring.BULLISH_PRICE_ACTION),
      f"{len(scoring.BULLISH)} vs {len(scoring.BULLISH_EVENT)}"
      f"+{len(scoring.BULLISH_PRICE_ACTION)}")
check("all == event + price_action (bearish)",
      len(scoring.BEARISH) == len(scoring.BEARISH_EVENT)
      + len(scoring.BEARISH_PRICE_ACTION))
check("halves do not overlap (bullish)",
      not (set(scoring.BULLISH_EVENT) & set(scoring.BULLISH_PRICE_ACTION)))
check("halves do not overlap (bearish)",
      not (set(scoring.BEARISH_EVENT) & set(scoring.BEARISH_PRICE_ACTION)))
check("merged dict preserves every weight (bullish)",
      all(scoring.BULLISH[k] == v
          for d in (scoring.BULLISH_EVENT, scoring.BULLISH_PRICE_ACTION)
          for k, v in d.items()))
check("merged dict preserves every weight (bearish)",
      all(scoring.BEARISH[k] == v
          for d in (scoring.BEARISH_EVENT, scoring.BEARISH_PRICE_ACTION)
          for k, v in d.items()))

# The split must actually separate the two kinds of phrase.
ev_only = "SEC approves spot bitcoin ETF after settling with the exchange"
pa_only = "Bitcoin surges to a record high as short squeeze accelerates"
check("event headline scores on the event half",
      scoring.score_sentiment(ev_only, "event")[0] > 0)
check("event headline scores zero on the price-action half",
      scoring.score_sentiment(ev_only, "price_action")[0] == 0.0,
      str(scoring.score_sentiment(ev_only, "price_action")))
check("price-action headline scores on the price-action half",
      scoring.score_sentiment(pa_only, "price_action")[0] > 0)
check("price-action headline scores zero on the event half",
      scoring.score_sentiment(pa_only, "event")[0] == 0.0,
      str(scoring.score_sentiment(pa_only, "event")))
try:
    scoring.score_sentiment("x", "nope")
    check("unknown subset raises ValueError", False, "no exception")
except ValueError:
    check("unknown subset raises ValueError", True)

# ---------------------------------------------------------------------------
print("\nSeries construction and sign convention")

obs_stub = [{"close": c, "net_impact": 0.0, "article_count": 0}
            for c in [100.0, 110.0, 99.0]]
sig, into, news = validation._series(obs_stub)
check("return_into[0] is 0 (no prior close)", into[0] == 0.0)
check("return_into[i] measures the hour ENDING at bar i",
      abs(into[1] - 0.1) < 1e-12, str(into[1]))
check("return_into tracks a fall correctly",
      abs(into[2] - (99.0 - 110.0) / 110.0) < 1e-12, str(into[2]))

check("_ic_tstat is 0 for degenerate n", validation._ic_tstat(0.5, 2) == 0.0)
check("_ic_tstat grows with n",
      validation._ic_tstat(0.1, 1000) > validation._ic_tstat(0.1, 100))
check("_ic_tstat is sign-preserving", validation._ic_tstat(-0.1, 500) < 0)
check("_ic_tstat is safe at |ic| == 1", validation._ic_tstat(1.0, 500) == 0.0)

# ---------------------------------------------------------------------------
print("\nBlock permutation")

vals = list(range(100))
perm = validation._block_permutation(vals, 10, random.Random(1))
check("block permutation preserves length", len(perm) == len(vals))
check("block permutation preserves the multiset", sorted(perm) == vals)
# Contiguity is the whole point: most neighbours must still be consecutive.
adjacent = sum(1 for i in range(1, len(perm)) if perm[i] - perm[i - 1] == 1)
check("block permutation keeps runs contiguous", adjacent >= 80, str(adjacent))
shuffled = vals[:]
random.Random(1).shuffle(shuffled)
adj_iid = sum(1 for i in range(1, len(shuffled))
              if shuffled[i] - shuffled[i - 1] == 1)
check("i.i.d. shuffle destroys contiguity (contrast)", adj_iid < 10,
      str(adj_iid))
check("block permutation handles a ragged final block",
      sorted(validation._block_permutation(list(range(25)), 10,
                                           random.Random(2))) == list(range(25)))

# ---------------------------------------------------------------------------
print("\nLead-lag recovers planted causal structure")


def plant(offset_bars: int, publish_before: bool = True) -> None:
    """
    Publish an article inside bar i whose sentiment matches the move realized
    over the hour ending at bar (i + offset_bars).

    offset_bars=+1 -> the article predicts the NEXT hour   (predictive)
    offset_bars= 0 -> it describes the CURRENT hour        (contemporaneous)
    offset_bars=-1 -> it describes the PREVIOUS hour       (reactive)
    """
    wipe_articles()
    for i in range(2, N_BARS - 2):
        target = i + offset_bars
        if not (0 <= target < N_BARS):
            continue
        move = RET_INTO[target]
        if move == 0:
            continue
        # Published a fraction of an hour before bar i's close, so it is
        # visible in bar i's 60-minute trailing window and no earlier one.
        seed_article("BTC", i - 0.1, 1.0 if move > 0 else -1.0)


for offset, expect in ((1, "PREDICTIVE"), (0, "CONTEMPORANEOUS"),
                       (-1, "REACTIVE")):
    plant(offset)
    res = validation.lead_lag("BTC", *RANGE, max_lag=4)
    check(f"offset {offset:+d}: peak lag is {offset:+d}",
          res.get("peak_lag") == offset, str(res.get("peak_lag")))
    check(f"offset {offset:+d}: verdict is {expect}",
          res.get("verdict") == expect, str(res.get("verdict")))

# The predictive plant must also agree with the backtest's own fwd_1h IC —
# lag +1 is defined to be exactly that quantity.
plant(1)
import backtest                                          # noqa: E402

bt = backtest.run("BTC", *RANGE, save=False)
bt_ic = bt["metrics"]["horizons"]["1h"]["information_coefficient"]
ll = validation.lead_lag("BTC", *RANGE, max_lag=4)
check("lag +1 IC equals the backtest's fwd_1h IC",
      abs(ll["ic_at_lag_1"] - bt_ic) < 5e-4, f"{ll['ic_at_lag_1']} vs {bt_ic}")

check("lead-lag reports both directions of mass",
      ll["forward_mass"] is not None and ll["backward_mass"] is not None)
check("predictive plant has more forward than backward mass",
      ll["forward_mass"] > ll["backward_mass"],
      f"{ll['forward_mass']} vs {ll['backward_mass']}")

# A signal with no relationship to price at all must not look predictive.
wipe_articles()
noise = random.Random(4)
for i in range(2, N_BARS - 2):
    seed_article("BTC", i - 0.1, noise.choice([-1.0, 1.0]))
res_noise = validation.lead_lag("BTC", *RANGE, max_lag=4)
check("pure noise produces a small peak IC at every lag",
      abs(res_noise["peak_ic"]) < 0.15, str(res_noise["peak_ic"]))

# ---------------------------------------------------------------------------
print("\nBlock bootstrap")

plant(1)
bs = validation.block_bootstrap("BTC", *RANGE, n_iter=300, seed=3)
check("real signal: observed IC is positive", bs["observed_ic"] > 0.1,
      str(bs["observed_ic"]))
check("real signal: block null is centred near zero",
      abs(bs["block"]["null_mean"]) < 0.05, str(bs["block"]["null_mean"]))
check("real signal: significant against the block null",
      bs["block"]["p_value_two_sided"] < 0.05,
      str(bs["block"]["p_value_two_sided"]))
check("real signal: verdict SIGNIFICANT", bs["verdict"] == "SIGNIFICANT",
      bs["verdict"])
check("both nulls are reported",
      bs["block"]["null"].startswith("moving block")
      and bs["iid"]["null"] == "i.i.d. shuffle")
check("block null is wider than the i.i.d. null",
      bs["block"]["null_sd"] >= bs["iid"]["null_sd"],
      f"block={bs['block']['null_sd']} iid={bs['iid']['null_sd']}")
check("t-stat inflation is reported", bs["tstat_inflation"] is not None)

wipe_articles()
noise = random.Random(5)
for i in range(2, N_BARS - 2):
    seed_article("BTC", i - 0.1, noise.choice([-1.0, 1.0]))
bs_n = validation.block_bootstrap("BTC", *RANGE, n_iter=300, seed=3)
check("noise: not significant against the block null",
      bs_n["block"]["p_value_two_sided"] > 0.05,
      str(bs_n["block"]["p_value_two_sided"]))
check("noise: verdict is NOT SIGNIFICANT or MARGINAL",
      bs_n["verdict"] in ("NOT SIGNIFICANT", "MARGINAL"), bs_n["verdict"])
check("bootstrap is deterministic for a fixed seed",
      validation.block_bootstrap("BTC", *RANGE, n_iter=100, seed=42)["block"]
      == validation.block_bootstrap("BTC", *RANGE, n_iter=100,
                                    seed=42)["block"])

# ---------------------------------------------------------------------------
print("\nTime-shift placebo")

plant(1)
ts = validation.time_shift_placebo("BTC", *RANGE, shifts=(-48, -24, 24, 48))
check("real signal: baseline IC is strong", ts["true_ic"] > 0.1,
      str(ts["true_ic"]))
check("real signal: every shift is tested",
      len(ts["shifts"]) == 4, str(len(ts["shifts"])))
check("real signal: misalignment destroys the IC",
      ts["max_survival"] < 0.25, str(ts["max_survival"]))
check("real signal: verdict CLEAN", ts["verdict"] == "CLEAN", ts["verdict"])
check("shift 0 is not reported as a shift",
      all(s["shift_h"] != 0 for s in ts["shifts"]))

# A SLOW signal: sentiment follows a multi-day cycle that the price also
# follows. The naive IC is real, but it survives displacement — which is
# exactly the failure this test exists to catch.
wipe_articles()
slow_closes = [100.0 * (1 + 0.30 * math.sin(2 * math.pi * i / 480.0))
               for i in range(N_BARS)]
seed_candles("SLOW-USD", slow_closes)
for i in range(2, N_BARS - 2):
    phase = math.sin(2 * math.pi * i / 480.0 + math.pi / 2)   # leads the price
    seed_article("SLOWCOIN", i - 0.1, 1.0 if phase > 0 else -1.0)

from config import ASSETS                                # noqa: E402

ASSETS["SLOWCOIN"] = {"name": "slow", "pair": "SLOW-USD", "patterns": [],
                      "proxies": []}
ts_slow = validation.time_shift_placebo("SLOWCOIN", *RANGE,
                                        shifts=(-48, -24, 24, 48))
check("slow drift: naive IC looks real", abs(ts_slow["true_ic"]) > 0.1,
      str(ts_slow["true_ic"]))
check("slow drift: IC survives displacement",
      ts_slow["max_survival"] >= validation.SURVIVAL_ALARM,
      str(ts_slow["max_survival"]))
check("slow drift: verdict LEAKING", ts_slow["verdict"] == "LEAKING",
      ts_slow["verdict"])

# ---------------------------------------------------------------------------
print("\nPlacebo events")

# Plant real events: a recognisable headline followed by a genuine drift.
#
# Two details matter and are easy to get wrong. The move must land strictly
# AFTER t0 — event_study measures `car_post` from the event bar forward, so a
# jump on the event bar itself lands in car_pre and this test would report the
# opposite of what it means to. And events must be spaced wider than the
# estimation window (168 bars + a 6-bar gap), or each event's baseline absorbs
# the previous event's drift, inflating the expected return and dragging every
# CAR negative.
wipe_articles()
shock_rets = RET_INTO[:]
event_bars = list(range(200, N_BARS - 120, 200))
for b in event_bars:
    for k in range(1, 7):                      # +0.6%/bar for 6 bars after t0
        if b + k < N_BARS:
            shock_rets[b + k] += 0.006
shock_closes = [100.0]
for i in range(1, N_BARS):
    shock_closes.append(shock_closes[-1] * (1 + shock_rets[i]))
seed_candles("SHOCK-USD", shock_closes)
ASSETS["SHOCKCOIN"] = {"name": "shock", "pair": "SHOCK-USD", "patterns": [],
                       "proxies": []}
for b in event_bars:
    seed_article("SHOCKCOIN", b - 0.1, 0.9,
                 title="SEC approves spot bitcoin ETF")

WIDE = ("2025-01-01", "2025-03-05")
pe = validation.placebo_events("SHOCKCOIN", *WIDE, n_draws=120, seed=9)
check("real events measured", pe.get("real_events", 0) >= 5,
      str(pe.get("real_events") or pe.get("error")))
check("placebo null for mean CAR is centred near zero",
      abs(pe["mean_car"]["null_mean"]) < 1.0,
      str(pe["mean_car"]["null_mean"]))
check("planted shock lands in the far tail of the null",
      pe["mean_car"]["percentile"] > 95, str(pe["mean_car"]["percentile"]))
check("false positive rate is a valid percentage",
      0.0 <= pe["false_positive_rate_pct"] <= 100.0,
      str(pe["false_positive_rate_pct"]))
check("planted shock is reported significant",
      pe["verdict"].startswith("SIGNIFICANT"), pe["verdict"])
check("placebo events are deterministic for a fixed seed",
      validation.placebo_events("SHOCKCOIN", *WIDE, n_draws=40,
                                seed=1)["mean_car"]
      == validation.placebo_events("SHOCKCOIN", *WIDE, n_draws=40,
                                   seed=1)["mean_car"])

# Same headlines, but on a series with no shock at all -> nothing to find.
wipe_articles()
seed_candles("FLAT-USD", CLOSES)
ASSETS["FLATCOIN"] = {"name": "flat", "pair": "FLAT-USD", "patterns": [],
                      "proxies": []}
for b in event_bars:
    seed_article("FLATCOIN", b - 0.1, 0.9,
                 title="SEC approves spot bitcoin ETF")
pe_flat = validation.placebo_events("FLATCOIN", *WIDE, n_draws=120, seed=9)
check("no-shock series is not significant",
      pe_flat["verdict"] == "NOT SIGNIFICANT"
      or pe_flat["signed_car"]["p_value_two_sided"] > 0.05,
      f"{pe_flat['verdict']} p={pe_flat['signed_car'].get('p_value_two_sided')}")

# ---------------------------------------------------------------------------
print("\nWrong-asset control")

plant(1)
# An independent series the BTC signal has no business predicting.
ctrl_rng = random.Random(77)
indep = [100.0]
for _ in range(N_BARS):
    indep.append(indep[-1] * (1 + ctrl_rng.gauss(0.0002, 0.006)))
seed_candles("INDEP", indep[:N_BARS])
# And a clone of BTC itself — the worst case, which must be caught.
seed_candles("CLONE", CLOSES)

wa = validation.wrong_asset_control("BTC", *RANGE,
                                    control_symbols=("INDEP", "CLONE"))
by_sym = {c["symbol"]: c for c in wa["controls"]}
check("native IC is measured", wa["native_ic"] > 0.1, str(wa["native_ic"]))
check("independent control is available", by_sym["INDEP"]["available"])
check("independent control IC is much weaker than native",
      by_sym["INDEP"]["ratio_vs_native"] < 0.4,
      str(by_sym["INDEP"]["ratio_vs_native"]))
check("cloned series reproduces the native IC",
      abs(by_sym["CLONE"]["ratio_vs_native"] - 1.0) < 0.05,
      str(by_sym["CLONE"]["ratio_vs_native"]))
check("a cloned control forces the GENERIC verdict",
      wa["verdict"] == "GENERIC", wa["verdict"])

wa_indep = validation.wrong_asset_control("BTC", *RANGE,
                                          control_symbols=("INDEP",))
check("independent control alone yields SPECIFIC",
      wa_indep["verdict"] == "SPECIFIC", wa_indep["verdict"])

missing = validation.wrong_asset_control("BTC", *RANGE,
                                         control_symbols=("NOTLOADED",))
check("missing control series is reported, not crashed",
      missing["controls"][0]["available"] is False)
check("missing control series suggests the backfill command",
      "controls" in missing["controls"][0]["hint"])
check("all controls missing -> UNAVAILABLE verdict",
      missing["verdict"] == "UNAVAILABLE", missing["verdict"])

# ---------------------------------------------------------------------------
print("\nControl-series backfill (stubbed yfinance, no network)")


class _Stamp:
    def __init__(self, dt): self._dt = dt
    def to_pydatetime(self): return self._dt


class _Cols(list):
    """Stands in for a pandas MultiIndex column set."""
    def __init__(self, names, nlevels=1):
        super().__init__(names)
        self.nlevels = nlevels

    def get_level_values(self, _level):
        return list(self)


class _Frame:
    def __init__(self, rows, nlevels=1, naive=False):
        self.columns = _Cols(["Open", "High", "Low", "Close", "Volume"],
                             nlevels)
        self.empty = not rows
        self._rows = rows
        self._naive = naive

    def iterrows(self):
        for i, r in enumerate(self._rows):
            dt = datetime(2025, 3, 1, i, tzinfo=None if self._naive
                          else timezone.utc)
            yield _Stamp(dt), r


class _FakeYF:
    def __init__(self, frame): self.frame = frame
    def download(self, sym, **kw): return self.frame


row = {"Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.5, "Volume": 100}
sys.modules["yfinance"] = _FakeYF(_Frame([dict(row) for _ in range(5)]))
res_ctl = validation.backfill_control_series(("FAKE1",), "2025-03-01",
                                             "2025-03-02")
check("control backfill writes candles", res_ctl["FAKE1"]["candles"] == 5,
      str(res_ctl["FAKE1"]))
check("control candles land in ohlcv",
      store.candle_coverage("FAKE1", 3600)["count"] == 5)

# Recent yfinance returns MultiIndex columns even for a single ticker.
sys.modules["yfinance"] = _FakeYF(
    _Frame([dict(row) for _ in range(3)], nlevels=2))
check("MultiIndex columns are flattened, not crashed on",
      validation.backfill_control_series(
          ("FAKE2",), "2025-03-01", "2025-03-02")["FAKE2"]["candles"] == 3)

# Naive timestamps must be assumed UTC rather than raising.
sys.modules["yfinance"] = _FakeYF(
    _Frame([dict(row) for _ in range(2)], naive=True))
check("tz-naive timestamps are treated as UTC",
      validation.backfill_control_series(
          ("FAKE3",), "2025-03-01", "2025-03-02")["FAKE3"]["candles"] == 2)

sys.modules["yfinance"] = _FakeYF(_Frame([]))
empty_res = validation.backfill_control_series(("FAKE4",), "2020-01-01",
                                               "2020-02-01")
check("empty frame explains the ~730-day hourly limit",
      empty_res["FAKE4"]["candles"] == 0 and "730" in empty_res["FAKE4"]["note"])


class _Boom:
    def download(self, *a, **k): raise RuntimeError("network down")


sys.modules["yfinance"] = _Boom()
check("a download failure is reported per symbol, not raised",
      "error" in validation.backfill_control_series(
          ("FAKE5",), "2025-03-01", "2025-03-02")["FAKE5"])
del sys.modules["yfinance"]

# ---------------------------------------------------------------------------
print("\nLoader contracts")

plant(1)
start = datetime(2025, 1, 1, tzinfo=timezone.utc)
end = datetime(2025, 2, 15, tzinfo=timezone.utc)
a_all = validation.load_articles("BTC", start, end, subset="all")
a_ev = validation.load_articles("BTC", start, end, subset="event")
check("subset='all' delegates to the stored sentiment column",
      len(a_all) > 100 and all("_sentiment" in a for a in a_all))
check("recomputed subsets return the same article population",
      len(a_ev) == len(a_all), f"{len(a_ev)} vs {len(a_all)}")
try:
    validation.load_articles("BTC", start, end, subset="event",
                             recompute=False)
    check("recompute=False on a half raises", False, "no exception")
except ValueError:
    check("recompute=False on a half raises", True)
try:
    validation.load_articles("BTC", start, end, subset="bogus")
    check("unknown subset raises in the loader", False, "no exception")
except ValueError:
    check("unknown subset raises in the loader", True)

# ---------------------------------------------------------------------------
print("\nOrchestration and persistence")

plant(1)
allres = validation.run_all("BTC", *RANGE, n_iter=100, n_draws=40,
                            shifts=(-24, 24), control_symbols=("INDEP",),
                            save=True)
check("run_all executes every test",
      set(allres["tests"]) == {"lead_lag", "lexicon_split", "block_bootstrap",
                               "time_shift", "placebo_events", "wrong_asset"},
      str(sorted(allres["tests"])))
check("run_all summarizes a verdict per test",
      len(allres["summary"]) >= 5, str(allres["summary"]))
check("run_all persists to backtest_runs", allres.get("run_id", 0) > 0)
saved = store.get_backtest(allres["run_id"])
check("saved run is tagged as a validation run",
      json.loads(saved["params"])["type"] == "validation")
check("saved results round-trip as JSON",
      json.loads(saved["results"])["tests"]["lead_lag"]["verdict"]
      == allres["tests"]["lead_lag"]["verdict"])

# Missing data must produce an explicit error, never a silent zero.
missing_asset = validation.lead_lag("NOSUCH", *RANGE)
check("unknown asset returns an error", "error" in missing_asset)
empty_range = validation.lead_lag("BTC", "2030-01-01", "2030-02-01")
check("empty date range returns an error", "error" in empty_range)

# ---------------------------------------------------------------------------
print(f"\n{'=' * 50}\n  {PASS} passed, {FAIL} failed\n{'=' * 50}\n")
sys.exit(1 if FAIL else 0)
