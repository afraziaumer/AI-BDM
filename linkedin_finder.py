"""
AI-BDM — LinkedIn company-page finder (enrichment step)
=======================================================

For each scraped business we already have its name, website and domain. This
module finds the business's LinkedIn **Company** page and adds it as a new
column.

Why not scrape LinkedIn directly? LinkedIn's ToS forbid it and the site is
heavily bot-blocked (login walls, aggressive detection). Instead we do what
commercial tools (Apollo, Clay, etc.) do to bootstrap a LinkedIn URL: run a
Google search — via the same Serper API the pipeline already uses — for the
company's LinkedIn page, then take the best-matching linkedin.com/company/ link.

Matching & confidence:
  - Query Google:  "<name>" <geo> site:linkedin.com/company
  - Take the first linkedin.com/company/<slug> organic result.
  - Confidence is "high" when the business's own domain appears in the result
    snippet (LinkedIn pages list their website) OR the name tokens strongly
    overlap the result title; otherwise "medium"; "none" when nothing matches.
  - Falls back to a personal profile (linkedin.com/in/) with "low" confidence
    when no Company page exists (common for very small practices).

Run:
  python linkedin_finder.py                       # enrich leads_clean.csv
  python linkedin_finder.py --in leads_clean.csv --out leads_with_linkedin.csv
  python linkedin_finder.py --geo "New York"      # bias the search by location
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

import phase1_pipeline as pp  # reuse Serper key/URL/timeout and _domain_key

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DEFAULT_IN = "leads_clean.csv"
DEFAULT_OUT = "leads_with_linkedin.csv"
CONCURRENCY = 4

_LI_COMPANY_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[^/?#\s\"']+", re.IGNORECASE
)
_LI_PROFILE_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^/?#\s\"']+", re.IGNORECASE
)
# Words to strip from a company name so "Sky Dental: Lower Manhattan's …"
# becomes just "Sky Dental" for a cleaner search.
_NAME_SPLIT_RE = re.compile(r"\s*[:|–—]\s*|\s+-\s+")
# Generic industry / location / suffix words that are NOT distinctive — a match
# on these means nothing (thousands of companies share them). A real match needs
# a token OUTSIDE this set (the actual brand).
_GENERIC = {
    "the", "and", "of", "in", "for", "your", "you", "llc", "inc", "ltd", "co",
    "corp", "group", "home", "best", "top", "premier", "modern", "trusted",
    "new", "york", "nyc", "city", "manhattan", "brooklyn", "queens", "bronx",
    "usa", "us", "america", "american", "national", "international",
    "dental", "dentist", "dentists", "dentistry", "clinic", "clinics", "care",
    "health", "healthcare", "medical", "center", "centre", "studio", "studios",
    "services", "service", "office", "offices", "practice", "associates",
    "family", "cosmetic", "general", "oral", "surgery", "smile", "smiles",
    "doctor", "doctors", "hospital", "wellness", "beauty", "salon", "spa",
    "solutions", "company", "official", "page", "welcome", "location", "locations",
}


def _clean_name(name: str) -> str:
    """Reduce a long scraped title to the core business name for searching."""
    first = _NAME_SPLIT_RE.split(name or "", maxsplit=1)[0].strip()
    return first or (name or "").strip()


def _brand_tokens(name: str) -> set:
    """Distinctive (non-generic, 3+ char) tokens — the actual brand words."""
    return {w for w in re.findall(r"[a-z0-9]+", name.lower())
            if len(w) >= 3 and w not in _GENERIC}


def _domain_label(website: str) -> str:
    """Brand stem from a domain: skydentalnyc.com -> 'skydentalnyc'."""
    dom = pp._domain_key(website) if website else ""
    return dom.split(".")[0] if dom else ""


def _normalize_li(url: str) -> str:
    """Canonicalise a LinkedIn URL to https://www.linkedin.com/<path> (no query)."""
    url = url.split("?")[0].rstrip("/")
    url = re.sub(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com",
                 "https://www.linkedin.com", url, flags=re.IGNORECASE)
    return url


async def _serper(session: aiohttp.ClientSession, query: str,
                  num: int = 6) -> List[Dict[str, Any]]:
    """One Serper (Google) search → list of organic results. [] on any failure."""
    if not pp.SERPER_API_KEY:
        return []
    headers = {"X-API-KEY": pp.SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": query, "num": num}
    try:
        async with session.post(
            pp.SERPER_SEARCH_URL, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=pp.SERPER_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception:  # noqa: BLE001 - network layer, degrade quietly
        return []
    return data.get("organic", []) or []


def _slug_of(url: str) -> str:
    """The company/profile handle from a LinkedIn URL, de-dashed & lowercased:
    linkedin.com/company/sky-dental-nyc -> 'skydentalnyc'."""
    m = re.search(r"linkedin\.com/(?:company|in)/([^/?#\s\"']+)", url, re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", m.group(1).lower()) if m else ""


def _label_match(label: str, deslug: str) -> bool:
    """True if a domain label and a LinkedIn slug clearly refer to the same
    brand (one contains the other, min 4 chars) — e.g. 'skydentalnyc' vs
    'skydental'."""
    if len(label) < 4 or len(deslug) < 4:
        return False
    return label in deslug or deslug in label


def _pick_company(results: List[Dict[str, Any]], name: str,
                  domain_label: str) -> Tuple[str, str]:
    """Choose the best LinkedIn URL, requiring a real brand/domain match.

    A `site:linkedin.com/company` search ALWAYS returns something, so we never
    accept a result just because it's first — it must share the business's
    distinctive brand token or domain stem, otherwise we report 'none' (better
    than linking the wrong company).
    """
    brand = _brand_tokens(name)
    best: Optional[Tuple[str, str]] = None  # (url, confidence)

    for regex in (_LI_COMPANY_RE, _LI_PROFILE_RE):
        for item in results:
            blob = " ".join([item.get("link", ""), item.get("title", ""),
                             item.get("snippet", "")])
            m = regex.search(blob)
            if not m:
                continue
            url = _normalize_li(m.group(0))
            deslug = _slug_of(url)
            title = (item.get("title", "") or "").lower()
            snippet = (item.get("snippet", "") or "").lower()
            hay_flat = re.sub(r"[^a-z0-9]", "", title + snippet)

            # Verified: the business's own domain stem is on the result or the
            # LinkedIn slug matches the domain stem.
            if domain_label and (_label_match(domain_label, deslug)
                                 or domain_label in hay_flat):
                return url, "high"
            # Brand match: a distinctive (non-generic) name token appears in the
            # slug or the result title.
            title_toks = _brand_tokens(title)
            if brand and (any(t in deslug for t in brand) or (brand & title_toks)):
                conf = "high" if regex is _LI_COMPANY_RE else "low"
                best = best or (url, conf)

    return best if best else ("", "none")


async def find_linkedin(session: aiohttp.ClientSession, name: str,
                        website: str = "", geo: str = "") -> Dict[str, str]:
    """Find a business's LinkedIn page. Returns {linkedin_url, confidence}."""
    clean = _clean_name(name)
    domain = pp._domain_key(website) if website else ""
    label = _domain_label(website)
    if not clean and not domain:
        return {"linkedin_url": "", "confidence": "none"}

    loc = f" {geo}" if geo else ""
    # Try by domain first (most precise), then by name.
    queries = []
    if domain:
        queries.append(f'"{domain}" site:linkedin.com')
    queries.append(f'"{clean}"{loc} site:linkedin.com/company')
    queries.append(f'{clean}{loc} linkedin')

    for q in queries:
        results = await _serper(session, q)
        url, conf = _pick_company(results, clean, label)
        if url:
            return {"linkedin_url": url, "confidence": conf}
    return {"linkedin_url": "", "confidence": "none"}


async def enrich(in_path: str, out_path: str, geo: str = "") -> Dict[str, int]:
    """Read a leads CSV, add linkedin_url + linkedin_confidence, write out_path."""
    with open(in_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"[linkedin] no rows in {in_path}")
        return {}

    fieldnames = list(rows[0].keys())
    for col in ("linkedin_url", "linkedin_confidence"):
        if col not in fieldnames:
            fieldnames.append(col)

    sem = asyncio.Semaphore(CONCURRENCY)
    found = 0

    async with aiohttp.ClientSession() as session:
        async def _one(row: Dict[str, str]) -> None:
            nonlocal found
            async with sem:
                res = await find_linkedin(
                    session,
                    row.get("company_name", ""),
                    row.get("website_url", "") or row.get("domain", ""),
                    geo,
                )
            row["linkedin_url"] = res["linkedin_url"] or "N/A"
            row["linkedin_confidence"] = res["confidence"]
            tag = res["confidence"]
            if res["linkedin_url"]:
                found += 1
            print(f"  [{tag:>6}] {row.get('company_name','')[:45]:45} -> "
                  f"{res['linkedin_url'] or '—'}")

        await asyncio.gather(*(_one(r) for r in rows))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[linkedin] {found}/{len(rows)} businesses matched -> {out_path}")
    return {"total": len(rows), "found": found}


def main() -> None:
    parser = argparse.ArgumentParser(description="Find each lead's LinkedIn page.")
    parser.add_argument("--in", dest="in_path", default=DEFAULT_IN,
                        help="Input leads CSV (default: leads_clean.csv).")
    parser.add_argument("--out", dest="out_path", default=DEFAULT_OUT,
                        help="Output CSV (default: leads_with_linkedin.csv).")
    parser.add_argument("--geo", default="",
                        help="Optional location to bias the search (e.g. 'New York').")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    asyncio.run(enrich(args.in_path, args.out_path, args.geo))


if __name__ == "__main__":
    main()
