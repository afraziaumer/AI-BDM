"""
Discovery Source Classifier — three-way search-result triage for Phase 1.

Replaces the old binary "business or reject" decision. Every search result
(organic web search, Google Maps, or a link found while mining a directory) is
classified into exactly one of:

    OFFICIAL         an individual company's own website -> queue it directly
    DISCOVERY_SOURCE a directory/listicle/association listing MANY businesses
                     -> never queued itself; its outbound links are mined and
                        THOSE become queued businesses (never recursively —
                        an outbound link that is itself another directory is
                        skipped, not mined again)
    NOISE            news, blogs, wikis, social posts, forums, PDFs, press
                     releases, generic informational pages -> rejected

Independent of the crawler by design (no import of phase1_pipeline): it only
ever hands back plain data (a classification, or a list of QueuedBusiness
records). phase1_pipeline.py's crawler receives nothing but a queue of
official website URLs — it has no idea whether a business came from organic
search, Google Maps, or a mined directory. New discovery-source types (Chamber
of Commerce, industry membership lists, LinkedIn company lists, ...) are added
by extending DISCOVERY_SOURCE_REGISTRY below — never by touching the crawler.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

import aiohttp
import tldextract
from bs4 import BeautifulSoup

logger = logging.getLogger("ai_bdm.discovery_classifier")

# Self-contained domain keying (offline public-suffix snapshot; no network
# fetch) — deliberately NOT imported from phase1_pipeline, so this module has
# zero dependency on the crawler.
_TLD = tldextract.TLDExtract(suffix_list_urls=())


def domain_key(url_or_host: str) -> str:
    """Registered domain (e.g. 'help.predictwind.com' -> 'predictwind.com')."""
    ext = _TLD.extract_str(url_or_host)
    domain = (
        getattr(ext, "top_domain_under_public_suffix", None)
        or getattr(ext, "registered_domain", None)
        or ext.domain
    )
    return (domain or "").lower()


def _brand(host: str) -> str:
    return _TLD.extract_str(host).domain.lower()


# ===========================================================================
# Classification result types
# ===========================================================================
class ResultCategory(str, Enum):
    OFFICIAL = "official_website"
    DISCOVERY_SOURCE = "discovery_source"
    NOISE = "noise"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    VERY_LOW = "very_low"


_CONFIDENCE_RANK = {
    Confidence.HIGH: 3, Confidence.MEDIUM: 2,
    Confidence.LOW: 1, Confidence.VERY_LOW: 0,
}


def confidence_rank(confidence: Confidence) -> int:
    """Sortable rank for a Confidence tier (higher = more trustworthy)."""
    return _CONFIDENCE_RANK[confidence]


@dataclass
class ClassificationResult:
    category: ResultCategory
    reason: str
    confidence: Optional[Confidence] = None   # populated for DISCOVERY_SOURCE
    source_name: Optional[str] = None         # display name, for DISCOVERY_SOURCE


@dataclass
class QueuedBusiness:
    """One official business homepage, ready for the crawler — with full
    discovery provenance so multiple sources pointing to the same company
    collapse into one crawl instead of one per source."""
    website: str
    title: str = ""
    snippet: str = ""
    discovered_from: List[str] = field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM

    def as_target(self) -> Dict[str, str]:
        """The plain dict shape the crawler already expects, plus provenance
        fields it simply ignores (additive, never breaks process_single_lead)."""
        return {
            "title": self.title, "website": self.website, "snippet": self.snippet,
            "discovered_from": list(self.discovered_from),
            "source_confidence": self.confidence.value,
        }


# ===========================================================================
# Registries — extend these, never the crawler, to add new source types
# ===========================================================================
# Known directory/association/registry brands worth MINING for outbound
# business links. TLD-agnostic (matches "clutch" whether .com/.co/.io).
DISCOVERY_SOURCE_REGISTRY: Dict[str, "SourceProfile"] = {}


@dataclass
class SourceProfile:
    display_name: str
    confidence: Confidence


def _register(brand: str, display_name: str, confidence: Confidence) -> None:
    DISCOVERY_SOURCE_REGISTRY[brand] = SourceProfile(display_name, confidence)


# High confidence — authoritative registries, maps, industry associations.
for _b, _n in (
    ("marinas", "Marinas.com"), ("navily", "Navily"), ("harbourmaps", "HarbourMaps"),
):
    _register(_b, _n, Confidence.HIGH)

# Medium confidence — general-purpose business directories / review sites.
for _b, _n in (
    ("yellowpages", "Yellow Pages"), ("yelp", "Yelp"), ("tripadvisor", "TripAdvisor"),
    ("clutch", "Clutch"), ("foursquare", "Foursquare"), ("mapquest", "MapQuest"),
    ("manta", "Manta"), ("hotfrog", "Hotfrog"), ("bbb", "Better Business Bureau"),
    ("crunchbase", "Crunchbase"), ("trustpilot", "Trustpilot"),
    ("justdial", "Justdial"), ("healthgrades", "Healthgrades"),
    ("zocdoc", "Zocdoc"), ("opencare", "Opencare"), ("citysearch", "Citysearch"),
):
    _register(_b, _n, Confidence.MEDIUM)

# Low confidence — smaller/regional or lower-quality directories.
for _b, _n in (
    ("cylex", "Cylex"), ("brownbook", "Brownbook"), ("n49", "N49"),
    ("cybo", "Cybo"), ("citypass", "CityPass"), ("sulekha", "Sulekha"),
    ("sitejabber", "Sitejabber"), ("vitals", "Vitals"), ("ratemds", "RateMDs"),
    ("hotels", "Hotels directory"),
):
    _register(_b, _n, Confidence.LOW)

# Genuinely NOISE — never mined, never treated as official. Split from the
# registry above on purpose: these brands don't reliably expose clean outbound
# links to individual verified business homepages (social feeds, wikis, ad
# networks, website builders whose pages ARE a business's site, not a
# directory of many).
NOISE_BRANDS = frozenset({
    # social media
    "facebook", "instagram", "twitter", "x", "linkedin", "youtube", "tiktok",
    "pinterest", "reddit", "threads", "snapchat",
    # wikis / Q&A
    "wikipedia", "wikivoyage", "quora",
    # website builders (a page hosted here IS a business's own low-quality
    # site, not a directory — never mine, but also never trust as "official")
    "wordpress", "blogspot", "blogger", "medium", "substack", "tumblr", "wix",
    "weebly", "squarespace", "godaddysites", "webador",
    # ad/tracker networks, app stores, chat, link shorteners
    "doubleclick", "googlesyndication", "googleadservices", "taboola",
    "outbrain", "adnxs", "apple", "itunes", "microsoft", "whatsapp",
    "telegram", "linktree", "tinyurl", "reklam5",
    # job boards / employer reviews (not business directories in this sense)
    "indeed", "glassdoor",
})
NOISE_EXACT_DOMAINS = frozenset({
    "wa.me", "t.me", "bit.ly", "linktr.ee", "apps.apple.com", "itunes.apple.com",
    "play.google.com", "m.me", "api.whatsapp.com", "goo.gl", "maps.app.goo.gl",
})

# Path/extension fragments that mark NOISE (news/press/forum/PDF) — distinct
# from listicle/guide pages below, which ARE worth mining.
NOISE_PATH_HINTS = (
    "/news/", "/press/", "/press-release/", "/press-releases/",
    "/forum/", "/forums/", "/thread/", "/threads/", "/wiki/",
    ".pdf",
)

# Listicle / guide / directory-shaped pages — an unrecognized brand that
# LOOKS like a "10 Best Marinas in Sydney" page is itself a mineable
# discovery source (it links out to individual businesses), just with lower
# confidence than a known-brand directory since we don't know its quality.
LISTICLE_PATH_HINTS = (
    "/blog", "/article", "/articles", "/guide", "/guides",
    "/region/", "/browse/", "/explore", "/category/", "/list",
    "/directory", "/directories",
)
LISTICLE_TITLE_RE = re.compile(
    r"\b("
    r"\d+\s+best|best\s+\d+|top\s+\d+|\d+\s+(?:best|top|great|famous|popular)|"
    r"the\s+best\b|best\b|top\b|most\s+popular\b|"
    r"a\s+guide\b|guide\s+to\b|ultimate\s+guide\b|complete\s+guide\b|"
    r"exploring\b|near\s+me\b|"
    r"(?:cafe|cafes|cafés?|coffee\s+shops?|restaurants?|hotels?|bars?|"
    r"marinas?|places?|things\s+to\s+do)\s+(?:in|near|around)\b|"
    r"where\s+to\b|must[- ]?visit\b|reviews?\b|ranked\b|listings?\b|directory\b"
    r")",
    re.IGNORECASE,
)

# Subdomains/anchors/paths marking a link as a utility/CTA/footer link (login,
# help, ads...) rather than an actual business — used when mining a directory
# page's outbound links, not for the top-level search result itself.
UTILITY_SUBDOMAINS = frozenset({
    "help", "support", "signup", "sign-up", "login", "signin", "account",
    "accounts", "blog", "shop", "store", "app", "apps", "api", "docs",
    "status", "mail", "satellite", "cdn", "static", "ads", "ad", "portal",
    "my", "dashboard", "go", "get", "link", "track",
})
UTILITY_ANCHOR_RE = re.compile(
    r"\b(sign\s?up|sign\s?in|log\s?in|login|register|subscribe|download|"
    r"activate|get\s?started|learn\s?more|read\s?more|contact\s?us|help|"
    r"support|faq|privacy|terms|cookies?|careers?|advertise|book\s?now|"
    r"buy\s?now|shop\s?now|free\s?now)\b",
    re.IGNORECASE,
)
UTILITY_PATH_HINTS = (
    "/signup", "/sign-up", "/signin", "/login", "/register", "/account",
    "/support", "/help", "/privacy", "/terms", "/cart", "/checkout",
    "/subscribe", "/download", "/advertise",
)


def is_utility_link(host: str, path: str, anchor: str = "") -> bool:
    """True if a link/URL is a sign-up/help/login/ad/CTA link, not a business
    site. Public: used both when mining a directory's outbound links and by
    data_pipeline.py's final governance pass over the clean export."""
    subdomain = host.split(".")[0] if "." in host else ""
    if subdomain in UTILITY_SUBDOMAINS:
        return True
    if any(hint in path.lower() for hint in UTILITY_PATH_HINTS):
        return True
    return bool(anchor and UTILITY_ANCHOR_RE.search(anchor))


def _is_noise_path(path: str) -> bool:
    low = path.lower()
    return any(hint in low for hint in NOISE_PATH_HINTS)


def _looks_like_listicle(title: str, path: str) -> bool:
    if any(hint in path.lower() for hint in LISTICLE_PATH_HINTS):
        return True
    return bool(title and LISTICLE_TITLE_RE.search(title))


# ===========================================================================
# The classifier
# ===========================================================================
def classify_search_result(url: str, title: str = "") -> ClassificationResult:
    """Classify one Google/Places search result (or directory outbound link).

    Order matters: exact noise domains/brands are checked first (never mined,
    never queued), then known discovery-source brands (mined, with their
    registered confidence), then explicit noise path patterns (news/press/
    forum/pdf — checked BEFORE the listicle pattern, since e.g. a forum thread
    titled "best marinas" is a discussion, not a curated directory), then
    listicle-shaped pages (mined, LOW confidence since the brand is
    unrecognized), and finally OFFICIAL as the default.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path or "/"

    if not host:
        return ClassificationResult(ResultCategory.NOISE, "no host in URL")
    if host in NOISE_EXACT_DOMAINS or any(
        host.endswith("." + d) for d in NOISE_EXACT_DOMAINS
    ):
        return ClassificationResult(ResultCategory.NOISE, f"known noise domain ({host})")

    brand = _brand(host)
    if brand in NOISE_BRANDS:
        return ClassificationResult(ResultCategory.NOISE, f"known noise brand ({brand})")

    if brand in DISCOVERY_SOURCE_REGISTRY:
        profile = DISCOVERY_SOURCE_REGISTRY[brand]
        return ClassificationResult(
            ResultCategory.DISCOVERY_SOURCE,
            f"known directory brand ({profile.display_name})",
            confidence=profile.confidence, source_name=profile.display_name,
        )

    if _is_noise_path(path):
        return ClassificationResult(
            ResultCategory.NOISE, "path matches news/press/forum/PDF pattern",
        )

    if _looks_like_listicle(title, path):
        return ClassificationResult(
            ResultCategory.DISCOVERY_SOURCE,
            "title/path matches a listicle/guide/directory pattern",
            confidence=Confidence.LOW, source_name=f"Unrecognized directory ({host})",
        )

    return ClassificationResult(ResultCategory.OFFICIAL, "no directory/noise signal matched")


# ===========================================================================
# Mining — extract businesses from ONE discovery source (never recursive)
# ===========================================================================
async def _native_get(session: aiohttp.ClientSession, url: str, timeout_s: int) -> Optional[str]:
    try:
        async with session.get(
            url, headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status < 400:
                return await resp.text(errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Native GET failed for %s: %s", url, exc)
    return None


async def extract_businesses_from_source(
    session: aiohttp.ClientSession,
    source_url: str,
    source_name: str,
    confidence: Confidence,
    seen_hosts: Set[str],
    needed: int,
    *,
    fetch_timeout_s: int = 12,
) -> List[QueuedBusiness]:
    """Fetch ONE discovery-source page and extract its outbound links to
    individual business homepages. Never follows a link that is ITSELF another
    discovery source or noise (no recursive directory mining) — only links
    that classify as OFFICIAL become QueuedBusiness records.

    Logs extraction stats (found / queued / skipped) so it's obvious where
    businesses are being filtered out.
    """
    html = await _native_get(session, source_url, fetch_timeout_s)
    if not html:
        logger.info("Discovery source unreachable, skipped: %s (%s)", source_url, source_name)
        return []

    source_domain = domain_key(source_url)
    found: List[QueuedBusiness] = []
    scanned = 0
    skipped_noise = 0
    skipped_recursive = 0
    skipped_utility = 0
    skipped_duplicate = 0

    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().startswith("http"):
            continue
        scanned += 1
        parsed = urlparse(href)
        host = parsed.netloc.lower().removeprefix("www.")
        domain = domain_key(href)
        anchor = a.get_text(strip=True)
        if not host or domain == source_domain:
            continue
        if domain in seen_hosts:
            skipped_duplicate += 1
            continue
        if is_utility_link(host, parsed.path, anchor):
            skipped_utility += 1
            continue
        outbound = classify_search_result(href, anchor)
        if outbound.category != ResultCategory.OFFICIAL:
            # A discovery source linking to ANOTHER discovery source or noise
            # is never mined recursively — just skipped.
            if outbound.category == ResultCategory.DISCOVERY_SOURCE:
                skipped_recursive += 1
            else:
                skipped_noise += 1
            continue

        seen_hosts.add(domain)
        found.append(QueuedBusiness(
            website=f"{parsed.scheme}://{parsed.netloc}/",
            title=anchor or host,
            snippet=f"(discovered via {source_name})",
            discovered_from=[source_name],
            confidence=confidence,
        ))
        if len(found) >= needed:
            break

    logger.info(
        "Mined %s (%s): %d link(s) scanned -> %d queued, %d skipped "
        "(noise=%d, other-directory=%d, utility=%d, duplicate=%d)",
        source_name, source_domain, scanned, len(found),
        skipped_noise + skipped_recursive + skipped_utility + skipped_duplicate,
        skipped_noise, skipped_recursive, skipped_utility, skipped_duplicate,
    )
    return found


# ===========================================================================
# Deduplicated, confidence-prioritized queue
# ===========================================================================
class DiscoveryQueue:
    """Accumulates official businesses from every discovery channel (organic
    search, Places, mined directories), deduped by registered domain.

    A business found via multiple sources is crawled ONCE — but every source
    that pointed to it is preserved in `discovered_from` for traceability.
    """

    def __init__(self) -> None:
        self._by_domain: Dict[str, QueuedBusiness] = {}
        self._order: List[str] = []

    def add(self, business: QueuedBusiness) -> bool:
        """Insert or merge. Returns True if this was a genuinely new business."""
        domain = domain_key(business.website)
        if not domain:
            return False
        existing = self._by_domain.get(domain)
        if existing is None:
            self._by_domain[domain] = business
            self._order.append(domain)
            return True
        for src in business.discovered_from:
            if src not in existing.discovered_from:
                existing.discovered_from.append(src)
        if _CONFIDENCE_RANK[business.confidence] > _CONFIDENCE_RANK[existing.confidence]:
            existing.confidence = business.confidence
        return False

    def extend(self, businesses: List[QueuedBusiness]) -> int:
        """Add many; returns how many were genuinely new."""
        return sum(1 for b in businesses if self.add(b))

    def pending(self, already_attempted: Set[str]) -> List[QueuedBusiness]:
        """Queued businesses not yet attempted, highest confidence first."""
        items = [
            self._by_domain[d] for d in self._order
            if d not in already_attempted
        ]
        return sorted(items, key=lambda b: _CONFIDENCE_RANK[b.confidence], reverse=True)

    def domains(self) -> Set[str]:
        return set(self._order)

    def __len__(self) -> int:
        return len(self._by_domain)
