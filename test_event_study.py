"""
Apollo — event study test suite (offline, no network).

The critical properties to pin down:

  * the estimation window must END BEFORE the event (no post-event leakage
    into the baseline)
  * a planted +X% jump must be recovered as approximately +X% CAR
  * drift in the baseline must be subtracted, not counted as abnormal
  * category classification must not silently mislabel obvious events

    python test_event_study.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_TMP_DB = os.path.join(tempfile.mkdtemp(), "apollo_es_test.db")
os.environ["APOLLO_DB"] = _TMP_DB

import event_study as es                       # noqa: E402
import store                                   # noqa: E402
import scoring                                 # noqa: E402

PASS, FAIL = 0, 0
BASE = datetime(2025, 6, 1, tzinfo=timezone.utc)


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  pass  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------------------
print("\nEvent classification")

cases = [
    ("SEC approves spot bitcoin ETF, trading begins Monday", "etf_approval", +1),
    ("SEC delays decision on spot solana ETF application", "etf_rejection_delay", -1),
    ("SEC sues major exchange over unregistered securities", "enforcement", -1),
    ("Court dismisses the SEC lawsuit against Ripple", "enforcement_relief", +1),
    ("DeFi protocol exploited for $200M", "security_incident", -1),
    ("Crypto lender files for bankruptcy, halts withdrawals", "insolvency", -1),
    ("Stablecoin depegs as reserves questioned", "depeg", -1),
    ("Fed cuts rates by 50bps in dovish surprise", "monetary_easing", +1),
    ("Fed raises rates as inflation runs hot", "monetary_tightening", -1),
    ("BlackRock launches new bitcoin investment product", "institutional_adoption", +1),
    ("Ceasefire agreed, tensions de-escalate", "geopolitical_relief", +1),
    ("Missile strike escalates regional conflict", "geopolitical_shock", -1),
]
for text, want_cat, want_dir in cases:
    got = es.classify(text)
    cats = [c for c, _ in got]
    ok = want_cat in cats
    check(f"{want_cat:<24} <- {text[:40]}...", ok, str(cats))
    if ok:
        d = dict(got)[want_cat]
        check(f"  direction prior is {want_dir:+d}", d == want_dir, str(d))

check("unrelated text classifies as nothing",
      es.classify("Local bakery wins county pie contest") == [])

# Word order: headlines put verb before OR after the noun. A one-directional
# pattern silently drops ~half of real events, which is invisible in the
# output and biases every downstream statistic. Both orders must match.
print("\n  Word-order robustness (regression)")
order_cases = [
    ("SEC approves spot bitcoin ETF", "etf_approval"),
    ("Spot bitcoin ETF approved by the SEC", "etf_approval"),
    ("Bitcoin ETF wins approval after long delay", "etf_approval"),
    ("SEC delays decision on spot solana ETF", "etf_rejection_delay"),
    ("Spot solana ETF decision delayed again", "etf_rejection_delay"),
    ("SEC rejects the proposed ether ETF", "etf_rejection_delay"),
    ("SEC sues major crypto exchange", "enforcement"),
    ("Major crypto exchange sued by the SEC", "enforcement"),
]
for text, want in order_cases:
    cats = [c for c, _ in es.classify(text)]
    check(f"  {want:<20} <- {text[:44]}", want in cats, str(cats))

# ---------------------------------------------------------------------------
print("\nCAR recovery on planted data")


def series(n: int, jump_at: int | None = None, jump: float = 0.05,
           drift: float = 0.0) -> list[float]:
    """Flat returns of `drift`, with an optional one-bar jump."""
    r = [drift] * n
    if jump_at is not None:
        r[jump_at] = jump
    return r


# A clean +5% jump one bar after the event, zero drift -> CAR ~= +5%.
N = 400
EV = 250
rets = series(N, jump_at=EV, jump=0.05)
car = es.compute_car(rets, EV, estimation_bars=168, gap=6, pre=3, post=24)
check("compute_car returns a result with adequate history", car is not None)
if car:
    check("planted +5% jump recovered as ~+5% CAR",
          abs(car["car"] - 0.05) < 1e-6, f"{car['car']:.6f}")
    check("post-event CAR captures the jump",
          abs(car["car_post"] - 0.05) < 1e-6, f"{car['car_post']:.6f}")
    check("pre-event CAR is ~0 when nothing happened before",
          abs(car["car_pre"]) < 1e-9, f"{car['car_pre']:.9f}")

# Negative jump -> negative CAR.
rets_d = series(N, jump_at=EV, jump=-0.05)
car_d = es.compute_car(rets_d, EV)
check("planted -5% jump recovered as negative CAR",
      car_d is not None and abs(car_d["car"] + 0.05) < 1e-6,
      str(car_d["car"] if car_d else None))

# Constant drift with no event -> abnormal return ~0 (drift is the baseline,
# so it must be subtracted rather than counted as abnormal).
rets_drift = series(N, drift=0.001)
car_drift = es.compute_car(rets_drift, EV)
check("pure drift produces ~zero abnormal return",
      car_drift is not None and abs(car_drift["car"]) < 1e-9,
      str(car_drift["car"] if car_drift else None))

# Drift PLUS a jump -> only the EXCESS over drift is abnormal.
# series() replaces the drift on the jump bar rather than adding to it, so the
# jump bar returns 5% where 0.1% was expected: abnormal = 4.9%, not 5.0%.
rets_both = series(N, jump_at=EV, jump=0.05, drift=0.001)
car_both = es.compute_car(rets_both, EV)
check("drift is subtracted, only the excess counts as abnormal",
      car_both is not None and abs(car_both["car"] - 0.049) < 1e-6,
      str(car_both["car"] if car_both else None))

# ---------------------------------------------------------------------------
print("\nNo look-ahead in the baseline")

# Put a huge move AFTER the event window. If the estimation window were
# (incorrectly) drawn from later data, the baseline mean would shift and CAR
# would change. It must not.
rets_after = series(N, jump_at=EV, jump=0.05)
for i in range(EV + 40, EV + 80):
    rets_after[i] = 0.10                  # violent post-window regime
car_after = es.compute_car(rets_after, EV)
check("post-window activity does not alter the baseline",
      car_after is not None and abs(car_after["car"] - 0.05) < 1e-6,
      str(car_after["car"] if car_after else None))

# The gap must exclude the bars immediately before the event.
rets_pre = series(N, drift=0.0)
for i in range(EV - 5, EV):
    rets_pre[i] = 0.08                    # pre-event leak/run-up inside the gap
car_pre = es.compute_car(rets_pre, EV, gap=6)
check("pre-event run-up inside the gap stays out of the baseline",
      car_pre is not None and abs(car_pre["baseline_mean"]) < 1e-9,
      str(car_pre["baseline_mean"] if car_pre else None))
check("that run-up still shows up in pre-event CAR",
      car_pre is not None and car_pre["car_pre"] > 0.1,
      str(car_pre["car_pre"] if car_pre else None))

# ---------------------------------------------------------------------------
print("\nGuards")

check("too little leading history returns None",
      es.compute_car([0.0] * 50, 10) is None)
check("event too close to the end returns None",
      es.compute_car([0.001] * 300, 295) is None)

# A zero-variance baseline must NOT discard the event: CAR is still well
# defined (actual minus expected). Only SCAR needs volatility.
flat = es.compute_car([0.0] * 400, 250)
check("zero-variance baseline still yields a CAR", flat is not None)
check("zero-variance baseline reports SCAR as None (not a fake 0)",
      flat is not None and flat["scar"] is None,
      str(flat["scar"] if flat else "no result"))
check("zero-variance baseline CAR is 0 when nothing happened",
      flat is not None and abs(flat["car"]) < 1e-12)

# With volatility present, SCAR should be a real number.
import random as _r
_r.seed(1)
noisy = [_r.gauss(0, 0.01) for _ in range(400)]
noisy[250] = 0.05
sc = es.compute_car(noisy, 250)
check("non-zero volatility yields a numeric SCAR",
      sc is not None and isinstance(sc["scar"], float), str(sc["scar"] if sc else None))
check("SCAR is positive for a positive shock",
      sc is not None and sc["scar"] > 0, str(sc["scar"] if sc else None))

# ---------------------------------------------------------------------------
print("\nBar index lookup")

times = [BASE + timedelta(hours=i) for i in range(100)]
check("exact match resolves to that bar",
      es._bar_index(times, BASE + timedelta(hours=10)) == 10)
check("between-bars resolves to the NEXT bar (first actionable)",
      es._bar_index(times, BASE + timedelta(hours=10, minutes=30)) == 11)
check("before series start resolves to bar 0",
      es._bar_index(times, BASE - timedelta(hours=5)) == 0)
check("after series end returns None",
      es._bar_index(times, BASE + timedelta(hours=500)) is None)

# ---------------------------------------------------------------------------
print("\nEnd-to-end study on planted data")

store.init_db()

# Price series: flat, except a +4% move right after each planted hack story.
N_BARS = 1400
price = 50_000.0
rows = []
# Enough events to clear MIN_EVENTS after windowing drops the edges.
hack_bars = {300, 400, 500, 600, 700, 800, 900, 1000}
for i in range(N_BARS):
    rows.append(((BASE + timedelta(hours=i)).isoformat(),
                 price, price * 1.001, price * 0.999, price, 5.0))
    # A hack is bearish: price drops on the bar AFTER the story lands.
    price *= 0.96 if (i - 1) in hack_bars else 1.0
store.upsert_candles("BTC-USD", 3600, rows)

for i in sorted(hack_bars):
    a = store.Article(
        title=f"Major crypto exchange hacked, bitcoin funds drained #{i}",
        summary="Attackers exploited a vulnerability and drained user funds.",
        url=f"https://example.com/hack{i}", source="coindesk",
        source_category="crypto", source_weight=1.3,
        published_at=(BASE + timedelta(hours=i)).isoformat())
    scoring.score_article(a)
    store.upsert_article(a)

res = es.run_study("BTC", "2025-06-01", "2025-09-01",
                   dedupe_hours=1.0, min_corroboration=1, save=False)
check("study runs without error", "error" not in res, str(res.get("error")))

if "error" not in res:
    check("hack events detected", res["events_measured"] >= 5,
          str(res["events_measured"]))
    sec = res["categories"].get("security_incident", {})
    if sec and not sec.get("note"):
        check("planted bearish events produce negative CAR",
              sec["mean_car_post_pct"] < -1.0, str(sec["mean_car_post_pct"]))
        check("expected direction recorded as bearish",
              sec["expected_direction"] == -1)
        check("realized move agrees with the bearish prior",
              (sec["direction_agreement_pct"] or 0) > 80,
              str(sec["direction_agreement_pct"]))
        check("lexicon sentiment agrees with realized move",
              (sec["lexicon_agreement_pct"] or 0) > 80,
              str(sec["lexicon_agreement_pct"]))
    else:
        check("security_incident category populated", False, str(sec))

    check("overall signed return is positive (shorting bad news works)",
          (res["overall"]["signed_by_prior_mean_pct"] or 0) > 0,
          str(res["overall"]["signed_by_prior_mean_pct"]))

# Dedupe: many outlets, one story, one event.
for k in range(8):
    a = store.Article(
        title=f"Major crypto exchange hacked, bitcoin funds drained #300",
        summary="Wire copy.", url=f"https://outlet{k}.com/hack300",
        source=f"outlet{k}", source_category="crypto", source_weight=1.0,
        published_at=(BASE + timedelta(hours=300, minutes=k * 5)).isoformat())
    scoring.score_article(a)
    store.upsert_article(a)

res2 = es.run_study("BTC", "2025-06-01", "2025-08-01",
                    dedupe_hours=6.0, min_corroboration=1, save=False)
check("dedupe collapses one story covered by many outlets",
      res2["events_after_dedupe"] <= res["events_after_dedupe"] + 1,
      f"{res['events_after_dedupe']} -> {res2['events_after_dedupe']}")

# ---------------------------------------------------------------------------
print(f"\n{'=' * 50}\n  {PASS} passed, {FAIL} failed\n{'=' * 50}\n")
sys.exit(1 if FAIL else 0)
