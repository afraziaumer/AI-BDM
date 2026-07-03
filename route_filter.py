"""
Step 3 — High-Intent Route Filtering (production-hardened).

The homepage HTML is already scraped (Step 2). This module decides which few
internal pages are worth the expensive downstream analysis. The reasoning model
NEVER sees HTML — only a small, ranked, deduplicated list of candidate URLs.

Pipeline (Parts A–E):
    A. extract_links        parse anchors + <link rel=canonical>, resolve <base>
    B. normalize_urls       protocol-relative, entity/percent decode, collapse
                            slashes, lowercase host, drop fragment, configurable
                            query handling, dedupe
    C. filter_junk          config-driven, extensible rule strategy
    D. (dedup)              enforced inside normalize_urls
    E. select_routes        deterministic pre-ranking → LLM picks ≤3 (retry +
                            heuristic fallback). Only the top-ranked shortlist is
                            sent to the model, minimizing tokens.

Public entry point:
    select_high_intent_routes(homepage_html, base_url, business_context) -> RouteResult

Reuses `_domain_key`/`_strip_tracking` from phase1_pipeline and
`get_client`/`call_llm` from LLM_planner — the planner is NOT modified.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import (
    urljoin, urlsplit, urlunsplit, parse_qsl, urlencode, unquote, quote,
)

from bs4 import BeautifulSoup

from LLM_planner import get_client, call_llm
from phase1_pipeline import _domain_key, _strip_tracking

logger = logging.getLogger("ai_bdm.route_filter")

MAX_SELECTED_URLS = 3          # hard cap on pages sent downstream
MAX_LLM_CANDIDATES = 20        # only the top-N ranked URLs are shown to the model
ROUTING_MODEL = "openai/gpt-oss-20b"  # documented; call_llm uses this as primary


# ===========================================================================
# Exceptions
# ===========================================================================
class RouteFilterError(Exception):
    """Base error for Step 3."""


class LLMRoutingError(RouteFilterError):
    """The routing model failed or returned unusable output after a retry."""


# ===========================================================================
# Result model
# ===========================================================================
@dataclass
class CandidateLink:
    """A candidate internal page with rich, local context for the planner.

    We already parse the HTML with BeautifulSoup, so we harvest every cheap
    semantic signal around each anchor — anchor text, title, aria-label, the
    nearest section heading, and where the link sits (header/nav/footer/main).
    This lets the routing LLM understand a link like `/book` labelled
    "Reserve your berth" under a "Reservations" heading WITHOUT crawling it,
    while staying a tiny fraction of the size of the raw HTML.
    """
    url: str                       # normalized, absolute
    anchor_text: str = ""          # visible link text ("Book Now", "Reserve")
    internal: bool = True          # same registered domain as the site
    title: str = ""                # <a title="..."> attribute
    aria_label: str = ""           # <a aria-label="..."> (accessibility label)
    heading: str = ""              # nearest preceding section heading (h1–h6)
    section: str = "body"          # header | nav | footer | main | aside | body

    @property
    def label(self) -> str:
        """Best available human-readable label for the link."""
        return self.anchor_text or self.aria_label or self.title

    def as_context(self) -> Dict[str, Any]:
        """Compact, non-empty context object sent to the routing model."""
        ctx: Dict[str, Any] = {"url": self.url}
        if self.label:
            ctx["anchor"] = self.label
        if self.heading:
            ctx["section_heading"] = self.heading
        if self.section and self.section != "body":
            ctx["location"] = self.section
        return ctx

    def as_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url, "anchor_text": self.anchor_text,
            "title": self.title, "aria_label": self.aria_label,
            "heading": self.heading, "section": self.section,
            "internal": self.internal,
        }


@dataclass
class RouteResult:
    base_url: str
    extracted_urls: List[str] = field(default_factory=list)          # normalized
    candidate_links: List[CandidateLink] = field(default_factory=list)  # post junk-filter
    # Selected pages, richest-first: [{"url", "priority", "reason"}].
    selected: List[Dict[str, Any]] = field(default_factory=list)
    selection_method: str = "llm"     # "llm" | "heuristic_fallback" | "trivial"

    @property
    def selected_urls(self) -> List[str]:
        """Convenience: just the selected URLs in priority order."""
        return [s["url"] for s in self.selected]

    @property
    def candidate_urls(self) -> List[str]:
        return [c.url for c in self.candidate_links]


# ===========================================================================
# Extensible junk-filter configuration
# ===========================================================================
@dataclass(frozen=True)
class JunkFilterConfig:
    """Declarative, extensible filtering rules.

    Extend by constructing a new config (e.g. `replace()`-style) rather than
    editing code — every rule is data, matched positionally against a URL's
    scheme / host / path segments / extension.
    """
    bad_schemes: frozenset = frozenset({
        "mailto", "javascript", "tel", "sms", "data", "ftp", "file", "callto",
        "skype", "whatsapp", "viber",
    })
    bad_extensions: frozenset = frozenset({
        # images / video / audio
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
        ".tiff", ".avif", ".mp4", ".webm", ".avi", ".mov", ".wmv", ".mkv",
        ".mp3", ".wav", ".ogg", ".flac",
        # documents / archives
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt",
        ".zip", ".rar", ".gz", ".tar", ".7z", ".dmg", ".exe", ".pkg",
        # code / data / fonts / calendar
        ".css", ".js", ".mjs", ".json", ".xml", ".rss", ".atom", ".ics",
        ".woff", ".woff2", ".ttf", ".eot", ".otf", ".map",
    })
    # Path segment tokens (matched against hyphen/slash-split segments).
    bad_path_tokens: frozenset = frozenset({
        "privacy", "privacy-policy", "terms", "tos", "terms-of-service",
        "terms-and-conditions", "cookie", "cookies", "cookie-policy", "legal",
        "gdpr", "disclaimer", "impressum", "accessibility",
        "careers", "career", "jobs", "job", "vacancies", "hiring",
        "blog", "news", "press", "media", "press-release", "newsroom",
        "rss", "feed", "feeds", "atom", "sitemap", "robots", "favicon",
        "logout", "signout", "sign-out", "unsubscribe",
        "cart", "checkout-success", "wishlist", "compare",
        "share", "download", "downloads", "print",
        "search", "tag", "tags", "category", "categories", "archive", "archives",
        "author", "wp-admin", "wp-login", "wp-json", "xmlrpc", "cdn-cgi",
        "assets", "static", "fonts", "media-library",
    })
    # Off-site hosts always dropped (social / chat / app stores / CDNs).
    offsite_domains: frozenset = frozenset({
        "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com",
        "linkedin.com", "youtube.com", "youtu.be", "tiktok.com", "pinterest.com",
        "reddit.com", "snapchat.com", "threads.net",
        "wa.me", "whatsapp.com", "t.me", "telegram.me", "telegram.org",
        "google.com", "goo.gl", "maps.google.com", "g.page",
        "apps.apple.com", "itunes.apple.com", "play.google.com",
        "gravatar.com", "cloudflare.com", "gstatic.com", "googleapis.com",
        "doubleclick.net", "googletagmanager.com",
    })


DEFAULT_JUNK_CONFIG = JunkFilterConfig()

# High-intent keywords → weight. Used for deterministic pre-LLM ranking and the
# fallback selector. Higher weight = stronger buy/gap signal.
_INTENT_WEIGHTS: Dict[str, int] = {
    "menu": 105, "menus": 105, "food-menu": 110, "restaurant-menu": 110,
    "booking": 100, "book": 90, "appointment": 100, "appointments": 100,
    "reserve": 95, "reservation": 95, "reservations": 95, "schedule": 90,
    "scheduling": 90, "online-ordering": 95, "order": 70, "checkout": 85,
    "pricing": 90, "price": 80, "plans": 80, "packages": 70, "rates": 70,
    "services": 75, "service": 70, "products": 70, "product": 65,
    "membership": 80, "member": 60, "subscribe": 60, "subscription": 65,
    "portal": 90, "client-portal": 100, "customer-portal": 100,
    "patient-portal": 100, "patient": 70, "client": 60, "customer": 60,
    "dashboard": 85, "platform": 80, "software": 80, "solutions": 75,
    "app": 85, "apps": 85, "mobile-app": 95, "ios": 80, "android": 80,
    "play": 60, "store": 55, "download-app": 90, "quote": 75, "signup": 70,
    "register": 65, "get-started": 70, "demo": 70, "contact": 50,
}
# Negative keywords that reduce a URL's priority (still allowed, just deprioritized).
_LOW_INTENT_TOKENS: frozenset = frozenset({
    "about", "history", "team", "gallery", "events", "faq", "testimonials",
    "reviews", "story", "mission", "values", "partners", "awards",
})


# ===========================================================================
# Part A — Link extraction (resilient to malformed HTML)
# ===========================================================================
# ASCII control chars + invisible/zero-width/directional marks + word-joiner + BOM.
# Built from explicit codepoint ranges so the source stays pure ASCII.
_INVISIBLE_CODEPOINTS = (
    list(range(0x00, 0x20))          # C0 control chars
    + [0x7F]                          # DEL
    + list(range(0x200B, 0x2010))    # ZWSP..RLM (zero-width + bidi marks)
    + list(range(0x202A, 0x202F))    # bidi embedding/override marks
    + [0x2060, 0xFEFF]               # word joiner, BOM / ZWNBSP
)
_CONTROL_CHARS_RE = re.compile("[" + "".join(map(chr, _INVISIBLE_CODEPOINTS)) + "]")


def _clean_href(raw: str) -> str:
    """Decode HTML entities, strip whitespace/control chars from an href."""
    return _CONTROL_CHARS_RE.sub("", _html.unescape(raw)).strip()


def extract_links(homepage_html: str) -> List[str]:
    """Parse the homepage and return anchor hrefs plus the canonical URL.

    Resilient to malformed markup (lxml recovers), missing hrefs, and empty/
    fragment-only links. Returns raw (unnormalized) href strings — normalization
    is a separate, testable step.
    """
    if not homepage_html:
        return []
    soup = BeautifulSoup(homepage_html, "lxml")

    hrefs: List[str] = []
    for a in soup.find_all("a", href=True):
        href = _clean_href(a["href"])
        if href and not href.startswith("#"):
            hrefs.append(href)

    # Canonical URL is a strong signal for the site's primary address.
    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    if canonical and canonical.get("href"):
        hrefs.append(_clean_href(canonical["href"]))

    return hrefs


def _base_href(homepage_html: str) -> Optional[str]:
    """Return the <base href> if the page declares one (affects relative URLs)."""
    if not homepage_html:
        return None
    soup = BeautifulSoup(homepage_html, "lxml")
    base = soup.find("base", href=True)
    return _clean_href(base["href"]) if base else None


# ===========================================================================
# Part B + D — Normalization and deduplication
# ===========================================================================
def _normalize_path(path: str) -> str:
    """Collapse duplicate slashes, decode escapes, re-encode unsafe chars."""
    if not path:
        return "/"
    # Decode percent-encoding, collapse duplicate slashes, then re-encode
    # unsafe characters such as spaces so the returned URL is still valid.
    path = unquote(path)
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return quote(path or "/", safe="/:@!$&'()*+,;=-._~")


def normalize_urls(
    hrefs: Sequence[str],
    base_url: str,
    *,
    drop_query: bool = False,
    keep_params: Optional[frozenset] = None,
    lowercase_path: bool = True,
) -> List[str]:
    """Resolve to absolute, canonicalize, and dedupe URLs.

    Handles: relative + nested-relative (`../`), protocol-relative (`//host`),
    entity-encoded (`&amp;`) and percent-encoded chars, duplicate slashes,
    mixed-case hosts, fragments, and whitespace.

    Query handling is configurable:
      - default            : strip only tracking params (utm_*, gclid…)
      - drop_query=True     : remove the query entirely
      - keep_params=frozenset({"id"}) : keep only these params (+ drop tracking)

    Order is preserved; duplicates (post-canonicalization) are removed.
    """
    seen: set = set()
    out: List[str] = []
    for raw in hrefs:
        href = _clean_href(raw)
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        try:
            absolute = urljoin(base_url, href)
            parts = urlsplit(absolute)
        except ValueError:
            continue  # malformed beyond recovery
        if parts.scheme not in ("http", "https"):
            continue

        host = parts.netloc.lower()
        # Strip default ports so :80/:443 don't create duplicate canonical URLs.
        if parts.scheme == "http" and host.endswith(":80"):
            host = host[:-3]
        elif parts.scheme == "https" and host.endswith(":443"):
            host = host[:-4]
        path = _normalize_path(parts.path)
        if lowercase_path:
            path = path.lower()

        # Query handling.
        if drop_query or not parts.query:
            query = ""
        else:
            pairs = parse_qsl(parts.query, keep_blank_values=True)
            if keep_params is not None:
                pairs = [(k, v) for k, v in pairs if k in keep_params]
            query = urlencode(pairs)

        rebuilt = urlunsplit((parts.scheme, host, path, query, ""))
        canonical = _strip_tracking(rebuilt)     # drop utm/gclid/etc.
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


# ===========================================================================
# Part C — Junk removal (config-driven, extensible)
# ===========================================================================
def _path_segments(path: str) -> List[str]:
    """Split a path into hyphen/slash-delimited tokens for precise matching."""
    tokens: List[str] = []
    for seg in path.lower().split("/"):
        if seg:
            tokens.extend(seg.split("-"))
            tokens.append(seg)  # also keep the whole segment (e.g. "privacy-policy")
    return tokens


def is_junk(url: str, base_domain: str, config: JunkFilterConfig = DEFAULT_JUNK_CONFIG) -> bool:
    """Return True if a URL should be discarded before ranking/LLM."""
    parts = urlsplit(url)
    if parts.scheme in config.bad_schemes:
        return True
    host = parts.netloc.lower().removeprefix("www.")
    path_low = parts.path.lower()

    # Off-domain / social / CDN.
    if _domain_key(url) != base_domain:
        return True
    if any(host == d or host.endswith("." + d) for d in config.offsite_domains):
        return True
    # Asset / document extensions.
    if any(path_low.endswith(ext) for ext in config.bad_extensions):
        return True
    # Low-value path tokens.
    segments = set(_path_segments(parts.path))
    if segments & config.bad_path_tokens:
        return True
    return False


def filter_junk(
    urls: Sequence[str], base_url: str, config: JunkFilterConfig = DEFAULT_JUNK_CONFIG
) -> List[str]:
    """Keep only useful, same-domain content pages (see JunkFilterConfig)."""
    base_domain = _domain_key(base_url)
    return [u for u in urls if not is_junk(u, base_domain, config)]


# ===========================================================================
# Deterministic URL scoring (pre-LLM ranking + fallback)
# ===========================================================================
def score_url(url: str) -> int:
    """Score a URL by high-intent path keywords. Higher = more useful."""
    segments = _path_segments(urlsplit(url).path)
    seg_set = set(segments)
    score = 0
    for token, weight in _INTENT_WEIGHTS.items():
        # token may be multi-part ("client-portal"); check whole-string presence.
        if token in seg_set or (("-" in token) and token in urlsplit(url).path.lower()):
            score += weight
    if seg_set & _LOW_INTENT_TOKENS:
        score -= 40
    # Prefer shallower pages (fewer path segments) as tie-breaker.
    depth = urlsplit(url).path.strip("/").count("/")
    score -= depth
    return score


def rank_urls(urls: Sequence[str]) -> List[Tuple[str, int]]:
    """Return URLs sorted by descending score, with their scores."""
    return sorted(((u, score_url(u)) for u in urls), key=lambda t: t[1], reverse=True)


# ===========================================================================
# Candidate extraction WITH rich local context — single HTML parse
# ===========================================================================
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# Landmark tags / ARIA roles / id-class hints -> normalized section name.
_SECTION_BY_TAG = {"header": "header", "nav": "nav", "footer": "footer",
                   "main": "main", "aside": "aside"}
_SECTION_BY_ROLE = {"banner": "header", "navigation": "nav",
                    "contentinfo": "footer", "main": "main"}


def _link_section(a_tag: Any) -> str:
    """Where the anchor lives: header / nav / footer / main / aside / body.

    Walks ancestors and checks landmark tags, ARIA roles, then id/class hints.
    Returns the first (closest) match, else 'body'.
    """
    for parent in a_tag.parents:
        name = getattr(parent, "name", None)
        if name in _SECTION_BY_TAG:
            return _SECTION_BY_TAG[name]
        get = getattr(parent, "get", None)
        if get is None:
            continue
        role = (get("role") or "").lower()
        if role in _SECTION_BY_ROLE:
            return _SECTION_BY_ROLE[role]
        ident = f"{get('id', '') or ''} {' '.join(get('class', []) or [])}".lower()
        if "footer" in ident:
            return "footer"
        if "navbar" in ident or "topbar" in ident or "masthead" in ident:
            return "header"
        if "nav" in ident or "menu" in ident:
            return "nav"
    return "body"


def _headings_by_anchor(soup: BeautifulSoup) -> Dict[int, str]:
    """Map each anchor -> nearest preceding heading text WITHIN THE SAME SECTION,
    in one document-order pass. Scoping to the section prevents a footer link
    from inheriting a heading that belonged to the main content above it.
    """
    current_text = ""
    current_section: Optional[str] = None
    mapping: Dict[int, str] = {}
    for el in soup.find_all(_HEADING_TAGS + ("a",)):
        if el.name in _HEADING_TAGS:
            current_text = _clean_href(el.get_text(" ", strip=True))[:120]
            current_section = _link_section(el)
        elif el.has_attr("href") and current_text:
            if _link_section(el) == current_section:   # same section only
                mapping[id(el)] = current_text
    return mapping


def _extract_candidates(
    homepage_html: str, base_url: str, config: JunkFilterConfig = DEFAULT_JUNK_CONFIG
) -> Tuple[List[str], List[CandidateLink]]:
    """Parse the homepage ONCE and return (all normalized URLs, junk-filtered
    internal candidates enriched with anchor text, title, aria-label, nearest
    heading, and section). One parse keeps this cheap at scale."""
    if not homepage_html:
        return [], []
    soup = BeautifulSoup(homepage_html, "lxml")

    base_tag = soup.find("base", href=True)
    declared_base = _clean_href(base_tag["href"]) if base_tag else None
    effective_base = urljoin(base_url, declared_base) if declared_base else base_url
    base_domain = _domain_key(base_url)
    heading_of = _headings_by_anchor(soup)

    seen_norm: set = set()
    normalized: List[str] = []
    best: Dict[str, CandidateLink] = {}

    for a in soup.find_all("a", href=True):
        href = _clean_href(a["href"])
        if not href or href.startswith("#"):
            continue
        norm = normalize_urls([href], effective_base)
        if not norm:
            continue
        url = norm[0]
        if url not in seen_norm:
            seen_norm.add(url)
            normalized.append(url)
        if _domain_key(url) != base_domain or is_junk(url, base_domain, config):
            continue

        anchor = _clean_href(a.get_text(" ", strip=True))[:120]
        cur = best.get(url)
        if cur is None:
            best[url] = CandidateLink(
                url=url, anchor_text=anchor, internal=True,
                title=_clean_href(a.get("title", "") or "")[:120],
                aria_label=_clean_href(a.get("aria-label", "") or "")[:120],
                heading=heading_of.get(id(a), ""),
                section=_link_section(a),
            )
        else:
            # Merge: keep the richest anchor text and fill any missing context.
            if len(anchor) > len(cur.anchor_text):
                cur.anchor_text = anchor
            if not cur.heading:
                cur.heading = heading_of.get(id(a), "")
            if cur.section == "body":
                cur.section = _link_section(a)

    # Canonical URL is a useful root signal even without anchor context.
    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    if canonical and canonical.get("href"):
        norm = normalize_urls([_clean_href(canonical["href"])], effective_base)
        if norm:
            url = norm[0]
            if url not in seen_norm:
                seen_norm.add(url)
                normalized.append(url)
            if (_domain_key(url) == base_domain and not is_junk(url, base_domain, config)
                    and url not in best):
                best[url] = CandidateLink(url=url, anchor_text="", internal=True)

    return normalized, list(best.values())


def extract_candidate_links(
    homepage_html: str, base_url: str, config: JunkFilterConfig = DEFAULT_JUNK_CONFIG
) -> List[CandidateLink]:
    """Normalized, deduped, junk-filtered internal candidates with anchor text."""
    return _extract_candidates(homepage_html, base_url, config)[1]


def score_candidate(link: CandidateLink) -> int:
    """Deterministic relevance score from URL + all local context signals.

    Anchor text, aria-label and the nearby heading ("Reservations") are strong
    semantic cues a bare URL may lack (e.g. "/p/1234"), so they add to the score.
    """
    score = score_url(link.url)
    text = f"{link.anchor_text} {link.aria_label} {link.title} {link.heading}".lower().strip()
    if text:
        for token, weight in _INTENT_WEIGHTS.items():
            if token in text or token.replace("-", " ") in text:
                score += weight // 2       # context = strong but secondary signal
        if any(t in text for t in _LOW_INTENT_TOKENS):
            score -= 20
    return score


def classify_link(link: CandidateLink) -> Tuple[str, str]:
    """Return (confidence, reason) grounded ONLY in navigation signals.

    Keeps Step 3 honest: it reports WHY a page looks promising from its link
    metadata, never speculating about page contents it hasn't crawled.
      high   — an intent keyword appears in the anchor/aria/heading text
      medium — an intent keyword appears in the URL path
      low    — no explicit signal; a generic link that merely might be relevant
    """
    label = f"{link.anchor_text} {link.aria_label} {link.heading}".lower()
    path = urlsplit(link.url).path.lower()
    best: Optional[Tuple[int, str, str, str]] = None  # (weight, conf, token, where)
    for token, weight in _INTENT_WEIGHTS.items():
        phrase = token.replace("-", " ")
        if phrase in label or token in label:
            cand = (weight, "high", token, "anchor/heading text")
        elif token in path or phrase in path:
            cand = (weight, "medium", token, "URL path")
        else:
            continue
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        return "low", "generic navigation link with no explicit intent signal"
    _, confidence, token, where = best
    return confidence, f"{where} signals '{token}'"


# ===========================================================================
# Part E/F/G — LLM route selection with knowledge gaps, reasons, validation
# ===========================================================================
_ROUTING_SYSTEM_PROMPT = (
    "You are a NAVIGATION PLANNER for a web crawler. You never see page content "
    "— only compact navigation metadata for each candidate internal link "
    "(url, anchor text, the section heading it sits under, and its location such "
    "as header/nav/footer).\n\n"
    "Your job: given the MISSING FIELDS the user still needs, choose the 2-3 pages "
    "MOST LIKELY to answer those fields. Return ONLY pages relevant to the missing "
    "fields.\n\n"
    "Prefer OFFICIAL BUSINESS WEBSITE routes in this order when they exist: "
    "1) official business page/home route, 2) official contact page, "
    "3) official menu/services/products page, 4) official booking/reservation page. "
    "Avoid review sites, aggregators, directories, listicles and ranking pages "
    "unless the user explicitly asks for them.\n\n"
    "For each chosen page, give an HONEST confidence grounded ONLY in the "
    "navigation signal you can see — never speculate about contents you have not "
    "crawled:\n"
    "  \"high\"   — the anchor text / heading explicitly names it (e.g. 'Book "
    "Now', 'Reserve', 'Download App').\n"
    "  \"medium\" — the URL path or heading suggests it (e.g. '/contact', a "
    "'Reservations' heading).\n"
    "  \"low\"    — a generic link that only MIGHT be relevant.\n\n"
    "Respond with ONLY valid JSON of exactly this form:\n"
    '{"selected": [{"url": "<url from the list>", "priority": 1, '
    '"confidence": "high", "reason": "anchor text says \'Reserve Berth\'"}]}\n'
    "At most 3 objects. priority 1 = most important. Use ONLY URLs from the list."
)

_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


def _loads_forgiving(text: str) -> Optional[Any]:
    """Best-effort JSON parse: whole string, then first embedded object/array."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _coerce_routes(data: Any) -> List[Dict[str, Any]]:
    """Normalize the model's payload into a list of {url, priority, reason}."""
    items: Optional[list] = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("selected", "routes", "selected_urls", "pages", "results"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    if items is None:
        return []
    routes: List[Dict[str, Any]] = []
    for it in items:
        if isinstance(it, str):
            routes.append({"url": it, "priority": None, "confidence": None, "reason": ""})
        elif isinstance(it, dict) and isinstance(it.get("url"), str):
            conf = str(it.get("confidence", "")).strip().lower()
            routes.append({
                "url": it["url"],
                "priority": it.get("priority") if isinstance(it.get("priority"), int) else None,
                "confidence": conf if conf in _VALID_CONFIDENCE else None,
                "reason": str(it.get("reason", ""))[:200],
            })
    return routes


def _parse_routes(
    text: str, by_url: Dict[str, CandidateLink]
) -> Optional[List[Dict[str, Any]]]:
    """Validate + normalize model output to ≤3 {url, priority, confidence, reason}.

    Only URLs from the candidate set survive. Missing/invalid confidence or
    reason is backfilled deterministically from the link's own navigation
    signals (classify_link), so every route is honest and complete.
    """
    data = _loads_forgiving(text)
    if data is None:
        return None
    seen: set = set()
    valid: List[Dict[str, Any]] = []
    for route in _coerce_routes(data):
        url = route["url"]
        if url in by_url and url not in seen:
            seen.add(url)
            valid.append(route)
    if not valid:
        return None
    valid.sort(key=lambda r: r["priority"] if r["priority"] is not None else 99)
    valid = valid[:MAX_SELECTED_URLS]
    for i, route in enumerate(valid, 1):
        route["priority"] = i
        # The LLM SELECTS pages (semantic routing), but confidence + reason are
        # derived from the actual navigation signals — never the model's
        # speculation about contents it hasn't crawled. This keeps Step 3 honest
        # and separates navigation planning from content verification.
        route["confidence"], route["reason"] = classify_link(by_url[route["url"]])
    return valid


def _heuristic_routes(candidates: Sequence[CandidateLink]) -> List[Dict[str, Any]]:
    """Deterministic fallback: top candidates by score, with confidence + reason."""
    ranked = sorted(candidates, key=score_candidate, reverse=True)[:MAX_SELECTED_URLS]
    routes: List[Dict[str, Any]] = []
    for i, link in enumerate(ranked, 1):
        confidence, reason = classify_link(link)
        routes.append({"url": link.url, "priority": i,
                       "confidence": confidence, "reason": reason})
    return routes


def select_routes(
    candidates: Sequence[CandidateLink],
    user_query: str = "",
    knowledge_gaps: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Pick ≤3 pages as [{url, priority, reason}]. Deterministic pre-rank → LLM
    (retry once) → deterministic fallback.

    Only the top MAX_LLM_CANDIDATES ranked candidates are shown to the model, and
    the LLM is skipped when there are already ≤ MAX_SELECTED_URLS candidates —
    both minimize token cost and latency.

    Returns (routes, method) with method ∈ {"llm","heuristic_fallback","trivial"}.
    """
    candidates = list(candidates)
    if not candidates:
        return [], "trivial"
    if len(candidates) <= MAX_SELECTED_URLS:
        return _heuristic_routes(candidates), "trivial"   # nothing to choose

    shortlist = sorted(candidates, key=score_candidate, reverse=True)[:MAX_LLM_CANDIDATES]
    by_url = {c.url: c for c in shortlist}

    gaps = list(knowledge_gaps or [])
    # Compact, information-rich JSON per link (url + anchor + heading + location).
    link_json = json.dumps([c.as_context() for c in shortlist], ensure_ascii=False)
    missing = ", ".join(gaps) if gaps else (user_query or "the user's information needs")
    user_content = (
        (f"User goal: {user_query}\n" if user_query else "")
        + f"Missing fields to find: {missing}\n\n"
        + f"Candidate links (JSON):\n{link_json}\n\n"
        + "Return only pages relevant to the missing fields."
    )
    messages = [
        {"role": "system", "content": _ROUTING_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001 - no client → deterministic fallback
        logger.warning("LLM client unavailable, using heuristic routing: %s", exc)
        return _heuristic_routes(shortlist), "heuristic_fallback"

    for attempt in (1, 2):  # initial + one retry (Part G)
        try:
            raw = call_llm(client, messages, response_format={"type": "json_object"})
            routes = _parse_routes(raw, by_url)
            if routes:
                logger.info("Routing model selected %d page(s) on attempt %d.",
                            len(routes), attempt)
                return routes, "llm"
            logger.warning("Routing model returned invalid/empty JSON (attempt %d).", attempt)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash Step 3
            logger.warning("Routing model call failed (attempt %d): %s", attempt, exc)

    logger.info("Falling back to deterministic route selection.")
    return _heuristic_routes(shortlist), "heuristic_fallback"


# ===========================================================================
# Orchestrator — the single public entry point for Step 3
# ===========================================================================
def select_high_intent_routes(
    homepage_html: str,
    base_url: str,
    user_query: str = "",
    knowledge_gaps: Optional[Sequence[str]] = None,
) -> RouteResult:
    """Run Parts A–G and return the full RouteResult.

    Args:
        homepage_html: the homepage HTML from Step 2 (raw store).
        base_url:      the site's base URL (for relative-link resolution).
        user_query:    the end user's goal (e.g. "marinas with booking + email").
        knowledge_gaps: the still-missing fields (e.g. ["Booking URL",
                        "Contact Email", "Mobile Application"]).

    Returns a RouteResult whose `.selected` is a priority-ordered list of routes.
    """
    normalized, candidates = _extract_candidates(homepage_html, base_url)
    logger.info(
        "Step 3: %d normalized links -> %d internal candidates (%s)",
        len(normalized), len(candidates), base_url,
    )
    selected, method = select_routes(candidates, user_query, knowledge_gaps)
    return RouteResult(
        base_url=base_url,
        extracted_urls=normalized,
        candidate_links=candidates,
        selected=selected,
        selection_method=method,
    )


# ===========================================================================
# CLI runner — load the homepage HTML for Step 3 from the raw store
# ===========================================================================
RAW_STORE = "scavenger_leads_cache.csv"


def load_homepage_from_store(
    website: str, csv_path: Optional[str] = None
) -> Optional[Tuple[str, str]]:
    """Return (base_url, html) for a business's homepage.

    Reads `scavenger_leads_cache.csv` (raw scraped HTML, Step 2). Matches on
    registered domain and prefers the row whose page_url is the site root, else
    any stored page of that site.
    """
    import csv as _csv
    import os as _os
    import sys as _sys
    _csv.field_size_limit(min(_sys.maxsize, 2**31 - 1))

    want = _domain_key(website)
    path = csv_path or RAW_STORE
    if not _os.path.exists(path):
        return None

    fallback: Optional[Tuple[str, str]] = None
    with open(path, newline="", encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            site = (row.get("website_url") or "").strip()
            if not site or _domain_key(site) != want:
                continue
            html = (row.get("raw_html") or "").strip()
            if not html:
                continue
            page = (row.get("page_url") or site).rstrip("/")
            if page == site.rstrip("/"):
                return site, html                # exact homepage
            if fallback is None:
                fallback = (site, html)
    if fallback:
        return fallback
    return None


def _print_result(result: RouteResult) -> None:
    print("\n" + "=" * 66)
    print("STEP 3 — HIGH-INTENT ROUTE FILTERING")
    print("=" * 66)
    print(f"Site             : {result.base_url}")
    print(f"Normalized links : {len(result.extracted_urls)}")
    print(f"Candidates       : {len(result.candidate_links)}")
    print(f"Selection method : {result.selection_method}")
    print("-" * 66)
    print("TOP CANDIDATES (score | url | anchor | section/heading):")
    for link in sorted(result.candidate_links, key=score_candidate, reverse=True)[:10]:
        print(f"   {score_candidate(link):>4}  {link.url}")
        print(f"         anchor={link.label or '-'!r}  section={link.section}"
              + (f"  heading={link.heading!r}" if link.heading else ""))
    print("-" * 66)
    print(f"SELECTED HIGH-INTENT PAGES ({len(result.selected)}):")
    for s in result.selected:
        conf = s.get("confidence", "")
        print(f"  [{s['priority']}] ({conf}) {s['url']}")
        print(f"       reason: {s.get('reason', '')}")
    print("=" * 66 + "\n")


def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Step 3 — high-intent route filtering (link extraction + "
                    "normalization + junk filtering + ranking + LLM routing).")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--website", help="Load this business's homepage HTML from the raw store CSV.")
    source.add_argument("--url", help="Fetch this URL live (requests) and run Step 3.")
    source.add_argument("--list", action="store_true", help="List businesses available in the CSV.")
    parser.add_argument("--csv", default=RAW_STORE, help="Raw store path.")
    parser.add_argument("--query", default="", help="User goal / query.")
    parser.add_argument("--gaps", default="",
                        help="Comma-separated knowledge gaps, e.g. "
                             "'Booking URL,Contact Email,Mobile Application'.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    if args.list:
        import csv as _csv
        import sys as _sys
        _csv.field_size_limit(min(_sys.maxsize, 2**31 - 1))
        seen: set = set()
        with open(args.csv, newline="", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                dom = _domain_key((row.get("website_url") or ""))
                if dom and dom not in seen and (row.get("raw_html") or "").strip():
                    seen.add(dom)
                    print("  ", row.get("website_url"))
        print(f"\n{len(seen)} businesses with homepage HTML in {args.csv}")
        return

    gaps = [g.strip() for g in args.gaps.split(",") if g.strip()]

    if args.website:
        loaded = load_homepage_from_store(args.website, args.csv)
        if not loaded:
            parser.error(f"No homepage HTML found for '{args.website}' in {args.csv}. "
                         f"Try --list to see available sites.")
        base_url, html = loaded
    else:  # --url : live fetch
        import requests
        from phase1_pipeline import BROWSER_HEADERS
        resp = requests.get(args.url, headers=BROWSER_HEADERS, timeout=20)
        resp.raise_for_status()
        html, base_url = resp.text, args.url

    result = select_high_intent_routes(html, base_url, args.query, gaps)
    _print_result(result)


if __name__ == "__main__":
    _cli()
