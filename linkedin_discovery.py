"""Step 2 of social/LinkedIn intelligence — when social_discovery.py found
no LinkedIn link on the business's own site, try to find its official
LinkedIn Company page.

IMPORTANT — this module NEVER fetches linkedin.com. It only:
  1. Generates candidate URL slugs from the company name/domain (cheap,
     local, no network) — these are CANDIDATES to check, never accepted on
     their own.
  2. Verifies candidates against Serper's search index (the same provider
     and constants already used for business discovery elsewhere in this
     project — see phase1_pipeline.SERPER_API_KEY/SERPER_SEARCH_URL) —
     i.e. what Google's own crawler has already indexed about that page,
     not a live scrape of LinkedIn itself.

A candidate is only accepted if Serper's index actually surfaces a
linkedin.com/company/... result whose title fuzzy-matches the business
name above a confidence floor. No match above that floor -> the result is
left null. "Never guess" is enforced structurally: nothing this module
returns is based on the guessed slug alone, only on independently-indexed
confirmation of it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger("ai_bdm.linkedin_discovery")

# Same "retry transient, don't retry deterministic" policy already
# established for LLM/scrape calls elsewhere in this project (see
# LLM_planner.call_llm, phase1_pipeline._scrapedo_fetch).
_MAX_ATTEMPTS = 2
_BACKOFF_BASE_S = 1.5

MIN_CONFIDENCE = 0.72  # below this, the caller stores null — never a low-confidence guess.

_WORD_RE = re.compile(r"[a-z0-9]+")
_GENERIC_SUFFIXES = ("inc", "llc", "ltd", "co", "corp", "company", "group", "the")


def _slugify(text: str) -> str:
    words = _WORD_RE.findall(text.lower())
    words = [w for w in words if w not in _GENERIC_SUFFIXES] or words
    return "-".join(words)


def generate_candidate_slugs(company_name: str, domain: str) -> List[str]:
    """Local, no-network candidate slugs — see module docstring: these are
    checked against Serper's index, never trusted on their own."""
    bare_domain = domain.split(".")[0]
    name_slug = _slugify(company_name) if company_name else ""
    candidates = []
    for s in (
        bare_domain,
        name_slug,
        f"{name_slug}-group" if name_slug else "",
        f"{bare_domain}-group",
        f"the-{name_slug}" if name_slug else "",
    ):
        if s and s not in candidates:
            candidates.append(s)
    return candidates


def _normalize_for_match(text: str) -> str:
    words = _WORD_RE.findall(text.lower())
    return " ".join(w for w in words if w not in _GENERIC_SUFFIXES)


def _title_confidence(company_name: str, result_title: str) -> float:
    """0..1 fuzzy match between the business name and a search result's
    title — the ONLY signal that turns a guessed slug (or a plain search)
    into an accepted match. Generic legal suffixes (Inc/LLC/Group...)
    stripped from both sides so they don't inflate or deflate the score.

    Deliberately the MINIMUM of two different signals, not either alone —
    verified empirically to matter: "Redondo Beach Marina" vs. "Redondo
    Beach Sportfishing" (a real, different business at the same location)
    scores 0.74 on character-level SequenceMatcher alone, purely from
    sharing the "Redondo Beach " prefix — above a naive 0.72 floor despite
    being the wrong business. Token-set overlap catches this: {redondo,
    beach, marina} vs {redondo, beach, sportfishing} = 0.5 Jaccard, since it
    penalizes the ONE WORD THAT ACTUALLY DISTINGUISHES the two businesses
    disagreeing, where character-diffing barely notices. Requiring both
    measures to agree is the same fix in spirit as the LinkedIn/Marina-del-
    Rey place-name-conflation prompt fix elsewhere in this project: a shared
    LOCATION or generic prefix must never be enough on its own."""
    a, b = _normalize_for_match(company_name), _normalize_for_match(result_title)
    if not a or not b:
        return 0.0
    char_ratio = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = set(a.split()), set(b.split())
    token_jaccard = (
        len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if (tokens_a | tokens_b) else 0.0
    )
    return min(char_ratio, token_jaccard)


@dataclass
class LinkedInMatch:
    url: str
    title: str
    snippet: str
    confidence: float


async def _serper_search(session: aiohttp.ClientSession, query: str) -> List[Dict[str, Any]]:
    """One raw Serper query -> its 'organic' results. Reuses this project's
    existing Serper constants/session convention (see
    phase1_pipeline.discover_targets) but is deliberately a plain, focused
    helper — LinkedIn/social discovery doesn't need discover_targets'
    business-discovery-specific pagination/dedup/directory-mining logic."""
    # Lazy import: phase1_pipeline imports THIS module at load time (to wire
    # it into the crawl pipeline), so a top-level `from phase1_pipeline
    # import ...` here would be a circular import that fails before either
    # module finishes loading. By the time this function actually RUNS,
    # phase1_pipeline is fully initialized — same lazy-import pattern
    # route_planner.py already uses for phase1_pipeline._domain_key.
    from phase1_pipeline import SERPER_API_KEY, SERPER_SEARCH_URL, SERPER_TIMEOUT_S

    if not SERPER_API_KEY:
        logger.error("Serper key missing — cannot verify LinkedIn candidates.")
        return []
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with session.post(
                SERPER_SEARCH_URL, json={"q": query, "num": 10}, headers=headers,
                timeout=aiohttp.ClientTimeout(total=SERPER_TIMEOUT_S),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("organic", []) or []
                if resp.status in (429, 500, 502, 503, 504) and attempt < _MAX_ATTEMPTS - 1:
                    import asyncio
                    await asyncio.sleep(_BACKOFF_BASE_S * (attempt + 1))
                    continue
                logger.warning("Serper search failed (status=%s) for %r", resp.status, query)
                return []
        except Exception as exc:  # noqa: BLE001 - network layer, degrade quietly
            logger.warning("Serper search error for %r: %s", query, exc)
            return []
    return []


def _best_company_match(company_name: str, results: List[Dict[str, Any]]) -> Optional[LinkedInMatch]:
    best: Optional[LinkedInMatch] = None
    for item in results:
        link = item.get("link", "")
        path = urlparse(link).path.lower()
        if "linkedin.com" not in link or not path.startswith("/company/"):
            continue
        title = item.get("title", "") or ""
        confidence = _title_confidence(company_name, title)
        if best is None or confidence > best.confidence:
            best = LinkedInMatch(
                url=link, title=title, snippet=item.get("snippet", "") or "",
                confidence=confidence,
            )
    return best


async def discover_linkedin_company(
    session: aiohttp.ClientSession, company_name: str, domain: str,
) -> Optional[LinkedInMatch]:
    """Try, in order: a direct name search, then each generated slug as a
    targeted site: search. Returns the highest-confidence match found across
    all of them, or None if nothing clears MIN_CONFIDENCE — never the
    best-of-a-bad-lot guess."""
    queries = [f'site:linkedin.com/company "{company_name}"'] if company_name else []
    queries += [
        f"site:linkedin.com/company {slug}"
        for slug in generate_candidate_slugs(company_name, domain)
    ]
    queries.append(f"site:linkedin.com {domain}")

    best: Optional[LinkedInMatch] = None
    for query in queries:
        results = await _serper_search(session, query)
        match = _best_company_match(company_name, results)
        if match and (best is None or match.confidence > best.confidence):
            best = match
        if best and best.confidence >= 0.95:
            break  # already about as confident as this method gets

    if best is None or best.confidence < MIN_CONFIDENCE:
        if best is not None:
            logger.info(
                "LinkedIn candidate for %s below confidence floor (%.2f < %.2f): %s",
                domain, best.confidence, MIN_CONFIDENCE, best.url,
            )
        return None
    return best
