"""
Website Classifier — decide whether a homepage deserves a DEEP crawl.

Runs once per newly-discovered site, right after the homepage is fetched (and
right after Wappalyzer has fingerprinted it) — BEFORE the crawl planner is
asked to plan a deep crawl and before any further page is fetched. Answers one
question: is this the official website of ONE business, or a directory /
aggregator / marketplace / travel-guide / review-site / listing-portal /
unrelated informational page that merely LISTS or MENTIONS many businesses?

    Official business website  -> deep_crawl          (crawl_planner runs as before)
    Directory / aggregator /
    marketplace / travel guide /
    tourism / review / listing  -> extract_businesses  (mine its homepage links
                                                         for real business URLs,
                                                         queue each as its OWN
                                                         crawl job; never store
                                                         this domain itself)
    News / government / blog /
    unrelated                   -> reject              (same handling as
                                                         extract_businesses —
                                                         mining is attempted but
                                                         will usually yield
                                                         nothing; never deep-
                                                         crawled or stored)

This closes the exact gap the discovery_classifier.py URL/title heuristic
can't: that classifier runs BEFORE anything is fetched, so it only catches
known brands or obviously listicle-shaped URLs/titles. A previously-unseen
directory (e.g. a small regional travel-guide site) sails through it as
"official" and gets deep-crawled as if its metro-station/venue-listing pages
were business intelligence about ONE company. This module catches that AFTER
the homepage's real content is available, using cheap deterministic signals
first and an LLM only when genuinely ambiguous — never the other way around.

Hybrid architecture (Step 2 of the redesign):
    extract_homepage_signals   cheap, deterministic homepage signals
        -> rule_based_score     0-100 "directory-likeness" score
        -> confident either way?  skip the LLM entirely (most sites land here)
        -> ambiguous?            compact summary sent to the LLM for a
                                  category + confidence + recommended_action
        -> LLM unavailable/bad?  fail OPEN to deep_crawl — a classifier bug or
                                  outage can only ever waste credits on an
                                  undetected directory, never drop a real
                                  business (same invariant as crawl_planner)

Caching: the decision is a fact about the SITE, not the query, so it's cached
by domain alone (site_classifications.json, TTL-bounded) — independent of
storage.py's per-business staging, since a directory never gets a business
storage folder to cache it alongside.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from bs4 import BeautifulSoup

import discovery_classifier as dc
from discovery_classifier import LISTICLE_TITLE_RE, domain_key
from LLM_planner import call_llm, get_client

logger = logging.getLogger("ai_bdm.website_classifier")

CLASSIFICATION_CACHE_FILE = "site_classifications.json"
CACHE_TTL_DAYS = 30
MAX_BUSINESSES_TO_EXTRACT = 20

# Rule-based score thresholds (0-100 "directory-likeness"). Between the two,
# the signal is genuinely ambiguous and worth an LLM call.
DIRECTORY_SCORE_HIGH = 55   # >= this: confidently a directory/aggregator
DIRECTORY_SCORE_LOW = 15    # <= this: confidently a single official business


class SiteCategory(str, Enum):
    OFFICIAL_BUSINESS = "official_business_website"
    DIRECTORY = "business_directory"
    REVIEW_SITE = "review_website"
    MARKETPLACE = "marketplace"
    TRAVEL_GUIDE = "travel_guide"
    TOURISM = "tourism_website"
    GOVERNMENT = "government_website"
    NEWS = "news_website"
    BLOG = "blog"
    LISTING_PORTAL = "listing_portal"
    UNKNOWN = "unknown"


_VALID_CATEGORIES = {c.value for c in SiteCategory}
_VALID_ACTIONS = {"deep_crawl", "extract_businesses", "reject"}

# Directory/CTA phrases that overwhelmingly show up on listing/aggregator
# homepages and almost never on a single business's own site.
DIRECTORY_PHRASES = (
    "submit listing", "submit your listing", "claim business", "claim your business",
    "claim your listing", "browse categories", "browse by category", "travel guide",
    "things to do", "business listings", "business directory", "add your business",
    "get listed", "featured listings", "find businesses", "search businesses",
    "list your business", "add a listing", "explore categories",
)

SCHEMA_DIRECTORY_TYPES = {"itemlist", "collectionpage", "searchresultspage"}
SCHEMA_OFFICIAL_TYPES = {
    "localbusiness", "organization", "restaurant", "store", "hotel",
    "corporation", "professionalservice", "foodestablishment",
}

# Wappalyzer technology names/categories that indicate directory/marketplace
# software (WordPress directory plugins, listing-site builders, etc.).
_DIRECTORY_TECH_HINTS = (
    "geodirectory", "directorist", "edirectory", "directorypress",
    "brilliant directory", "sabai directory", "directoryengine", "listeo",
)


@dataclass
class HomepageSignals:
    title: str
    meta_description: str
    headings: List[str] = field(default_factory=list)
    nav_anchor_texts: List[str] = field(default_factory=list)
    footer_anchor_texts: List[str] = field(default_factory=list)
    outbound_domains: Set[str] = field(default_factory=set)
    schema_types: Set[str] = field(default_factory=set)
    matched_phrases: List[str] = field(default_factory=list)
    directory_tech: List[str] = field(default_factory=list)
    pattern_count: int = 0
    pattern_names: List[str] = field(default_factory=list)


def extract_homepage_signals(
    html: str, url: str, domain: str, tech_raw: Optional[Dict[str, Any]] = None,
) -> HomepageSignals:
    """Cheap, deterministic signals from the homepage's ALREADY-fetched HTML.

    Independently re-parses the same html string crawl_planner.py and
    tech_stack.py already parse for their own purposes — consistent with this
    codebase's existing pattern of each optional stage doing its own
    lightweight, decoupled parse rather than sharing a mutable soup object.
    """
    from crawl_planner import detect_patterns, extract_homepage_candidates

    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = (
        soup.find("meta", attrs={"name": "description"})
        or soup.find("meta", attrs={"property": "og:description"})
    )
    meta_desc = (meta.get("content", "") if meta else "").strip()
    headings = [
        h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])
    ][:20]

    nav_anchors: List[str] = []
    for nav in soup.find_all("nav"):
        nav_anchors.extend(a.get_text(" ", strip=True) for a in nav.find_all("a"))
    footer_anchors: List[str] = []
    for footer in soup.find_all("footer"):
        footer_anchors.extend(a.get_text(" ", strip=True) for a in footer.find_all("a"))

    outbound_domains: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().startswith("http"):
            continue
        d = domain_key(href)
        if d and d != domain:
            outbound_domains.add(d)

    schema_types: Set[str] = set()
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            if isinstance(t, list):
                schema_types.update(str(x).lower() for x in t)
            elif t:
                schema_types.add(str(t).lower())

    text_blob = " ".join([title, meta_desc] + headings).lower()
    matched_phrases = [p for p in DIRECTORY_PHRASES if p in text_blob]

    directory_tech: List[str] = []
    for name, info in (tech_raw or {}).items():
        low = name.lower()
        if any(hint in low for hint in _DIRECTORY_TECH_HINTS):
            directory_tech.append(name)
            continue
        cats = [str(c).lower() for c in (info.get("categories") or [])]
        if any("directory" in c or "marketplace" in c for c in cats):
            directory_tech.append(name)

    try:
        candidates = extract_homepage_candidates(html, url, domain)
        _individual, patterns = detect_patterns(candidates)
    except Exception as exc:  # noqa: BLE001 - signal extraction is best-effort
        logger.debug("Pattern signal unavailable for %s: %s", domain, exc)
        patterns = []

    return HomepageSignals(
        title=title, meta_description=meta_desc, headings=headings,
        nav_anchor_texts=[a for a in nav_anchors if a],
        footer_anchor_texts=[a for a in footer_anchors if a],
        outbound_domains=outbound_domains, schema_types=schema_types,
        matched_phrases=matched_phrases, directory_tech=directory_tech,
        pattern_count=len(patterns), pattern_names=[p.pattern for p in patterns],
    )


def rule_based_score(signals: HomepageSignals) -> Tuple[int, List[str]]:
    """0-100 'directory-likeness' score from cheap deterministic signals."""
    score = 0
    reasons: List[str] = []

    if signals.matched_phrases:
        score += 40
        reasons.append(f"directory/CTA phrases matched: {', '.join(signals.matched_phrases[:3])}")
    if len(signals.outbound_domains) >= 15:
        score += 25
        reasons.append(f"{len(signals.outbound_domains)} distinct outbound domains")
    elif len(signals.outbound_domains) >= 8:
        score += 15
        reasons.append(f"{len(signals.outbound_domains)} distinct outbound domains")
    if signals.schema_types & SCHEMA_DIRECTORY_TYPES:
        hit = signals.schema_types & SCHEMA_DIRECTORY_TYPES
        score += 30
        reasons.append(f"schema.org type indicates a listing page ({', '.join(hit)})")
    if signals.directory_tech:
        score += 25
        reasons.append(f"directory/marketplace technology detected ({', '.join(signals.directory_tech[:3])})")
    if signals.pattern_count >= 3:
        score += 20
        reasons.append(f"{signals.pattern_count} distinct repeating URL-listing patterns on homepage")
    title_meta = f"{signals.title} {signals.meta_description}"
    if LISTICLE_TITLE_RE.search(title_meta):
        score += 15
        reasons.append("title/meta matches a listicle/guide pattern")
    if signals.schema_types & SCHEMA_OFFICIAL_TYPES:
        hit = signals.schema_types & SCHEMA_OFFICIAL_TYPES
        score -= 20
        reasons.append(f"schema.org type indicates a single business ({', '.join(hit)})")

    return max(0, min(100, score)), reasons


def _guess_category(signals: HomepageSignals) -> str:
    """Best-effort sub-category, for logging/telemetry only — the pipeline
    only ever branches on `action`, never on this."""
    blob = " ".join([signals.title, signals.meta_description] + signals.headings).lower()
    if any(p in blob for p in ("travel guide", "things to do", "itinerary", "trip planner")):
        return SiteCategory.TRAVEL_GUIDE.value
    if any(p in blob for p in ("tourism", "visitor guide")):
        return SiteCategory.TOURISM.value
    if any(p in blob for p in ("review", "reviews", "rated", "ratings")):
        return SiteCategory.REVIEW_SITE.value
    if "marketplace" in blob or any(t.lower() in ("shopify", "woocommerce") for t in signals.directory_tech):
        return SiteCategory.MARKETPLACE.value
    return SiteCategory.DIRECTORY.value


# ===========================================================================
# LLM fallback for ambiguous rule scores
# ===========================================================================
_CLASSIFIER_SYSTEM_PROMPT = (
    "You are a WEBSITE CLASSIFIER for a B2B lead-generation crawler. You are "
    "given a compact summary of ONE website's homepage (title, meta "
    "description, headings, nav/footer link text, how many distinct external "
    "domains it links to, any directory-style phrases found, detected "
    "repeating URL-listing patterns, and any directory/marketplace software "
    "detected). Decide whether this homepage belongs to:\n\n"
    "  - a single business's own official website (deep_crawl) — worth "
    "fetching more of its pages for business intelligence\n"
    "  - a directory, aggregator, marketplace, travel guide, tourism site, "
    "review site, or listing portal that discovers/lists MANY businesses but "
    "is not itself one (extract_businesses) — its links should be mined for "
    "real business websites instead of deep-crawling this site\n"
    "  - unrelated content — news, government, or a blog with no single "
    "business and no mineable business listings (reject)\n\n"
    "Respond with ONLY valid JSON of exactly this form:\n"
    '{"category": "official_business_website", "confidence": 85, '
    '"recommended_action": "deep_crawl", "reasoning": "..."}\n'
    f"category must be one of: {sorted(_VALID_CATEGORIES)}\n"
    f"recommended_action must be one of: {sorted(_VALID_ACTIONS)}"
)


def build_prompt(
    signals: HomepageSignals, url: str, user_query: str = ""
) -> List[Dict[str, str]]:
    summary = {
        "url": url,
        "title": signals.title,
        "meta_description": signals.meta_description,
        "headings": signals.headings[:10],
        "nav_links": signals.nav_anchor_texts[:20],
        "footer_links": signals.footer_anchor_texts[:20],
        "distinct_outbound_domains": len(signals.outbound_domains),
        "matched_directory_phrases": signals.matched_phrases,
        "repeating_url_patterns": signals.pattern_names[:10],
        "directory_marketplace_tech_detected": signals.directory_tech,
        "schema_org_types": sorted(signals.schema_types),
    }
    content = (
        f"User's business search context: {user_query or '(general lead collection)'}\n\n"
        f"Homepage summary (JSON):\n{json.dumps(summary, ensure_ascii=False)}\n\n"
        "Classify this website."
    )
    return [
        {"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def call_classifier_llm(messages: List[Dict[str, str]]) -> Optional[str]:
    """Call the shared Groq client. Returns None (never raises) on ANY
    failure — the caller always has the fail-open deep_crawl default."""
    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001 - no client -> fail open
        logger.info("[Classifier] LLM unavailable (%s).", exc)
        return None
    try:
        return call_llm(client, messages, response_format={"type": "json_object"})
    except Exception as exc:  # noqa: BLE001 - degrade, never break the crawl
        logger.info("[Classifier] LLM call failed (%s).", exc)
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


def parse_classifier_response(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    data = _loads_forgiving(raw)
    if not isinstance(data, dict):
        return None
    category = data.get("category")
    action = data.get("recommended_action")
    if category not in _VALID_CATEGORIES or action not in _VALID_ACTIONS:
        return None
    try:
        confidence = max(0, min(100, int(data.get("confidence", 50))))
    except (TypeError, ValueError):
        confidence = 50
    reasoning = str(data.get("reasoning", ""))[:300]
    return {
        "category": category, "confidence": confidence,
        "recommended_action": action, "reasoning": reasoning,
    }


# ===========================================================================
# Result type + orchestration
# ===========================================================================
@dataclass
class ClassificationResult:
    category: str
    confidence: int
    action: str            # "deep_crawl" | "extract_businesses" | "reject"
    method: str             # "rule_based" | "llm" | "fallback_deep_crawl" | "cached" | "error_fallback"
    reasoning: str

    @property
    def deep_crawl(self) -> bool:
        return self.action == "deep_crawl"


_cache_lock = threading.Lock()


def _load_cache() -> Dict[str, Any]:
    try:
        with open(CLASSIFICATION_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    with _cache_lock:
        with open(CLASSIFICATION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)


def _cached_entry_is_valid(entry: Dict[str, Any], content_hash: str) -> bool:
    """A cached classification is only reused if the homepage's content
    hasn't meaningfully changed AND the entry isn't older than the TTL — a
    site that gets repurposed (business -> directory, or vice versa) is
    reclassified even inside the TTL window instead of serving a stale
    verdict for up to CACHE_TTL_DAYS."""
    if entry.get("content_hash") != content_hash:
        return False
    age_days = (time.time() - entry.get("classified_at_epoch", 0)) / 86400
    return age_days <= CACHE_TTL_DAYS


def _classify_uncached(
    domain: str, url: str, html: str,
    tech_raw: Optional[Dict[str, Any]], user_query: str,
) -> ClassificationResult:
    signals = extract_homepage_signals(html, url, domain, tech_raw)
    score, reasons = rule_based_score(signals)

    if score >= DIRECTORY_SCORE_HIGH:
        category = _guess_category(signals)
        logger.info("[Classifier] %s: rule-based DIRECTORY (score=%d): %s",
                    domain, score, "; ".join(reasons))
        return ClassificationResult(
            category=category, confidence=min(95, 50 + score // 2),
            action="extract_businesses", method="rule_based",
            reasoning="; ".join(reasons) or "high directory-likeness score",
        )
    if score <= DIRECTORY_SCORE_LOW:
        logger.info("[Classifier] %s: rule-based OFFICIAL BUSINESS (score=%d).", domain, score)
        return ClassificationResult(
            category=SiteCategory.OFFICIAL_BUSINESS.value, confidence=max(60, 90 - score),
            action="deep_crawl", method="rule_based",
            reasoning="low directory-likeness score; no directory/CTA signals found",
        )

    # Ambiguous — ask the LLM. Falls back to deep_crawl (never drop a real
    # business over an ambiguous signal) if the LLM is unavailable or junk.
    logger.info("[Classifier] %s: ambiguous rule score (%d) — asking LLM.", domain, score)
    messages = build_prompt(signals, url, user_query)
    raw = call_classifier_llm(messages)
    parsed = parse_classifier_response(raw)
    if parsed is None:
        logger.info(
            "[Classifier] LLM unavailable/invalid for %s — defaulting to "
            "deep_crawl (never drop a real business on an ambiguous signal).",
            domain,
        )
        return ClassificationResult(
            category=SiteCategory.UNKNOWN.value, confidence=40,
            action="deep_crawl", method="fallback_deep_crawl",
            reasoning=f"ambiguous rule score ({score}), LLM unavailable",
        )
    logger.info("[Classifier] %s: LLM classified as %s (confidence=%d, action=%s).",
                domain, parsed["category"], parsed["confidence"], parsed["recommended_action"])
    return ClassificationResult(
        category=parsed["category"], confidence=parsed["confidence"],
        action=parsed["recommended_action"], method="llm",
        reasoning=parsed["reasoning"],
    )


def classify_homepage(
    domain: str, url: str, html: str,
    tech_raw: Optional[Dict[str, Any]] = None,
    user_query: str = "",
) -> ClassificationResult:
    """Decide whether `domain`'s homepage is an official business (deep_crawl)
    or a directory/aggregator/informational site (extract_businesses/reject).

    NEVER raises. On any internal failure, degrades to the pre-existing
    behavior (deep_crawl) — a classifier bug or outage can only ever waste
    credits on an undetected directory, never drop a real business.
    """
    content_hash = dc.compute_content_hash(html)
    try:
        cache = _load_cache()
        cached = cache.get(domain)
        if cached and _cached_entry_is_valid(cached, content_hash):
            logger.info("[Classifier] Reusing cached classification for %s: %s (%s).",
                        domain, cached["category"], cached["action"])
            return ClassificationResult(
                category=cached["category"], confidence=cached["confidence"],
                action=cached["action"], method=cached.get("method", "cached"),
                reasoning=cached.get("reasoning", ""),
            )
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        logger.warning("[Classifier] Cache read failed for %s: %s", domain, exc)

    try:
        result = _classify_uncached(domain, url, html, tech_raw, user_query)
    except Exception as exc:  # noqa: BLE001 - classification must never break the crawl
        logger.warning("[Classifier] Classification failed for %s, defaulting to "
                       "deep crawl: %s", domain, exc)
        return ClassificationResult(
            category=SiteCategory.UNKNOWN.value, confidence=0,
            action="deep_crawl", method="error_fallback",
            reasoning=f"classification error: {exc}",
        )

    try:
        cache = _load_cache()
        cache[domain] = {
            "category": result.category, "confidence": result.confidence,
            "action": result.action, "method": result.method,
            "reasoning": result.reasoning, "classified_at_epoch": time.time(),
            "content_hash": content_hash,
        }
        _save_cache(cache)
    except Exception as exc:  # noqa: BLE001 - caching is best-effort
        logger.warning("[Classifier] Failed to cache classification for %s: %s", domain, exc)

    return result


# ===========================================================================
# Business extraction (Step 3) — mine the ALREADY-fetched homepage HTML
# ===========================================================================
def mine_homepage_businesses(
    html: str, url: str, domain: str, classification: ClassificationResult,
    needed: int = MAX_BUSINESSES_TO_EXTRACT,
) -> List[Dict[str, str]]:
    """Best-effort extraction of real business links from a directory-like
    homepage's ALREADY-fetched HTML (no extra fetch). Returns plain dicts in
    the same shape QueuedBusiness.as_target() produces, ready to feed into
    run_pipeline's DiscoveryQueue.

    Empty on any failure or when the page simply has no outbound business
    links (e.g. a JS-rendered SPA whose server-rendered HTML is nearly empty)
    — NEVER raises; a directory with nothing minable is just discarded, never
    deep-crawled or stored in its own right.
    """
    try:
        confidence = (
            dc.Confidence.HIGH if classification.confidence >= 70
            else dc.Confidence.MEDIUM if classification.confidence >= 40
            else dc.Confidence.LOW
        )
        source_name = f"{classification.category} ({domain})"
        businesses = dc.extract_businesses_from_html(
            html, url, source_name, confidence, seen_hosts=set(), needed=needed,
        )
        return [b.as_target() for b in businesses]
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        logger.warning("[Classifier] Business extraction failed for %s: %s", domain, exc)
        return []
