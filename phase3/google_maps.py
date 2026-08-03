"""Phase 3 — Google Maps review signal (first platform of the review-mining step).

For each already-committed business we have its name, domain and physical
address (from Phase 1). This module finds that business's Google Maps
listing and pulls its rating, review count, category and phone number — the
"cheap tier" from the project plan (full review TEXT is a later addition, not
part of this first pass).

Why Serper's /places endpoint instead of scraping maps.google.com directly?
Same reasoning as linkedin_finder.py's approach to LinkedIn: it's the same
Serper API the rest of the pipeline already pays for and trusts, it returns
clean structured JSON (no JS-heavy page to scrape), and it never touches
Google Maps' own page directly.

Identity safety: a Places search can return the wrong business (chains,
generic names). Before trusting a result we require its address to share at
least one real token with the business's own already-scraped address — no
match, no data, never a guess (see _looks_like_match).

Run:
  python -m phase3.google_maps --domain skydentalnyc.com --name "Sky Dental" \
      --address "123 Main St, New York" --geo "New York"
  python -m phase3.google_maps --in leads_clean.csv --out leads_with_maps.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re
import sys
from typing import Any, Dict, List, Optional

import aiohttp

from phase3 import store
from phase3.config import (
    DEFAULT_CONCURRENCY,
    SERPER_API_KEY,
    SERPER_PLACES_URL,
    SERPER_TIMEOUT_S,
)

logger = logging.getLogger("Phase3GoogleMaps")

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DEFAULT_IN = "leads_clean.csv"
DEFAULT_OUT = "leads_with_maps.csv"
PLATFORM = "google_maps"


# Generic address/geo words that prove nothing on their own — two different
# businesses in the same city both mention the city name, so a match has to
# come from something more specific (street, plaza, area name), the same
# principle as linkedin_finder.py's _GENERIC brand-token filter.
_GENERIC_ADDRESS = {
    "street", "st", "road", "rd", "avenue", "ave", "block", "sector", "plaza",
    "markaz", "town", "city", "area", "district", "phase", "society",
    "pakistan", "islamabad", "lahore", "karachi", "rawalpindi", "peshawar",
    "quetta", "faisalabad", "multan", "dubai", "sharjah", "uae", "usa",
    "united", "states", "new", "york",
}


def _tokens(text: str) -> set:
    """Lowercase alnum tokens, 3+ chars, with generic address/geo words
    stripped out — matching on a shared city name alone is a false positive,
    not real evidence two addresses are the same place."""
    return {
        w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) >= 3 and w not in _GENERIC_ADDRESS
    }


def _looks_like_match(candidate_address: str, known_address: str) -> bool:
    """True if the two addresses share at least one DISTINCTIVE token (a
    street/plaza/area name, a plot number) — not just the same city. If we
    don't know the business's own address, we can't verify anything — treat
    that as NOT a confident match rather than guessing."""
    if not known_address:
        return False
    known = _tokens(known_address)
    got = _tokens(candidate_address)
    return bool(known & got)


def _phone_matches(candidate_phone: str, known_phones: str) -> bool:
    """True if the Places result's phone shares its last 9 digits (the
    national significant number, tolerant of country-code/leading-zero/
    formatting differences between sources) with ANY of the business's own
    already-scraped phone number(s) — `known_phones` may be "|"-joined
    ("+92... | +92...", the same separator phase1_pipeline.CONTACT_SEP uses;
    duplicated here as a literal rather than imported, to avoid a circular
    import with phase1_pipeline — see this module's own docstring) since a
    site can expose more than one.

    A phone number is just as strong an identity signal as a street address
    (arguably stronger — effectively unique per business) so this is a fully
    independent, equally-trusted verification path, used when no address is
    on file rather than as a weaker fallback. Same "never guess" rule as
    _looks_like_match: no known phone means no verification is possible, so
    this returns False, never a guess.
    """
    if not known_phones or not candidate_phone:
        return False
    candidate_digits = re.sub(r"\D", "", candidate_phone)
    if len(candidate_digits) < 9:
        return False
    candidate_tail = candidate_digits[-9:]
    for known in known_phones.split("|"):
        known_digits = re.sub(r"\D", "", known)
        if len(known_digits) >= 9 and known_digits[-9:] == candidate_tail:
            return True
    return False


def _name_only_match(candidate_title: str, known_name: str) -> bool:
    """Last-resort, LOWER-confidence check used only when Serper's Places API
    returned exactly one candidate and gave us neither an address nor a phone
    number to verify it against (a real, common gap — not every listing has
    those fields populated). True if EVERY significant word of the business's
    own name appears in the candidate's title — not just an overlap, which
    would be too weak alone. Requiring full containment still tolerates the
    candidate title having extra words appended (a city/area name, a tagline)
    without accepting a look-alike that's missing part of our own name.

    Deliberately NOT applied when there's more than one candidate — with
    several results returned, an unverified name-only pick has real odds of
    choosing the wrong location of a similarly-named chain; there, no
    address/phone match still means no match, exactly as before.
    """
    known = _tokens(known_name)
    if not known:
        return False
    got = _tokens(candidate_title)
    return known.issubset(got)


async def _serper_places(session: aiohttp.ClientSession, query: str,
                          num: int = 5) -> Optional[List[Dict[str, Any]]]:
    """One Serper Places search -> list of place results.

    Returns `[]` only when the request genuinely SUCCEEDED with zero places
    (a real "no listing" signal, safe to cache forever). Returns `None` on
    any failure (network error, timeout, non-200) -- distinct from `[]` so
    callers can tell "this business has no Google listing" apart from "the
    request itself failed," and skip caching the latter. Caching a transient
    failure as a permanent miss (observed live: a business with a confirmed
    Google Maps match minutes earlier got silently locked in as "no Serper
    results" after one bad network moment) is worse than just retrying next
    run -- this never raises either way, matching the fail-safe rule for
    every Phase 3 source.
    """
    if not SERPER_API_KEY:
        return []
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": query, "num": num}
    try:
        async with session.post(
            SERPER_PLACES_URL, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=SERPER_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                logger.warning("Serper Places request failed (status=%s) for %r", resp.status, query)
                return None
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("Serper Places request error for %r: %s", query, exc)
        return None
    return data.get("places", []) or []


def _extract_fields(place: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the fields we care about out of one Serper place result,
    defensively — Serper's field names have shifted before, so every read
    falls back to an alternate key rather than raising."""
    rating_count = (
        place.get("ratingCount")
        or place.get("reviewsCount")
        or place.get("reviews")
        or 0
    )
    return {
        "title": place.get("title", ""),
        "address": place.get("address", ""),
        "rating": place.get("rating"),
        "rating_count": rating_count,
        "category": place.get("category", ""),
        "phone": place.get("phoneNumber", ""),
        "cid": place.get("cid", ""),
    }


async def find_place(session: aiohttp.ClientSession, name: str,
                      address: str = "", geo: str = "",
                      phone: str = "") -> Dict[str, Any]:
    """Find a business's Google Maps listing. Returns a record with
    `matched: bool` — callers must check this before trusting any field,
    exactly like linkedin_finder's confidence tiers.

    Verified by address OR phone number (either independently sufficient —
    see _looks_like_match / _phone_matches), with a third, deliberately
    LOWER-confidence fallback — exact name containment against a SINGLE
    returned candidate (see _name_only_match) — for when Serper genuinely has
    neither an address nor a phone on file for the listing, which happens for
    real, legitimate businesses. `match_basis` on a matched result records
    which one actually confirmed it ("address" / "phone" / "name_only"), and
    `confidence` is "low" only for the name_only path, so callers can treat
    that tier differently if needed instead of it being indistinguishable
    from a fully-verified match.
    """
    if not name:
        return {"matched": False, "reason": "no business name given"}

    query = f"{name} {geo}".strip()
    results = await _serper_places(session, query)
    if results is None:
        # The request itself failed (network/timeout/non-200) -- distinct
        # from a real "Serper successfully found nothing," which is safe to
        # trust. Flagged so enrich_domain() knows not to cache this forever.
        return {"matched": False, "reason": "Serper Places request failed", "transient_failure": True}
    if not results:
        return {"matched": False, "reason": "no Serper results"}

    for place in results:
        fields = _extract_fields(place)
        if _looks_like_match(fields["address"], address):
            fields["matched"] = True
            fields["match_basis"] = "address"
            fields["query"] = query
            return fields
        if _phone_matches(fields["phone"], phone):
            fields["matched"] = True
            fields["match_basis"] = "phone"
            fields["query"] = query
            return fields

    if len(results) == 1:
        fields = _extract_fields(results[0])
        if _name_only_match(fields["title"], name):
            fields["matched"] = True
            fields["match_basis"] = "name_only"
            fields["confidence"] = "low"
            fields["query"] = query
            return fields

    # Nothing matched confidently -> report the top candidate for visibility
    # but do NOT mark it matched (no address/phone/exact-name-alone match).
    top = _extract_fields(results[0])
    top["matched"] = False
    top["reason"] = "no address/phone/exact-name match with known business details"
    top["query"] = query
    return top


async def enrich_domain(session: aiohttp.ClientSession, domain: str, name: str,
                         address: str = "", geo: str = "", phone: str = "",
                         max_age_days: Optional[float] = None) -> Dict[str, Any]:
    """Cache-first Google Maps lookup for one already-committed domain.

    Follows the plan's universal pattern: cache check first, external call
    only on a miss, result saved back to cache either way isn't needed for a
    miss (nothing to save), and any failure returns matched=False rather than
    raising — the caller decides what "field left blank" means downstream.
    """
    cached = store.load(domain, PLATFORM, max_age_days=max_age_days)
    if cached is not None:
        return cached

    result = await find_place(session, name, address, geo, phone)
    # Cache both matches and definite misses forever -- but NOT a transient
    # request failure (see find_place/_serper_places): caching that would
    # permanently lock in "no match" for a business that may well have one,
    # just because one lookup hit a bad network moment. Leaving it uncached
    # means the next run simply retries instead of trusting a false miss
    # forever. Repeating an unchanged genuine no-result lookup spends Serper
    # credits without adding information; users can delete this platform's
    # JSON file when they intentionally want a fresh lookup.
    if not result.get("transient_failure"):
        store.save(domain, PLATFORM, result)
    return result


async def enrich(in_path: str, out_path: str, geo: str = "") -> Dict[str, int]:
    """Read a leads CSV, add Google Maps rating/review-count columns, write
    out_path. Mirrors linkedin_finder.enrich()'s batch shape exactly."""
    with open(in_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"[maps] no rows in {in_path}")
        return {}

    fieldnames = list(rows[0].keys())
    for col in ("maps_rating", "maps_rating_count", "maps_category", "maps_matched"):
        if col not in fieldnames:
            fieldnames.append(col)

    sem = asyncio.Semaphore(DEFAULT_CONCURRENCY)
    matched = 0

    async with aiohttp.ClientSession() as session:
        async def _one(row: Dict[str, str]) -> None:
            nonlocal matched
            domain = row.get("domain", "") or row.get("website_url", "")
            async with sem:
                res = await enrich_domain(
                    session, domain,
                    row.get("company_name", ""),
                    row.get("physical_address", ""),
                    geo,
                    row.get("phone_number", "") or row.get("phone", ""),
                )
            row["maps_matched"] = "yes" if res.get("matched") else "no"
            row["maps_rating"] = res.get("rating", "") or ""
            row["maps_rating_count"] = res.get("rating_count", "") or ""
            row["maps_category"] = res.get("category", "") or ""
            if res.get("matched"):
                matched += 1
            tag = "match " if res.get("matched") else "no-match"
            print(f"  [{tag}] {row.get('company_name','')[:45]:45} -> "
                  f"rating={res.get('rating', '—')} "
                  f"count={res.get('rating_count', '—')}")

        await asyncio.gather(*(_one(r) for r in rows))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[maps] {matched}/{len(rows)} businesses matched -> {out_path}")
    return {"total": len(rows), "matched": matched}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find each lead's Google Maps rating/review-count, or test one business."
    )
    parser.add_argument("--in", dest="in_path", default=None,
                        help=f"Input leads CSV for batch mode (default: {DEFAULT_IN}).")
    parser.add_argument("--out", dest="out_path", default=DEFAULT_OUT,
                        help=f"Output CSV for batch mode (default: {DEFAULT_OUT}).")
    parser.add_argument("--geo", default="", help="Location to bias the search, e.g. 'Islamabad'.")
    parser.add_argument("--domain", default=None, help="Single-business test mode: the domain key.")
    parser.add_argument("--name", default=None, help="Single-business test mode: business name.")
    parser.add_argument("--address", default="", help="Single-business test mode: known address.")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.domain and args.name:
        async def _single() -> None:
            async with aiohttp.ClientSession() as session:
                res = await enrich_domain(session, args.domain, args.name, args.address, args.geo)
                print(res)
        asyncio.run(_single())
        return

    asyncio.run(enrich(args.in_path or DEFAULT_IN, args.out_path, args.geo))


if __name__ == "__main__":
    main()
