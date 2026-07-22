"""Decision-maker source #4 (last resort): public search, when nothing was
found on the website itself (source #1), in its structured data (#2), or
its contact pages (#3).

Uses `site:company.com CEO` / `site:company.com Leadership` style Serper
queries — restricted to the business's OWN domain, so results are still
"what this business's own public pages say," just found via a different
route than the crawl (e.g. a page the crawler didn't reach, or one Google
indexed a cached snippet of). Never queries a third-party platform here —
that's linkedin_discovery.py's job.

Lower confidence than on-site extraction: a search snippet is a fragment of
a page, with less surrounding context to disambiguate a name/title pair, so
this is used strictly as a fallback (see phase1_pipeline.py's orchestration
— only called when sources #1-3 found nobody).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import aiohttp

from decision_maker_extractor import (
    _INLINE_PAIR_RE,
    _GENERIC_TITLE_RE,
    _TITLE_RE,
    _build_person,
    _looks_like_person_name,
)

logger = logging.getLogger("ai_bdm.public_search_decision_makers")

_SEARCH_TERMS = ("CEO", "Founder", "Leadership", "Team", "Management", "Executive")
CONFIDENCE = 0.55  # below every on-site source's confidence — a search
                   # snippet has the least context of any source here.


async def _serper_search(session: aiohttp.ClientSession, query: str) -> List[Dict[str, Any]]:
    # Lazy import: see linkedin_discovery.py's identical note — avoids a
    # circular import with phase1_pipeline, which imports this module.
    from phase1_pipeline import SERPER_API_KEY, SERPER_SEARCH_URL, SERPER_TIMEOUT_S

    if not SERPER_API_KEY:
        return []
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    try:
        async with session.post(
            SERPER_SEARCH_URL, json={"q": query, "num": 10}, headers=headers,
            timeout=aiohttp.ClientTimeout(total=SERPER_TIMEOUT_S),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("organic", []) or []
            logger.warning("Serper search failed (status=%s) for %r", resp.status, query)
            return []
    except Exception as exc:  # noqa: BLE001 - network layer, degrade quietly
        logger.warning("Serper search error for %r: %s", query, exc)
        return []


# A real job title is never a run-on sentence. Verified via real testing: a
# customer-story snippet ("Sam Altman, OpenAI cofounder and CEO, speaks with
# John Collison, Stripe cofounder and president, about the AI industry...")
# matches the "Name, Title" shape but the "title" half runs on past the
# SECOND comma into an entirely different clause — snippets are flowing
# prose, not a team page's clean "Name, Title" card layout, so the same
# regex needs a tighter stop condition here specifically.
_MAX_SNIPPET_TITLE_WORDS = 6


def _extract_from_snippet(snippet: str, source_url: str) -> List[Dict[str, Any]]:
    """Same name+title heuristic as decision_maker_extractor.py, applied to
    a search snippet's text instead of a crawled page's cleaned lines —
    snippets are typically 1-2 sentences, so this scans it as one blob of
    candidate lines (split on sentence-ish punctuation) rather than newlines."""
    people: List[Dict[str, Any]] = []
    for chunk in snippet.replace(";", ".").split("."):
        chunk = chunk.strip()
        if not chunk:
            continue
        inline = _INLINE_PAIR_RE.match(chunk)
        if not inline:
            continue
        # Take only up to the first internal comma — a real title never has
        # one; anything after it is prose continuing into a new clause.
        title = inline.group(2).split(",")[0].strip()
        if not (_TITLE_RE.search(title) or _GENERIC_TITLE_RE.search(title)):
            continue
        if len(title.split()) > _MAX_SNIPPET_TITLE_WORDS:
            continue
        name = inline.group(1).strip()
        if _looks_like_person_name(name):
            people.append(_build_person(name, title, source_url, CONFIDENCE, []))
    return people


async def discover_via_public_search(
    session: aiohttp.ClientSession, domain: str,
) -> List[Dict[str, Any]]:
    """Try each `site:domain <term>` query; return every name+title pair
    found in the result snippets, deduped by name. Best-effort — an empty
    list here just means "still nobody found," not an error."""
    people: List[Dict[str, Any]] = []
    seen_names: set = set()
    for term in _SEARCH_TERMS:
        results = await _serper_search(session, f"site:{domain} {term}")
        for item in results:
            link = item.get("link", "")
            snippet = item.get("snippet", "") or ""
            title = item.get("title", "") or ""
            for source_text in (title, snippet):
                for person in _extract_from_snippet(source_text, link or f"https://{domain}"):
                    key = person["name"].lower()
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    person["source"] = "search"
                    people.append(person)
        if people:
            break  # stop at the first search term that actually surfaced someone
    return people
