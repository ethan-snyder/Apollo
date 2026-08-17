# Apollo

News aggregation and real-time signal generation for BTC-USD, ETH-USD, and SOL-USD.

Apollo continuously pulls news from ~90 sources, deduplicates it, tags each
article to the assets it affects, scores it bullish/bearish, and rolls those
scores into directional signals over multiple time horizons.

## The terminal

```
python main.py
```

Opens the Apollo shell — a single typed-command interface over everything:
collection, signals, backfill, backtesting, event studies, and paper trading.
`help` lists all commands, `quit` exits.

```
  apollo ▸ sig btc          current signals across all windows
  apollo ▸ top              highest-impact articles, last 24h
  apollo ▸ why 4821         why one article scored the way it did
  apollo ▸ watch            auto-refreshing signal monitor
  apollo ▸ events btc       event study
  apollo ▸ paper 1h         simulated trading run
```

Every command also works one-shot from a normal shell:
`python main.py term sig btc`. The legacy numbered menu is still at
`python main.py menu`.

Colour is 24-bit where supported, with 256-colour and plain-text fallbacks.
Windows VT mode is enabled automatically. `NO_COLOR=1` disables styling
entirely, and output is auto-plain when piped or redirected.

## What it collects

Everything below is free. Nothing in the first four groups needs an API key.

| Group | Sources | Relevance |
|---|---|---|
| Crypto trade press | CoinDesk, The Block, Blockworks, Cointelegraph, Decrypt, DL News, The Defiant, CryptoSlate, + 10 more | Fastest coverage of crypto-specific events |
| Protocol primary sources | Ethereum Foundation, Solana, Bitcoin Core, Kraken, a16z crypto | Upgrades and incidents, ahead of press pickup |
| Regulators & central banks | Fed (press/monetary/speeches), SEC, CFTC, Treasury, OCC, ECB, BLS, BEA, White House | The single largest discrete mover of crypto prices |
| Macro & geopolitical | WSJ, FT, CNBC, MarketWatch, NYT, BBC, Al Jazeera, Guardian, SCMP, Economist, + Reuters/Bloomberg/AP via Google News | Risk-on/risk-off context |
| APIs (keyless) | GDELT, CoinGecko, Fear & Greed, DefiLlama, Federal Register, SEC EDGAR full-text | Non-English/regional coverage, price + liquidity context, rulemaking |
| APIs (optional key) | Alpaca websocket, CryptoPanic, Finnhub, NewsAPI | Real-time push; extra breadth |

Add or reweight sources by editing `FEEDS` in `config.py` — no other file needs
to change. Run `python main.py feeds` any time to check which are currently
reachable; sources occasionally move or retire their RSS endpoints.

## How scoring works (overview)

Each article is tagged to the asset(s) it's about, scored bullish/bearish, and
combined into a signed impact number. Article-level scores roll up into a
directional signal (BULLISH / BEARISH / NEUTRAL, with a confidence score) per
asset over several time windows.

The scoring logic itself lives in `scoring.py`, which is kept out of version
control (see `.gitignore`) — the exact lexicon and weighting are treated as
proprietary for now. If you have local access to the file, `SCORING_FAQ.md`
(also gitignored) documents the full methodology and how to extend it.

## Deduplication

The same story arrives from many outlets. Apollo dedupes on two hashes:
canonicalized URL (tracking params stripped) and aggressively normalized
headline (lowercased, punctuation and stopwords removed). Either match is
treated as a duplicate.

Duplicates aren't discarded — each additional outlet increments a
`corroboration` counter on the original row. How many independent outlets
picked a story up is itself a signal.

## Data model

SQLite (`apollo.db`, WAL mode):

- `articles` — deduped articles with scores and the original payload
- `article_sources` — every outlet sighting, including duplicates
- `market_snapshots` — price, 24h change, volume, BTC dominance, Fear & Greed,
  chain TVL, stablecoin supply
- `signals` — computed signal history per asset per window
- `feed_state` — ETag/Last-Modified and per-feed health

Query it directly, or `python main.py export` for JSONL.

```sql
-- Highest-impact BTC news in the last 6 hours
SELECT published_at, impact, source, title
FROM articles
WHERE assets LIKE '%"BTC"%'
  AND published_at >= datetime('now','-6 hours')
ORDER BY ABS(impact) DESC LIMIT 20;

-- Signal history for backtesting against price
SELECT computed_at, asset, window_min, net_impact, confidence, direction
FROM signals WHERE asset='BTC' AND window_min=60
ORDER BY computed_at DESC;
```

## Backtesting & paper trading

```
python main.py backfill 2025-01-01 2026-01-01   # historical prices + news
python main.py coverage                          # what history is loaded
python main.py backtest BTC 2025-01-01 2026-01-01
python main.py paper 1h                          # simulated trading, $10k
```

Paper trading is **simulated only** — no broker is contacted and no real
orders are placed. Runs persist for later analysis.

See **[BACKTESTING.md](BACKTESTING.md)** for the full guide: how look-ahead
bias is prevented, how to read IC / hit rate / decile tables, and the
important caveat that historical *news* (unlike prices) is only available via
Alpaca's keyed archive.

## Architecture

```
main.py         CLI + interactive menu
daemon.py       async orchestrator; runs all collectors concurrently
config.py       feed registry, assets, cadences, weights
store.py        SQLite, dedup, queries
scoring.py      tagging, sentiment lexicon, signal aggregation (gitignored)
historical.py   backfill: Coinbase OHLCV, Alpaca news archive, Fear & Greed
backtest.py     chronological replay + predictive-power metrics
paper_trader.py simulated trading engine with run persistence
collectors/
  base.py         Collector ABC, HTTP client w/ retry + conditional GET
  rss.py          ~68-feed firehose
  gdelt.py        GDELT Doc 2.0
  market.py       CoinGecko, Fear & Greed, DefiLlama
  government.py   Federal Register, SEC EDGAR full-text
  equities.py     Alpaca websocket, Yahoo Finance
  keyed.py        CryptoPanic, Finnhub, NewsAPI (optional)
```

Each collector runs its own loop at its own cadence and swallows its own
exceptions, so one dead source can't stall the rest. RSS polling uses
conditional GET, so unchanged feeds cost a 304 and no parsing; feeds that fail
repeatedly back off exponentially.

Adding a source means subclassing `Collector`, implementing `collect()` to
return `Article` objects, and decorating with `@register("name")`. Scoring,
dedup, and persistence are handled by the base class.

## Known limitations

- Sentiment is lexicon-based. It handles negation and hedging but not sarcasm,
  complex conditionals, or novel phrasing.
- Source weights and lexicon weights are hand-set priors, not fitted. The
  `signals` table plus price history is what you'd need to calibrate them.
- Google News RSS entries link to a redirect rather than the publisher, so
  URL-based dedup relies on the title hash for those.
- No survivorship-bias-free archive: signals are computed live. Backtesting
  needs the data to accumulate first.
- `scoring.py` is required for the program to run and is not tracked in git —
  if you clone this repo fresh elsewhere, that file needs to be copied in
  separately or the app will fail to import.

## Next steps

The `signals` table is designed to be joined against price data for
calibration — that's the natural next piece. `score_detail` (stored per
article) gives per-article labels usable as training data if you later want
to replace the lexicon with a learned classifier.

---

# Terminal Guide

Step-by-step for running Apollo from a Windows terminal (PyCharm's built-in
terminal, PowerShell, or Command Prompt). Skip to whichever section matches
where you're stuck.

## 1. Confirm Python is on PATH

```
python --version
```

If that fails with a Microsoft Store prompt instead of a version number,
Python isn't on PATH. Use the `py` launcher instead, which ships with the
python.org installer and is usually available regardless:

```
py --version
```

Use `py` in place of `python` for every command below if that's your situation.

## 2. Set up a virtual environment (recommended)

From the project folder:

```
py -m venv .venv
.venv\Scripts\activate
```

Your prompt should now show `(.venv)` at the start of the line. `.venv/` is
already excluded in `.gitignore`.

## 3. Install dependencies

```
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `pip` itself is missing (`No module named pip`), bootstrap it first:

```
python -m ensurepip --upgrade
```

If some packages fail to build from source, this is usually because you're on
a very new Python version without prebuilt wheels yet. Nothing in this project
requires anything past Python 3.10 — switching to a 3.11–3.13 interpreter
resolves it.

## 4. Configure (optional)

```
copy .env.example .env
```

Apollo runs fully keyless out of the box. Open `.env` if you want to add:

- `SEC_USER_AGENT` — recommended; the SEC blocks generic user agents on
  EDGAR/full-text search. Set it to `YourName/YourApp (you@email.com)`.
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — enables the real-time news
  websocket. Free at alpaca.markets (paper account is enough).
- `CRYPTOPANIC_TOKEN`, `FINNHUB_KEY`, `NEWSAPI_KEY` — optional extra sources,
  each free-tier. Collectors self-disable if these are blank.

## 5. Verify sources are reachable

```
python main.py feeds
```

This hits every RSS feed once and reports ok/FAIL per source. Run this after
any network change or if collection results look thin — feed URLs move over
time and this is the fastest way to catch it.

## 6. Run a single collection pass

```
python main.py once
```

Pulls from every enabled source once, scores everything, and prints new vs.
duplicate counts per collector. Good for a quick check that things are working
before leaving the daemon running.

## 7. Check what it found

```
python main.py signals          # current directional signals, BTC/ETH/SOL/MACRO
python main.py top BTC          # highest-impact BTC articles in the last 24h
python main.py stats            # database totals, top sources, any failing feeds
```

## 8. Run continuously

```
python main.py run
```

Starts the daemon: every collector runs on its own cadence indefinitely.
Ctrl+C to stop. To run only specific collectors (useful for debugging one
source):

```
python main.py run rss,coingecko
```

## 9. Backfill history and backtest

```
python main.py backfill 2025-01-01 2026-01-01
python main.py coverage
python main.py backtest BTC 2025-01-01 2026-01-01
python main.py backtests
```

The backfill is slow (news paging dominates) but idempotent — safe to re-run
over the same range. Historical *news* requires Alpaca keys in `.env`; prices
and Fear & Greed backfill without any key. See
[BACKTESTING.md](BACKTESTING.md) for how to interpret the output.

## 10. Paper trade (simulated)

```
python main.py paper 1h                 # 1 hour, $10,000 starting cash
python main.py paper 7d                 # one week
python main.py paper 0                  # unbounded, Ctrl+C to stop
python main.py paper 4h BTC,ETH         # specific assets only

python main.py papers                   # list past runs
python main.py paperrun 3               # trade log for run #3
```

No broker is contacted and no real orders are placed. Ctrl+C stops early and
still saves the run.

## 11. Everything in one place

Running `python main.py` with no arguments opens an interactive menu covering
all of the above — useful if you'd rather not remember the subcommands.

## 12. Run the test suites

```
python test_apollo.py       # 64 checks — scoring, dedup, parsing
python test_backtest.py     # 67 checks — look-ahead, stats, accounting
```

131 offline checks total, no network required. Run these after editing
`config.py`, `scoring.py`, or any collector to catch import errors and
regressions before they show up mid-run.

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| `python` opens the Microsoft Store | Not on PATH | Use `py` instead, or fix PATH in Settings → Advanced app settings → App execution aliases |
| `No module named pip` | Fresh Python install without pip | `python -m ensurepip --upgrade` |
| A feed shows `ClientConnectorCertificateError` | Missing/incomplete CA trust store (common on some Windows Python installs) | Already handled — `requirements.txt` includes `certifi`, and the HTTP client pins to it automatically |
| SEC feeds return 403 or get blocked | Generic/missing User-Agent | Set `SEC_USER_AGENT` in `.env` to a real name + email |
| A feed fails in `python main.py feeds` | Source moved/retired its RSS endpoint | Check `config.py`'s `FEEDS` list — dead ones are commented with why; update the URL or remove the entry |
| Everything imports but `once`/`run` produce 0 articles | No network access from the environment | Confirm outbound HTTPS isn't blocked; test with `python main.py feeds` |
