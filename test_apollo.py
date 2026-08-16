"""
Apollo self-test — no network required.

Covers the parts most likely to break silently: URL/title normalization,
dedup behaviour, asset tagging, sentiment polarity (including negation and
hedging), RSS parsing, and signal aggregation.

    python test_apollo.py
"""
from __future__ import annotations

import os
import sys
import tempfile

# Point the store at a throwaway DB before anything imports config.
_TMP_DB = os.path.join(tempfile.mkdtemp(), "apollo_test.db")
os.environ["APOLLO_DB"] = _TMP_DB

import store                                     # noqa: E402
import scoring                                   # noqa: E402
from config import Feed                          # noqa: E402
from collectors.rss import parse_feed, clean_html  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  pass  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------------------
print("\nURL canonicalization")
check("strips tracking params",
      store.canonical_url("https://www.coindesk.com/markets/x?utm_source=tw&a=1")
      == "https://coindesk.com/markets/x?a=1")
check("drops www and fragment",
      store.canonical_url("http://www.example.com/a/#top")
      == "https://example.com/a")
check("handles empty input", store.canonical_url(None) == "")

print("\nTitle normalization")
t1 = store.normalize_title("Bitcoin Surges Past $70,000 as ETF Inflows Hit a Record")
t2 = store.normalize_title("bitcoin surges past $70,000 as ETF inflows hit record!")
check("near-duplicate headlines collapse", t1 == t2, f"{t1!r} != {t2!r}")
check("stopwords removed", "the" not in t1.split())

# ---------------------------------------------------------------------------
print("\nStorage and dedup")
store.init_db()

a1 = store.Article(title="SEC Approves Spot Solana ETF Applications",
                   url="https://coindesk.com/policy/sol-etf?utm_source=x",
                   source="coindesk", source_category="crypto", source_weight=1.3,
                   published_at="2026-08-16T12:00:00Z")
a2 = store.Article(title="SEC approves spot Solana ETF applications!",
                   url="https://decrypt.co/sol-etf-approved",
                   source="decrypt", source_category="crypto", source_weight=1.0,
                   published_at="2026-08-16T12:04:00Z")
a3 = store.Article(title="Ethereum Foundation announces Pectra upgrade timeline",
                   url="https://blog.ethereum.org/pectra",
                   source="ethereum_blog", source_category="crypto",
                   source_weight=1.4, published_at="2026-08-16T11:00:00Z")

for a in (a1, a2, a3):
    scoring.score_article(a)

id1, new1 = store.upsert_article(a1)
id2, new2 = store.upsert_article(a2)
id3, new3 = store.upsert_article(a3)

check("first article inserted", new1 is True)
check("near-duplicate from another outlet suppressed", new2 is False)
check("duplicate maps to the same row", id1 == id2, f"{id1} != {id2}")
check("distinct article inserted", new3 is True and id3 != id1)

row = store.get_conn().execute("SELECT corroboration FROM articles WHERE id=?",
                               (id1,)).fetchone()
check("corroboration incremented to 2", row["corroboration"] == 2,
      f"got {row['corroboration']}")

_, again = store.upsert_article(a2)
row = store.get_conn().execute("SELECT corroboration FROM articles WHERE id=?",
                               (id1,)).fetchone()
check("re-seeing the same outlet does not inflate corroboration",
      row["corroboration"] == 2, f"got {row['corroboration']}")

# ---------------------------------------------------------------------------
print("\nAsset tagging")
check("BTC tagged from headline",
      "BTC" in scoring.tag_assets("Bitcoin ETF sees record inflows"))
check("ETH tagged", "ETH" in scoring.tag_assets("Ethereum staking yields fall"))
check("SOL tagged", "SOL" in scoring.tag_assets("Solana network hits record TVL"))
check("MACRO tagged",
      "MACRO" in scoring.tag_assets("Federal Reserve signals a rate cut in September"))
check("unrelated text tags nothing",
      scoring.tag_assets("Local bakery wins county pie contest") == {})
check("headline mention outranks body mention",
      scoring.tag_assets("Bitcoin rallies\nnothing here")["BTC"]
      > scoring.tag_assets("Markets move\nbitcoin was mentioned once")["BTC"])

print("\nTicker extraction")
tk = scoring.extract_tickers("COIN and MSTR rallied while $NVDA slipped")
check("proxy + $-prefixed tickers found",
      {"COIN", "MSTR", "NVDA"} <= set(tk), str(tk))

# ---------------------------------------------------------------------------
print("\nSentiment polarity")
cases = [
    ("SEC approves spot bitcoin ETF, record inflows follow", 1),
    ("Bitcoin crashes as exchange halts withdrawals amid insolvency fears", -1),
    ("Fed cuts interest rates by 50bps, dovish tone surprises markets", 1),
    ("Fed raises rates as inflation climbs hotter than expected", -1),
    ("Major DeFi protocol exploited, $200M drained", -1),
    ("Court dismisses SEC lawsuit against Ripple", 1),
    ("Stablecoin depegs, contagion spreads across lenders", -1),
]
for text, expected in cases:
    s, _ = scoring.score_sentiment(text)
    sign = 1 if s > 0 else (-1 if s < 0 else 0)
    check(f"{'bullish' if expected > 0 else 'bearish'}: {text[:44]}...",
          sign == expected, f"score={s}")

print("\nNegation and hedging")
s_plain, _ = scoring.score_sentiment("SEC approves the bitcoin ETF")
s_neg, _ = scoring.score_sentiment("SEC does not approve the bitcoin ETF")
check("negation flips polarity", s_neg < 0 < s_plain, f"{s_plain} -> {s_neg}")

s_hedge, _ = scoring.score_sentiment("SEC may approve the bitcoin ETF")
check("hedged claim scores weaker than asserted",
      0 < s_hedge < s_plain, f"plain={s_plain} hedged={s_hedge}")

print("\nRegressions found against live CoinDesk data")

# "Bullish" is a crypto exchange; it was firing as positive sentiment in
# headlines that were plainly negative.
s, _ = scoring.score_sentiment(
    "Tokenization stocks slip as SEC delay puts speed bump in crypto's push. "
    "Bullish, Coinbase, and Circle were among the names lower on Friday.")
check("exchange named 'Bullish' does not read as bullish sentiment", s < 0, str(s))
s, _ = scoring.score_sentiment("Traders build a bullish case for a Q4 breakout")
check("adjectival 'bullish case' still reads bullish", s > 0, str(s))

# Sector-wide crypto news used to score zero relevance and get dropped.
a = store.Article(title="Crypto wallet provider reveals a data breach exposing "
                        "40,000 customers", summary="Private keys remain safe.",
                  source="t", source_weight=1.0,
                  published_at="2026-08-16T12:00:00Z")
scoring.score_article(a)
check("sector-wide crypto news is tagged CRYPTO", "CRYPTO" in a.assets, str(a.assets))
check("CRYPTO relevance spills onto the majors",
      {"BTC", "ETH", "SOL"} <= set(a.assets), str(a.assets))
check("spillover is a fraction, not full relevance",
      a.score_detail["asset_relevance"]["BTC"]
      < a.score_detail["asset_relevance"]["CRYPTO"])
check("sector-wide breach carries non-zero bearish impact", a.impact < 0, str(a.impact))

# Coin-specific reporting must still outrank spillover.
b = store.Article(title="Bitcoin crashes 20% as miners capitulate",
                  source="t", source_weight=1.0,
                  published_at="2026-08-16T12:00:00Z")
scoring.score_article(b)
check("named-asset article outranks spillover on relevance",
      b.score_detail["asset_relevance"]["BTC"] > 0.5)

# Market-commentary vocabulary that previously scored zero.
for text in ("Cluster of headwinds weigh on bitcoin as the picture sours",
             "Bitcoin wipes out last week's gains, ETFs see a two-day drawdown",
             "Bitcoin fails to hold support and gives back gains"):
    s, _ = scoring.score_sentiment(text)
    check(f"commentary reads bearish: {text[:40]}...", s < 0, str(s))

s, _ = scoring.score_sentiment(
    "Israel's largest bank to offer crypto trading to retail customers")
check("adoption/distribution news reads bullish", s > 0, str(s))
s, _ = scoring.score_sentiment("UBS ramps up its Bitcoin exposure in ETF call options")
check("institution increasing exposure reads bullish", s > 0, str(s))

print("\nScore bounds")
extreme = store.Article(
    title="Bitcoin surges to all-time high as ETF approval sparks record inflows "
          "and institutional adoption accelerates in a massive rally breakout",
    source="test", source_weight=1.5, published_at="2026-08-16T12:00:00Z")
scoring.score_article(extreme)
check("sentiment stays within [-1, 1]", -1.0 <= extreme.sentiment <= 1.0,
      str(extreme.sentiment))
check("relevance stays within [0, 1]", 0.0 <= extreme.relevance <= 1.0,
      str(extreme.relevance))

print("\nRecency decay")
r_new = scoring.recency_factor("2999-01-01T00:00:00+00:00")
r_none = scoring.recency_factor(None)
check("future/fresh timestamp gives full weight", r_new == 1.0, str(r_new))
check("missing timestamp gives a partial default", 0 < r_none < 1)

# ---------------------------------------------------------------------------
print("\nRSS parsing")
SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
<item>
  <title><![CDATA[Bitcoin slips as U.S. inflation runs hotter]]></title>
  <link>https://example.com/a?utm_source=rss</link>
  <pubDate>Sun, 16 Aug 2026 12:00:00 +0000</pubDate>
  <description><![CDATA[<p>Spot ETFs saw <b>outflows</b> for a second day.</p>]]></description>
</item>
<item>
  <title>Empty link item</title>
  <pubDate>bad date here</pubDate>
</item>
</channel></rss>"""

feed = Feed("testfeed", "https://example.com/rss", "crypto", 1.1, 60, tags=["BTC"])
arts = parse_feed(feed, SAMPLE)
check("both items parsed", len(arts) == 2, str(len(arts)))
check("HTML stripped from summary", "<b>" not in arts[0].summary
      and "outflows" in arts[0].summary)
check("pubDate converted to ISO UTC",
      arts[0].published_at == "2026-08-16T12:00:00+00:00", str(arts[0].published_at))
check("unparseable date becomes None", arts[1].published_at is None)
check("feed tags carried into raw", arts[0].raw["_feed_tags"] == ["BTC"])
check("source weight inherited", arts[0].source_weight == 1.1)

scoring.score_article(arts[0])
check("parsed article scores bearish", arts[0].sentiment < 0, str(arts[0].sentiment))
check("parsed article tagged BTC", "BTC" in arts[0].assets, str(arts[0].assets))

check("clean_html handles None", clean_html(None) == "")

# ---------------------------------------------------------------------------
print("\nSignal aggregation")


class Row(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


bull = [Row(impact=0.6, sentiment=0.7, title=f"bull {i}", source="s") for i in range(8)]
bear = [Row(impact=-0.6, sentiment=-0.7, title=f"bear {i}", source="s") for i in range(8)]

agg_b = scoring.aggregate_signal(bull, 60)
agg_x = scoring.aggregate_signal(bull + bear, 60)
agg_0 = scoring.aggregate_signal([], 60)

check("unanimous bullish set reads bullish",
      agg_b["direction"] in ("BULLISH", "STRONG_BULLISH"), agg_b["direction"])
check("unanimous set has high confidence", agg_b["confidence"] > 0.5,
      str(agg_b["confidence"]))
check("evenly split set reads neutral", agg_x["direction"] == "NEUTRAL",
      agg_x["direction"])
check("split set has lower confidence than unanimous",
      agg_x["confidence"] < agg_b["confidence"])
check("empty input is safe", agg_0["article_count"] == 0
      and agg_0["direction"] == "NEUTRAL")
check("top drivers included", len(agg_b["detail"]["top_drivers"]) > 0)

# ---------------------------------------------------------------------------
print("\nQueries and stats")
s = store.stats()
check("stats returns article count", s["total_articles"] == 2, str(s["total_articles"]))
check("per-asset counts present", "BTC" in s["by_asset"])
check("recent_articles filters by asset",
      all("ETH" in (r["assets"] or "")
          for r in store.recent_articles(asset="ETH", limit=10)))

store.record_snapshot("test", "price_usd", 63000.0, "BTC", {"src": "unit-test"})
snap = store.latest_snapshot("price_usd", "BTC")
check("snapshot round-trips", snap is not None and snap["value"] == 63000.0)

store.record_signal("BTC", 60, 5, 1.2, 0.4, "BULLISH", 0.7, {"k": "v"})
check("signal round-trips", len(store.latest_signals()) == 1)

out = os.path.join(os.path.dirname(_TMP_DB), "export.jsonl")
n = store.export_jsonl(out)
check("JSONL export writes every row", n == 2, str(n))

# ---------------------------------------------------------------------------
print(f"\n{'=' * 50}\n  {PASS} passed, {FAIL} failed\n{'=' * 50}\n")
sys.exit(1 if FAIL else 0)
