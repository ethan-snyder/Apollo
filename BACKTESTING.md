# Backtesting & Paper Trading

How we evaluate whether the indicators actually predict anything, and how to
run simulated trading sessions.

---

## Read this first: the data constraint

**Price history is easy. News history is not.**

Coinbase serves full 2025 hourly OHLCV for free with no key — that side is
solved. But RSS feeds only ever serve *current* items. There is no way to
reconstruct 2025 news from the ~68 live feeds in `config.py`; those feeds
simply don't expose an archive.

The only deep news archive wired into this project is **Alpaca's News API**
(Benzinga content, back to 2015), which requires `ALPACA_API_KEY` and
`ALPACA_SECRET_KEY` in `.env`. Without those keys you can backfill prices and
Fear & Greed, but a news-driven backtest will have nothing to test and
`main.py backtest` will say so explicitly rather than silently reporting
garbage.

**What this means for interpreting 2025 results:**

| Consideration | Impact on your conclusions |
|---|---|
| Alpaca/Benzinga is equities-first | Crypto coverage is real but thinner than a crypto-native feed. Expect lower article counts per hour than the live system produces today. |
| Source mix differs from live | Backtest sees ~1 source; production sees ~68. Source-weight diversity can't be validated this way. |
| Lexicon was written in 2026 | The phrase list was partly tuned by looking at recent headlines. Testing it on 2025 data is not fully out-of-sample. This is the single biggest caveat. |
| No survivorship correction | Only articles Alpaca still serves are visible; anything retracted or removed is invisible. |

Treat a 2025 backtest as a **sanity check on machinery and a rough directional
read**, not as validated alpha. The honest out-of-sample test is forward
paper trading from today, which is why that's built too.

---

## Look-ahead protection

A backtest that accidentally sees the future produces beautiful, meaningless
numbers, and fails silently. Four specific protections, all covered by tests
in `test_backtest.py`:

1. **Impact is recomputed, never read from the DB.** The stored
   `articles.impact` was computed at ingest time with recency measured against
   the wall clock *of scraping*. Using it would leak information about when we
   happened to run the scraper. `backtest._impact_at()` recomputes decay
   relative to the simulated timestamp instead.
2. **Only `published_at <= t` is visible at bar `t`.** Enforced by a
   two-pointer sweep over time-ordered articles.
3. **Forward returns run close-to-close, from `t` forward.** News up to `t` is
   acted on at a price still available after that news existed.
4. **Articles with no timestamp are dropped**, not assumed fresh — an unknown
   publication time can't be safely placed in the timeline.

The test suite plants an article in the future and asserts it contributes
exactly zero, plants a known-predictive signal and asserts positive IC, then
inverts that signal and asserts the IC flips sign.

---

## Step 1 — Backfill history

```
python main.py backfill 2025-01-01 2026-01-01
```

Pulls, in order:

- **Coinbase OHLCV** — BTC-USD, ETH-USD, SOL-USD at 1h and 1d granularity.
  Keyless. ~8,760 hourly candles per pair per year.
- **Fear & Greed** — full daily history from alternative.me. Keyless.
- **Alpaca news** — Benzinga archive, filtered to `BTCUSD, ETHUSD, SOLUSD,
  COIN, MSTR, MARA, RIOT, CLSK, HOOD`. Requires keys.

This takes a while (the news paging dominates). It's idempotent — candles use
`INSERT OR REPLACE` and articles go through the same dedup as live collection,
so re-running over the same range is safe.

Check what landed:

```
python main.py coverage
```

---

## Step 2 — Run a backtest

```
python main.py backtest BTC 2025-01-01 2026-01-01
python main.py backtest ETH 2025-01-01 2026-01-01
python main.py backtest SOL 2025-01-01 2026-01-01
```

Every run is saved to the `backtest_runs` table with full params and results
as JSON, so runs stay comparable after the scoring code changes underneath
them. List them with `python main.py backtests`.

### Reading the output

**Information Coefficient (IC)** — Spearman rank correlation between signal
strength and forward return. This is the headline number.

| IC | Interpretation |
|---|---|
| < 0.02 | No signal. Indistinguishable from noise. |
| 0.02–0.05 | Weak but potentially real if consistent across assets and horizons. |
| 0.05–0.10 | Genuinely good for a news-based signal. |
| > 0.15 | Be suspicious. Check for look-ahead or overfitting before believing it. |

Real-world equity/crypto signals live at IC 0.02–0.05. If you see 0.5, you
have a bug, not a discovery — that's roughly what the synthetic test fixture
produces *by construction*.

**Hit rate** — directional accuracy on bars where `|net_impact| >= 0.05`.
50% is a coin flip. 52–55% is meaningful with enough samples.

**t-statistic** — significance of the signal-following return series. |t| > 2
is the conventional bar; below that, the mean return isn't distinguishable
from zero.

**Decile table** — the most informative single output. Sort every bar by
signal strength, cut into ten buckets, show mean forward return per bucket.
**A monotone gradient is the strongest evidence the signal carries
information.** A jagged table with a strong top decile and nothing else is
usually noise or a handful of outliers.

**Long/short decile spread** — top decile mean return minus bottom decile.
The single cleanest summary of whether the signal separates outcomes.

**Strategy simulation** — long/flat (optionally long/short) with 10bps
per-side fees, compared against buy-and-hold over the identical window.
Beating buy-and-hold in a bull year is *hard* and mostly not the point; the
useful question is whether risk-adjusted return (Sharpe) and drawdown improve.

### What to actually look for

Signal quality is about **consistency, not magnitude**:

- Does IC hold the same sign across BTC, ETH, and SOL?
- Does it degrade smoothly as the horizon lengthens (1h > 4h > 24h)? Genuine
  news signal decays. A signal that's strongest at 24h and absent at 1h is
  suspicious.
- Is the decile gradient monotone, or driven by one extreme bucket?
- Does the `by_direction` table show BULLISH bars outperforming BEARISH bars?

---

## Step 3 — Paper trading

**Simulated only. No broker is contacted, no orders are placed, no real money
moves at any point.** Every fill is arithmetic against a live Coinbase quote.

```
python main.py paper 1h              # 1-hour session, $10,000
python main.py paper 30m             # 30 minutes
python main.py paper 7d              # one week
python main.py paper 0               # unbounded, Ctrl+C to stop
python main.py paper 4h BTC,ETH      # restrict to specific assets
```

Duration accepts `s`, `m`, `h`, `d`, `w` suffixes, a bare number (seconds), or
`0`/`forever` for unbounded. Ctrl+C stops early and **still saves the run**.

### How it trades

Every 60 seconds (configurable) it fetches live prices, computes the current
signal over a 60-minute trailing news window, and sets a target allocation:

```
target_fraction = max_allocation × signal_strength × confidence_scale
```

- No position unless `confidence >= 0.20` **and** `|net_impact| >= 0.30`
- Capped at **40% of equity per asset**
- Rebalances only when the target moves more than 5% of equity — stops signal
  jitter from churning the book and bleeding fees
- 10bps fee per side
- Long/flat by default; `allow_short=True` enables shorts
- Never exceeds available cash (no simulated margin)

### Reviewing runs

```
python main.py papers            # list all runs with returns
python main.py paperrun 3        # full trade log + equity range for run #3
```

Everything persists across sessions in `paper_runs`, `paper_trades`, and
`paper_equity`. Each trade records the signal that caused it (direction, net
impact, confidence) alongside price and realized P&L — so you can go back and
ask "what was the system thinking here?" rather than just seeing a fill.

---

## Event studies (usually more informative than the backtest)

```
python main.py events BTC 2025-01-01 2026-01-01
```

**Why this beats continuous IC.** IC scores every hourly bar, but most bars
contain no real news — so the handful that matter get buried under thousands
of near-zero observations. IC ends up measuring "does ambient chatter track
price," which is mostly noise. An event study only looks where something
actually happened, reaches significance with far fewer observations, and
answers a more useful question: **which kinds of news does the lexicon read
correctly?**

### Method

1. Detect events and assign each a category from the taxonomy in
   `event_study.py` (ETF approval, enforcement, hack, insolvency, depeg,
   monetary easing/tightening, geopolitical shock, ~20 total).
2. For each event at `t0`, fit a baseline over an **estimation window ending
   strictly before** `t0` — 168 bars, ending 6 bars prior. The gap keeps
   pre-event drift or leakage out of the baseline.
3. `AR = actual return − expected return`; `CAR = Σ AR` across the event window
   (default −3h to +24h).
4. Average across events, then t-test against zero.

A constant-mean baseline is used rather than a full market model because
crypto majors have no clean independent index — BTC effectively *is* the
index, so regressing BTC against a BTC-inclusive benchmark would be circular.

### Reading the output

```
category                    n  exp      CAR%       t   pos%  prior✓   lex✓
etf_approval               13 bull    +7.069   11.76   100%    100%   100% *
security_incident          22 bear    -4.858   -5.74     9%     91%    91% *
```

- **exp** — the prior a human trader would hold for this category
- **CAR%** — mean cumulative abnormal return after the event
- **t** — significance; `*` marks |t| ≥ 2
- **prior✓** — how often the realized move agreed with that prior
- **lex✓** — how often *your lexicon's sentiment* agreed with the realized move

The last two columns are the payload. Cases worth acting on:

| Pattern | Meaning |
|---|---|
| High prior✓, high lex✓ | Category works. Weight it up. |
| High prior✓, low lex✓ | The event matters but your lexicon misreads it — a phrasing gap in `scoring.py`. |
| Low prior✓ (~50%) | The event is already priced in by the time it's published, or the prior is wrong. |
| prior✓ near 0% | Systematically backwards — the market fades this news. Genuinely tradeable, inverted. |

The **AAR-by-hour** breakdown under the strongest category shows *when* the
move lands. A spike at +1h then flat means the market reacts immediately and
you'd need to be fast. A gradual climb over 6–12h means there's time to act.
Material movement in the *negative* hours means information leaked before
publication — the news isn't the cause.

### Tuning

```python
import event_study
event_study.run_study("BTC", "2025-01-01", "2026-01-01",
                      post=6,                 # shorter reaction window
                      min_corroboration=3,    # only multi-outlet stories
                      min_relevance=0.7)      # only clearly-about-BTC articles
```

Raising `min_corroboration` is the single most effective noise filter — it
restricts to stories multiple outlets independently picked up.

Add categories by editing `EVENT_TYPES` in `event_study.py`: `"name":
(regex, expected_direction)` where direction is `+1`/`-1`/`0`.

**Word-order warning when adding patterns:** headlines put the verb before or
after the noun with roughly equal frequency ("SEC approves bitcoin ETF" vs
"Bitcoin ETF approved by SEC"). A one-directional pattern silently drops about
half of real events — invisible in the output, but it biases every statistic
downstream. Match both orders. `test_event_study.py` has regression tests for
exactly this.

## Analyzing further

The tables are designed to be queried directly. Some starting points:

```sql
-- Does signal strength actually separate outcomes? (join signals to prices)
SELECT s.direction, COUNT(*) n, AVG(s.net_impact) avg_signal, AVG(s.confidence) avg_conf
FROM signals s
WHERE s.asset = 'BTC' AND s.window_min = 60
GROUP BY s.direction;

-- Which sources drive the highest-magnitude articles?
SELECT source, COUNT(*) n, AVG(ABS(impact)) avg_abs_impact
FROM articles
WHERE assets LIKE '%"BTC"%'
GROUP BY source
HAVING n > 20
ORDER BY avg_abs_impact DESC;

-- Paper trading equity curve for plotting
SELECT ts, equity, cash FROM paper_equity WHERE run_id = 1 ORDER BY ts;

-- Win rate by signal direction across all paper trades
SELECT signal_direction,
       COUNT(*) n,
       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) wins,
       ROUND(AVG(realized_pnl), 2) avg_pnl
FROM paper_trades
WHERE realized_pnl != 0
GROUP BY signal_direction;

-- Does corroboration (multi-outlet pickup) predict bigger moves?
SELECT corroboration, COUNT(*) n, AVG(ABS(impact)) avg_abs_impact
FROM articles GROUP BY corroboration ORDER BY corroboration;
```

`python main.py export` dumps everything to JSONL if you'd rather work in
pandas or a notebook.

---

## Suggested next steps for validating efficacy

0. **Run the event study first.** It's more robust on sparse news than the
   continuous backtest, and the `lex✓` column tells you directly which
   categories your lexicon reads correctly — that's actionable in a way an
   aggregate IC number isn't.
1. **Backfill and run all three assets.** Consistency across BTC/ETH/SOL is
   more informative than a strong result on one.
2. **Vary the news window.** `backtest.run(window_min=...)` — try 15, 30, 60,
   240. If results only work at one specific window, that's overfitting.
3. **Split the year.** Run H1 and H2 separately. A signal that works in one
   half and not the other isn't stable.
4. **Compare against a shuffled control.** Randomize the sentiment column and
   re-run; IC should collapse to ~0. If it doesn't, something is leaking.
5. **Start a long paper run.** This is the only genuinely out-of-sample test
   available, since the lexicon was written with knowledge of past headlines.
   `python main.py paper 0` and let it accumulate.

## Testing

```
python test_apollo.py         # 64 checks: scoring, dedup, parsing
python test_backtest.py       # 67 checks: look-ahead, stats, accounting
python test_event_study.py    # 62 checks: CAR recovery, baseline isolation
```

193 checks, all fully offline. Run them after touching `scoring.py`,
`backtest.py`, `event_study.py`, or `paper_trader.py`.

The event-study suite plants known shocks into synthetic series and asserts
the exact CAR is recovered, that post-event activity can't alter the baseline,
and that pre-event run-up stays out of it. It also regression-tests headline
word-order handling, which is where the taxonomy is most likely to silently
lose events.
