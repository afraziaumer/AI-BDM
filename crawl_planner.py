"""
LLM Crawl Planner — decide WHICH pages of a site are worth crawling, before
the crawler fetches anything past the homepage.

The crawler used to queue nearly every internal link it found (bounded only by
MAX_PAGES_PER_SITE=40) — wasteful when a site has hundreds of near-identical
product/blog/listing pages. This module runs once per site, right after the
homepage is fetched, and turns "crawl everything" into "crawl only the pages
likely to matter for THIS query, within a small budget":

    homepage links (already filtered by phase1_pipeline's noise rules)
        -> detect_patterns          repeating URL structures (/boats/*, /blog/*...)
                                     compressed into ONE summary each, not N URLs
        -> build_navigation_summary  a small JSON: explicit pages + pattern summaries
        -> plan_crawl_with_llm       query-aware: LLM scores every candidate/pattern
                                     0-100 and recommends a page budget
        -> (LLM unavailable/fails)  deterministic_fallback_plan — a fixed scoring
                                     table (Home/About/Services high, Blog/News
                                     low, legal/utility zero) — the crawl NEVER
                                     aborts just because the LLM is down
        -> select_crawl_urls         top-priority individual URLs, budget-capped

Independent of the crawler by design: phase1_pipeline.py calls exactly one
function (plan_site_crawl) and gets back a list of URLs to queue. Everything
downstream (HTML cleaning, Wappalyzer, storage) is unaffected — it just
receives a shorter queue.

Caching (Step 7): the plan is staged/committed alongside a site's pages (see
storage.stage_crawl_plan), so a future crawl of the SAME domain reuses it
without another LLM call — unless the homepage's link structure changed
(structure_signature) or the query's intent changed meaningfully
(intent_signature), or the cached plan is older than CACHE_TTL_DAYS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

import discovery_classifier as dc
from LLM_planner import call_llm, get_client
from storage import get_store

logger = logging.getLogger("ai_bdm.crawl_planner")

# --- Budget / bounds --------------------------------------------------------
MIN_CRAWL_BUDGET = 3
MAX_CRAWL_BUDGET = 15            # deliberately well under the old ~40-page norm
DEFAULT_CRAWL_BUDGET = 6
MIN_CRAWL_PRIORITY = 30          # a page must clear this bar to be worth fetching —
                                  # "priority > 0" let borderline/irrelevant pages
                                  # (a model admitting "not relevant" but scoring 5-15
                                  # instead of 0) through; this enforces the model's
                                  # own low-confidence signal instead of ignoring it
MAX_LLM_CANDIDATES = 40          # cap on individual pages + patterns shown to the model
PATTERN_MIN_CHILDREN = 4         # a path prefix with >= this many children -> one pattern
CACHE_TTL_DAYS = 30               # re-plan a site at least this often regardless

PLANNER_MODEL = "openai/gpt-oss-20b"  # documented; call_llm handles primary/fallback


# ===========================================================================
# Stage 1 — homepage candidate extraction (reuses the crawler's own filtering)
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

    soup = BeautifulSoup(html, "lxml")
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
# Stage 2 — pattern detection (compress repeating URL structures)
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
    """
    by_parent: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        segments = [s for s in urlsplit(c["url"]).path.rstrip("/").split("/") if s]
        if len(segments) < 2:
            by_parent[""].append(c)     # top-level page, never pattern-grouped
        else:
            parent = "/" + "/".join(segments[:-1])
            by_parent[parent].append(c)

    individual: List[Dict[str, Any]] = []
    patterns: List[UrlPattern] = []
    for parent, items in by_parent.items():
        if parent and len(items) >= PATTERN_MIN_CHILDREN:
            anchors = [i["anchor"] for i in items[:3] if i.get("anchor")]
            patterns.append(UrlPattern(
                pattern=f"{parent}/*", count=len(items),
                example_urls=[i["url"] for i in items[:3]],
                anchor_examples=anchors,
                location=items[0].get("location", "body"),
            ))
        else:
            individual.extend(items)
    return individual, patterns


# ===========================================================================
# Stage 3 — build the lightweight navigation summary
# ===========================================================================
def build_navigation_summary(
    homepage_url: str, individual: Sequence[Dict[str, Any]], patterns: Sequence[UrlPattern]
) -> Dict[str, Any]:
    """A small JSON describing the site's structure — explicit pages plus
    pattern summaries — small enough for cheap, fast LLM processing."""
    pages: List[Dict[str, Any]] = []
    for c in individual:
        entry: Dict[str, Any] = {"url": c["url"], "anchor": c.get("anchor", "")}
        if c.get("title"):
            entry["title"] = c["title"]
        if c.get("location") and c["location"] != "body":
            entry["location"] = c["location"]
        pages.append(entry)
    for p in patterns:
        entry = {"pattern": p.pattern, "count": p.count,
                 "anchor": p.anchor_examples[0] if p.anchor_examples else "",
                 "example_urls": p.example_urls}
        if p.location != "body":
            entry["location"] = p.location
        pages.append(entry)
    return {"homepage": homepage_url, "pages": pages}


# ===========================================================================
# Stage 4 — LLM crawl planner (query-aware, priority-scored)
# ===========================================================================
_PLANNER_SYSTEM_PROMPT = (
    "You are a CRAWL PLANNER, not a content analyzer. You will never answer "
    "the user's question — your ONLY job is deciding which pages of a "
    "company's website should be FETCHED in order to eventually answer it.\n\n"
    "You are given the user's request and a lightweight navigation summary of "
    "one website: individual pages (url, anchor text, title, nav location) and "
    "PATTERN summaries for repeating page groups (e.g. \"/boats/*\" representing "
    "250 near-identical listing pages) — a pattern stands for the WHOLE group, "
    "never crawl it page-by-page.\n\n"
    "For EVERY candidate (individual page or pattern), assign a priority score "
    "from 0 (useless for this query) to 100 (certainly needed). Infer "
    "relevance dynamically from the URL, anchor text, title, nav location and "
    "the user's request — do NOT rely on hardcoded page names. A repeating "
    "listing pattern (products, blog posts, team bios, portfolio items, news "
    "archives) should almost always score LOW — those pages rarely help answer "
    "a business-qualification query, regardless of the query. Pages likely to "
    "carry company overview, services/offerings, contact/portal/login, and "
    "anything the user's specific request calls out should score HIGH.\n\n"
    "Also recommend a crawl budget: the number of pages actually worth "
    f"fetching, between {MIN_CRAWL_BUDGET} and {MAX_CRAWL_BUDGET}.\n\n"
    "Respond with ONLY valid JSON of exactly this form:\n"
    '{"crawl_plan": [{"url": "/about", "priority": 95, "reason": "..."}, '
    '{"pattern": "/boats/*", "priority": 5, "reason": "..."}], '
    '"recommended_pages": 6}\n'
    "Use \"url\" for individual pages (copied exactly from the summary) and "
    "\"pattern\" for pattern entries (copied exactly). Include every candidate "
    "you were given, each with a priority and a short honest reason."
)


def build_prompt(user_query: str, nav_summary: Dict[str, Any]) -> List[Dict[str, str]]:
    payload = json.dumps(nav_summary, ensure_ascii=False)
    user_content = (
        f"User request: {user_query or '(no specific query — general lead collection)'}\n\n"
        f"Navigation summary (JSON):\n{payload}\n\n"
        "Score every page/pattern and recommend a crawl budget."
    )
    return [
        {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def call_planner_llm(messages: List[Dict[str, str]]) -> Optional[str]:
    """Call the shared Groq client. Returns None (never raises) on ANY
    failure — the caller always has the deterministic fallback planner."""
    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001 - no client -> fallback planner
        logger.info("[Planner] LLM unavailable (%s).", exc)
        return None
    try:
        return call_llm(client, messages, response_format={"type": "json_object"})
    except Exception as exc:  # noqa: BLE001 - degrade, never crash the crawl
        logger.info("[Planner] LLM call failed (%s).", exc)
        return None


def _loads_forgiving(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


def parse_planner_response(
    raw: Optional[str],
    valid_urls: Set[str],
    valid_patterns: Set[str],
) -> Optional[Dict[str, Any]]:
    """Validate + normalize the model's output. Returns None if unusable (the
    caller then falls back to the deterministic scored planner).

    Only urls/patterns that were actually offered survive (no hallucinated
    routes); recommended_pages is clamped to [MIN_CRAWL_BUDGET, MAX_CRAWL_BUDGET].
    """
    if raw is None:
        return None
    data = _loads_forgiving(raw)
    if not isinstance(data, dict):
        return None
    items = data.get("crawl_plan")
    if not isinstance(items, list):
        return None

    plan: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        pattern = item.get("pattern")
        try:
            priority = max(0, min(100, int(item.get("priority", 0))))
        except (TypeError, ValueError):
            priority = 0
        reason = str(item.get("reason", ""))[:200]
        if isinstance(url, str) and url in valid_urls:
            plan.append({"url": url, "priority": priority, "reason": reason})
        elif isinstance(pattern, str) and pattern in valid_patterns:
            plan.append({"pattern": pattern, "priority": priority, "reason": reason})
    if not plan:
        return None

    try:
        budget = int(data.get("recommended_pages", DEFAULT_CRAWL_BUDGET))
    except (TypeError, ValueError):
        budget = DEFAULT_CRAWL_BUDGET
    budget = max(MIN_CRAWL_BUDGET, min(MAX_CRAWL_BUDGET, budget))

    plan.sort(key=lambda p: p["priority"], reverse=True)
    return {"crawl_plan": plan, "recommended_pages": budget}


# ===========================================================================
# Deterministic fallback planner — used whenever the LLM is unavailable
# ===========================================================================
# A fixed URL/anchor keyword -> score table. Checked in order; first match
# wins. This NEVER calls out to anything — pure, fast, always available.
_FALLBACK_SCORES: List[Tuple[frozenset, int]] = [
    (frozenset({"about", "company", "overview"}), 95),
    (frozenset({"services", "solutions"}), 95),
    (frozenset({"products"}), 90),
    (frozenset({"contact"}), 85),
    (frozenset({"projects", "portfolio", "case-studies", "case-study"}), 85),
    (frozenset({"industries", "capabilities", "expertise"}), 80),
    (frozenset({"portal", "customer-portal", "dashboard", "account"}), 75),
    (frozenset({"login", "signin", "sign-in"}), 70),
    (frozenset({"blog", "news"}), 20),
    (frozenset({"careers", "career", "jobs"}), 10),
    (frozenset({"gallery"}), 5),
    (frozenset({"privacy", "terms", "cookies", "cookie-policy"}), 0),
]


def _fallback_score(url: str, anchor: str) -> int:
    path = urlsplit(url).path.rstrip("/")
    if not path:
        return 100  # homepage
    tokens: Set[str] = set()
    for seg in path.lower().split("/"):
        if seg:
            tokens.add(seg)
            tokens.update(seg.split("-"))
    tokens.update((anchor or "").lower().split())
    for keywords, score in _FALLBACK_SCORES:
        if tokens & keywords:
            return score
    return 40  # unknown page: neither clearly valuable nor clearly noise


def deterministic_fallback_plan(
    individual: Sequence[Dict[str, Any]], patterns: Sequence[UrlPattern],
    budget: int = DEFAULT_CRAWL_BUDGET,
) -> Dict[str, Any]:
    """Fixed-table scoring — the crawl NEVER stops just because the LLM is
    down. Patterns always score low (they're bulk/repetitive by definition)."""
    logger.info("[Planner] Using deterministic fallback planner.")
    plan: List[Dict[str, Any]] = [
        {"url": c["url"], "priority": _fallback_score(c["url"], c.get("anchor", "")),
         "reason": "Deterministic fallback scoring (LLM unavailable)."}
        for c in individual
    ]
    plan.extend(
        {"pattern": p.pattern, "priority": 5,
         "reason": "Repeating listing pattern — deterministic fallback deprioritizes these."}
        for p in patterns
    )
    plan.sort(key=lambda p: p["priority"], reverse=True)
    return {"crawl_plan": plan, "recommended_pages": budget}


# ===========================================================================
# Selection — turn a crawl plan into the actual URLs to queue
# ===========================================================================
def select_crawl_urls(plan: Dict[str, Any]) -> List[str]:
    """Top-priority individual URLs (never patterns — a pattern represents
    many pages and is never crawled page-by-page), capped at the budget.

    Requires priority >= MIN_CRAWL_PRIORITY, not just > 0 — a low-but-nonzero
    score (5, 10, 15...) usually means the model itself flagged the page as
    barely relevant (e.g. "wrong city/region"); that signal should exclude the
    page, not just deprioritize it.
    """
    budget = plan.get("recommended_pages", DEFAULT_CRAWL_BUDGET)
    urls = [p["url"] for p in plan.get("crawl_plan", [])
            if "url" in p and p.get("priority", 0) >= MIN_CRAWL_PRIORITY]
    return urls[:budget]


# ===========================================================================
# Caching (Step 7) — signatures for structure + intent
# ===========================================================================
def compute_structure_signature(candidates: Sequence[Dict[str, Any]]) -> str:
    """A cheap fingerprint of the homepage's link structure. Changes only when
    the SET of distinct URLs meaningfully changes — page-content edits don't
    affect it, so a plan survives routine copy updates on the same pages."""
    fingerprint = "|".join(sorted({c["url"] for c in candidates}))
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]


def compute_intent_signature(
    industry: str, exclude_keywords: Sequence[str], include_keywords: Sequence[str]
) -> str:
    """A cheap fingerprint of what the user is asking for. Two queries with
    the same industry/keyword-set are treated as 'the same intent' — anything
    else means the cached plan may no longer prioritize the right pages."""
    fingerprint = json.dumps(
        [industry.lower().strip(),
         sorted(k.lower() for k in exclude_keywords),
         sorted(k.lower() for k in include_keywords)],
        sort_keys=True,
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]


def _cached_plan_is_valid(
    cached: Dict[str, Any], structure_sig: str, intent_sig: str, content_hash: str
) -> bool:
    if cached.get("structure_signature") != structure_sig:
        logger.info("[Planner] Cached plan stale: site structure changed.")
        return False
    if cached.get("intent_signature") != intent_sig:
        logger.info("[Planner] Cached plan stale: query intent changed.")
        return False
    # Belt-and-suspenders alongside structure_signature: a page whose LINK SET
    # is unchanged but whose actual content was overhauled (e.g. a business
    # site repurposed into something else) still invalidates the plan.
    if cached.get("content_hash") and cached["content_hash"] != content_hash:
        logger.info("[Planner] Cached plan stale: homepage content changed.")
        return False
    age_days = (time.time() - cached.get("generated_at_epoch", 0)) / 86400
    if age_days > CACHE_TTL_DAYS:
        logger.info("[Planner] Cached plan stale: older than %d days.", CACHE_TTL_DAYS)
        return False
    return True


# ===========================================================================
# Public entry point
# ===========================================================================
def plan_site_crawl(
    domain: str,
    homepage_url: str,
    homepage_html: str,
    user_query: str = "",
    industry: str = "",
    exclude_keywords: Optional[Sequence[str]] = None,
    include_keywords: Optional[Sequence[str]] = None,
) -> List[str]:
    """Decide which of the homepage's linked pages are worth crawling.

    Reuses a cached plan (see storage.read_crawl_plan) when the site's link
    structure and the query's intent both match what the plan was built for
    and it isn't stale; otherwise builds a fresh one (LLM, falling back to a
    deterministic scoring table on any failure) and stages it for this crawl
    to commit or discard alongside the rest of the business's data.

    Returns the list of URLs to queue for crawling — NEVER raises; on any
    internal error this degrades to returning every candidate URL unfiltered,
    so a bug here can only ever mean "less savings", never "less recall".
    """
    from phase1_pipeline import _domain_key  # local: avoid module-level circularity

    exclude_keywords = list(exclude_keywords or [])
    include_keywords = list(include_keywords or [])
    domain = _domain_key(domain)

    try:
        candidates = extract_homepage_candidates(homepage_html, homepage_url, domain)
    except Exception as exc:  # noqa: BLE001 - planning must never break the crawl
        logger.warning("[Planner] Candidate extraction failed for %s: %s", domain, exc)
        return []

    if not candidates:
        return []

    structure_sig = compute_structure_signature(candidates)
    intent_sig = compute_intent_signature(industry, exclude_keywords, include_keywords)
    content_hash = dc.compute_content_hash(homepage_html)

    store = get_store()
    try:
        cached = store.read_crawl_plan(domain)
    except Exception:  # noqa: BLE001
        cached = None
    if cached and _cached_plan_is_valid(cached, structure_sig, intent_sig, content_hash):
        logger.info("[Planner] Reusing cached crawl plan for %s (%d page(s)).",
                    domain, len(cached.get("selected_urls", [])))
        return cached["selected_urls"]

    individual, patterns = detect_patterns(candidates)
    shortlist = individual[:MAX_LLM_CANDIDATES]
    nav_summary = build_navigation_summary(homepage_url, shortlist, patterns)

    valid_urls = {c["url"] for c in shortlist}
    valid_patterns = {p.pattern for p in patterns}

    messages = build_prompt(user_query, nav_summary)
    raw = call_planner_llm(messages)
    plan = parse_planner_response(raw, valid_urls, valid_patterns)
    if plan is None:
        plan = deterministic_fallback_plan(shortlist, patterns)
    else:
        logger.info("[Planner] LLM crawl plan for %s: %d candidate(s) scored, "
                    "budget=%d.", domain, len(plan["crawl_plan"]), plan["recommended_pages"])

    selected = select_crawl_urls(plan)
    logger.info("[Planner] Selected pages for %s: %s", domain, ", ".join(
        urlsplit(u).path or "/" for u in selected
    ) or "(none)")

    record = {
        "domain": domain,
        "homepage_url": homepage_url,
        "structure_signature": structure_sig,
        "intent_signature": intent_sig,
        "content_hash": content_hash,
        "user_query": user_query,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generated_at_epoch": time.time(),
        "crawl_plan": plan["crawl_plan"],
        "recommended_pages": plan["recommended_pages"],
        "selected_urls": selected,
        "patterns_ignored": [p.pattern for p in patterns],
    }
    try:
        store.stage_crawl_plan(domain, record)
    except Exception as exc:  # noqa: BLE001 - caching is best-effort
        logger.warning("[Planner] Failed to stage crawl plan for %s: %s", domain, exc)

    return selected
