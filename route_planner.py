"""
LLM Route Planner — identify a company website's most valuable pages.

This module is a SEPARATE pipeline stage from Step 3 (`route_filter.py`). Step 3
answers a query-specific question ("which pages fill THIS lead's missing
fields?"). This module answers a general one, asked once per crawled site
before any downstream AI processing: "which pages describe what this business
actually does?" (products, services, capabilities, projects, company overview
— see PRIORITY_HINTS below). It is intentionally query-agnostic.

Pipeline:
    crawl_index.csv (per-page metadata) + storage/<domain>/links.json
        -> load_page_metadata      (Stage 0: read metadata, no file I/O beyond the index)
        -> build_link_graph        (Stage 0: which pages link to which, in what anchor text)
        -> rule_based_prefilter    (Stage 1: instant discard/boost, NO LLM — cheap)
        -> maybe_fetch_snippets    (Stage 2: read a SMALL number of .txt files, only if
                                    metadata alone left too few confident candidates)
        -> build_prompt            (Stage 3: compact JSON per candidate)
        -> call_planner_llm        (Stage 3: reuses LLM_planner.get_client/call_llm)
        -> parse_planner_response  (Stage 4: validate, clamp to MIN..MAX_SELECTED pages)
    -> {"selected_pages": [...], "confidence": "high|medium|low"}

Explicitly OUT OF SCOPE (belongs to later stages, not here):
    - embeddings / cosine similarity
    - lead qualification
    - detailed page content analysis
This module's job ends the moment it returns the selected page list.

Reuses existing infrastructure instead of duplicating it:
    - storage.get_store()                 the modular storage layer (R2-ready)
    - phase1_pipeline._domain_key          canonical domain key
    - route_filter.normalize_urls          URL canonicalization (mechanical, not policy)
    - LLM_planner.get_client / call_llm    the shared Groq client + primary/fallback model
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from LLM_planner import call_llm, get_client
from phase1_pipeline import _domain_key
from route_filter import normalize_urls
from storage import get_store

logger = logging.getLogger("ai_bdm.route_planner")

MIN_SELECTED_PAGES = 3
MAX_SELECTED_PAGES = 8
MAX_LLM_CANDIDATES = 20     # only the top-N ranked pages are ever shown to the model
MAX_SNIPPET_READS = 5       # hard cap on .txt files opened when metadata is ambiguous
SNIPPET_CHARS = 400         # per-file peek size — enough context, not a page dump
MIN_CONFIDENT_CANDIDATES = MIN_SELECTED_PAGES  # below this, read snippets for help

PLANNER_MODEL = "openai/gpt-oss-20b"  # documented; call_llm handles primary/fallback


class RoutePlannerError(Exception):
    """Base error for the Route Planner."""


# ===========================================================================
# Rule-based taxonomy — the "no LLM" pre-filter (Stage 1)
# ===========================================================================
# Tokens matched against URL path segments and page-title words. Whole-token
# matching (not substring) so "news" doesn't match "newsletter-signup"-as-junk
# false positives in the other direction.
IGNORE_HINTS: frozenset = frozenset({
    "privacy", "privacy-policy", "cookie", "cookies", "cookie-policy",
    "terms", "tos", "terms-of-service", "terms-and-conditions",
    "login", "signin", "sign-in", "logout", "signout",
    "register", "signup", "sign-up", "search", "cart", "checkout",
    "sitemap", "404", "500", "error", "not-found",
})
HIGH_PRIORITY_HINTS: frozenset = frozenset({
    "products", "product", "services", "service", "solutions", "solution",
    "capabilities", "capability", "industries", "industry",
    "manufacturing", "engineering", "equipment", "technologies", "technology",
    "portfolio", "projects", "project", "case-studies", "case-study", "casestudy",
    "about", "company", "overview", "our-company",
    "business-units", "business-unit", "divisions", "division",
    "markets", "markets-served", "expertise", "what-we-do", "whatwedo",
})
MEDIUM_PRIORITY_HINTS: frozenset = frozenset({
    "leadership", "team", "management", "certifications", "certification",
    "iso", "sustainability", "innovation", "locations", "location",
})
LOW_PRIORITY_HINTS: frozenset = frozenset({
    "careers", "career", "jobs", "job", "news", "blog", "events", "event",
    "press", "press-releases", "press-release", "investors",
    "investor-relations", "faq", "faqs",
})
TIER_SCORE = {"high": 30, "medium": 15, "low": 5, "unknown": 10}
HOMEPAGE_LINK_BONUS = 12     # linked directly from the homepage (closest proxy to main nav)
IN_DEGREE_BONUS_CAP = 10     # linked from many internal pages -> structurally important
WORD_COUNT_BONUS_CAP = 8     # thin pages carry little business information


# ===========================================================================
# Data model
# ===========================================================================
@dataclass
class PageMeta:
    """One page's metadata row from the crawl index (no page text)."""
    domain: str
    website_url: str
    page_url: str
    page_title: str = ""
    page_type: str = ""
    txt_path: str = ""
    http_status: str = ""
    crawl_status: str = ""
    word_count: int = 0


@dataclass
class LinkGraph:
    """Internal link relationships for one domain, from persisted links.json."""
    homepage_url: str
    edges: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)  # page -> [{url, anchor}]
    homepage_linked: Set[str] = field(default_factory=set)   # pages linked from the homepage
    in_degree: Counter = field(default_factory=Counter)      # page -> # internal pages linking to it
    anchors_for: Dict[str, List[str]] = field(default_factory=dict)  # page -> anchor texts pointing to it


@dataclass
class ScoredCandidate:
    """A page carried into ranking/LLM shortlisting, with its rule-based signals."""
    meta: PageMeta
    tier: str                 # "high" | "medium" | "low" | "unknown"
    score: float
    nav_linked: bool
    anchors: List[str] = field(default_factory=list)
    snippet: str = ""          # optional short content peek (Stage 2)

    def as_context(self) -> Dict[str, Any]:
        """Compact JSON sent to the LLM — metadata + signals, never full text."""
        ctx: Dict[str, Any] = {
            "url": self.meta.page_url,
            "title": self.meta.page_title,
            "tier": self.tier,
        }
        if self.meta.page_type:
            ctx["page_type"] = self.meta.page_type
        if self.nav_linked:
            ctx["linked_from_homepage"] = True
        if self.anchors:
            ctx["anchor_text"] = self.anchors[:3]
        if self.meta.word_count:
            ctx["word_count"] = self.meta.word_count
        if self.snippet:
            ctx["content_preview"] = self.snippet
        return ctx


# ===========================================================================
# Stage 0a — metadata loading
# ===========================================================================
def load_page_metadata(domain: str) -> List[PageMeta]:
    """Read this domain's rows from the crawl index via the storage layer.

    Pure metadata — no .txt files are opened here. This is the cheap, primary
    decision source the rest of the planner works from.
    """
    want = _domain_key(domain)
    pages: List[PageMeta] = []
    for row in get_store().read_index():
        if _domain_key(row.get("domain") or row.get("website_url") or "") != want:
            continue
        try:
            word_count = int(row.get("word_count") or 0)
        except ValueError:
            word_count = 0
        pages.append(PageMeta(
            domain=want,
            website_url=row.get("website_url", ""),
            page_url=row.get("page_url", ""),
            page_title=row.get("page_title", ""),
            page_type=row.get("page_type", ""),
            txt_path=row.get("txt_path", ""),
            http_status=row.get("http_status", ""),
            crawl_status=row.get("crawl_status", ""),
            word_count=word_count,
        ))
    return pages


# ===========================================================================
# Stage 0b — website graph construction
# ===========================================================================
def build_link_graph(domain: str, pages: Sequence[PageMeta]) -> LinkGraph:
    """Build internal link relationships from persisted links.json.

    `links.json` maps each crawled page -> the internal links found on it (with
    anchor text), captured at crawl time before cleaning. There is no per-link
    "header/nav/footer" location in the streamed store, so the best available
    proxy for "main navigation" is: pages linked directly from the homepage.
    """
    homepage_url = next((p.page_url for p in pages if p.page_type == "home"), "")
    if not homepage_url and pages:
        # Fall back to the shortest-path page (typically the homepage).
        homepage_url = min(pages, key=lambda p: len(urlsplit(p.page_url).path)).page_url

    raw_links = get_store().read_links(_domain_key(domain))
    graph = LinkGraph(homepage_url=homepage_url, edges=raw_links)

    base = homepage_url or (pages[0].website_url if pages else "")
    for source_page, links in raw_links.items():
        source_is_home = source_page == homepage_url
        for link in links:
            targets = normalize_urls([link.get("url", "")], base)
            if not targets:
                continue
            target = targets[0]
            graph.in_degree[target] += 1
            anchor = (link.get("anchor") or "").strip()
            if anchor:
                graph.anchors_for.setdefault(target, []).append(anchor)
            if source_is_home:
                graph.homepage_linked.add(target)
    return graph


# ===========================================================================
# Stage 1 — rule-based pre-filter (NO LLM: instant discard / instant boost)
# ===========================================================================
def _match_tier(page_url: str, page_title: str, page_type: str) -> Tuple[str, bool]:
    """Return (tier, is_ignored) from URL path segments and title words.

    Explicit token matches (URL/title against the priority hint sets) ALWAYS
    take precedence over the crawler's own `page_type` guess, since page_type
    is a coarser heuristic (e.g. it buckets "leadership"/"team" pages under
    "about") that would otherwise silently override a clearer, more specific
    signal — a "/leadership" page must land as MEDIUM even though the crawler
    tagged it page_type="about". `page_type` is used only as a last-resort
    fallback when no token in the URL or title matches anything.
    """
    path = urlsplit(page_url).path.lower()
    segments: Set[str] = set()
    for seg in path.split("/"):
        if seg:
            segments.add(seg)
            segments.update(seg.split("-"))
    title_words = set((page_title or "").lower().replace("-", " ").split())
    tokens = segments | title_words

    if tokens & IGNORE_HINTS:
        return "unknown", True
    if tokens & HIGH_PRIORITY_HINTS:
        return "high", False
    if tokens & MEDIUM_PRIORITY_HINTS:
        return "medium", False
    if tokens & LOW_PRIORITY_HINTS:
        return "low", False
    # No explicit token match anywhere — fall back to the crawler's page_type.
    if page_type in ("home", "about", "services", "products"):
        return "high", False
    if page_type in ("blog", "legal"):
        return "low", False
    return "unknown", False


def rule_based_prefilter(
    pages: Sequence[PageMeta], graph: LinkGraph
) -> Tuple[List[ScoredCandidate], List[ScoredCandidate]]:
    """Instantly discard obvious junk, tier-score and boost the rest.

    Returns (kept, discarded). `kept` is sorted by score, richest first — the
    caller takes only the top MAX_LLM_CANDIDATES from it before calling the LLM.
    """
    kept: List[ScoredCandidate] = []
    discarded: List[ScoredCandidate] = []
    max_words = max((p.word_count for p in pages), default=0) or 1

    for meta in pages:
        if meta.crawl_status and meta.crawl_status != "ok":
            continue  # failed/empty fetch — nothing to route to
        tier, ignored = _match_tier(meta.page_url, meta.page_title, meta.page_type)
        nav_linked = meta.page_url in graph.homepage_linked
        anchors = graph.anchors_for.get(meta.page_url, [])
        candidate = ScoredCandidate(meta=meta, tier=tier, score=0.0,
                                    nav_linked=nav_linked, anchors=anchors)
        if ignored:
            discarded.append(candidate)
            continue

        score = TIER_SCORE[tier]
        if nav_linked:
            score += HOMEPAGE_LINK_BONUS
        score += min(graph.in_degree.get(meta.page_url, 0), IN_DEGREE_BONUS_CAP)
        score += round(WORD_COUNT_BONUS_CAP * (meta.word_count / max_words))
        candidate.score = score
        kept.append(candidate)

    kept.sort(key=lambda c: c.score, reverse=True)
    return kept, discarded


# ===========================================================================
# Stage 2 — optional, bounded content peek
# ===========================================================================
def maybe_fetch_snippets(candidates: Sequence[ScoredCandidate]) -> None:
    """Read a SMALL number of .txt files only if metadata left too few confident
    picks (fewer than MIN_CONFIDENT_CANDIDATES clear high/medium tier hits).

    Mutates candidates in place. Targets the highest-scoring "unknown" tier
    pages first — these are exactly the ones a URL/title alone couldn't place,
    so a short content peek is the cheapest way to disambiguate them.
    """
    confident = sum(1 for c in candidates if c.tier in ("high", "medium"))
    if confident >= MIN_CONFIDENT_CANDIDATES:
        return

    store = get_store()
    ambiguous = [c for c in candidates if c.tier == "unknown" and c.meta.txt_path]
    ambiguous.sort(key=lambda c: c.score, reverse=True)
    for candidate in ambiguous[:MAX_SNIPPET_READS]:
        text = store.read_page_text(candidate.meta.txt_path)
        if text:
            candidate.snippet = text[:SNIPPET_CHARS]
        del text


# ===========================================================================
# Stage 3 — prompt generation + LLM interaction
# ===========================================================================
_PLANNER_SYSTEM_PROMPT = (
    "You are a WEBSITE ROUTE PLANNER. You never analyze page content in depth "
    "— you only decide which pages of a company's website are most likely to "
    "contain important BUSINESS information, from compact page metadata.\n\n"
    "Prioritize pages describing: products, services, solutions, capabilities, "
    "industries, manufacturing, engineering, equipment, technologies, "
    "portfolio, projects, case studies, company overview/about, business "
    "units, markets served, expertise, what the company does.\n"
    "Medium priority: leadership, certifications, sustainability, innovation, "
    "locations.\n"
    "Low priority: careers, news, blog, events, press releases, investors, FAQs.\n"
    "Each candidate already carries a rule-based 'tier' (high/medium/low/"
    "unknown) and whether it is linked directly from the homepage — treat "
    "these as strong hints, not absolute rules; use the title, url, anchor "
    "text and any content preview to make the final call.\n\n"
    f"Return between {MIN_SELECTED_PAGES} and {MAX_SELECTED_PAGES} pages — "
    "never more. Respond with ONLY valid JSON of exactly this form:\n"
    '{"selected_pages": [{"url": "<url from the list>", "priority": 1, '
    '"reason": "short honest reason grounded in the metadata shown"}], '
    '"confidence": "high"}\n'
    'confidence reflects how clearly the site\'s pages map to the priority '
    "categories above (\"high\", \"medium\", or \"low\"). Use ONLY urls from "
    "the candidate list."
)


def build_prompt(candidates: Sequence[ScoredCandidate]) -> List[Dict[str, str]]:
    """Compact JSON-per-candidate user message. No raw page text is ever sent
    in bulk — only the optional short preview attached in Stage 2."""
    payload = json.dumps([c.as_context() for c in candidates], ensure_ascii=False)
    user_content = (
        f"Candidate pages (JSON, {len(candidates)} total):\n{payload}\n\n"
        "Select the pages most likely to describe what this business actually "
        "does."
    )
    return [
        {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def call_planner_llm(messages: List[Dict[str, str]]) -> Optional[str]:
    """Call the shared Groq client. Returns None (never raises) on any failure
    so the caller can fall back to the deterministic rule-based ranking."""
    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001 - no client -> deterministic fallback
        logger.warning("LLM client unavailable, using rule-based plan: %s", exc)
        return None
    try:
        return call_llm(client, messages, response_format={"type": "json_object"})
    except Exception as exc:  # noqa: BLE001 - degrade, never crash the planner
        logger.warning("Route planner LLM call failed: %s", exc)
        return None


# ===========================================================================
# Stage 4 — response parsing + validation
# ===========================================================================
def _loads_forgiving(text: str) -> Optional[Any]:
    """Best-effort JSON parse: whole string, then first embedded object."""
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


_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


def parse_planner_response(
    raw: Optional[str], by_url: Dict[str, ScoredCandidate]
) -> Optional[Dict[str, Any]]:
    """Validate + normalize the model's output. Returns None if unusable (the
    caller then falls back to the deterministic rule-based plan).

    Only URLs from the candidate set survive (no hallucinated routes); results
    are clamped to MIN..MAX_SELECTED_PAGES.
    """
    if raw is None:
        return None
    data = _loads_forgiving(raw)
    if not isinstance(data, dict):
        return None

    items = data.get("selected_pages")
    if not isinstance(items, list):
        return None

    seen: Set[str] = set()
    selected: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        url = item["url"]
        if url not in by_url or url in seen:
            continue
        seen.add(url)
        selected.append({
            "url": url,
            "txt_path": by_url[url].meta.txt_path,
            "reason": str(item.get("reason", ""))[:200] or _default_reason(by_url[url]),
        })
    if len(selected) < MIN_SELECTED_PAGES:
        # Top up from the highest-scoring unused candidates so the response
        # still meets the minimum, without ever exceeding the maximum.
        for candidate in sorted(by_url.values(), key=lambda c: c.score, reverse=True):
            if len(selected) >= MIN_SELECTED_PAGES:
                break
            if candidate.meta.page_url in seen:
                continue
            seen.add(candidate.meta.page_url)
            selected.append({
                "url": candidate.meta.page_url,
                "txt_path": candidate.meta.txt_path,
                "reason": _default_reason(candidate),
            })
    if not selected:
        return None
    selected = selected[:MAX_SELECTED_PAGES]
    for i, route in enumerate(selected, 1):
        route["priority"] = i

    confidence = str(data.get("confidence", "")).strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        confidence = _infer_confidence(selected, by_url)
    return {"selected_pages": selected, "confidence": confidence}


def _default_reason(candidate: ScoredCandidate) -> str:
    """Honest, metadata-grounded reason when the model omits or we auto-fill one."""
    if candidate.nav_linked:
        return f"Linked from the homepage; matches '{candidate.tier}' priority signals"
    if candidate.tier != "unknown":
        return f"URL/title matches '{candidate.tier}' priority category"
    return "Highest-ranked remaining candidate by internal link structure"


def _infer_confidence(
    selected: Sequence[Dict[str, Any]], by_url: Dict[str, ScoredCandidate]
) -> str:
    """Derive an overall confidence from how cleanly the selection matches the
    known priority taxonomy — never the model's own unfounded claim."""
    tiers = [by_url[s["url"]].tier for s in selected if s["url"] in by_url]
    strong = sum(1 for t in tiers if t in ("high", "medium"))
    if not tiers:
        return "low"
    ratio = strong / len(tiers)
    if ratio >= 0.75:
        return "high"
    if ratio >= 0.4:
        return "medium"
    return "low"


def heuristic_plan(candidates: Sequence[ScoredCandidate]) -> Dict[str, Any]:
    """Deterministic fallback: top-ranked candidates by rule-based score alone."""
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)[:MAX_SELECTED_PAGES]
    selected = [
        {
            "url": c.meta.page_url,
            "txt_path": c.meta.txt_path,
            "priority": i,
            "reason": _default_reason(c),
        }
        for i, c in enumerate(ranked, 1)
    ]
    by_url = {c.meta.page_url: c for c in candidates}
    confidence = _infer_confidence(selected, by_url) if selected else "low"
    return {"selected_pages": selected, "confidence": confidence}


# ===========================================================================
# Public entry point
# ===========================================================================
def plan_routes(website: str) -> Dict[str, Any]:
    """Run all stages and return the selected high-value pages for one site.

    Args:
        website: the business's site (URL or bare domain) — must already be
                 committed to storage (see storage.PageStore.commit_domain).

    Returns {"selected_pages": [{url, txt_path, priority, reason}], "confidence"}.
    Returns an empty plan ({"selected_pages": [], "confidence": "low"}) if the
    domain has no committed pages.
    """
    domain = _domain_key(website)
    pages = load_page_metadata(domain)
    if not pages:
        logger.info("No crawled pages found for %s — nothing to route.", domain)
        return {"selected_pages": [], "confidence": "low"}

    graph = build_link_graph(domain, pages)
    kept, discarded = rule_based_prefilter(pages, graph)
    logger.info(
        "Route Planner: %d page(s) -> %d candidate(s), %d discarded (%s)",
        len(pages), len(kept), len(discarded), domain,
    )
    if not kept:
        return {"selected_pages": [], "confidence": "low"}

    # Trivial path: already within bounds -> skip the LLM call entirely.
    if len(kept) <= MAX_SELECTED_PAGES:
        return heuristic_plan(kept)

    shortlist = kept[:MAX_LLM_CANDIDATES]
    maybe_fetch_snippets(shortlist)     # bounded, only if metadata was ambiguous
    by_url = {c.meta.page_url: c for c in shortlist}

    messages = build_prompt(shortlist)
    raw = call_planner_llm(messages)
    plan = parse_planner_response(raw, by_url)
    if plan is not None:
        return plan

    logger.info("Falling back to deterministic route plan for %s.", domain)
    return heuristic_plan(kept)


# ===========================================================================
# CLI — standalone testing
# ===========================================================================
def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="LLM Route Planner — select a committed site's most "
                    "valuable pages from metadata + link structure.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--website", help="Domain or URL to plan routes for.")
    source.add_argument("--list", action="store_true",
                        help="List domains available in the crawl index.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    if args.list:
        seen: Set[str] = set()
        for row in get_store().read_index():
            dom = _domain_key(row.get("domain") or row.get("website_url") or "")
            if dom and dom not in seen:
                seen.add(dom)
                print("  ", row.get("website_url"))
        print(f"\n{len(seen)} domain(s) in the crawl index")
        return

    plan = plan_routes(args.website)
    print("\n" + "=" * 66)
    print("LLM ROUTE PLANNER")
    print("=" * 66)
    print(f"Site       : {args.website}")
    print(f"Confidence : {plan['confidence']}")
    print(f"Selected   : {len(plan['selected_pages'])} page(s)")
    print("-" * 66)
    for p in plan["selected_pages"]:
        print(f"  [{p['priority']}] {p['url']}")
        print(f"       txt_path : {p['txt_path']}")
        print(f"       reason   : {p['reason']}")
    print("=" * 66 + "\n")


if __name__ == "__main__":
    _cli()
