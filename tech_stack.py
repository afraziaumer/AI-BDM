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
import re
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
# Two DIFFERENT detection engines ship inside the SAME installed
# python-Wappalyzer package, against the SAME technologies.json database:
#
#   1. The legacy `Wappalyzer`/`WebPage` classes (wappalyzer/Wappalyzer.py) —
#      what this module used exclusively before. Only checks 5 signal types:
#      url, headers, script SRC, meta tags, raw html text.
#   2. `wappalyzer.core.analyzer.analyze_from_response` — checks up to 12:
#      certIssuer, scriptSrc, dom, meta, xhr, html, js (inline scripts),
#      cookies, headers, url, dns (TXT/MX/NS/SOA/CNAME), robots.txt — plus
#      the same technology-implies-chain expansion.
#
# Engine 1 alone is almost certainly why CRM/payments/chat/auth/CDN fields
# were coming back null so often: many SaaS tools are ONLY reliably
# fingerprinted via a COOKIE (e.g. HubSpot's `hubspotutk`, Shopify's
# `_shopify_y`) or a DNS TXT record (many email/CRM providers publish a
# verification TXT record) — signals engine 1 never looks at, full stop.
#
# We now run BOTH engines and merge their output into one dict — nothing
# engine 1 could find is lost, and engine 2 adds cookies/dom/xhr/dns/robots
# on top, all from data we ALREADY have in memory except two genuinely cheap
# extras: one DNS lookup (no HTTP at all) and one small robots.txt fetch.
# Deliberately NOT using engine 2's "balanced"/"full" scan_type — both of
# those ALSO fetch the content of every external <script src> tag found on
# the page (unbounded extra requests per business); we use "fast" (which
# still gets cookies/dom/xhr/meta/html/headers/url/inline-js) and add DNS +
# robots ourselves as two small, explicitly bounded extras.
_WAPPALYZER = None  # lazy singleton: technologies.json is loaded once, reused


def _get_wappalyzer():
    global _WAPPALYZER
    if _WAPPALYZER is None:
        from wappalyzer import Wappalyzer  # optional dependency, imported lazily
        _WAPPALYZER = Wappalyzer.latest()
    return _WAPPALYZER


def _run_wappalyzer_on_webpage(webpage) -> Dict[str, Dict[str, Any]]:
    """Run the LEGACY engine (url/headers/scriptSrc/meta/html only) against a
    WebPage and return the complete raw result.

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


class _CookieJarShim:
    """Duck-types just enough of `requests.cookies.RequestsCookieJar` for
    `analyze_from_response` (it only ever calls `.cookies.get_dict()`)."""
    def __init__(self, cookies: Dict[str, str]):
        self._cookies = cookies

    def get_dict(self) -> Dict[str, str]:
        return self._cookies


class _FakeResponse:
    """Duck-types just enough of `requests.Response` for
    `wappalyzer.core.analyzer.analyze_from_response` to run against data we
    ALREADY have in memory — no second fetch. Headers are wrapped
    case-insensitively (the same type `requests.Response.headers` normally
    is) since tech_db fingerprint keys and our own captured header casing
    aren't guaranteed to match otherwise."""
    def __init__(self, text: str, url: str, headers: Dict[str, str], cookies: Dict[str, str]):
        self.text = text
        self.url = url
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.cookies = _CookieJarShim(cookies or {})


def _parse_set_cookie_headers(raw_cookies: Optional[List[str]]) -> Dict[str, str]:
    """[{"...raw Set-Cookie header string..."}] -> {name: value}. Best-effort;
    a malformed cookie header is skipped, never raised."""
    if not raw_cookies:
        return {}
    from http.cookies import SimpleCookie
    parsed: Dict[str, str] = {}
    for raw in raw_cookies:
        try:
            jar = SimpleCookie()
            jar.load(raw)
            for key, morsel in jar.items():
                parsed[key] = morsel.value
        except Exception:  # noqa: BLE001 - cookie parsing is best-effort
            continue
    return parsed


_TECH_DB_PATCHED = False


def _ensure_corrected_tech_db() -> None:
    """The installed python-Wappalyzer package has a real bug in
    wappalyzer/core/config.py: it loads technologies.json's ENTIRE top-level
    JSON object as `tech_db` directly — {"$schema": "../schema.json",
    "technologies": {...1270 real fingerprints...}, "categories": {...}} —
    instead of unwrapping the nested "technologies" key. Every consumer that
    iterates `tech_db.items()` (engine 2's analyze_from_response, and
    get_cats_and_groups) then treats "$schema"'s STRING value as if it were a
    per-technology fingerprint dict. It crashes deterministically the moment
    it reaches `'js' in tech_data` for that entry: "js" is a genuine substring
    of "../schema.json" (the ".json" at the end), so the membership check
    passes and the next line's `tech_data['js']` tries to index a plain
    string, raising "string indices must be integers, not 'str'".

    We correct this ONCE, monkey-patching the fixed dict into every module
    that already did `from wappalzyer.core.config import tech_db` (a
    name-binding import — patching wappalyzer.core.config.tech_db alone
    would NOT reach modules that already imported the name into their own
    namespace). This is a data/wiring bug in the installed library, not
    something an integration on our side can route around any other way.
    """
    global _TECH_DB_PATCHED
    if _TECH_DB_PATCHED:
        return
    from wappalyzer.core import analyzer as _wappalyzer_analyzer
    from wappalyzer.core import config as _wappalyzer_config
    from wappalyzer.core import utils as _wappalyzer_utils

    raw_db = _wappalyzer_config.tech_db
    corrected = raw_db.get("technologies", raw_db) if isinstance(raw_db, dict) else {}
    # Defensive: drop anything that still isn't a per-tech dict (covers a
    # possible future package version with a different malformed shape).
    corrected = {k: v for k, v in corrected.items() if isinstance(v, dict)}
    _wappalyzer_config.tech_db = corrected
    _wappalyzer_analyzer.tech_db = corrected
    _wappalyzer_utils.tech_db = corrected
    _TECH_DB_PATCHED = True
    logger.info(
        "Corrected a data-loading bug in the installed python-Wappalyzer "
        "package (technologies.json wasn't being unwrapped) — %d real "
        "technology fingerprints now usable (was 2 bogus entries).",
        len(corrected),
    )


def _extended_static_detections(
    url: str, html: str, headers: Optional[Dict[str, str]], cookies: Optional[Dict[str, str]],
) -> Dict[str, Dict[str, Any]]:
    """Engine 2 (see module docstring above), scan_type="fast" — cookies,
    DOM structure, XHR/scriptSrc URL patterns, meta, raw html, inline-script
    JS patterns, headers, and url — all from data already in memory, no
    network request. Normalized to the SAME {"versions", "categories",
    "confidence"} shape engine 1 returns (plus a bonus "groups" key) so every
    downstream consumer works unchanged."""
    from wappalyzer.core.analyzer import analyze_from_response

    _ensure_corrected_tech_db()
    response = _FakeResponse(text=html, url=url, headers=headers, cookies=cookies)
    raw = analyze_from_response(response, scan_type="fast")
    normalized: Dict[str, Dict[str, Any]] = {}
    for name, info in raw.items():
        version = info.get("version") or ""
        normalized[name] = {
            "versions": [version] if version else [],
            "categories": info.get("categories", []),
            "confidence": info.get("confidence", 100),
            "groups": info.get("groups", []),
        }
    return normalized


def _dns_based_detections(domain: str) -> Dict[str, Dict[str, Any]]:
    """DNS TXT/MX/NS/SOA/CNAME record fingerprint matching — genuinely free
    (no HTTP request at all), and one of the few reliable signals for email/
    CRM/verification providers that never appear anywhere in a site's HTML
    (e.g. an SPF/DKIM TXT record naming the email platform in use)."""
    _ensure_corrected_tech_db()  # MUST run before the import below binds tech_db
    from wappalyzer.core.config import tech_db
    from wappalyzer.core.matcher import match_dict
    from wappalyzer.core.utils import get_cats_and_groups
    from wappalyzer.parsers.dns import get_dns

    records = get_dns(domain)
    if not any(records.values()):
        return {}
    detected: Dict[str, Dict[str, Any]] = {}
    for name, tech_data in tech_db.items():
        if "dns" not in tech_data:
            continue
        matched, version, confidence = match_dict(tech_data["dns"], records)
        if matched:
            cats, groups = get_cats_and_groups(name)
            detected[name] = {"versions": [version] if version else [], "categories": cats,
                              "confidence": confidence, "groups": groups}
    return detected


def _robots_based_detections(url: str) -> Dict[str, Dict[str, Any]]:
    """robots.txt content fingerprint matching — one small extra native
    fetch (not through Scrape.do; no proxy credits), the same class of
    cheap reconnaissance request phase1_pipeline.py already makes for
    sitemap/contact discovery."""
    _ensure_corrected_tech_db()  # MUST run before the import below binds tech_db
    from wappalyzer.core.config import tech_db
    from wappalyzer.core.matcher import match
    from wappalyzer.core.utils import get_cats_and_groups
    from wappalyzer.parsers.robots import get_robots

    text = get_robots(url)
    if not text:
        return {}
    detected: Dict[str, Dict[str, Any]] = {}
    for name, tech_data in tech_db.items():
        if "robots" not in tech_data:
            continue
        matched, version, confidence = match(tech_data["robots"], text)
        if matched:
            cats, groups = get_cats_and_groups(name)
            detected[name] = {"versions": [version] if version else [], "categories": cats,
                              "confidence": confidence, "groups": groups}
    return detected


def _merge_raw_detections(
    base: Dict[str, Dict[str, Any]], extra: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Union two {tech_name: {...}} detections from different signal sources
    — NEVER drops a technology either side found. An overlapping tech takes
    the higher confidence and the union of versions/categories/groups."""
    merged = {name: dict(info) for name, info in base.items()}
    for name, info in extra.items():
        if name not in merged:
            merged[name] = dict(info)
            continue
        existing = merged[name]
        existing["confidence"] = max(existing.get("confidence", 0), info.get("confidence", 0))
        existing["versions"] = sorted(set(existing.get("versions", [])) | set(info.get("versions", [])))
        existing["categories"] = sorted(set(existing.get("categories", [])) | set(info.get("categories", [])))
        if info.get("groups") or existing.get("groups"):
            existing["groups"] = sorted(set(existing.get("groups", [])) | set(info.get("groups", [])))
    return merged


def analyze_raw_html(
    domain: str, html: str,
    headers: Optional[Dict[str, str]] = None,
    cookies: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Fingerprint technologies from HTML/headers ALREADY fetched by the
    crawler. Makes NO network request for the main detection pass — this is
    the integrated Phase-1 path. `cookies` is the list of raw Set-Cookie
    header strings from the SAME response (see phase1_pipeline.py), used for
    cookie-based fingerprints that engine 1 alone could never see.

    Runs the legacy engine, the extended static engine (cookies/dom/xhr/
    inline-js), a DNS lookup, and a robots.txt fetch — merging all four into
    ONE dict. Each stage is independently best-effort: a failure in any one
    of them only means fewer signals, never a broken profile (see the
    per-stage try/except in the caller, phase1_pipeline.py's crawl_site).
    """
    from wappalyzer import WebPage
    url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}/"
    webpage = WebPage(url, html=html, headers=headers or {})
    raw = _run_wappalyzer_on_webpage(webpage)

    cookie_dict = _parse_set_cookie_headers(cookies)
    try:
        raw = _merge_raw_detections(raw, _extended_static_detections(url, html, headers, cookie_dict))
    except Exception as exc:  # noqa: BLE001 - extra signal is best-effort
        logger.warning("Extended Wappalyzer detection failed for %s: %s", domain, exc)

    try:
        raw = _merge_raw_detections(raw, _dns_based_detections(domain))
    except Exception as exc:  # noqa: BLE001 - extra signal is best-effort
        logger.warning("DNS-based tech detection failed for %s: %s", domain, exc)

    try:
        raw = _merge_raw_detections(raw, _robots_based_detections(url))
    except Exception as exc:  # noqa: BLE001 - extra signal is best-effort
        logger.warning("robots.txt-based tech detection failed for %s: %s", domain, exc)

    return raw


def _fetch_and_analyze(domain: str) -> tuple:
    """Independent, single-request fallback path — used ONLY when a domain
    was never profiled during its Phase-1 crawl (see get_stored_profile) and
    for the standalone CLI. Never used by the integrated crawler path.

    Returns (raw, html) — html is returned too so the caller can also run
    run_rule_based_detection on it; ("", "") on total failure."""
    url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}/"
    from wappalyzer import WebPage
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=FALLBACK_FETCH_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.info("Tech-stack fallback fetch failed for %s: %s", domain, exc)
        return {}, ""
    if resp.status_code >= 400:
        return {}, ""
    raw = _run_wappalyzer_on_webpage(WebPage.new_from_response(resp))
    cookie_dict = requests.utils.dict_from_cookiejar(resp.cookies)
    try:
        raw = _merge_raw_detections(
            raw, _extended_static_detections(url, resp.text, dict(resp.headers), cookie_dict),
        )
    except Exception as exc:  # noqa: BLE001 - extra signal is best-effort
        logger.warning("Extended Wappalyzer detection failed for %s: %s", domain, exc)
    try:
        raw = _merge_raw_detections(raw, _dns_based_detections(domain))
    except Exception as exc:  # noqa: BLE001 - extra signal is best-effort
        logger.warning("DNS-based tech detection failed for %s: %s", domain, exc)
    try:
        raw = _merge_raw_detections(raw, _robots_based_detections(url))
    except Exception as exc:  # noqa: BLE001 - extra signal is best-effort
        logger.warning("robots.txt-based tech detection failed for %s: %s", domain, exc)
    return raw, resp.text


# ===========================================================================
# Secondary rule-based detection — independent of Wappalyzer's fingerprint DB
# ===========================================================================
# A second, INDEPENDENT pass over the raw HTML for named indicators of common
# business tools, regardless of whether Wappalyzer's technologies.json has a
# maintained fingerprint for them. Lower confidence than a real Wappalyzer
# match (these are broad keyword/domain patterns, not curated signatures) —
# every finding is tagged source="Rule Engine" so callers never confuse the
# two. A category name here doubles as the NORMALIZED_BUCKETS bucket it fills
# (see build_capabilities) when Wappalyzer found nothing for that bucket.
_RULE_INDICATORS: Dict[str, List[tuple]] = {
    "crm": [
        ("Salesforce", r"salesforce|force\.com|lightning\.force"),
        ("HubSpot", r"hubspot|hs-scripts\.com|hsforms"),
        ("Zoho", r"zoho\.com|zohopublic"),
        ("Microsoft Dynamics", r"dynamics\.com|dynamics[\s-]?365"),
        ("Pipedrive", r"pipedrive"),
        ("Freshworks", r"freshworks|freshsales|freshdesk"),
        ("Bitrix24", r"bitrix24"),
    ],
    "login_system": [
        ("Login/Sign-in page", r"\b(log[\s-]?in|sign[\s-]?in)\b"),
        ("My Account", r"\bmy[\s-]?account\b"),
        ("Client Portal", r"\bclient[\s-]?portal\b"),
        ("Dashboard", r"\bdashboard\b"),
        ("Member Area", r"\bmember[\s-]?(area|portal|login)\b"),
    ],
    "payments": [
        ("Stripe", r"stripe\.com|js\.stripe\.com"),
        ("PayPal", r"paypal\.com|paypalobjects"),
        ("Square", r"squareup\.com|square\.site"),
        ("Authorize.net", r"authorize\.net"),
        ("Checkout", r"\bcheckout\b"),
        ("Cart", r"\b(shopping[\s-]?)?cart\b"),
        ("Buy Now", r"\bbuy[\s-]?now\b"),
    ],
    "chat": [
        ("Intercom", r"intercom\.io|widget\.intercom"),
        ("Drift", r"drift\.com|js\.driftt\.com"),
        ("Crisp", r"crisp\.chat"),
        ("Zendesk", r"zendesk|zdassets"),
        ("Tawk.to", r"tawk\.to"),
        ("LiveChat", r"livechatinc|livechat\.com"),
    ],
    "analytics": [
        ("Google Analytics", r"google-analytics\.com|gtag\("),
        ("GA4", r"googletagmanager\.com/gtag/js\?id=G-|\bG-[A-Z0-9]{6,}\b"),
        ("Google Tag Manager", r"googletagmanager\.com"),
        ("Mixpanel", r"mixpanel\.com"),
        ("Hotjar", r"hotjar\.com"),
        ("Segment", r"segment\.(com|io)|cdn\.segment"),
        ("Meta Pixel", r"connect\.facebook\.net.*fbevents|fbq\("),
    ],
    "authentication": [
        ("OAuth", r"\boauth\b"),
        ("Auth0", r"auth0\.com"),
        ("Okta", r"okta\.com"),
        ("Clerk", r"clerk\.(dev|com)"),
        ("Firebase Auth", r"identitytoolkit\.googleapis\.com|firebaseapp\.com"),
    ],
}
# Generic English words/phrases (as opposed to a specific vendor domain or
# script) score lower — "dashboard" or "cart" appearing in text is much
# weaker evidence than a literal "stripe.com" script reference.
_RULE_GENERIC_TERMS = frozenset({
    "Login/Sign-in page", "My Account", "Client Portal", "Dashboard",
    "Member Area", "Checkout", "Cart", "Buy Now", "OAuth",
})


def run_rule_based_detection(html: str) -> List[Dict[str, Any]]:
    """Secondary, Wappalyzer-independent scan of the raw HTML (covers script
    src attributes, inline script text, meta tags, and visible content all
    at once, since we regex the whole raw HTML string) for named indicators
    of common business tools. Never raises — an empty list on any failure."""
    try:
        haystack = html or ""
        findings: List[Dict[str, Any]] = []
        for category, indicators in _RULE_INDICATORS.items():
            for tech_name, pattern in indicators:
                if not re.search(pattern, haystack, re.IGNORECASE):
                    continue
                confidence = 55 if tech_name in _RULE_GENERIC_TERMS else 85
                findings.append({
                    "technology": tech_name, "category": category,
                    "version": None, "confidence": confidence, "source": "Rule Engine",
                })
        return findings
    except Exception as exc:  # noqa: BLE001 - never breaks the crawl
        logger.warning("Rule-based tech detection failed: %s", exc)
        return []


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
                "confidence": info.get("confidence", 100), "source": "wappalyzer"}
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
    raw: Dict[str, Dict[str, Any]], discovered_urls: Iterable[str],
    rule_based: Optional[List[Dict[str, Any]]] = None,
) -> tuple:
    """(has_login_page, has_customer_portal) from an Authentication-category
    detection, a login/portal-shaped URL among everything Phase 1 discovered
    on this site (fetched or merely linked-to — both are valid signals), OR
    the secondary rule engine spotting a login/sign-in/dashboard/portal
    indicator directly in the HTML that the URL-path heuristic missed."""
    urls = list(discovered_urls)
    has_login = bool(
        any("auth" in name.lower() for name, info in raw.items()
            if "Authentication" in info.get("categories", []))
        or _paths_match(urls, _LOGIN_PATH_HINTS)
        or any(r["category"] == "login_system" for r in (rule_based or []))
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

    {"status", id_field, "version", "confidence", "reason", "source"} — ALL
    keys always present (never omitted) so downstream consumers never need to
    guess which fields exist for which status. `confidence` is 0-1
    (Wappalyzer reports 0-100); `reason` is populated only when status !=
    "detected". `source` is "wappalyzer" or "rule_engine" (see build_capabilities).
    """
    if items:
        top = items[0]
        return {
            "status": STATUS_DETECTED,
            id_field: top["name"],
            "version": top.get("version"),
            "confidence": round(min(100, top.get("confidence", 100)) / 100, 2),
            "reason": None,
            "source": top.get("source", "wappalyzer"),
        }
    return {
        "status": STATUS_UNKNOWN,
        id_field: None,
        "version": None,
        "confidence": None,
        "reason": f"No public fingerprint detected for {category_label}.",
        "source": None,
    }


def build_capabilities(
    normalized: Dict[str, List[Dict[str, Any]]],
    has_login: bool, has_portal: bool,
    rule_based: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Structured, 3-state business summary — the quick-glance file
    (tech_stack.json). Every category is an object, never a bare bool/null:

        {"crm": {"status": "detected", "technology": "HubSpot",
                 "version": null, "confidence": 0.97, "reason": null,
                 "source": "wappalyzer"},
         "backend": {"status": "unknown", "technology": null, "version": null,
                     "confidence": null,
                     "reason": "No public fingerprint detected for backend.",
                     "source": null}}

    `status: "unknown"` means "not publicly detectable" — it must never be
    read downstream as "confirmed absent" (see module docstring / STATUS_*).

    A bucket Wappalyzer left empty is filled from the secondary rule engine's
    findings for that same bucket (see run_rule_based_detection), if any —
    Wappalyzer's own fingerprint match always takes priority and is never
    overridden by a looser keyword rule.
    """
    rule_by_bucket: Dict[str, List[Dict[str, Any]]] = {}
    for r in (rule_based or []):
        rule_by_bucket.setdefault(r["category"], []).append(r)

    caps: Dict[str, Any] = {}
    for bucket in NORMALIZED_BUCKETS:
        id_field = "provider" if bucket in _PROVIDER_STYLE_CATEGORIES else "technology"
        items = normalized.get(bucket) or []
        if items:
            caps[bucket] = _status_object(items, bucket, id_field)
        elif rule_by_bucket.get(bucket):
            top = max(rule_by_bucket[bucket], key=lambda r: r["confidence"])
            caps[bucket] = {
                "status": STATUS_DETECTED, id_field: top["technology"],
                "version": top.get("version"),
                "confidence": round(top["confidence"] / 100, 2),
                "reason": None, "source": "rule_engine",
            }
        else:
            caps[bucket] = _status_object([], bucket, id_field)

    # Capability-style entries inferred from multiple signals (Authentication
    # tech + discovered URL paths + rule-engine indicators), not a single
    # named vendor — same 3-state shape, just without a "technology"/
    # "provider" identity to report.
    caps["login_system"] = (
        {"status": STATUS_DETECTED, "confidence": None, "reason": None, "source": None} if has_login
        else {"status": STATUS_UNKNOWN, "confidence": None,
              "reason": "No login/authentication page or technology publicly detected.",
              "source": None}
    )
    caps["customer_portal"] = (
        {"status": STATUS_DETECTED, "confidence": None, "reason": None, "source": None} if has_portal
        else {"status": STATUS_UNKNOWN, "confidence": None,
              "reason": "No customer-portal-shaped page publicly discovered.",
              "source": None}
    )
    return caps


def build_website_profile(
    domain: str,
    raw: Dict[str, Dict[str, Any]],
    *,
    homepage_url: str = "",
    fetch_method: str = "",
    discovered_urls: Optional[Iterable[str]] = None,
    rule_based: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Assemble the full technical knowledge-base profile for one domain.
    Nothing is ever discarded — every Wappalyzer detection (across all its
    signal types: url/headers/scriptSrc/meta/html/dom/xhr/js/cookies/dns/
    robots) and every secondary rule-engine finding is preserved somewhere in
    this profile, even when it doesn't fit one of the curated buckets below.

    {
      "raw_wappalyzer": {...},        exactly as Wappalyzer returned it (all
                                       signal types merged; see analyze_raw_html)
      "normalized_tech_stack": {...}, curated business buckets
      "versions": {...},              tech name -> versions, preserved exactly
      "categories": {...},            EVERY wappalyzer category -> tech names
      "capabilities": {...},          flattened quick-glance summary
      "detected_services": [...],     flat, deduped list of every tech name
      "rule_based_detections": [...], every secondary rule-engine finding
      "all_detections": [...],        {technology, category, version,
                                       confidence, source} for EVERY
                                       Wappalyzer + rule-engine finding, in
                                       one unified list
      "crawl_metadata": {...},
      "last_scanned": "...", "scan_version": "..."
    }
    """
    rule_based = rule_based or []
    normalized = build_normalized_tech_stack(raw)
    has_login, has_portal = detect_login_and_portal(raw, discovered_urls or [], rule_based)
    capabilities = build_capabilities(normalized, has_login, has_portal, rule_based)

    all_detections: List[Dict[str, Any]] = []
    for name, info in raw.items():
        categories = info.get("categories") or ["Uncategorized"]
        version = (info.get("versions") or [None])[0]
        for cat in categories:
            all_detections.append({
                "technology": name, "category": cat, "version": version,
                "confidence": info.get("confidence", 100), "source": "Wappalyzer",
            })
    all_detections.extend(rule_based)

    return {
        "raw_wappalyzer": raw,
        "normalized_tech_stack": normalized,
        "versions": build_versions_view(raw),
        "categories": build_categories_view(raw),
        "capabilities": capabilities,
        "detected_services": sorted(raw.keys()),
        "rule_based_detections": rule_based,
        "all_detections": all_detections,
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
    raw, homepage_html = _fetch_and_analyze(domain_key)
    if not raw:
        # Refresh failed (site down/blocking) — serve the stale profile rather
        # than nothing; a stale-but-present profile beats an empty one.
        if stored is not None:
            stored.setdefault("sales_signals", generate_sales_signals_from_profile(stored))
            return stored
        return {"error": "fetch_failed", "domain": domain_key,
                "capabilities": {}, "sales_signals": []}

    discovered = _indexed_urls_for_domain(domain_key)
    rule_based = run_rule_based_detection(homepage_html)
    profile = build_website_profile(domain_key, raw, discovered_urls=discovered, rule_based=rule_based)
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
