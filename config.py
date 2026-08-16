"""
Apollo — central configuration.

Everything tunable lives here: the feed registry, poll cadences, asset
definitions, and source trust weights. No API keys are required for any
source in FEEDS or the keyless collectors; keyed collectors degrade to
no-ops when their env var is absent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("APOLLO_DB", ROOT / "apollo.db"))
LOG_PATH = ROOT / "system_actions.log"

USER_AGENT = os.getenv(
    "APOLLO_USER_AGENT",
    "ApolloNewsScrapper/1.0 (research; ethansnyder445@gmail.com)",
)

# SEC requires a real contact string in the UA or it will rate-limit/ban.
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", USER_AGENT)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

# Optional — collectors skip themselves if these are unset.
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")

HTTP_TIMEOUT = 25
HTTP_CONCURRENCY = 12
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
# Each asset maps to the patterns that indicate an article is *about* it.
# Patterns are matched case-insensitively on word boundaries in scoring.py.

ASSETS: dict[str, dict] = {
    "BTC": {
        "name": "Bitcoin",
        "pair": "BTC-USD",
        "patterns": [
            r"bitcoin", r"\bbtc\b", r"\bxbt\b", r"satoshi", r"lightning network",
            r"bitcoin etf", r"\bibit\b", r"\bfbtc\b", r"\bgbtc\b", r"halving",
            r"\bmicrostrategy\b", r"\bstrategy inc\b", r"\bmstr\b", r"metaplanet",
            r"bitcoin mining", r"hashrate", r"hash rate", r"\bordinals\b",
            r"strategic bitcoin reserve",
        ],
        "proxies": ["MSTR", "MARA", "RIOT", "CLSK", "COIN", "IBIT", "HUT"],
    },
    "ETH": {
        "name": "Ethereum",
        "pair": "ETH-USD",
        "patterns": [
            r"ethereum", r"\beth\b", r"\bether\b", r"vitalik", r"\berc-?20\b",
            r"ethereum etf", r"\bethe\b", r"\betha\b", r"layer[- ]?2", r"\bl2\b",
            r"arbitrum", r"optimism", r"\bbase chain\b", r"\bzksync\b",
            r"staking", r"restaking", r"eigenlayer", r"\blido\b", r"\bsteth\b",
            r"\bdencun\b", r"\bpectra\b", r"\beip-\d+", r"gas fees",
        ],
        "proxies": ["COIN", "ETHE", "BITW"],
    },
    "SOL": {
        "name": "Solana",
        "pair": "SOL-USD",
        "patterns": [
            r"solana", r"\bsol\b", r"anatoly", r"yakovenko", r"\bjito\b",
            r"\bjupiter exchange\b", r"\bphantom wallet\b", r"\braydium\b",
            r"\bpump\.?fun\b", r"solana etf", r"\bfiredancer\b", r"\bsaga\b",
            r"solana mobile", r"\bmemecoin", r"\bbonk\b", r"\bhelium\b",
        ],
        "proxies": ["COIN", "GLXY"],
    },
    # CRYPTO is a pseudo-asset for sector-wide news that never names a specific
    # coin — an exchange hack, a stablecoin depeg, a custody failure. Without
    # it these stories score zero relevance and get dropped, which is exactly
    # backwards: they often move all three majors at once. scoring.py spills a
    # fraction of CRYPTO relevance onto BTC/ETH/SOL.
    "CRYPTO": {
        "name": "Crypto sector-wide",
        "pair": None,
        "patterns": [
            r"crypto\w*", r"digital asset", r"blockchain", r"stablecoin",
            r"\busdt\b", r"\busdc\b", r"tether", r"\bcircle\b", r"\bdefi\b",
            r"\bweb3\b", r"\bnft\b", r"\btoken\w*", r"altcoin", r"\bcoinbase\b",
            r"\bbinance\b", r"\bkraken\b", r"\bgemini\b", r"\bbitfinex\b",
            r"\bokx\b", r"\bbybit\b", r"\bripple\b", r"\bxrp\b",
            r"crypto exchange", r"digital currenc", r"virtual currenc",
            r"self-custody", r"hardware wallet", r"crypto wallet",
            r"cold storage", r"seed phrase", r"private keys",
            r"\bmica\b", r"\bclarity act\b", r"\bgenius act\b",
        ],
        "proxies": ["COIN", "CRCL", "BITW", "GLXY", "HOOD"],
    },
    # MACRO is a pseudo-asset: geopolitical/monetary news that moves all risk
    # assets. Tagged articles feed a market-wide risk-on/risk-off signal.
    "MACRO": {
        "name": "Macro / Geopolitical",
        "pair": None,
        "patterns": [
            r"federal reserve", r"\bfomc\b", r"\bthe fed\b", r"interest rate",
            r"rate (?:cut|hike|decision)", r"\bcpi\b", r"\bpce\b", r"inflation",
            r"\bppi\b", r"nonfarm payroll", r"jobs report", r"unemployment rate",
            r"\bjerome powell\b", r"\bpowell\b", r"quantitative (?:easing|tightening)",
            r"treasury yield", r"\b10-year\b", r"yield curve", r"\bdxy\b",
            r"dollar index", r"recession", r"\bgdp\b", r"debt ceiling",
            r"government shutdown", r"\btariff", r"trade war", r"sanction",
            r"\bopec\b", r"oil price", r"crude", r"\bgeopolit", r"\bwar\b",
            r"military strike", r"\bceasefire\b", r"\bsec\b", r"\bcftc\b",
            r"\bregulat", r"\betf approval\b", r"\bstablecoin\b", r"\bcbdc\b",
            r"\bbasel\b", r"\bimf\b", r"central bank", r"\becb\b",
            r"bank of japan", r"\bboj\b", r"\bcarry trade\b",
        ],
        "proxies": ["SPY", "QQQ", "DXY", "TLT", "GLD"],
    },
}

CRYPTO_ASSETS = ["BTC", "ETH", "SOL"]


# ---------------------------------------------------------------------------
# Feed registry
# ---------------------------------------------------------------------------

@dataclass
class Feed:
    """A single RSS/Atom endpoint."""
    key: str
    url: str
    category: str          # crypto | macro | equities | regulator | geopolitical
    weight: float = 1.0    # source trust multiplier applied to signal strength
    poll_seconds: int = 120
    tags: list[str] = field(default_factory=list)


# Weight guide:
#   1.4-1.5  primary regulator / central bank (the actual source of truth)
#   1.2-1.3  top-tier wires and crypto trade press with a newsroom
#   0.9-1.1  solid mainstream and specialist outlets
#   0.6-0.8  aggregators, opinion-heavy, or high-noise feeds

FEEDS: list[Feed] = [
    # ---- Crypto-native trade press -------------------------------------
    Feed("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto", 1.3, 90),
    Feed("cointelegraph", "https://cointelegraph.com/rss", "crypto", 1.1, 90),
    Feed("theblock", "https://www.theblock.co/rss.xml", "crypto", 1.3, 90),
    Feed("decrypt", "https://decrypt.co/feed", "crypto", 1.0, 120),
    Feed("bitcoinmagazine", "https://bitcoinmagazine.com/.rss/full/", "crypto", 0.9, 180),
    Feed("bitcoinist", "https://bitcoinist.com/feed/", "crypto", 0.7, 180),
    Feed("newsbtc", "https://www.newsbtc.com/feed/", "crypto", 0.7, 180),
    Feed("cryptoslate", "https://cryptoslate.com/feed/", "crypto", 0.9, 150),
    Feed("cryptobriefing", "https://cryptobriefing.com/feed/", "crypto", 0.9, 180),
    Feed("bitcoincom", "https://news.bitcoin.com/feed/", "crypto", 0.8, 180),
    Feed("ambcrypto", "https://ambcrypto.com/feed/", "crypto", 0.7, 180),
    Feed("beincrypto", "https://beincrypto.com/feed/", "crypto", 0.8, 150),
    Feed("dlnews", "https://www.dlnews.com/arc/outboundfeeds/rss/", "crypto", 1.0, 180),
    Feed("blockworks", "https://blockworks.co/feed", "crypto", 1.2, 120),
    Feed("protos", "https://protos.com/feed/", "crypto", 0.8, 240),
    Feed("cryptopotato", "https://cryptopotato.com/feed/", "crypto", 0.7, 240),
    Feed("coinjournal", "https://coinjournal.net/news/feed/", "crypto", 0.7, 240),
    Feed("thedefiant", "https://thedefiant.io/api/feed", "crypto", 1.0, 180),

    # ---- Ecosystem / protocol primary sources --------------------------
    Feed("ethereum_blog", "https://blog.ethereum.org/en/feed.xml", "crypto", 1.4, 900,
         tags=["ETH"]),
    Feed("solana_news", "https://solana.com/news/rss.xml", "crypto", 1.4, 900,
         tags=["SOL"]),
    Feed("bitcoin_core", "https://bitcoincore.org/en/rss.xml", "crypto", 1.4, 1800,
         tags=["BTC"]),
    # coinbase_blog: no public RSS endpoint as of Aug 2026 (checked /rss.xml,
    # /rss, /feed — none resolve; the blog page itself has no <link rel=alternate>
    # feed tag). Coinbase news still reaches us via yfinance (ticker COIN) and
    # the trade press above.
    Feed("kraken_blog", "https://blog.kraken.com/feed", "crypto", 1.0, 1800),
    # binance_blog removed: confirmed geo-restricted to US IPs (returns HTTP
    # 202 with no body rather than the feed). No fix on the URL side.
    # a16z_crypto / galaxy_research: no working public RSS found (a16zcrypto.com,
    # a16z.com, and galaxy.com/insights all 404 or serve no feed as of Aug 2026).
    # Both appear to have moved to gated newsletters. Removed rather than left
    # pointing at a dead URL — re-add in config.py if they resurface one.

    # ---- Macro / markets wires -----------------------------------------
    Feed("cnbc_finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                         "?partnerId=wrss01&id=10000664", "macro", 1.0, 120),
    Feed("cnbc_economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                         "?partnerId=wrss01&id=20910258", "macro", 1.0, 120),
    Feed("cnbc_markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                         "?partnerId=wrss01&id=20409666", "macro", 1.0, 120),
    Feed("yahoo_finance", "https://finance.yahoo.com/news/rssindex", "equities", 0.8, 120),
    Feed("marketwatch_top", "https://feeds.content.dowjones.io/public/rss/mw_topstories",
         "equities", 1.0, 120),
    Feed("marketwatch_rt", "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
         "equities", 1.0, 90),
    Feed("marketwatch_bulletins", "https://feeds.content.dowjones.io/public/rss/mw_bulletins",
         "equities", 1.2, 90),
    Feed("ft_markets", "https://www.ft.com/markets?format=rss", "macro", 1.2, 180),
    Feed("ft_world", "https://www.ft.com/world?format=rss", "geopolitical", 1.2, 300),
    Feed("economist_finance", "https://www.economist.com/finance-and-economics/rss.xml",
         "macro", 1.1, 1800),
    Feed("wsj_markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
         "macro", 1.2, 180),
    Feed("wsj_world", "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
         "geopolitical", 1.2, 300),
    Feed("zerohedge", "https://feeds.feedburner.com/zerohedge/feed", "macro", 0.6, 180),
    Feed("investing_news", "https://www.investing.com/rss/news.rss", "macro", 0.8, 150),
    Feed("investing_crypto", "https://www.investing.com/rss/news_301.rss", "crypto", 0.8, 150),
    Feed("seekingalpha_mkt", "https://seekingalpha.com/market_currents.xml", "equities", 0.8, 180),
    Feed("businessinsider_mkt", "https://markets.businessinsider.com/rss/news", "equities", 0.7, 240),

    # ---- Wires via Google News (Reuters/AP/Bloomberg have no open RSS) --
    # NOTE: the "allinurl:" operator returned 0 items in production testing —
    # Google News' search RSS doesn't reliably honor it. "site:" is the
    # standard, verified-working restriction operator.
    Feed("gnews_reuters_biz",
         "https://news.google.com/rss/search?q=site:reuters.com+when:12h"
         "&hl=en-US&gl=US&ceid=US:en", "macro", 1.2, 300),
    Feed("gnews_bloomberg",
         "https://news.google.com/rss/search?q=site:bloomberg.com+when:12h"
         "&hl=en-US&gl=US&ceid=US:en", "macro", 1.2, 300),
    Feed("gnews_ap",
         "https://news.google.com/rss/search?q=site:apnews.com+when:12h"
         "&hl=en-US&gl=US&ceid=US:en", "geopolitical", 1.1, 300),
    Feed("gnews_btc",
         "https://news.google.com/rss/search?q=bitcoin+when:6h&hl=en-US&gl=US&ceid=US:en",
         "crypto", 0.8, 300, tags=["BTC"]),
    Feed("gnews_eth",
         "https://news.google.com/rss/search?q=ethereum+when:6h&hl=en-US&gl=US&ceid=US:en",
         "crypto", 0.8, 300, tags=["ETH"]),
    Feed("gnews_sol",
         "https://news.google.com/rss/search?q=solana+when:6h&hl=en-US&gl=US&ceid=US:en",
         "crypto", 0.8, 300, tags=["SOL"]),
    Feed("gnews_fed",
         "https://news.google.com/rss/search?q=%22federal+reserve%22+OR+FOMC+when:12h"
         "&hl=en-US&gl=US&ceid=US:en", "macro", 1.0, 600),
    Feed("gnews_crypto_reg",
         "https://news.google.com/rss/search?q=crypto+regulation+OR+SEC+crypto+when:12h"
         "&hl=en-US&gl=US&ceid=US:en", "macro", 0.9, 600),

    # ---- Regulators & central banks (primary sources, highest weight) ---
    Feed("fed_press", "https://www.federalreserve.gov/feeds/press_all.xml",
         "regulator", 1.5, 300),
    Feed("fed_monetary", "https://www.federalreserve.gov/feeds/press_monetary.xml",
         "regulator", 1.5, 300),
    Feed("fed_speeches", "https://www.federalreserve.gov/feeds/speeches.xml",
         "regulator", 1.4, 600),
    Feed("sec_press", "https://www.sec.gov/news/pressreleases.rss", "regulator", 1.5, 300),
    # Verified against the live endpoint (Aug 2026) — the old
    # /rss/litigation/litreleases.xml path 404s; SEC moved litigation releases
    # under /enforcement-litigation/.
    Feed("sec_litigation", "https://www.sec.gov/enforcement-litigation/litigation-releases/rss",
         "regulator", 1.4, 600),
    Feed("cftc_press", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", "regulator", 1.4, 600),
    # Verified working, but this is Treasury's site-wide feed (press releases
    # mixed with FAQ updates, program pages, etc.) — the old /rss/press.xml
    # path is gone and no press-only feed replaced it. Weight kept modest to
    # reflect the extra noise; scoring still filters on content, not source.
    Feed("treasury_press", "https://home.treasury.gov/rss.xml", "macro", 1.1, 600),
    # ofac_recent removed: OFAC officially retired its Recent Actions RSS feed
    # on 2026-01-31 in favor of GovDelivery email. Sanctions news still surfaces
    # via treasury_press, gdelt, and federal_register.
    Feed("bls_news", "https://www.bls.gov/feed/bls_latest.rss", "macro", 1.5, 600),
    Feed("bea_news", "https://apps.bea.gov/rss/rss.xml", "macro", 1.4, 900),
    # ECB URL is correct per ecb.europa.eu's own RSS index page; the failure
    # seen in testing was a TLS handshake error (ClientConnectorCertificateError),
    # not a bad URL. See HttpClient in collectors/base.py — the connector now
    # pins to certifi's CA bundle, which is the usual fix for this on Windows
    # Python installs that ship without a populated system trust store.
    Feed("ecb_press", "https://www.ecb.europa.eu/rss/press.html", "regulator", 1.3, 900),
    # imf_news removed: imf.org/en/News/RSS is a client-rendered SPA shell with
    # no feed in the initial response, and returns HTTP 403 to non-browser
    # clients. No working alternative found. IMF commentary still surfaces via
    # gdelt and the macro/geopolitical wires above.
    Feed("whitehouse", "https://www.whitehouse.gov/news/feed/", "geopolitical", 1.2, 600),
    # Verified against OCC's own RSS index page — the old
    # occ_news_releases.xml path 404s; correct filename is occ_news.xml.
    Feed("occ_news", "https://www.occ.gov/rss/occ_news.xml", "regulator", 1.3, 900),
    # fdic_press: FDIC's own /news/press-releases/rss.xml 404s. This points at
    # their GovDelivery distribution feed instead — best-effort, unverified
    # from this environment (network-restricted); check `python main.py feeds`
    # after setup and drop it from FEEDS if it doesn't resolve.
    Feed("fdic_press", "https://public.govdelivery.com/topics/USFDIC_26/feed.rss",
         "regulator", 1.2, 900),
    # finra_news removed: both /about/news-center/rss and the
    # feeds.finra.org/news-and-events/feed candidate 404 in production. FINRA
    # appears to have no working public RSS feed as of Aug 2026 — their
    # developer center points to paid API subscriptions instead. FINRA
    # enforcement news still surfaces via sec_press, sec_litigation, and the
    # broad wire feeds (it's routinely covered as SEC/finance news anyway).

    # ---- Geopolitical ---------------------------------------------------
    Feed("aljazeera", "https://www.aljazeera.com/xml/rss/all.xml", "geopolitical", 0.9, 300),
    Feed("bbc_world", "https://feeds.bbci.co.uk/news/world/rss.xml", "geopolitical", 1.1, 300),
    Feed("bbc_business", "https://feeds.bbci.co.uk/news/business/rss.xml", "macro", 1.1, 300),
    Feed("guardian_world", "https://www.theguardian.com/world/rss", "geopolitical", 1.0, 300),
    Feed("nyt_world", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
         "geopolitical", 1.1, 300),
    Feed("nyt_business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
         "macro", 1.1, 300),
    Feed("dw_world", "https://rss.dw.com/rdf/rss-en-world", "geopolitical", 0.9, 600),
    Feed("scmp_china", "https://www.scmp.com/rss/4/feed", "geopolitical", 0.9, 600),
    # politico_econ removed: rss.politico.com/economy.xml 404s and no working
    # replacement was found (Politico's public RSS surface has shrunk).
    # cfr_analysis removed: cfr.org has no single working analysis/commentary
    # feed as of Aug 2026 — only region-specific feeds under feeds.cfr.org,
    # none of which map cleanly to "crypto/macro relevant." Geopolitical
    # analysis still comes through ft_world, economist_finance, and gdelt.
]


# ---------------------------------------------------------------------------
# Polling cadence for non-RSS collectors (seconds)
# ---------------------------------------------------------------------------
POLL_INTERVALS = {
    "gdelt": 300,
    "coingecko": 120,
    "fear_greed": 900,
    "federal_register": 1800,
    "sec_firehose": 300,
    "yfinance": 600,
    "cryptopanic": 300,
    "finnhub": 300,
    "defillama": 900,
    "economic_calendar": 3600,
}

# Tickers watched by the equities-side collectors (crypto-correlated names).
WATCH_TICKERS = [
    "COIN", "MSTR", "MARA", "RIOT", "CLSK", "HUT", "GLXY", "CRCL", "HOOD",
    "SPY", "QQQ", "NVDA", "TSLA",
]

# CoinGecko ids for the market-context snapshot.
COINGECKO_IDS = ["bitcoin", "ethereum", "solana"]

# GDELT queries — geopolitical/macro events with crypto or risk-asset relevance.
GDELT_QUERIES = [
    "bitcoin OR cryptocurrency",
    "ethereum OR solana",
    '"federal reserve" OR "interest rates"',
    '"crypto regulation" OR "digital assets"',
    "sanctions OR tariffs OR (trade war)",
]
