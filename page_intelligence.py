"""Page Intelligence — a generic, reusable per-page semantic summary.

Built once per crawled page, alongside its cleaned `.txt` (see
`storage.stage_page_context` / `<page_name>_context.json`). This is NOT a
Route-Planner-specific format: it's a small, self-contained JSON object that
lets ANY downstream AI component (Route Planning, RAG retrieval, business
qualification, lead scoring, website summarization, tech-stack reasoning,
opportunity/CRM/mobile-app detection, future modules) reason about a page's
purpose without loading its full text.

Two-phase build, because "which pages link here, with what anchor text" is
only knowable once the WHOLE site has been crawled:
  1. `build_page_context(...)` — called per-page, DURING the streaming crawl
     (phase1_pipeline.crawl_site). Fills everything derivable from that one
     page alone. `anchors` starts empty.
  2. `enrich_anchors(domain)` — called ONCE per domain, AFTER commit (so
     links.json is final). Reads every committed context file + the domain's
     link graph and back-fills each page's `anchors` field.

Fields are never fabricated: anything not derivable from the page is left
null/empty, not guessed.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

# --- token-optimization limits (kept lightweight: dozens/hundreds of these
# may be sent to an LLM at once) -------------------------------------------
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 180
MAX_INTRO_CHARS = 250
MAX_OUTRO_CHARS = 250
MAX_H2S = 5
MAX_ANCHORS = 5
_WORDS_PER_MINUTE = 225  # reading-time estimate

# --- page type taxonomy ----------------------------------------------------
PAGE_TYPES = frozenset({
    "home", "about", "services", "products", "pricing", "contact", "support",
    "documentation", "faq", "careers", "team", "blog", "blog_post", "news",
    "case_study", "portfolio", "locations", "booking", "events", "downloads",
    "privacy", "terms", "dashboard", "login", "portal", "checkout", "cart",
    "knowledge_base", "api", "developers", "integrations", "customer_success",
    "resources", "other",
})

# URL-path / title token -> page_type, checked in priority order (most
# specific first) so e.g. "pricing" wins over the more generic "products".
# Deliberately separate from phase1_pipeline._PAGE_TYPE_HINTS (a narrower,
# older 7-category classifier already relied on elsewhere for crawl_index.csv
# + route_planner's rule tiers) — changing that one's output values would
# require re-auditing every consumer of the "page_type" index column.
_TYPE_HINTS: List[tuple] = [
    ("checkout", frozenset({"checkout"})),
    ("cart", frozenset({"cart", "basket"})),
    ("pricing", frozenset({"pricing", "plans", "plan", "price", "prices"})),
    ("login", frozenset({"login", "signin", "sign-in", "log-in"})),
    ("dashboard", frozenset({"dashboard"})),
    ("portal", frozenset({"portal", "myaccount", "my-account", "account"})),
    ("api", frozenset({"api"})),
    ("developers", frozenset({"developer", "developers", "sdk"})),
    ("integrations", frozenset({"integration", "integrations"})),
    ("documentation", frozenset({"docs", "documentation", "guide", "guides"})),
    ("knowledge_base", frozenset({"kb", "knowledge-base", "knowledgebase"})),
    ("faq", frozenset({"faq", "faqs"})),
    ("support", frozenset({"support", "help", "helpdesk", "help-center"})),
    ("customer_success", frozenset({"customer-success", "customers"})),
    ("contact", frozenset({"contact", "contact-us", "kontakt", "contacto", "get-in-touch"})),
    ("booking", frozenset({"book", "booking", "reserve", "reservations", "schedule-demo", "demo"})),
    ("case_study", frozenset({"case-study", "case-studies", "casestudy",
                              "customer-story", "customer-stories",
                              "success-story", "success-stories"})),
    ("portfolio", frozenset({"portfolio", "projects", "project", "our-work"})),
    ("locations", frozenset({"locations", "location", "find-us", "branches"})),
    ("careers", frozenset({"careers", "career", "jobs", "job"})),
    ("team", frozenset({"team", "leadership", "management"})),
    ("events", frozenset({"events", "event"})),
    ("downloads", frozenset({"downloads", "download"})),
    ("privacy", frozenset({"privacy", "privacy-policy"})),
    ("terms", frozenset({"terms", "tos", "terms-of-service", "terms-and-conditions"})),
    ("news", frozenset({"news", "press", "press-releases", "press-release"})),
    ("resources", frozenset({"resources", "resource"})),
    ("services", frozenset({"services", "service", "capabilities", "capability",
                             "what-we-do", "whatwedo", "solutions", "solution"})),
    ("products", frozenset({"products", "product"})),
    ("about", frozenset({"about", "about-us", "company", "our-company",
                          "who-we-are", "our-story", "overview"})),
    ("blog", frozenset({"blog", "blogs", "insights", "articles", "article"})),
]

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(*parts: str) -> set:
    """Lowercase tokens from URL path + title: each "/"-or-space-separated
    segment is kept WHOLE (so multi-word hints like "case-studies" or
    "customer-success" still match) AND ALSO hyphen-split into sub-words (so
    single-word hints like "pricing" still match inside "pricing-plans")."""
    toks: set = set()
    for part in parts:
        if not part:
            continue
        low = part.lower()
        for seg in re.split(r"[/?#&=. ]+", low):
            seg = seg.strip("-_")
            if not seg:
                continue
            toks.add(seg)
            toks.update(s for s in seg.split("-") if s)
    return toks


def classify_page_type(
    url: str, title: str = "", headings: Optional[Sequence[str]] = None,
    text: str = "", is_home: bool = False,
) -> str:
    """Classify a page into ONE of PAGE_TYPES using URL + title + headings +
    body content together (never URL alone).

    "blog_post" vs "blog": a bare "/blog" or "/news" index page is the
    listing ("blog"); a deeper URL under it (e.g. "/blog/my-post-title") is
    an individual article ("blog_post").
    """
    if is_home:
        return "home"
    path = urlsplit(url).path.strip("/")
    toks = _tokens(path, title, " ".join(headings or []))

    for ptype, hints in _TYPE_HINTS:
        if toks & hints:
            if ptype == "blog":
                segments = [s for s in path.split("/") if s]
                blog_idx = next((i for i, s in enumerate(segments)
                                 if s.lower() in {"blog", "blogs", "news", "insights",
                                                   "articles", "article", "press"}), None)
                if blog_idx is not None and len(segments) > blog_idx + 1:
                    return "blog_post"
                return "blog"
            return ptype
    return "other"


def _heading_level(line: str) -> Optional[tuple]:
    """("#"*n, text) for a cleaned-text heading line (see
    phase1_pipeline.clean_page_for_llm — headings are `#`-prefixed, one `#`
    per HTML heading level). None for a non-heading line."""
    m = re.match(r"^(#{1,6})\s+(.*)$", line)
    if not m:
        return None
    return len(m.group(1)), m.group(2).strip()


def extract_structure(lines: Sequence[str]) -> Dict[str, Any]:
    """{h1, h2s} from cleaned page lines. Ignores H3+ (token-budget: the user
    asked to drop finer heading levels since H1/H2 already carry the page's
    real structure/intent)."""
    h1: Optional[str] = None
    h2s: List[str] = []
    for line in lines:
        parsed = _heading_level(line)
        if not parsed:
            continue
        level, text = parsed
        if not text:
            continue
        if level == 1 and h1 is None:
            h1 = text[:MAX_TITLE_CHARS]
        elif level == 2 and len(h2s) < MAX_H2S:
            h2s.append(text[:MAX_TITLE_CHARS])
    return {"h1": h1, "h2s": h2s}


def _body_lines(lines: Sequence[str]) -> List[str]:
    """Non-heading lines, list-marker stripped — the same "real sentence"
    convention route_planner.py's page-preview builder already uses."""
    body: List[str] = []
    for line in lines:
        if _heading_level(line):
            continue
        body.append(line[2:].strip() if line.startswith("- ") else line)
    return [b for b in body if b]


def _accumulate_lines(ordered: Sequence[str], limit: int) -> List[str]:
    """Take whole lines, in the given order, up to `limit` total chars —
    never cutting a line mid-sentence."""
    out: List[str] = []
    total = 0
    for line in ordered:
        if out and total + 1 + len(line) > limit:
            break
        out.append(line)
        total += len(line) + (1 if len(out) > 1 else 0)
        if total >= limit:
            break
    return out


# Nav menus / hero buttons / CTAs render as short "- " list-item lines after
# cleaning (e.g. "- About", "- Contact Us", "- Book Now") — genuine sentences
# in a page's real body copy are almost never this short. A cross-page
# boilerplate dedupe already strips nav that's IDENTICAL across pages (see
# phase1_pipeline.crawl_site's home_lines filtering), but a page's own local
# nav/menu can still differ page-to-page and slip through that filter, so
# intro/outro extraction applies its own minimum-word-count gate on top.
_MIN_SENTENCE_WORDS = 4


def _sentence_like_lines(body: Sequence[str]) -> List[str]:
    return [ln for ln in body if len(ln.split()) >= _MIN_SENTENCE_WORDS]


def extract_content(lines: Sequence[str]) -> Dict[str, Optional[str]]:
    """{intro, outro}: ~150-250 chars of real body content from the start and
    end of the page (never headings, list markers, or short nav/menu/button
    labels — see _sentence_like_lines). Accumulates whole lines up to the
    char budget so a phrase is never cut mid-sentence. None if the page has
    no sentence-like body text at all."""
    body = _sentence_like_lines(_body_lines(lines))
    if not body:
        return {"intro": None, "outro": None}

    intro = " ".join(_accumulate_lines(body, MAX_INTRO_CHARS))[:MAX_INTRO_CHARS].strip() or None
    outro_lines = list(reversed(_accumulate_lines(list(reversed(body)), MAX_OUTRO_CHARS)))
    outro = " ".join(outro_lines)[:MAX_OUTRO_CHARS].strip() or None
    return {"intro": intro, "outro": outro}


def _reading_time_minutes(word_count: int) -> int:
    return max(1, round(word_count / _WORDS_PER_MINUTE)) if word_count else 0


# Ultra-generic anchor texts carry no routing signal (see enrich_anchors).
_GENERIC_ANCHORS = frozenset({
    "here", "click here", "read more", "learn more", "more", "more info",
    "link", "this page", "details", "see more", "view more", "",
})


def build_page_context(
    *, url: str, page_name: str,
    page_title: str = "", meta_description: str = "",
    canonical_url: str = "", og_title: str = "", og_description: str = "",
    lines: Optional[Sequence[str]] = None,
    word_count: int = 0, internal_links: int = 0, external_links: int = 0,
    is_home: bool = False,
) -> Dict[str, Any]:
    """Assemble one page's context JSON. `lines` should be the FINAL cleaned
    lines for this page (post cross-page boilerplate dedupe, where
    applicable — see phase1_pipeline.crawl_site) so intro/outro/headings
    reflect real, non-repeated content.

    `anchors` starts empty — filled later by `enrich_anchors`, once the whole
    site's link graph is known.
    """
    lines = lines or []
    path = urlsplit(url).path or "/"
    structure = extract_structure(lines)
    content = extract_content(lines)
    headings_for_type = ([structure["h1"]] if structure["h1"] else []) + structure["h2s"]
    page_type = classify_page_type(url, page_title, headings_for_type, "", is_home)
    heading_count = sum(1 for ln in lines if _heading_level(ln))
    paragraph_count = len(_body_lines(lines))

    return {
        "url": url,
        "path": path,
        "page_name": page_name,
        "page_type": page_type,
        "meta": {
            "title": (page_title or None) and page_title[:MAX_TITLE_CHARS],
            "description": (meta_description or None) and meta_description[:MAX_DESCRIPTION_CHARS],
            "canonical_url": canonical_url or None,
            "og_title": (og_title or None) and og_title[:MAX_TITLE_CHARS],
            "og_description": (og_description or None) and og_description[:MAX_DESCRIPTION_CHARS],
        },
        "structure": structure,
        "content": content,
        "anchors": [],
        "metrics": {
            "word_count": word_count,
            "heading_count": heading_count,
            "paragraph_count": paragraph_count,
            "internal_links": internal_links,
            "external_links": external_links,
            "reading_time_minutes": _reading_time_minutes(word_count),
        },
    }


def _normalize_anchor(anchor: str) -> str:
    return " ".join((anchor or "").strip().split())


def enrich_anchors(domain: str) -> int:
    """Back-fill every committed page's `anchors` field from the domain's
    FINAL link graph (storage/<domain>/links.json), now that every page and
    every internal link is known. Call once, right after commit_domain.

    Returns the number of context files updated. Best-effort per-file: a
    single bad/missing file never aborts the rest of the domain.
    """
    # Local imports: avoid a hard import-time dependency from every caller of
    # build_page_context (e.g. mid-crawl, before storage/route_filter are
    # needed) on these heavier modules.
    from route_filter import normalize_urls
    from storage import get_store

    store = get_store()
    links = store.read_links(domain)
    if not links:
        return 0

    filenames = store.list_page_context_filenames(domain)
    if not filenames:
        return 0

    # url -> context filename, so incoming anchors can be attached by target.
    contexts: Dict[str, Dict[str, Any]] = {}
    url_to_filename: Dict[str, str] = {}
    for filename in filenames:
        ctx = store.read_page_context(domain, filename)
        if not ctx:
            continue
        contexts[filename] = ctx
        url_to_filename[ctx.get("url", "")] = filename

    anchors_for: Dict[str, List[str]] = {}
    for source_page, page_links in links.items():
        for link in page_links:
            targets = normalize_urls([link.get("url", "")], source_page)
            if not targets:
                continue
            anchor = _normalize_anchor(link.get("anchor", ""))
            if not anchor or anchor.lower() in _GENERIC_ANCHORS:
                continue
            anchors_for.setdefault(targets[0], []).append(anchor)

    updated = 0
    for target_url, anchor_list in anchors_for.items():
        filename = url_to_filename.get(target_url)
        if not filename:
            continue
        # Dedupe case-insensitively, keep first-seen casing, cap at MAX_ANCHORS.
        seen_lower: set = set()
        deduped: List[str] = []
        for a in anchor_list:
            key = a.lower()
            if key in seen_lower:
                continue
            seen_lower.add(key)
            deduped.append(a)
            if len(deduped) >= MAX_ANCHORS:
                break
        if not deduped:
            continue
        contexts[filename]["anchors"] = deduped
        try:
            store.write_page_context(domain, filename, contexts[filename])
            updated += 1
        except Exception:  # noqa: BLE001 - best-effort per file
            continue
    return updated
