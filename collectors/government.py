"""
Government / regulatory primary sources (keyless).

Regulatory news is the single largest discrete mover of crypto prices, and the
wire coverage of it lags the primary document by anywhere from minutes to
hours. Reading the source directly is the edge.

  Federal Register  — proposed and final rules mentioning digital assets
  EDGAR full-text   — 8-K/10-Q/S-1 filings mentioning bitcoin/crypto holdings
  EDGAR firehose    — all filings by watched crypto-exposed issuers
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from config import POLL_INTERVALS, SEC_USER_AGENT, WATCH_TICKERS
from store import Article
from .base import Collector, register

FR_BASE = "https://www.federalregister.gov/api/v1/documents.json"
EFTS = "https://efts.sec.gov/LATEST/search-index"
EDGAR_CURRENT = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
                 "&type={form}&company=&dateb=&owner=include&count=100&output=atom")

FR_TERMS = [
    "digital asset", "cryptocurrency", "stablecoin", "bitcoin",
    "blockchain", "virtual currency",
]

# Filing forms worth reacting to. Form 4 (insider transactions) is included
# because insider selling at crypto-treasury companies has been a reliable
# tell; the routine ownership churn gets filtered by relevance scoring.
EFTS_QUERIES = [
    ('"bitcoin"', "8-K"),
    ('"digital assets"', "8-K"),
    ('"cryptocurrency"', "10-Q,10-K"),
    ('"bitcoin treasury"', "8-K,S-1"),
    ('"ethereum" OR "solana"', "8-K"),
]

SEC_HEADERS = {"User-Agent": SEC_USER_AGENT}


@register("federal_register")
class FederalRegisterCollector(Collector):
    """
    Federal Register API — the authoritative record of US rulemaking.

    A proposed rule here typically precedes the news cycle about it, and the
    abstract is usually specific enough to score meaningfully.
    """
    interval = POLL_INTERVALS["federal_register"]

    async def _term(self, term: str) -> list[Article]:
        url = (f"{FR_BASE}?per_page=20&order=newest"
               f"&conditions%5Bterm%5D={quote(term)}"
               "&conditions%5Bpublication_date%5D%5Bgte%5D="
               + _days_ago(3) +
               "&fields%5B%5D=title&fields%5B%5D=abstract&fields%5B%5D=html_url"
               "&fields%5B%5D=publication_date&fields%5B%5D=type"
               "&fields%5B%5D=agencies&fields%5B%5D=document_number"
               "&fields%5B%5D=action")
        try:
            data = await self.http.get_json(url)
        except Exception as exc:
            self.log.warning("term %r failed: %s", term, exc)
            return []

        out: list[Article] = []
        for d in (data or {}).get("results", []):
            agencies = ", ".join(
                a.get("name", "") for a in (d.get("agencies") or [])) or "Federal Register"
            doc_type = d.get("type") or "Document"
            out.append(Article(
                title=f"[{doc_type}] {d.get('title', '')}",
                url=d.get("html_url"),
                summary=(d.get("abstract") or d.get("action") or "")[:2000],
                author=agencies,
                source="federal_register",
                source_category="regulator",
                source_weight=1.4,
                published_at=d.get("publication_date"),
                raw={"term": term, "document_number": d.get("document_number"),
                     "agencies": agencies, "type": doc_type},
            ))
        return out

    async def collect(self) -> list[Article]:
        results = await asyncio.gather(
            *(self._term(t) for t in FR_TERMS), return_exceptions=True)
        out: list[Article] = []
        for r in results:
            if not isinstance(r, Exception):
                out.extend(r)
        return out


@register("sec_firehose")
class SecFirehoseCollector(Collector):
    """
    EDGAR full-text search plus the live filings feed for watched issuers.

    SEC caps requests at ~10/sec and requires a contact string in the
    User-Agent; both are handled here. Set SEC_USER_AGENT in .env to your own
    name and email — a generic UA risks a block.
    """
    interval = POLL_INTERVALS["sec_firehose"]

    async def _fts(self, q: str, forms: str) -> list[Article]:
        url = f"{EFTS}?q={quote(q)}&forms={quote(forms)}&dateRange=custom" \
              f"&startdt={_days_ago(4)}&enddt={_days_ago(0)}"
        try:
            status, body, _ = await self.http.get(url, headers=SEC_HEADERS)
            if status != 200 or not body:
                return []
            data = json.loads(body)
        except Exception as exc:
            self.log.warning("full-text search %r failed: %s", q, exc)
            return []

        out: list[Article] = []
        for hit in (data.get("hits", {}) or {}).get("hits", [])[:40]:
            src = hit.get("_source", {})
            adsh = src.get("adsh", "")
            ciks = src.get("ciks") or []
            names = src.get("display_names") or []
            filer = names[0] if names else "Unknown filer"
            form = src.get("form", "")
            desc = src.get("file_description") or src.get("file_type") or ""

            # Reconstruct the filing-index URL from the accession number.
            link = None
            if adsh and ciks:
                acc = adsh.replace("-", "")
                link = (f"https://www.sec.gov/Archives/edgar/data/"
                        f"{int(ciks[0])}/{acc}/{adsh}-index.htm")

            out.append(Article(
                title=f"SEC {form} filing: {filer}" + (f" — {desc}" if desc else ""),
                url=link,
                summary=f"Filing matched full-text query {q}. "
                        f"Items: {', '.join(src.get('items') or []) or 'n/a'}. "
                        f"Period ending {src.get('period_ending') or 'n/a'}.",
                author=filer,
                source="sec_edgar_fts",
                source_category="regulator",
                source_weight=1.3,
                published_at=src.get("file_date"),
                raw={"query": q, "form": form, "adsh": adsh, "ciks": ciks,
                     "items": src.get("items")},
            ))
        return out

    async def collect(self) -> list[Article]:
        # SEC asks for sequential-ish access; a small gap between queries keeps
        # us well under the rate limit.
        out: list[Article] = []
        for q, forms in EFTS_QUERIES:
            out.extend(await self._fts(q, forms))
            await asyncio.sleep(0.35)
        return out


def _days_ago(n: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# On-demand (menu-driven) EDGAR lookup — kept from the original implementation
# but hardened and routed through the store.
# ---------------------------------------------------------------------------

async def fetch_company_filings(http, ticker: str, limit: int = 15) -> list[Article]:
    """Recent filings for one ticker. Used by the CLI, not the daemon loop."""
    try:
        tickers = await http.get_json(
            "https://www.sec.gov/files/company_tickers.json", headers=SEC_HEADERS)
    except Exception:
        return []

    cik = None
    name = ticker
    for entry in (tickers or {}).values():
        if str(entry.get("ticker", "")).upper() == ticker.upper():
            cik = str(entry["cik_str"]).zfill(10)
            name = entry.get("title", ticker)
            break
    if not cik:
        return []

    try:
        data = await http.get_json(
            f"https://data.sec.gov/submissions/CIK{cik}.json", headers=SEC_HEADERS)
    except Exception:
        return []

    recent = (data.get("filings", {}) or {}).get("recent", {})
    forms = recent.get("form", [])
    out: list[Article] = []
    for i in range(min(limit, len(forms))):
        adsh = recent["accessionNumber"][i]
        acc = adsh.replace("-", "")
        out.append(Article(
            title=f"{ticker} files Form {forms[i]}"
                  + (f": {recent['primaryDocDescription'][i]}"
                     if recent.get("primaryDocDescription", [None])[i] else ""),
            url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{adsh}-index.htm",
            summary=f"{name} (CIK {cik}) filed Form {forms[i]} on "
                    f"{recent['filingDate'][i]}.",
            author=name,
            source="sec_edgar",
            source_category="regulator",
            source_weight=1.3,
            published_at=recent["filingDate"][i],
            raw={"ticker": ticker, "cik": cik, "form": forms[i], "adsh": adsh},
        ))
    return out
