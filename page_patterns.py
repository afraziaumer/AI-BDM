"""Deterministic, non-LLM homepage link utilities: extract a homepage's own
internal links (with DOM location + anchor/title text) and compress
repeating URL structures (e.g. "/boats/*" representing 250 near-identical
listing pages) into single pattern summaries.

Relocated verbatim from the now-deleted crawl_planner.py (the LLM Crawl
Planner was removed as part of the token-usage refactor — see
phase1_pipeline.py's crawl-queue-building code, which now simply queues every
noise-filtered homepage link instead of having an LLM pick a subset).
website_classifier.py still depends on these two pure functions for its own
deterministic "how many repeating listing patterns does this homepage have"
signal (extract_homepage_signals) — nothing here calls an LLM.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

PATTERN_MIN_CHILDREN = 4         # a path prefix with >= this many children -> one pattern


# ===========================================================================
# Homepage candidate extraction (reuses the crawler's own filtering)
# ===========================================================================
def _link_location(a_tag: Any) -> str:
    """Best-effort DOM location for one anchor: nav > header > footer > main."""
    for parent in a_tag.parents:
        name = getattr(parent, "name", None)
        if name == "nav":
            return "nav"
        if name == "header":
            return "header"
        if name == "footer":
            return "footer"
        if name in ("main", "article"):
            return "main"
    return "body"


def extract_homepage_candidates(
    html: str, base_url: str, root_domain: str
) -> List[Dict[str, Any]]:
    """Homepage links enriched with the anchor's title attribute + DOM location.

    Reuses phase1_pipeline._extract_internal_link_pairs for the actual
    filtering (domain/scheme/asset/query/noise rules) so that logic lives in
    exactly ONE place — this just adds two extra signals on top, from the same
    already-parsed soup.
    """
    from phase1_pipeline import _extract_internal_link_pairs  # local: avoid a
    # module-level circular import (phase1_pipeline imports this module too).

    soup = BeautifulSoup(html, "html.parser")
    pairs = _extract_internal_link_pairs(soup, base_url, root_domain)
    accepted = {p["url"] for p in pairs}

    location_by_url: Dict[str, str] = {}
    title_by_url: Dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if href in accepted and href not in location_by_url:
            location_by_url[href] = _link_location(a)
            title_by_url[href] = (a.get("title") or "").strip()[:120]

    for p in pairs:
        p["location"] = location_by_url.get(p["url"], "body")
        p["title"] = title_by_url.get(p["url"], "")
    return pairs


# ===========================================================================
# Pattern detection (compress repeating URL structures)
# ===========================================================================
@dataclass
class UrlPattern:
    pattern: str
    count: int
    example_urls: List[str]
    anchor_examples: List[str]
    location: str = "body"


def detect_patterns(
    candidates: Sequence[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[UrlPattern]]:
    """Group candidates sharing a path prefix into pattern summaries.

    A path prefix (e.g. "/boats") with >= PATTERN_MIN_CHILDREN distinct child
    pages (boat-001, boat-002, ...) becomes ONE UrlPattern instead of N
    candidate rows. Top-level single-segment pages (/about, /contact) are
    never grouped — there's no shared parent to group them under.

    Tries prefix depths from MOST specific (all-but-last-segment, the
    original single-level behavior) down to LEAST specific (just the first
    segment), stopping at the first depth where a group of un-grouped
    candidates reaches PATTERN_MIN_CHILDREN. Real gap found live: a listing
    page whose URL varies in the last TWO segments together (e.g. an OTA's
    "/hotel-deals/en-us/<region-id>/hotels-in-<city>.ssp", where BOTH the
    id and the slug differ per link) got a unique "parent" per URL under
    the single-level version -- 10 obviously-repeating links, zero
    detected patterns, directory-likeness score far too low to flag it.
    Falling back to shallower prefixes (here, "/hotel-deals/en-us", 2
    segments) catches this without weakening the original single-level
    case, which is always tried first since it's the deepest/most specific.
    """
    by_depth: Dict[int, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    max_depth = 0
    for c in candidates:
        segments = [s for s in urlsplit(c["url"]).path.rstrip("/").split("/") if s]
        c["_segments"] = segments
        for depth in range(1, len(segments)):  # depth = how many leading segments form the prefix
            prefix = "/" + "/".join(segments[:depth])
            by_depth[depth][prefix].append(c)
            max_depth = max(max_depth, depth)

    remaining = {id(c): c for c in candidates}
    patterns: List[UrlPattern] = []
    for depth in range(max_depth, 0, -1):  # most specific first
        for prefix, items in by_depth[depth].items():
            group = [c for c in items if id(c) in remaining]
            if len(group) >= PATTERN_MIN_CHILDREN:
                anchors = [i["anchor"] for i in group[:3] if i.get("anchor")]
                patterns.append(UrlPattern(
                    pattern=f"{prefix}/*", count=len(group),
                    example_urls=[i["url"] for i in group[:3]],
                    anchor_examples=anchors,
                    location=group[0].get("location", "body"),
                ))
                for c in group:
                    remaining.pop(id(c), None)

    individual = list(remaining.values())
    for c in candidates:
        c.pop("_segments", None)
    return individual, patterns
