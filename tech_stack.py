"""
Tech Stack Detection — a processing step INSIDE Phase 1, not a separate stage.

    ZenRows/native fetch
            |
            v
        Raw HTML  (already in memory, one page, per the streaming crawler)
        /        \\
       v          v
  HTML Cleaning   Wappalyzer Detection   <- this module (analyze_raw_html)
       |          |
       v          v
  Cleaned Text   Tech Stack Profile
        \\        /
         v      v
       Store Results
            |
            v
       Delete Raw HTML
            |
            v
     Continue Crawling

`analyze_raw_html()` is called by phase1_pipeline.crawl_site() on the
HOMEPAGE's raw HTML while it is still in memory — the SAME response already
fetched for the crawl, never a second request. It runs unconditionally for
every newly-crawled business (collection is not gated by query intent); only
SURFACING the result to a user is intent-gated (see get_stored_profile below).

Runs once per domain, on the homepage. A site's underlying stack (CMS, CRM,
analytics, hosting) essentially never differs page-to-page, and the output
artifacts are themselves per-domain (`tech_stack.json`, `website_profile.json`
— singular, not one per page), so re-fingerprinting every crawled page would
be pure waste for zero additional signal.

Storage IS the cache. Per requirement, a future query must check
storage/<domain>/tech_stack.json (via storage.get_store().read_tech_profile)
before ever re-running Wappalyzer. get_stored_profile() is that check; it only
falls back to a fresh (independent, single-request) scan when a domain was
never profiled during its Phase-1 crawl at all.

Separated per "keep modular" requirement:
    Detection       -> analyze_raw_html, _run_wappalyzer_on_webpage
    Categorization   -> build_categories_view, build_normalized_tech_stack,
                        NORMALIZED_BUCKET_MAP (curated on top of fully
                        preserved dynamic/raw data — nothing is lost for
                        categories this table doesn't know about)
    Profile assembly -> build_website_profile, build_capabilities
    Sales signals    -> SignalRule registry, generate_sales_signals
    Public API       -> get_stored_profile + the has_/is_/detect_ helpers

Future technology detectors (BuiltWith, etc.) plug in here, alongside
Wappalyzer, WITHOUT the crawler changing: phase1_pipeline.py calls exactly one
function (analyze_raw_html) and stores exactly one profile dict.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

import requests

logger = logging.getLogger("ai_bdm.tech_stack")

TECH_PROFILE_SCHEMA_VERSION = "1.0"
TECH_PROFILE_TTL_DAYS = 60  # a stored profile older than this is refreshed on
                            # next query-time read (see get_stored_profile) —
                            # without this, a profile written once was NEVER
                            # re-checked, so a business switching CRM/hosting
                            # long after being crawled would go undetected forever

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
FALLBACK_FETCH_TIMEOUT_S = 10  # only used by the query-time backfill scan (rare)


# ===========================================================================
# Detection — build a WebPage from HTML/headers ALREADY IN MEMORY (no fetch)
# ===========================================================================
_WAPPALYZER = None  # lazy singleton: technologies.json is loaded once, reused


def _get_wappalyzer():
    global _WAPPALYZER
    if _WAPPALYZER is None:
        from wappalyzer import Wappalyzer  # optional dependency, imported lazily
        _WAPPALYZER = Wappalyzer.latest()
    return _WAPPALYZER


def _run_wappalyzer_on_webpage(webpage) -> Dict[str, Dict[str, Any]]:
    """Run Wappalyzer against a WebPage and return the complete raw result.

    {tech_name: {"versions": [...], "categories": [...], "confidence": int}}
    Nothing is filtered here — every technology and category Wappalyzer
    returns is preserved verbatim (plus confidence, folded in per-tech).
    """
    wappalyzer = _get_wappalyzer()
    detected = wappalyzer.analyze_with_versions_and_categories(webpage)
    for name, info in detected.items():
        confidence = wappalyzer.get_confidence(name) or 100
        info["confidence"] = min(100, confidence if isinstance(confidence, int) else 100)
    return detected


def analyze_raw_html(
    domain: str, html: str, headers: Optional[Dict[str, str]] = None
) -> Dict[str, Dict[str, Any]]:
    """Fingerprint technologies from HTML/headers ALREADY fetched by the
    crawler. Makes NO network request — this is the integrated Phase-1 path.
    """
    from wappalyzer import WebPage
    url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}/"
    webpage = WebPage(url, html=html, headers=headers or {})
    return _run_wappalyzer_on_webpage(webpage)


def _fetch_and_analyze(domain: str) -> Dict[str, Dict[str, Any]]:
    """Independent, single-request fallback path — used ONLY when a domain
    was never profiled during its Phase-1 crawl (see get_stored_profile) and
    for the standalone CLI. Never used by the integrated crawler path."""
    url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}/"
    from wappalyzer import WebPage
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=FALLBACK_FETCH_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.info("Tech-stack fallback fetch failed for %s: %s", domain, exc)
        return {}
    if resp.status_code >= 400:
        return {}
    return _run_wappalyzer_on_webpage(WebPage.new_from_response(resp))


# ===========================================================================
# Categorization — dynamic (future-proof) + a curated business-friendly view
# ===========================================================================
def build_categories_view(raw: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    """Category name -> [tech names]. Built ENTIRELY from whatever categories
    Wappalyzer actually returned for each detected technology — no hardcoded
    category list, so new Wappalyzer categories appear automatically."""
    categories: Dict[str, List[str]] = {}
    for name, info in raw.items():
        for cat in info.get("categories", []):
            categories.setdefault(cat, []).append(name)
    return categories


def build_versions_view(raw: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    """Tech name -> versions, preserved exactly as Wappalyzer reported them."""
    return {name: info.get("versions", []) for name, info in raw.items() if info.get("versions")}


# Curated on top of the fully-preserved dynamic data above — this table is a
# CONVENIENCE layer, not a filter. A technology whose Wappalyzer category
# isn't listed here still exists in raw_wappalyzer/categories/versions; it
# just won't also appear in this business-friendly view.
NORMALIZED_BUCKET_MAP: Dict[str, str] = {
    # frontend
    "JavaScript frameworks": "frontend", "JavaScript libraries": "frontend",
    "JavaScript graphics": "frontend", "UI frameworks": "frontend",
    "Web frameworks": "frontend", "Mobile frameworks": "frontend",
    "Static site generator": "frontend", "Font scripts": "frontend", "Widgets": "frontend",
    # backend
    "Programming languages": "backend", "Web servers": "backend",
    "Web server extensions": "backend", "Reverse proxies": "backend",
    "Containers": "backend", "Operating systems": "backend", "Load balancers": "backend",
    # cms
    "CMS": "cms", "Blogs": "cms", "Wikis": "cms", "LMS": "cms", "DMS": "cms",
    "WordPress plugins": "cms", "WordPress themes": "cms", "Drupal themes": "cms",
    "Page builders": "cms", "Headless CMS": "cms",
    # hosting / cloud
    "Hosting": "hosting", "Hosting panels": "hosting", "CDN": "hosting",
    "IaaS": "cloud", "PaaS": "cloud",
    # analytics
    "Analytics": "analytics", "RUM": "analytics", "Tag managers": "analytics",
    "SEO": "analytics", "A/B Testing": "analytics", "Personalization": "analytics",
    "Segmentation": "analytics", "Surveys": "analytics",
    # crm / marketing
    "CRM": "crm", "Customer data platform": "crm",
    "Marketing automation": "marketing", "Advertising": "marketing",
    "Retargeting": "marketing", "Affiliate programs": "marketing",
    "Referral marketing": "marketing", "Email": "marketing",
    "Loyalty & rewards": "marketing", "Reviews": "marketing", "Content curation": "marketing",
    # security
    "Security": "security", "SSL/TLS certificate authorities": "security",
    "Cookie compliance": "security", "Browser fingerprinting": "security",
    # payments / ecommerce
    "Payment processors": "payments", "Buy now pay later": "payments",
    "Ecommerce": "ecommerce", "Ecommerce frontends": "ecommerce",
    "Shopify apps": "ecommerce", "Shopify themes": "ecommerce", "Cart abandonment": "ecommerce",
    # chat / support
    "Live chat": "chat", "Message boards": "chat",
    # auth
    "Authentication": "authentication",
    # performance
    "Performance": "performance", "Caching": "performance",
    # database
    "Databases": "database", "Database managers": "database",
}
NORMALIZED_BUCKETS = (
    "cms", "frontend", "backend", "hosting", "analytics", "crm", "marketing",
    "security", "payments", "chat", "authentication", "performance",
    "ecommerce", "database", "cloud",
)


def build_normalized_tech_stack(
    raw: Dict[str, Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """Curated business-friendly buckets (see NORMALIZED_BUCKETS), built on
    top of the fully-preserved raw detection. A tech can land in more than one
    bucket if Wappalyzer assigns it categories that map to different buckets
    (e.g. a CDN that is also a security/WAF service) — that's correct, not a
    bug. Nothing unmapped is lost — it still lives in categories/raw_wappalyzer."""
    buckets: Dict[str, List[Dict[str, Any]]] = {b: [] for b in NORMALIZED_BUCKETS}
    for name, info in raw.items():
        item = {"name": name, "version": (info.get("versions") or [None])[0],
                "confidence": info.get("confidence", 100)}
        seen: set = set()
        for cat in info.get("categories", []):
            bucket = NORMALIZED_BUCKET_MAP.get(cat)
            if bucket and bucket not in seen:
                buckets[bucket].append(item)
                seen.add(bucket)
    return buckets


def _frameworks(raw: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cross-cutting view: every technology in any category whose name
    contains 'framework', regardless of which bucket it also lands in."""
    return [
        {"name": name, "version": (info.get("versions") or [None])[0],
         "confidence": info.get("confidence", 100)}
        for name, info in raw.items()
        if any("framework" in cat.lower() for cat in info.get("categories", []))
    ]


# ===========================================================================
# Page-list cross-reference — a signal Phase 1 already produced
# ===========================================================================
# Wappalyzer only sees the homepage. Login/portal detection is far stronger
# using the pages Phase 1 already discovered on this site while crawling it —
# a page URL is a signal even when its content wasn't kept (e.g. thin/dup).
_LOGIN_PATH_HINTS = ("login", "signin", "sign-in", "account", "my-account")
_PORTAL_PATH_HINTS = ("portal", "dashboard", "client-area", "customer-portal", "members")


def _paths_match(urls: Iterable[str], hints: tuple) -> bool:
    for url in urls:
        path = urlsplit(url).path.lower()
        if any(hint in path for hint in hints):
            return True
    return False


def detect_login_and_portal(
    raw: Dict[str, Dict[str, Any]], discovered_urls: Iterable[str]
) -> tuple:
    """(has_login_page, has_customer_portal) from an Authentication-category
    detection OR a login/portal-shaped URL among everything Phase 1 discovered
    on this site (fetched or merely linked-to — both are valid signals)."""
    urls = list(discovered_urls)
    has_login = bool(
        any("auth" in name.lower() for name, info in raw.items()
            if "Authentication" in info.get("categories", []))
        or _paths_match(urls, _LOGIN_PATH_HINTS)
    )
    has_portal = _paths_match(urls, _PORTAL_PATH_HINTS)
    return has_login, has_portal


def _indexed_urls_for_domain(domain: str) -> List[str]:
    """Discovered URLs for an already-committed domain, from the crawl index
    (used only by the query-time backfill path, not the integrated crawler)."""
    try:
        from storage import get_store
        from phase1_pipeline import _domain_key
    except ImportError:
        return []
    want = _domain_key(domain)
    return [
        row.get("page_url", "") for row in get_store().read_index()
        if _domain_key(row.get("domain") or row.get("website_url") or "") == want
    ]


# ===========================================================================
# Profile assembly
# ===========================================================================
# Detection semantics: Wappalyzer only sees what a site PUBLICLY exposes. Many
# real technologies (backend systems, CRMs, ERPs, internal portals, databases,
# auth providers, internal APIs...) are invisible from the outside — a missing
# fingerprint is NOT evidence of absence. Every capability is therefore one of:
#   "detected"         positively identified (a fingerprint matched)
#   "unknown"          no public fingerprint found — absence NOT implied
#   "confirmed_absent" strong evidence of true absence — Wappalyzer generally
#                       cannot establish this, so nothing here ever emits it
#                       automatically; the value exists for a future rule (or
#                       a human) that has stronger evidence than a public scan.
STATUS_DETECTED = "detected"
STATUS_UNKNOWN = "unknown"
STATUS_CONFIRMED_ABSENT = "confirmed_absent"

# Categories reported with a "provider" field (a specific SaaS/vendor a site
# either uses or doesn't) rather than "technology" (a structural/technical
# choice). Purely a naming convention for readability; the status semantics
# are identical either way.
_PROVIDER_STYLE_CATEGORIES = frozenset({"payments", "analytics", "chat", "marketing"})


def _status_object(
    items: List[Dict[str, Any]], category_label: str, id_field: str = "technology",
) -> Dict[str, Any]:
    """One category's structured status object — never a bare bool/null.

    {"status", id_field, "version", "confidence", "reason"} — ALL keys always
    present (never omitted) so downstream consumers never need to guess which
    fields exist for which status. `confidence` is 0-1 (Wappalyzer reports
    0-100); `reason` is populated only when status != "detected".
    """
    if items:
        top = items[0]
        return {
            "status": STATUS_DETECTED,
            id_field: top["name"],
            "version": top.get("version"),
            "confidence": round(min(100, top.get("confidence", 100)) / 100, 2),
            "reason": None,
        }
    return {
        "status": STATUS_UNKNOWN,
        id_field: None,
        "version": None,
        "confidence": None,
        "reason": f"No public fingerprint detected for {category_label}.",
    }


def build_capabilities(
    normalized: Dict[str, List[Dict[str, Any]]],
    has_login: bool, has_portal: bool,
) -> Dict[str, Any]:
    """Structured, 3-state business summary — the quick-glance file
    (tech_stack.json). Every category is an object, never a bare bool/null:

        {"crm": {"status": "detected", "technology": "HubSpot",
                 "version": null, "confidence": 0.97, "reason": null},
         "backend": {"status": "unknown", "technology": null, "version": null,
                     "confidence": null,
                     "reason": "No public fingerprint detected for backend."}}

    `status: "unknown"` means "not publicly detectable" — it must never be
    read downstream as "confirmed absent" (see module docstring / STATUS_*).
    """
    caps: Dict[str, Any] = {}
    for bucket in NORMALIZED_BUCKETS:
        id_field = "provider" if bucket in _PROVIDER_STYLE_CATEGORIES else "technology"
        caps[bucket] = _status_object(normalized.get(bucket) or [], bucket, id_field)

    # Capability-style entries inferred from multiple signals (Authentication
    # tech + discovered URL paths), not a single named vendor — same 3-state
    # shape, just without a "technology"/"provider" identity to report.
    caps["login_system"] = (
        {"status": STATUS_DETECTED, "confidence": None, "reason": None} if has_login
        else {"status": STATUS_UNKNOWN, "confidence": None,
              "reason": "No login/authentication page or technology publicly detected."}
    )
    caps["customer_portal"] = (
        {"status": STATUS_DETECTED, "confidence": None, "reason": None} if has_portal
        else {"status": STATUS_UNKNOWN, "confidence": None,
              "reason": "No customer-portal-shaped page publicly discovered."}
    )
    return caps


def build_website_profile(
    domain: str,
    raw: Dict[str, Dict[str, Any]],
    *,
    homepage_url: str = "",
    fetch_method: str = "",
    discovered_urls: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Assemble the full technical knowledge-base profile for one domain.

    {
      "raw_wappalyzer": {...},        exactly as Wappalyzer returned it
      "normalized_tech_stack": {...}, curated business buckets
      "versions": {...},              tech name -> versions, preserved exactly
      "categories": {...},            EVERY wappalyzer category -> tech names
      "capabilities": {...},          flattened quick-glance summary
      "detected_services": [...],     flat, deduped list of every tech name
      "crawl_metadata": {...},
      "last_scanned": "...", "scan_version": "..."
    }
    """
    normalized = build_normalized_tech_stack(raw)
    has_login, has_portal = detect_login_and_portal(raw, discovered_urls or [])
    capabilities = build_capabilities(normalized, has_login, has_portal)

    return {
        "raw_wappalyzer": raw,
        "normalized_tech_stack": normalized,
        "versions": build_versions_view(raw),
        "categories": build_categories_view(raw),
        "capabilities": capabilities,
        "detected_services": sorted(raw.keys()),
        "crawl_metadata": {
            "domain": domain,
            "homepage_url": homepage_url,
            "fetch_method": fetch_method,
            "technology_count": len(raw),
        },
        "last_scanned": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scan_version": TECH_PROFILE_SCHEMA_VERSION,
    }


# ===========================================================================
# Public entry point — storage-first, scan only as a last resort
# ===========================================================================
def _profile_age_days(profile: Dict[str, Any]) -> Optional[float]:
    last_scanned = profile.get("last_scanned")
    if not last_scanned:
        return None
    try:
        scanned_at = datetime.fromisoformat(last_scanned)
    except ValueError:
        return None
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - scanned_at).total_seconds() / 86400


def get_stored_profile(domain: str, allow_fallback_scan: bool = True) -> Dict[str, Any]:
    """The single query-time read path. ALWAYS checks storage first (per
    requirement: never re-scan when a stored profile already exists AND it's
    still fresh — see TECH_PROFILE_TTL_DAYS).

    Falls back to one independent, single-request scan when either:
      - this domain was never profiled during its own Phase-1 crawl (e.g.
        crawled before this feature existed), or
      - a profile exists but is older than TECH_PROFILE_TTL_DAYS (a business
        can change CRM/hosting/analytics long after being crawled; without
        this, a profile written once was never re-checked, ever).
    Either way the refreshed result is persisted so future calls hit storage.
    """
    try:
        from storage import get_store
        from phase1_pipeline import _domain_key
    except ImportError:
        get_store = None  # type: ignore[assignment]
        _domain_key = lambda d: d.strip().lower().removeprefix("www.")  # noqa: E731

    domain_key = _domain_key(domain)
    stored = get_store().read_tech_profile(domain_key) if get_store is not None else None
    if stored is not None:
        age_days = _profile_age_days(stored)
        if age_days is None or age_days <= TECH_PROFILE_TTL_DAYS:
            logger.info("Tech-stack profile HIT in storage for %s.", domain_key)
            stored.setdefault("sales_signals", generate_sales_signals_from_profile(stored))
            return stored
        logger.info(
            "Tech-stack profile for %s is %.0f day(s) old (> %d) — refreshing.",
            domain_key, age_days, TECH_PROFILE_TTL_DAYS,
        )

    if not allow_fallback_scan:
        return stored if stored is not None else {"error": "not_profiled", "domain": domain_key}

    logger.info("Running one-off fallback scan for %s (missing or stale profile).", domain_key)
    raw = _fetch_and_analyze(domain_key)
    if not raw:
        # Refresh failed (site down/blocking) — serve the stale profile rather
        # than nothing; a stale-but-present profile beats an empty one.
        if stored is not None:
            stored.setdefault("sales_signals", generate_sales_signals_from_profile(stored))
            return stored
        return {"error": "fetch_failed", "domain": domain_key,
                "capabilities": {}, "sales_signals": []}

    discovered = _indexed_urls_for_domain(domain_key)
    profile = build_website_profile(domain_key, raw, discovered_urls=discovered)
    profile["sales_signals"] = generate_sales_signals_from_profile(profile)
    if get_store is not None and get_store().has_domain(domain_key):
        get_store().write_tech_profile_now(domain_key, profile["capabilities"], profile)
    return profile


# Backward-compatible alias (the name used when this module was first wired
# into main.py/phase1_pipeline.py's Step 4).
def get_tech_stack(domain: str) -> Dict[str, Any]:
    return get_stored_profile(domain)


# --- boolean / list helpers — all read from the single get_stored_profile() --
# NOTE: these collapse the 3-state model to a bool for convenience. False here
# means "not positively detected" — it covers BOTH "unknown" (Wappalyzer simply
# couldn't see it) and the near-never-used "confirmed_absent". Callers that
# need to distinguish those two should inspect
# get_stored_profile(domain)["capabilities"][x]["status"] directly instead.
def _is_detected(domain: str, capability: str) -> bool:
    caps = get_stored_profile(domain).get("capabilities", {})
    return caps.get(capability, {}).get("status") == STATUS_DETECTED


def has_crm(domain: str) -> bool:
    return _is_detected(domain, "crm")


def has_customer_portal(domain: str) -> bool:
    return _is_detected(domain, "customer_portal")


def has_login_page(domain: str) -> bool:
    return _is_detected(domain, "login_system")


def _has_tech(domain: str, tech_name: str) -> bool:
    raw = get_stored_profile(domain).get("raw_wappalyzer", {})
    return tech_name.lower() in {n.lower() for n in raw}


def is_wordpress(domain: str) -> bool:
    return _has_tech(domain, "WordPress")


def is_shopify(domain: str) -> bool:
    return _has_tech(domain, "Shopify")


def is_react(domain: str) -> bool:
    return _has_tech(domain, "React")


def is_nextjs(domain: str) -> bool:
    return _has_tech(domain, "Next.js")


def is_outdated_stack(domain: str) -> bool:
    """Heuristic: a legacy CMS/backend with no modern JS frontend in sight, or
    an explicitly old detected version of a known technology."""
    profile = get_stored_profile(domain)
    if profile.get("error"):
        return False
    normalized = profile.get("normalized_tech_stack", {})
    if normalized.get("cms") and not normalized.get("frontend"):
        return True
    for versions in profile.get("versions", {}).values():
        for version in versions:
            major = version.split(".")[0]
            if major.isdigit() and int(major) <= 5:
                return True
    return False


def detect_frameworks(domain: str) -> List[Dict[str, Any]]:
    return _frameworks(get_stored_profile(domain).get("raw_wappalyzer", {}))


def detect_hosting(domain: str) -> List[Dict[str, Any]]:
    return get_stored_profile(domain).get("normalized_tech_stack", {}).get("hosting", [])


def detect_cms(domain: str) -> List[Dict[str, Any]]:
    return get_stored_profile(domain).get("normalized_tech_stack", {}).get("cms", [])


def detect_backend(domain: str) -> List[Dict[str, Any]]:
    return get_stored_profile(domain).get("normalized_tech_stack", {}).get("backend", [])


def detect_security(domain: str) -> List[Dict[str, Any]]:
    return get_stored_profile(domain).get("normalized_tech_stack", {}).get("security", [])


def detect_analytics(domain: str) -> List[Dict[str, Any]]:
    return get_stored_profile(domain).get("normalized_tech_stack", {}).get("analytics", [])


def detect_marketing_tools(domain: str) -> List[Dict[str, Any]]:
    return get_stored_profile(domain).get("normalized_tech_stack", {}).get("marketing", [])


def generate_sales_signals(domain: str) -> List[Dict[str, Any]]:
    return get_stored_profile(domain).get("sales_signals", [])


# ===========================================================================
# Sales signal / business opportunity rule engine
# ===========================================================================
@dataclass
class SignalRule:
    """One addable, isolated business-opportunity rule.

    `applies` inspects the full website_profile dict and returns a confidence
    in [0, 1] if the signal fires, else None. `suppresses` lists rule ids to
    skip when this rule fires (e.g. a modern-stack finding suppresses legacy-
    rebuild advice) — no if/else chain coupling every rule to every other.
    """
    id: str
    signal: str
    applies: Callable[[Dict[str, Any]], Optional[float]]
    recommended_services: List[str]
    # "positively_detected": the signal is grounded in something Wappalyzer
    # actually found (e.g. an old version string, a modern framework).
    # "not_publicly_detected": grounded in an ABSENCE of a public fingerprint —
    # never a confirmed-absence claim (see STATUS_* / module docstring). Future
    # AI-BDM modules should treat these two differently: the former is a fact,
    # the latter is "worth asking about", not "confirmed missing".
    evidence: str = "positively_detected"
    suppresses: List[str] = field(default_factory=list)


def _bucket(p: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    return p.get("normalized_tech_stack", {}).get(name, [])


def _cap_status(p: Dict[str, Any], name: str) -> str:
    return p.get("capabilities", {}).get(name, {}).get("status", STATUS_UNKNOWN)


def _rule_modern_stack(p: Dict[str, Any]) -> Optional[float]:
    modern_frontend = any(t["name"] in ("React", "Next.js", "Vue.js", "Nuxt.js")
                          for t in _bucket(p, "frontend"))
    modern_hosting = any(t["name"] in ("Cloudflare", "Vercel", "Netlify")
                         for t in _bucket(p, "hosting"))
    return 0.8 if (modern_frontend and modern_hosting and _bucket(p, "crm")) else None


def _rule_no_crm(p: Dict[str, Any]) -> Optional[float]:
    return 0.82 if _cap_status(p, "crm") != STATUS_DETECTED and not p.get("error") else None


def _rule_no_analytics(p: Dict[str, Any]) -> Optional[float]:
    return 0.75 if _cap_status(p, "analytics") != STATUS_DETECTED and not p.get("error") else None


def _rule_no_customer_portal(p: Dict[str, Any]) -> Optional[float]:
    if p.get("error"):
        return None
    return 0.65 if _cap_status(p, "customer_portal") != STATUS_DETECTED else None


def _rule_no_login(p: Dict[str, Any]) -> Optional[float]:
    if p.get("error"):
        return None
    return 0.6 if _cap_status(p, "login_system") != STATUS_DETECTED else None


def _rule_legacy_wordpress(p: Dict[str, Any]) -> Optional[float]:
    if not any(t["name"] == "WordPress" for t in _bucket(p, "cms")):
        return None
    return 0.91 if not _bucket(p, "frontend") else 0.6


def _rule_outdated_version(p: Dict[str, Any]) -> Optional[float]:
    for versions in p.get("versions", {}).values():
        for version in versions:
            major = version.split(".")[0]
            if major.isdigit() and int(major) <= 5:
                return 0.7
    return None


# Registered in priority order; modern-stack fires first so it can suppress
# the legacy/rebuild-flavored rules below it.
SIGNAL_RULES: List[SignalRule] = [
    SignalRule(
        id="modern_stack", signal="Modern technology stack detected",
        applies=_rule_modern_stack,
        recommended_services=[
            "AI Automation", "CRM Integrations", "Workflow Automation",
            "Custom Internal Tools", "Mobile Applications", "AI Chatbots",
            "Business Process Automation",
        ],
        suppresses=["legacy_wordpress", "outdated_version"],
    ),
    SignalRule(
        id="no_crm", signal="CRM not publicly detected",
        applies=_rule_no_crm, evidence="not_publicly_detected",
        recommended_services=["CRM Development", "Sales Automation", "Lead Management"],
    ),
    SignalRule(
        id="no_analytics", signal="Analytics not publicly detected",
        applies=_rule_no_analytics, evidence="not_publicly_detected",
        recommended_services=[
            "Google Analytics Implementation", "Conversion Tracking",
            "Marketing Dashboard", "Business Intelligence Reporting",
        ],
    ),
    SignalRule(
        id="no_customer_portal", signal="Customer portal not publicly detected",
        applies=_rule_no_customer_portal, evidence="not_publicly_detected",
        recommended_services=[
            "Customer Portal", "Client Dashboard", "Order Tracking Portal", "Member Portal",
        ],
    ),
    SignalRule(
        id="no_login", signal="Login/authentication system not publicly detected",
        applies=_rule_no_login, evidence="not_publicly_detected",
        recommended_services=["Authentication System", "Customer Accounts", "Employee Portal"],
    ),
    SignalRule(
        id="legacy_wordpress", signal="Legacy WordPress installation",
        applies=_rule_legacy_wordpress,
        recommended_services=[
            "Website Redesign", "Security Upgrade", "Performance Optimization",
        ],
    ),
    SignalRule(
        id="outdated_version", signal="Very old technology version detected",
        applies=_rule_outdated_version,
        recommended_services=[
            "Security Audit", "CMS Migration", "Platform Upgrade", "Managed Maintenance",
        ],
    ),
]


def generate_sales_signals_from_profile(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run the rule registry against an already-built website_profile dict.

    Every signal carries an `evidence` tag so callers never conflate the two:
      "positively_detected"    grounded in something Wappalyzer actually found
      "not_publicly_detected"  grounded in an ABSENCE of a public fingerprint —
                               a prompt to ask/verify, never a confirmed-
                               missing claim (see STATUS_* / module docstring)
    A firing rule can suppress others by id so contradictory advice (e.g.
    "rebuild" alongside "modern stack") never ships.
    """
    if profile.get("error"):
        return []
    fired: List[Dict[str, Any]] = []
    suppressed: set = set()
    for rule in SIGNAL_RULES:
        if rule.id in suppressed:
            continue
        confidence = rule.applies(profile)
        if confidence is None:
            continue
        fired.append({
            "signal": rule.signal,
            "confidence": round(confidence, 2),
            "evidence": rule.evidence,
            "recommended_services": list(rule.recommended_services),
        })
        suppressed.update(rule.suppresses)
    return fired


# ===========================================================================
# CLI — standalone testing (uses the independent fallback scan, on purpose)
# ===========================================================================
def _cli() -> None:
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        description="Tech Stack Detection — inspect a domain's stored profile, "
                    "or run a one-off scan if it was never profiled by Phase 1.")
    parser.add_argument("--domain", required=True, help="Domain or URL to inspect.")
    parser.add_argument("--json", action="store_true", help="Print the full profile as JSON.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    profile = get_stored_profile(args.domain)
    if args.json:
        print(_json.dumps(profile, ensure_ascii=False, indent=2, default=str))
        return

    print("\n" + "=" * 66)
    print("TECH STACK DETECTION")
    print("=" * 66)
    print(f"Domain : {args.domain}")
    if profile.get("error"):
        print(f"Error  : {profile['error']}")
    else:
        print("CAPABILITIES (status: detected / unknown — never assume unknown means absent):")
        for name, cap in profile["capabilities"].items():
            status = cap["status"]
            ident = cap.get("technology") or cap.get("provider")
            if status == STATUS_DETECTED:
                detail = ident or "yes"
                if cap.get("version"):
                    detail += f" v{cap['version']}"
                print(f"  {name:<18}: {status:<10} {detail}")
            else:
                print(f"  {name:<18}: {status:<10} ({cap.get('reason', '')})")
        print("-" * 66)
        print(f"detected services ({len(profile['detected_services'])}): "
              f"{', '.join(profile['detected_services'])}")
        print("-" * 66)
        signals = profile.get("sales_signals", [])
        print(f"SALES SIGNALS ({len(signals)}):")
        for s in signals:
            print(f"  [{s['confidence']:.2f}] ({s['evidence']}) {s['signal']}")
            print(f"       -> {', '.join(s['recommended_services'])}")
    print("=" * 66 + "\n")


if __name__ == "__main__":
    _cli()
