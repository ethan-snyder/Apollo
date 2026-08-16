"""
Market-context collectors (keyless).

These don't produce news articles; they write `market_snapshots` rows. The
point is that a headline alone is not a signal — "SEC delays ETF decision"
means something different when BTC is up 8% on the day versus down 12% with
Fear & Greed at 12. Storing context alongside news makes that joinable later.

Sources:
  CoinGecko      — price, 24h change, volume, market cap, BTC dominance
  Alternative.me — Crypto Fear & Greed Index
  DefiLlama      — chain TVL and stablecoin supply (liquidity proxy)
"""
from __future__ import annotations

import asyncio
import json

from config import COINGECKO_IDS, POLL_INTERVALS
from store import Article, record_snapshot
from .base import Collector, register

CG_BASE = "https://api.coingecko.com/api/v3"
FNG_URL = "https://api.alternative.me/fng/?limit=2"
LLAMA_CHAINS = "https://api.llama.fi/v2/chains"
LLAMA_STABLES = "https://stablecoins.llama.fi/stablecoins?includePrices=false"

_ID_TO_ASSET = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}


@register("coingecko")
class CoinGeckoCollector(Collector):
    """
    Price/volume context. CoinGecko's free tier is roughly 10-30 calls/min;
    two calls every two minutes sits comfortably inside that.
    """
    interval = POLL_INTERVALS["coingecko"]

    async def collect(self) -> list[Article]:
        ids = ",".join(COINGECKO_IDS)
        price_url = (f"{CG_BASE}/simple/price?ids={ids}&vs_currencies=usd"
                     "&include_24hr_change=true&include_24hr_vol=true"
                     "&include_market_cap=true")

        data, glob = await asyncio.gather(
            self.http.get_json(price_url),
            self.http.get_json(f"{CG_BASE}/global"),
            return_exceptions=True,
        )

        if not isinstance(data, Exception):
            for cid, vals in (data or {}).items():
                asset = _ID_TO_ASSET.get(cid, cid.upper())
                await asyncio.to_thread(
                    record_snapshot, "coingecko", "price_usd",
                    vals.get("usd"), asset, vals)
                await asyncio.to_thread(
                    record_snapshot, "coingecko", "change_24h_pct",
                    vals.get("usd_24h_change"), asset, None)
                await asyncio.to_thread(
                    record_snapshot, "coingecko", "volume_24h_usd",
                    vals.get("usd_24h_vol"), asset, None)
        else:
            self.log.warning("price fetch failed: %s", data)

        if not isinstance(glob, Exception):
            d = (glob or {}).get("data", {})
            pct = d.get("market_cap_percentage", {})
            await asyncio.to_thread(
                record_snapshot, "coingecko", "btc_dominance_pct",
                pct.get("btc"), "BTC", None)
            await asyncio.to_thread(
                record_snapshot, "coingecko", "eth_dominance_pct",
                pct.get("eth"), "ETH", None)
            await asyncio.to_thread(
                record_snapshot, "coingecko", "total_market_cap_usd",
                (d.get("total_market_cap") or {}).get("usd"), None, None)
        else:
            self.log.warning("global fetch failed: %s", glob)

        return []   # context only, no articles


@register("fear_greed")
class FearGreedCollector(Collector):
    """
    Crypto Fear & Greed Index (0 = extreme fear, 100 = extreme greed).

    Emits a snapshot every run, plus an *article* when the reading crosses a
    regime boundary — those transitions are the tradeable part, not the level.
    """
    interval = POLL_INTERVALS["fear_greed"]

    _BANDS = [(0, 25, "Extreme Fear"), (25, 45, "Fear"), (45, 55, "Neutral"),
              (55, 75, "Greed"), (75, 101, "Extreme Greed")]

    @classmethod
    def _band(cls, v: float) -> str:
        for lo, hi, name in cls._BANDS:
            if lo <= v < hi:
                return name
        return "Unknown"

    async def collect(self) -> list[Article]:
        data = await self.http.get_json(FNG_URL)
        entries = (data or {}).get("data", [])
        if not entries:
            return []

        cur = entries[0]
        value = float(cur.get("value", 0))
        band = cur.get("value_classification") or self._band(value)
        await asyncio.to_thread(
            record_snapshot, "alternative.me", "fear_greed", value, None,
            {"classification": band})

        if len(entries) < 2:
            return []

        prev_val = float(entries[1].get("value", value))
        prev_band = entries[1].get("value_classification") or self._band(prev_val)
        if prev_band == band:
            return []

        direction = "improving" if value > prev_val else "deteriorating"
        return [Article(
            title=f"Crypto Fear & Greed Index moves to {band} ({value:.0f}) "
                  f"from {prev_band} ({prev_val:.0f})",
            summary=f"Market sentiment regime shift — {direction}. "
                    f"Extreme Fear readings have historically marked local "
                    f"bottoms; Extreme Greed, local tops.",
            source="fear_greed_index",
            source_category="macro",
            source_weight=1.0,
            url=f"https://alternative.me/crypto/fear-and-greed-index/#{value:.0f}",
            raw={"value": value, "prev": prev_val, "band": band,
                 "_feed_tags": ["BTC", "ETH", "SOL"]},
        )]


@register("defillama")
class DefiLlamaCollector(Collector):
    """
    On-chain liquidity context: per-chain TVL and total stablecoin supply.

    Stablecoin supply is a decent proxy for dry powder entering or leaving the
    crypto system; chain TVL tracks where that capital is actually deployed.
    """
    interval = POLL_INTERVALS["defillama"]

    _CHAINS = {"Ethereum": "ETH", "Solana": "SOL", "Bitcoin": "BTC"}

    async def collect(self) -> list[Article]:
        chains, stables = await asyncio.gather(
            self.http.get_json(LLAMA_CHAINS),
            self.http.get_json(LLAMA_STABLES),
            return_exceptions=True,
        )

        if not isinstance(chains, Exception):
            for c in chains or []:
                asset = self._CHAINS.get(c.get("name"))
                if asset:
                    await asyncio.to_thread(
                        record_snapshot, "defillama", "chain_tvl_usd",
                        c.get("tvl"), asset, None)
        else:
            self.log.warning("chains fetch failed: %s", chains)

        if not isinstance(stables, Exception):
            total = 0.0
            for s in (stables or {}).get("peggedAssets", []):
                circ = s.get("circulating") or {}
                val = circ.get("peggedUSD")
                if isinstance(val, (int, float)):
                    total += val
            if total:
                await asyncio.to_thread(
                    record_snapshot, "defillama", "stablecoin_supply_usd",
                    total, None, None)
        else:
            self.log.warning("stablecoins fetch failed: %s", stables)

        return []
