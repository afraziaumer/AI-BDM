"""
AI BDM Platform - Phase 1 Engine Pipeline
=========================================

Query -> Intent -> web-search discovery -> two-tier scrape -> contacts -> CSV.

Execution order:
  1. Intent deconstruction (LLM_planner.py): also extracts result_limit and the
     web search query, plus expanded exclude/include keywords for Step 5.
  2. Discovery via paginated Serper web search (returns business websites
     directly and scales to the requested count).
  3. Two-tier scavenger scrape: fast native aiohttp request first, then
     ZenRows residential proxy + JS render only for blocked URLs.
  4. Extract email + phone, then thread-safe append to a local CSV archive.

The number of results comes from the query itself ("give me 50 marinas...");
there is no --limit flag. Env keys are read flexibly:
  - Groq   : groq_llm_apikey1  | GROQ_API_KEY
  - Serper : serper            | SERPER_API_KEY
  - ZenRows: zenrows           | ZENROWS_API_KEY

Run:
  ./env/bin/python phase1_pipeline.py --query "give me 50 marinas in Dubai with no crm"
  ./env/bin/python phase1_pipeline.py --query "marinas in Miami with no smart monitoring"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import socket
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, unquote, urljoin

import aiohttp
import phonenumbers
import tldextract
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from email_extractor import EmailExtractor

# Reuse the already-tested Step-1 planner instead of duplicating LLM logic.
from LLM_planner import plan_query, classify_business

# Raw HTML fields can exceed the default 128 KB CSV field limit; raise it so the
# cache reader (load_cache) and any downstream reads don't crash.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# --- Logging ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("Phase1Engine")

# aiohttp logs the raw getaddrinfo tracebacks ("gaierror exception in shielded
# future") at ERROR level when a DNS lookup blips. Our request code already
# catches and handles those failures, so silence the noisy internal tracebacks.
logging.getLogger("aiohttp.connector").setLevel(logging.CRITICAL)


def _quiet_dns_exception_handler(loop, context) -> None:
    """Swallow ONLY intermittent DNS-resolution noise from shielded futures.

    aiohttp shields its DNS lookups; when one blips, the exception surfaces via
    the event loop's default handler as a scary gaierror traceback even though
    the request itself is caught and handled. Suppress just those; defer every
    other error to the default handler so nothing real is hidden.
    """
    exc = context.get("exception")
    if isinstance(exc, socket.gaierror):
        return
    loop.default_exception_handler(context)


def _make_connector() -> "aiohttp.TCPConnector":
    """Build a DNS-resilient aiohttp connector.

    Two changes make this robust to the intermittent DNS blips seen on macOS /
    sandboxed networks (`gaierror: nodename nor servname provided`):

      * use_dns_cache + ttl_dns_cache: a host is resolved once and its IP reused
        for 5 minutes, so a later blip on the same host doesn't fail the request.
      * family=AF_INET: force IPv4 only. macOS getaddrinfo often attempts an IPv6
        (AAAA) lookup that fails with exactly this error even when IPv4 works.
    """
    return aiohttp.TCPConnector(
        use_dns_cache=True,
        ttl_dns_cache=300,
        family=socket.AF_INET,
        limit=100,
        limit_per_host=8,
    )


# --- Configuration ---------------------------------------------------------
load_dotenv()

SERPER_API_KEY = os.getenv("serper") or os.getenv("SERPER_API_KEY")
# Tier-2 scraping provider: Scrape.do (replaces ZenRows). Token is read from the
# existing env field so .env needs no renaming — prefers `scrapedo`, but falls
# back to the old `zenrows` field if the token still lives there.
SCRAPEDO_API_KEY = (
    os.getenv("scrapedo") or os.getenv("SCRAPEDO_API_KEY")
    or os.getenv("zenrows") or os.getenv("ZENROWS_API_KEY")
)

OUTPUT_CSV_FILE = "scavenger_leads_cache.csv"
# Tracks which Serper page each general-search query is up to, so the next run
# picks up where the previous left off (pages 1-3 run 1 → pages 4-6 run 2…).
SEARCH_STATE_FILE = "search_state.json"
# Records the most recent query and the set of businesses it touched, so the
# data-quality pipeline can scope its report to just the latest query instead of
# the whole (cumulative) raw store.
LAST_RUN_FILE = "last_run.json"

SERPER_SEARCH_URL = "https://google.serper.dev/search"
SERPER_PLACES_URL = "https://google.serper.dev/places"
SCRAPEDO_URL = "https://api.scrape.do/"

# Tunable operational parameters (kept as named constants, not magic numbers).
DEFAULT_RESULT_LIMIT = 20        # used when the query/plan specifies no count
MAX_SEARCH_PAGES = 10            # always sweep the top 10 Google (Serper) pages
SEARCH_RESULTS_PER_PAGE = 10     # Serper returns ~10 organic results per page
MAX_DIRECTORIES_TO_HARVEST = 6   # cap directory pages we crawl for outbound links
MAX_PLACES_PAGES = 8             # Google Maps (Places) pages to sweep for businesses
SERPER_TIMEOUT_S = 15            # discovery requests
NATIVE_FETCH_TIMEOUT_S = 12      # Tier-1 free request
SCRAPEDO_TIMEOUT_S = 30          # Tier-2 proxied + JS render (give up if slower)
HTTP_OK = (200, 201)
HTTP_BLOCKED = (403, 429, 503)   # statuses that should escalate to Tier-2

# Whole-site crawl: from each business homepage, follow same-domain internal
# links and scrape every page (capped so a large site can't run forever).
MAX_PAGES_PER_SITE = 25          # hard cap on pages scraped per business
MAX_CRAWL_DEPTH = 2              # homepage + direct links + one useful second hop
# Hard cap on fetch ATTEMPTS per site (successful or 404). Bounds the cost of
# probing contact-path candidates that may not exist on a given site.
MAX_FETCH_ATTEMPTS = 60
MIN_CACHED_PAGES_TO_TRUST = 3    # shallower cache entries are refreshed when links exist

# URL path fragments that indicate a contact/about page. Fetched before any
# other subpage (10x higher email density) and used to filter sitemap URLs.
# Multilingual: emails on EU sites live on Impressum/Kontakt/Contatti/etc., and
# Germany/Austria/Switzerland legally MANDATE an Impressum page with contact data.
CONTACT_PAGE_HINTS = frozenset({
    # English
    "contact", "contact-us", "contactus", "reach-us", "get-in-touch",
    "about", "about-us", "aboutus", "team", "staff", "people",
    "hello", "connect", "enquiry", "enquiries", "imprint", "legal",
    # German (impressum = legally required contact page)
    "impressum", "kontakt", "ueber-uns", "uber-uns",
    # French
    "contactez-nous", "nous-contacter", "mentions-legales", "a-propos",
    # Italian / Spanish / Portuguese / Dutch
    "contatti", "chi-siamo", "contacto", "contactanos", "contato",
    "quienes-somos", "sobre-nosotros",
})

# Explicit contact-page paths probed directly even when the homepage doesn't
# link to them (unlinked /impressum, /contact pages are common).
CONTACT_PATH_CANDIDATES = (
    "/contact", "/contact-us", "/contactus", "/get-in-touch", "/reach-us",
    "/about", "/about-us", "/team",
    "/impressum", "/kontakt",            # DE/AT/CH
    "/contatti",                          # IT
    "/contacto",                          # ES
    "/contato",                           # PT
    "/contact-nl",                        # NL fallback
    "/mentions-legales", "/nous-contacter",  # FR
)

# Where to look for an XML sitemap when robots.txt doesn't name one.
SITEMAP_CANDIDATES = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")
MAX_SITEMAP_CONTACT_URLS = 20    # cap contact URLs harvested from sitemaps
MAX_CHILD_SITEMAPS = 8           # cap nested sitemaps followed from an index
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_ROBOTS_SITEMAP_RE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)\s*$")

# Post-scrape lead filtering = the negative-keyword "no X" rules (Step 5) plus
# the LLM relevance classifier (Step 5.5). ENABLED: discovery heuristics catch
# obvious junk (directories/blogs/app-stores) but can't catch everything, so the
# classifier is the semantic backstop that drops aggregators, product shops,
# wrong-location and unrelated results before they're stored as leads.
LEAD_FILTERING_ENABLED = True

# When True, each chosen email's domain is MX-validated (DNS only, no mail sent)
# before it is accepted. Undeliverable/dead domains are dropped, so stored emails
# are deliverable. Results are cached per domain so re-checks are free.
VERIFY_EMAIL_MX = True

# Confidence floor for accepting an email. An on-domain business address scores
# ~100+; a business's own gmail clears this; a lone off-domain template/demo
# address (e.g. "here@sota.my" on a .ae site) scores below it and is dropped —
# storing no email is better than storing a wrong one.
EMAIL_MIN_SCORE = 40

# Separator used to pack multiple emails/phones into a single CSV cell, e.g.
# "info@x.com | sales@x.com". Chosen so it never collides with an email/phone.
CONTACT_SEP = " | "

# Volatile URL query params that change between runs (Google's srsltid, ad-click
# ids, UTM campaign tags). Stripped before a URL is used as a cache key so the
# same site isn't re-scraped just because its tracking tag changed.
TRACKING_PARAMS = frozenset({
    "srsltid", "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "yclid",
    "mc_eid", "igshid", "_ga", "ref", "ref_src",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
})
# File extensions that are assets, not readable pages — never crawled.
SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".pdf", ".zip", ".rar", ".gz", ".mp4", ".mp3", ".avi", ".mov", ".wmv",
    ".css", ".js", ".json", ".xml", ".rss", ".woff", ".woff2", ".ttf", ".eot",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
)

# --- Contact extraction ---
# Email: strict RFC-5321 subset. Requires a real TLD (alpha-only, 2+ chars)
# and uses word boundaries so it cannot match inside asset paths or URLs.
EMAIL_RE = re.compile(
    r"(?<![a-zA-Z0-9._%+\-])"   # not preceded by an email char
    r"[a-zA-Z0-9._%+\-]{1,64}"  # local-part (max 64 per RFC)
    r"@"
    r"[a-zA-Z0-9\-]{1,63}"      # domain label
    r"(?:\.[a-zA-Z0-9\-]{1,63})*"  # optional sub-domains
    r"\.[a-zA-Z]{2,}"            # TLD — alpha-only, no digits
    r"(?![a-zA-Z0-9._%+\-])",   # not followed by an email char
    re.IGNORECASE,
)
# Fallback phone RE used ONLY when the planner provides no phone_regex.
# Very loose on purpose — it is just a candidate collector; _clean_phone
# and phonenumbers.parse do the real validation.
_PHONE_RE_FALLBACK = re.compile(r"\+?\d[\d\s().\-]{7,}\d")
# Substrings that mark a regex 'email' match as junk.
JUNK_EMAIL_HINTS = (
    "example.", "yourdomain", "domain.com", "email@", "sentry", "wixpress",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", "@2x", "u003e", "name@",
    "noreply@", "no-reply@", "donotreply@",
)

# Domains that are directories/aggregators/social, not a business's own site.
# Web search for "<category> in <place>" surfaces many of these; we skip them
# so the pipeline scrapes actual business homepages.
AGGREGATOR_DOMAINS = (
    # social
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.com", "reddit.com",
    # directories / reviews / maps
    "yelp.com", "tripadvisor.com", "mapquest.com", "foursquare.com",
    "google.com", "goo.gl", "maps.app.goo.gl", "wikipedia.org",
    "yellowpages.com", "bbb.org", "indeed.com", "glassdoor.com",
    # travel / booking aggregators
    "booking.com", "expedia.com", "agoda.com", "trip.com", "kayak.com",
    "marinas.com", "navily.com", "visitdubai.com", "lonelyplanet.com",
    # ad / tracker networks
    "reklam5.com", "doubleclick.net", "googlesyndication.com",
    "googleadservices.com", "taboola.com", "outbrain.com", "adnxs.com",
    # social share / chat / link shorteners / app stores (exact)
    "wa.me", "t.me", "bit.ly", "linktr.ee", "apps.apple.com", "itunes.apple.com",
    "play.google.com", "m.me", "api.whatsapp.com",
)

# Brand-level blocklist (TLD-agnostic): matches the registered-domain LABEL, so
# tripadvisor.com AND tripadvisor.be / yelp.co.uk / zomato.pk are ALL blocked.
# These are review sites, directories, aggregators, listicles and Q&A sites —
# never the official business website we want. (A real business named e.g.
# "bookingmarina.com" is safe: its label is "bookingmarina", not "booking".)
AGGREGATOR_BRANDS = frozenset({
    # restaurant / food reviews + delivery + directories
    "tripadvisor", "yelp", "zomato", "opentable", "foodpanda", "restaurantguru",
    "wanderlog", "eater", "thefork", "grubhub", "ubereats", "deliveroo",
    "doordash", "happycow", "timeout", "citysearch", "sirved", "menupix",
    "pakistanfoodportal", "pakistanirestaurants", "restaurantji", "allmenus",
    # travel / hotel aggregators + review
    "booking", "expedia", "agoda", "trivago", "kayak", "hotels", "hostelworld",
    "lonelyplanet", "getyourguide", "viator", "evendo",
    # general directories / listings / Q&A / ranking
    "yellowpages", "foursquare", "mapquest", "yell", "manta", "hotfrog",
    "quora", "reddit", "wikipedia", "wikivoyage", "crunchbase", "glassdoor",
    "indeed", "bbb", "clutch", "trustpilot", "sitejabber", "justdial",
    "sulekha", "citypass", "cylex", "brownbook", "n49", "cybo",
    # blog / publishing platforms (never an official business homepage)
    "wordpress", "blogspot", "blogger", "medium", "substack", "tumblr",
    "wix", "weebly", "squarespace", "godaddysites", "webador",
    # app stores + social / chat + link shorteners (brand-level; short ones like
    # wa.me / t.me / bit.ly are exact-matched in AGGREGATOR_DOMAINS instead).
    "apple", "itunes", "microsoft", "whatsapp", "telegram",
    "linktree", "tinyurl",
})

# Subdomains, anchor texts and paths that mark a link as a utility/CTA/footer
# link (sign-up, help, login, ads...) rather than an actual business site.
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

# Offline domain extractor (bundled public-suffix snapshot; no network fetch).
_TLD = tldextract.TLDExtract(
    suffix_list_urls=(),
    cache_dir=os.getenv("TLDEXTRACT_CACHE", ".tldextract-cache"),
)

# Path fragments and title patterns that mark a result as an article/listicle/
# directory page rather than a single business homepage.
ARTICLE_PATH_HINTS = (
    "/blog", "/news", "/article", "/articles", "/guide", "/guides",
    "/region/", "/browse/", "/explore", "/wiki/", "/category/", "/list",
    "/directory", "/directories",
)
# Listicle / directory / review title signals — matched ANYWHERE in the title
# (not just the start), since "Cafe Near Me - Best Coffee Shops in Islamabad"
# and "Exploring the Best Cafes" are listicles too.
ARTICLE_TITLE_RE = re.compile(
    r"\b("
    r"\d+\s+best|best\s+\d+|top\s+\d+|\d+\s+(?:best|top|great|famous|popular)|"
    r"the\s+best\b|best\b|top\b|"
    r"a\s+guide\b|guide\s+to\b|ultimate\s+guide\b|complete\s+guide\b|"
    r"exploring\b|near\s+me\b|"
    # "<category> in <city>" listicle phrasing (cafes/coffee shops/restaurants in …)
    r"(?:cafe|cafes|cafés?|coffee\s+shops?|restaurants?|hotels?|bars?|"
    r"places?|things\s+to\s+do)\s+(?:in|near|around)\b|"
    r"where\s+to\b|must[- ]?visit\b|reviews?\b|ranked\b|listings?\b|directory\b"
    r")",
    re.IGNORECASE,
)

CSV_HEADERS = [
    "company_name",
    "website_url",           # root site (groups all pages of one business)
    "page_url",              # the specific page this row was scraped from
    "page_title",            # the page's <title>
    "meta_description",      # the page's meta description (short clean summary)
    "email",
    "phone_number",
    "physical_address",
    "scrape_source_method",  # "NATIVE" or "SCRAPEDO"
    "date_added",            # ISO-8601 UTC timestamp this row was first stored
    "page_text",             # cleaned main content (nav/footer/cookie removed)
    "raw_html",              # the page's raw HTML (kept separate from page_text)
]

# Tags whose contents are never useful page text and would pollute the store.
_NOISE_TAGS = ("script", "style", "noscript", "template", "svg", "head")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

ANTI_BOT_SIGNATURES = [
    "just a moment...",
    "checking your browser",
    "captcha",
    "ddos protection",
    "cf-browser-verification",
    "attention required",
]

# A single lock guards CSV appends so concurrent tasks never interleave rows.
_csv_lock = threading.Lock()


# === Step 1: Intent deconstruction =========================================
async def deconstruct_intent(user_prompt: str) -> Dict[str, Any]:
    """Translate a messy human query into the structured planning blueprint.

    Delegates to LLM_planner.plan_query (sync, with its own primary/fallback
    model failover) and runs it off the event loop so we stay non-blocking.
    """
    logger.info("Deconstructing intent for query: %r", user_prompt)
    plan = await asyncio.to_thread(plan_query, user_prompt)
    logger.info("Intent blueprint: %s", json.dumps(plan, ensure_ascii=False))
    return plan


# === Step 2: Target footprint discovery ====================================
def _is_aggregator(host: str) -> bool:
    """True if host is a review/directory/aggregator, not an official business.

    Checks exact/suffix domains AND the TLD-agnostic brand label, so country
    variants (tripadvisor.be, yelp.co.uk, zomato.pk) are all caught.
    """
    if any(host == d or host.endswith("." + d) for d in AGGREGATOR_DOMAINS):
        return True
    brand = _TLD.extract_str(host).domain.lower()
    return brand in AGGREGATOR_BRANDS


def _domain_key(url_or_host: str) -> str:
    """Registered domain (e.g. 'help.predictwind.com' -> 'predictwind.com').

    Used to de-duplicate so a company's many subdomains count as one business.
    """
    ext = _TLD.extract_str(url_or_host)
    # top_domain_under_public_suffix is the non-deprecated equivalent of registered_domain
    domain = (
        getattr(ext, "top_domain_under_public_suffix", None)
        or getattr(ext, "registered_domain", None)
        or ext.domain
    )
    return (domain or "").lower()


_DOMAIN_IN_QUERY_RE = re.compile(r"\b((?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})\b")


def _detect_domain(text: str) -> str:
    """Return the registered domain mentioned in a query, else "".

    Fallback for specific-search detection when the planner doesn't flag one
    (e.g. the user types "xyzmarina.com"). Validates against the public suffix
    list so "e.g." or "5.5" aren't mistaken for domains.
    """
    for match in _DOMAIN_IN_QUERY_RE.findall(text or ""):
        candidate = match.lower().removeprefix("www.")
        if _TLD.extract_str(candidate).suffix:  # has a real TLD
            return _domain_key(candidate)
    return ""


def _strip_tracking(url: str) -> str:
    """Drop volatile tracking query params (srsltid, utm_*, gclid...) and the
    fragment, giving one stable URL for the same page across runs. Without this
    the cache misses whenever Google re-stamps a result with a new srsltid tag.
    """
    if not url:
        return url
    try:
        p = urlparse(url)
    except ValueError:
        return url
    kept = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(kept), ""))


def _is_utility_link(host: str, path: str, anchor: str) -> bool:
    """True if a harvested link is a sign-up/help/login/ad/CTA link, not a site."""
    subdomain = host.split(".")[0]
    if subdomain in UTILITY_SUBDOMAINS:
        return True
    if any(hint in path.lower() for hint in UTILITY_PATH_HINTS):
        return True
    return bool(anchor and UTILITY_ANCHOR_RE.search(anchor))


def _is_internal_crawl_noise(host: str, path: str) -> bool:
    """True for same-site pages that are not useful crawl targets.

    This intentionally does NOT inspect anchor text. Internal CTAs such as
    "Contact Us", "Book Now", "Reserve", and "Menu" are exactly the pages the
    extraction/routing stages need, even though those phrases are utility noise
    when harvesting outbound links from a third-party directory.
    """
    subdomain = host.split(".")[0]
    if subdomain in {"cdn", "static", "assets", "img", "images", "media"}:
        return True
    low = path.lower()
    noisy_fragments = (
        "/wp-admin", "/wp-login", "/wp-json", "/cdn-cgi", "/cart", "/wishlist",
        "/privacy", "/terms", "/cookies", "/cookie-policy", "/sitemap",
        "/robots.txt", "/feed", "/rss", "/tag/", "/author/", "/search",
    )
    return any(fragment in low for fragment in noisy_fragments)


def _looks_like_article(title: str, path: str) -> bool:
    """True if the result is a blog/listicle/directory page, not a homepage."""
    if any(hint in path.lower() for hint in ARTICLE_PATH_HINTS):
        return True
    return bool(title and ARTICLE_TITLE_RE.match(title))


async def _read_html(resp: aiohttp.ClientResponse) -> str:
    """Decode a response body as UTF-8 first (most sites), falling back to the
    declared charset only if that fails. Avoids the mojibake (â€™, Ã©) you get
    when aiohttp trusts a wrong/missing charset header on a UTF-8 page.
    """
    raw = await resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(resp.charset or "latin-1", errors="replace")


async def _native_get(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Cheap, best-effort native GET (no ZenRows). Returns HTML or None."""
    try:
        async with session.get(
            url, headers=BROWSER_HEADERS,
            timeout=aiohttp.ClientTimeout(total=NATIVE_FETCH_TIMEOUT_S),
        ) as resp:
            if resp.status in HTTP_OK:
                return await _read_html(resp)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Native GET failed for %s: %s", url, exc)
    return None


async def harvest_directory(
    session: aiohttp.ClientSession,
    directory_url: str,
    seen_hosts: Set[str],
    needed: int,
) -> List[Dict[str, Any]]:
    """Follow a directory/listicle's outbound links to individual businesses.

    Fetches the directory cheaply (native only), then returns up to `needed`
    new targets: external links whose host is not the directory itself, not an
    aggregator, and not already seen. Each business is normalized to its
    homepage URL so the scraper hits the root site.
    """
    html = await _native_get(session, directory_url)
    if not html:
        return []

    source_domain = _domain_key(directory_url)
    found: List[Dict[str, Any]] = []
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().startswith("http"):  # only cross-site absolute links
            continue
        parsed = urlparse(href)
        host = parsed.netloc.lower().removeprefix("www.")
        domain = _domain_key(href)
        anchor = a.get_text(strip=True)
        if not host or domain == source_domain or domain in seen_hosts:
            continue
        if _is_aggregator(host) or _looks_like_article(anchor, parsed.path):
            continue
        # Drop footer/CTA/utility/ad links (sign-up, help, login, satellite...).
        if _is_utility_link(host, parsed.path, anchor):
            logger.debug("Skipping utility link: %s (%r)", href, anchor)
            continue
        seen_hosts.add(domain)
        found.append(
            {
                "title": anchor or host,
                "website": f"{parsed.scheme}://{parsed.netloc}/",  # business homepage
                "snippet": f"(harvested from {source_domain})",
            }
        )
        if len(found) >= needed:
            break

    if found:
        logger.info("Harvested %d businesses from directory %s", len(found), source_domain)
    return found


# === Search-state persistence ==============================================
_state_lock = threading.Lock()


def _load_search_state() -> Dict[str, int]:
    """Load {query -> next_serper_page} from disk. Returns {} if missing."""
    try:
        with open(SEARCH_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_search_state(state: Dict[str, int]) -> None:
    with _state_lock:
        with open(SEARCH_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)


def _save_last_run(query: str, results: List[Dict[str, Any]]) -> None:
    """Record the latest query and the registered domains of the businesses it
    touched, so the data-quality pipeline can scope its report to this run only.

    Skipped when the run produced no results so a failed/empty run never wipes
    the previous (still-valid) scope.
    """
    domains = sorted({
        _domain_key(r.get("website_url") or "")
        for r in results
        if r.get("website_url")
    } - {""})
    if not domains:
        return
    payload = {
        "query": query,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "domains": domains,
    }
    with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def load_last_run() -> Dict[str, Any]:
    """Return the most recent run's scope ({query, timestamp, domains}) or {}."""
    try:
        with open(LAST_RUN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# === Phone formatting ======================================================
def _format_phone(raw: str, country_hint: str = "") -> str:
    """Parse and format a phone number into E.164 (+XXXXXXXXXXX).

    Prefer a genuinely valid number formatted as E.164; if none of the regions
    yields a valid number, keep the cleaned raw value rather than dropping it
    (recall first — a possibly-messy real number beats an empty cell).
    `country_hint` is a two-letter ISO code from the site's geo (e.g. "AE") so
    local numbers without a leading + still parse.
    """
    if not raw:
        return ""
    raw = raw.strip()
    # Try with the geo hint first, then without (handles explicit + numbers).
    for region in ([country_hint.upper()] if country_hint else []) + [None]:
        try:
            parsed = phonenumbers.parse(raw, region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
        except phonenumbers.NumberParseException:
            continue
    # Not a valid number. Keep it ONLY if it explicitly carries an international
    # "+" prefix (a real number we just can't validate); otherwise it's a stray
    # digit run the regex mis-caught (e.g. "212206612192") — drop it.
    return raw if raw.startswith("+") else ""


# Map common country/city strings to ISO-3166-1 alpha-2 codes for the hint.
_GEO_TO_COUNTRY: Dict[str, str] = {
    "usa": "US", "united states": "US", "texas": "US", "florida": "US",
    "california": "US", "new york": "US", "miami": "US", "houston": "US",
    "uae": "AE", "dubai": "AE", "abu dhabi": "AE", "united arab emirates": "AE",
    "uk": "GB", "united kingdom": "GB", "london": "GB",
    "canada": "CA", "australia": "AU", "germany": "DE",
    "france": "FR", "india": "IN", "singapore": "SG",
    "qatar": "QA", "saudi arabia": "SA", "bahrain": "BH",
    "malaysia": "MY", "thailand": "TH", "portugal": "PT",
    "spain": "ES", "italy": "IT", "mexico": "MX",
}


def _country_code_from_geo(geo: str) -> str:
    """Return a best-guess ISO country code from a geo_location string."""
    low = geo.lower()
    for token, code in _GEO_TO_COUNTRY.items():
        if token in low:
            return code
    return ""


async def discover_places(
    session: aiohttp.ClientSession,
    query: str,
    limit: int,
    exclude_domains: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Google Maps (Serper Places) discovery — real individual businesses.

    Organic web search for a query like "marinas in Argentina" returns
    directories and blog listicles, not the marinas themselves. The Places
    endpoint returns actual business listings (name, address, website), which is
    the right source when the user wants "N businesses in a location". Only
    listings that expose a website are returned, since we need a site to scrape.
    """
    if not SERPER_API_KEY:
        logger.error("Serper key missing (set `serper` or `SERPER_API_KEY` in .env).")
        return []

    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    targets: List[Dict[str, Any]] = []
    seen_hosts: Set[str] = set(exclude_domains or ())

    for page in range(1, MAX_PLACES_PAGES + 1):
        if len(targets) >= limit:
            break
        payload = {"q": query, "page": page}
        try:
            async with session.post(
                SERPER_PLACES_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=SERPER_TIMEOUT_S),
            ) as resp:
                if resp.status != 200:
                    logger.error("Serper places failed (status=%s).", resp.status)
                    break
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001 - network layer, log and degrade
            logger.error("Serper places error (page %d): %s", page, exc)
            break

        places = data.get("places", [])
        if not places:
            break

        for item in places:
            website = (item.get("website") or "").strip()
            if not website:
                continue                         # need a site to scrape
            host = urlparse(website).netloc.lower().removeprefix("www.")
            domain = _domain_key(website)
            if not host or domain in seen_hosts or _is_aggregator(host):
                continue
            seen_hosts.add(domain)
            targets.append({
                "title": item.get("title", "") or "",
                "website": website,
                "snippet": item.get("address", "") or "",
            })
            if len(targets) >= limit:
                break

    logger.info("Places discovery: %d businesses with websites for %r.",
                len(targets), query)
    return targets


async def discover_targets(
    session: aiohttp.ClientSession,
    query: str,
    limit: int,
    exclude_domains: Optional[Set[str]] = None,
    start_page: int = 1,
) -> tuple[List[Dict[str, Any]], int]:
    """Paginated Serper web search starting at `start_page`.

    Returns (targets, last_page_fetched). The caller persists `last_page_fetched`
    so the next run resumes from `last_page_fetched + 1` instead of page 1,
    progressively working deeper into Google results across repeated queries.
    `exclude_domains` seeds the dedup set so already-stored businesses are skipped.
    """
    if not SERPER_API_KEY:
        logger.error("Serper key missing (set `serper` or `SERPER_API_KEY` in .env).")
        return [], start_page

    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    logger.info(
        "Discovery query: %r | pages %d+ | target %d new | %d known excluded",
        query, start_page, limit, len(exclude_domains or ()),
    )

    targets: List[Dict[str, Any]] = []
    directories: List[str] = []
    seen_hosts: Set[str] = set(exclude_domains or ())
    page = start_page
    last_page = start_page

    while len(targets) < limit and page <= start_page + MAX_SEARCH_PAGES - 1:
        payload = {"q": query, "num": SEARCH_RESULTS_PER_PAGE, "page": page}
        try:
            async with session.post(
                SERPER_SEARCH_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=SERPER_TIMEOUT_S),
            ) as resp:
                if resp.status != 200:
                    logger.error("Serper search failed (status=%s).", resp.status)
                    break
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001 - network layer, log and degrade
            logger.error("Serper search error (page %d): %s", page, exc)
            break

        organic = data.get("organic", [])
        if not organic:
            logger.info("Serper returned no results at page %d — end of index.", page)
            break

        last_page = page
        for item in organic:
            link = item.get("link", "")
            title = item.get("title", "") or ""
            parsed = urlparse(link)
            host = parsed.netloc.lower().removeprefix("www.")
            domain = _domain_key(link)
            if not host or domain in seen_hosts or _is_aggregator(host):
                continue
            if _looks_like_article(title, parsed.path):
                directories.append(link)
                continue
            seen_hosts.add(domain)
            targets.append(
                {"title": title, "website": link, "snippet": item.get("snippet", "")}
            )
            if len(targets) >= limit:
                break
        page += 1

    direct_count = len(targets)

    # Top up from directories when direct results fell short.
    for directory_url in directories[:MAX_DIRECTORIES_TO_HARVEST]:
        if len(targets) >= limit:
            break
        harvested = await harvest_directory(
            session, directory_url, seen_hosts, needed=limit - len(targets)
        )
        targets.extend(harvested)

    logger.info(
        "Discovery: %d sites (%d direct, %d harvested) | pages %d-%d",
        len(targets), direct_count, len(targets) - direct_count, start_page, last_page,
    )
    return targets, last_page


# === Step 3: Two-tier scavenger scrape =====================================
async def execute_scavenger_scrape(
    session: aiohttp.ClientSession, target_url: str
) -> Dict[str, Any]:
    """Tier-1 native fetch; on block/error fail over to Tier-2 Scrape.do."""
    # --- Tier 1: low-cost native request ---
    try:
        logger.info("Tier-1 native fetch: %s", target_url)
        async with session.get(
            target_url,
            headers=BROWSER_HEADERS,
            timeout=aiohttp.ClientTimeout(total=NATIVE_FETCH_TIMEOUT_S),
        ) as resp:
            body = await _read_html(resp)
            waf_detected = any(sig in body.lower() for sig in ANTI_BOT_SIGNATURES)
            if resp.status in HTTP_OK and not waf_detected:
                logger.info("Tier-1 success: %s", target_url)
                return {"html": body, "method": "NATIVE"}
            logger.warning(
                "Tier-1 blocked (status=%s, waf=%s): %s",
                resp.status, waf_detected, target_url,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tier-1 connection failed for %s: %s", target_url, exc)

    # --- Tier 2: Scrape.do (premium proxy + JS render) for blocked sites ---
    if not SCRAPEDO_API_KEY:
        logger.error("Scrape.do token missing; cannot escalate %s", target_url)
        return {"html": "", "method": "FAILED"}

    logger.info("Tier-2 Scrape.do escalation: %s", target_url)
    params: Dict[str, str] = {
        "token": SCRAPEDO_API_KEY,
        "url": target_url,
        # render=true and super=true require paid add-ons; omit both so the
        # request works on the base plan. Add them back if the account upgrades:
        #   "render": "true",   # JS rendering
        #   "super": "true",    # premium residential proxies
    }
    try:
        async with session.get(
            SCRAPEDO_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=SCRAPEDO_TIMEOUT_S),
        ) as resp:
            if resp.status == 200:
                body = await _read_html(resp)
                logger.info("Tier-2 success: %s", target_url)
                return {"html": body, "method": "SCRAPEDO"}
            logger.error("Tier-2 Scrape.do failed (status=%s): %s", resp.status, target_url)
            return {"html": "", "method": "FAILED"}
    except asyncio.TimeoutError:
        logger.error("Tier-2 Scrape.do timed out (>%ss): %s", SCRAPEDO_TIMEOUT_S, target_url)
        return {"html": "", "method": "FAILED"}
    except Exception as exc:  # noqa: BLE001
        logger.error("Tier-2 Scrape.do error for %s: %s", target_url, repr(exc))
        return {"html": "", "method": "FAILED"}


# === Step 3.5: Whole-site crawl ============================================
def _normalize_page_url(url: str) -> str:
    """Canonical form for de-dup: drop fragment, lowercase host, trim trailing /."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{host}{path}{query}"


def _extract_internal_links(html: str, base_url: str, root_domain: str) -> List[str]:
    """Return same-domain, crawlable page links found in `html`."""
    soup = BeautifulSoup(html, "lxml")
    links: List[str] = []
    seen: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        # Resolve relative links exactly as a browser would.
        href = urljoin(base_url, href)
        parsed = urlparse(href)
        if _domain_key(href) != root_domain:       # same business only
            continue
        if parsed.path.lower().endswith(SKIP_EXTENSIONS):  # assets, not pages
            continue
        if _is_internal_crawl_noise(parsed.netloc.lower().removeprefix("www."),
                                    parsed.path):
            continue
        norm = _normalize_page_url(href)
        if norm in seen:
            continue
        seen.add(norm)
        links.append(href)
    return links


def _contact_priority(url: str) -> int:
    """Return a sort key for crawl ordering: lower = fetch sooner.

    Contact/about pages are fetched before product/news/gallery pages because
    they have 10x higher email density. Everything else gets equal priority.
    """
    path = urlparse(url).path.lower().strip("/")
    last_segment = path.rsplit("/", 1)[-1]
    if last_segment in CONTACT_PAGE_HINTS or path in CONTACT_PAGE_HINTS:
        return 0   # highest priority
    for hint in CONTACT_PAGE_HINTS:
        if hint in path:
            return 1
    return 2       # normal pages


def _path_is_contactish(url: str) -> bool:
    """True if a URL's path looks like a contact/about/legal page (any language)."""
    path = urlparse(url).path.lower()
    return any(hint in path for hint in CONTACT_PAGE_HINTS)


async def _discover_contact_urls(
    session: aiohttp.ClientSession, root_url: str, root_domain: str
) -> List[str]:
    """Find the pages where emails actually live — via robots.txt, the XML
    sitemap, and a fixed list of likely contact paths — WITHOUT fetching the
    pages themselves. Returns ordered, deduped, same-domain absolute URLs.

    This is the single biggest recall lever: contact data clusters on a handful
    of predictable pages (contact/about/team/impressum), and the sitemap usually
    hands us their exact URLs instead of us having to crawl and hope.
    """
    base = urlparse(root_url)
    origin = f"{base.scheme}://{base.netloc}"
    found: List[str] = []
    seen: Set[str] = set()

    def _add(url: str) -> None:
        if _domain_key(url) != root_domain:
            return
        norm = _normalize_page_url(url)
        if norm not in seen:
            seen.add(norm)
            found.append(url)

    # 1) Fixed probe list — deterministic, language-aware, catches UNLINKED pages.
    for path in CONTACT_PATH_CANDIDATES:
        _add(f"{origin}{path}")

    # 2) robots.txt → authoritative Sitemap: locations (+ common defaults).
    sitemap_urls: List[str] = []
    robots = await _native_get(session, f"{origin}/robots.txt")
    if robots:
        sitemap_urls.extend(_ROBOTS_SITEMAP_RE.findall(robots))
    sitemap_urls.extend(f"{origin}{p}" for p in SITEMAP_CANDIDATES)

    # 3) Fetch sitemaps (following one level of sitemap-index nesting) and keep
    #    only the contact-ish URLs.
    harvested = 0
    checked: Set[str] = set()
    for sm in sitemap_urls:
        if harvested >= MAX_SITEMAP_CONTACT_URLS:
            break
        sm_norm = _normalize_page_url(sm)
        if sm_norm in checked or _domain_key(sm) != root_domain:
            continue
        checked.add(sm_norm)
        xml = await _native_get(session, sm)
        if not xml:
            continue
        locs = _LOC_RE.findall(xml)
        # A sitemap index points to more sitemaps; follow them once.
        if locs and all(loc.lower().endswith(".xml") for loc in locs[:3]):
            for child in locs[:MAX_CHILD_SITEMAPS]:
                if harvested >= MAX_SITEMAP_CONTACT_URLS:
                    break
                child_norm = _normalize_page_url(child)
                if child_norm in checked or _domain_key(child) != root_domain:
                    continue
                checked.add(child_norm)
                child_xml = await _native_get(session, child)
                if not child_xml:
                    continue
                for loc in _LOC_RE.findall(child_xml):
                    if _path_is_contactish(loc):
                        _add(loc)
                        harvested += 1
        else:
            for loc in locs:
                if _path_is_contactish(loc):
                    _add(loc)
                    harvested += 1

    return found


async def crawl_site(
    session: aiohttp.ClientSession,
    root_url: str,
    root_html: str,
    root_method: str,
) -> List[Dict[str, str]]:
    """Contact-first crawl of one business site.

    Order of attack:
      1. Probe known contact pages discovered via robots.txt / sitemap / a fixed
         path list (where emails actually live — including unlinked pages).
      2. Then follow the homepage's own internal links, contact/about first.

    The homepage was already fetched via the two-tier scrape; subpages use the
    same native-then-Scrape.do fetch path so blocked internal pages do not
    silently disappear. Bounded by MAX_PAGES_PER_SITE (pages kept) and
    MAX_FETCH_ATTEMPTS (total fetches, so missing probes can't run up the cost).
    Returns [{page_url, html, method}].
    """
    root_domain = _domain_key(root_url)
    pages: List[Dict[str, str]] = [
        {"page_url": root_url, "html": root_html, "method": root_method}
    ]
    visited: Set[str] = {_normalize_page_url(root_url)}

    # High-value contact pages first (sitemap/robots/probe), then homepage links.
    priority_urls = await _discover_contact_urls(session, root_url, root_domain)
    homepage_links = sorted(
        _extract_internal_links(root_html, root_url, root_domain),
        key=_contact_priority,
    )
    # Priority URLs at depth 1; homepage links at depth 1 too, but after probes.
    queue: List[tuple[str, int]] = (
        [(u, 1) for u in priority_urls] + [(u, 1) for u in homepage_links]
    )

    head = 0
    attempts = 0
    while (
        head < len(queue)
        and len(pages) < MAX_PAGES_PER_SITE
        and attempts < MAX_FETCH_ATTEMPTS
    ):
        page_url, depth = queue[head]
        head += 1
        norm = _normalize_page_url(page_url)
        if norm in visited or depth > MAX_CRAWL_DEPTH:
            continue
        visited.add(norm)

        attempts += 1
        fetched = await execute_scavenger_scrape(session, page_url)
        html = fetched.get("html") or ""
        if not html:
            continue
        pages.append({
            "page_url": page_url,
            "html": html,
            "method": fetched.get("method", "FAILED"),
        })

        if depth < MAX_CRAWL_DEPTH:
            inner = _extract_internal_links(html, page_url, root_domain)
            for link in sorted(inner, key=_contact_priority):
                if _normalize_page_url(link) not in visited:
                    queue.append((link, depth + 1))

    logger.info(
        "Crawled %d page(s) of %s (%d fetches, %d priority candidates)",
        len(pages), root_domain, attempts, len(priority_urls),
    )
    return pages


# === Step 4: Local CSV storage =============================================
_MOJIBAKE_MARKERS = ("Ã", "Ð", "Ñ", "Â", "â€", "Å", "Ÿ")


def _fix_mojibake(text: str) -> str:
    """Repair double-encoded text (e.g. 'Ð¡Ñ‚Ð°Ñ‚Ð¸Ð¸' -> 'Статии').

    Some sites serve UTF-8 text that was already mis-decoded as Windows-1252,
    producing garbled accented/Cyrillic characters. This reverses that by
    re-encoding to cp1252 and decoding as UTF-8 — but only keeps the result if
    it actually reduces the mojibake, so clean text is never corrupted.
    """
    if not text or not any(m in text for m in _MOJIBAKE_MARKERS):
        return text
    try:
        repaired = text.encode("cp1252", "ignore").decode("utf-8", "ignore")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    before = sum(text.count(m) for m in _MOJIBAKE_MARKERS)
    after = sum(repaired.count(m) for m in _MOJIBAKE_MARKERS)
    return repaired if after < before else text


def extract_page_fields(html: str) -> Dict[str, str]:
    """Extract a page's content WITHOUT dropping anything useful.

    Returns {page_title, meta_description, page_text}. We add the title and meta
    description as their own clean fields (organization), but page_text keeps the
    FULL visible text — including footers/nav where emails, phone numbers and
    addresses live. Only truly invisible tags (script/style/head) are removed.
    """
    empty = {"page_title": "", "meta_description": "", "page_text": ""}
    if not html:
        return empty
    soup = BeautifulSoup(html, "lxml")

    # Capture title + meta description before stripping the <head>.
    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = (
        soup.find("meta", attrs={"name": "description"})
        or soup.find("meta", attrs={"property": "og:description"})
    )
    meta_desc = (meta.get("content", "") if meta else "").strip()

    # Replace Cloudflare-obfuscated email placeholders with the real address so
    # "[email protected]" doesn't pollute the text and the email is recoverable.
    for el in soup.find_all(attrs={"data-cfemail": True}):
        real = _decode_cf_email(el.get("data-cfemail", ""))
        if real:
            el.replace_with(real)

    # Remove only non-visible tags; keep ALL real content (nav/footer included).
    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    # Keep line structure so callers can de-duplicate the boilerplate that
    # repeats across a site's pages (menu/header/footer). Blanks dropped.
    # Each line is repaired for mojibake (double-encoded UTF-8) along the way.
    lines = [
        _fix_mojibake(" ".join(ln.split()))
        for ln in soup.get_text(separator="\n").split("\n")
        if ln.strip()
    ]
    return {
        "page_title": _fix_mojibake(title),
        "meta_description": _fix_mojibake(meta_desc),
        "page_text": " ".join(lines),
        "lines": lines,
    }


def _valid_email(candidate: str) -> bool:
    """Reject asset filenames, placeholders, noreply, and malformed addresses."""
    if not candidate or "@" not in candidate:
        return False
    low = candidate.lower()
    if any(h in low for h in JUNK_EMAIL_HINTS):
        return False
    local, _, domain = candidate.partition("@")
    # Local part must exist and not be all dots
    if not local or local.replace(".", "") == "":
        return False
    # Domain must have at least one dot and a TLD of 2+ alpha chars
    parts = domain.split(".")
    if len(parts) < 2:
        return False
    tld = parts[-1]
    if not tld.isalpha() or len(tld) < 2:
        return False
    # Reject consecutive dots
    if ".." in candidate:
        return False
    return True


def _decode_cf_email(encoded: str) -> Optional[str]:
    """Decode a Cloudflare-obfuscated email (its data-cfemail hex string).

    Cloudflare replaces real emails on the page with a placeholder that reads
    "[email protected]" and stashes the real address XOR-encoded in a
    `data-cfemail` attribute. This reverses that so we recover the real email.
    """
    try:
        key = int(encoded[:2], 16)
        decoded = "".join(
            chr(int(encoded[i:i + 2], 16) ^ key)
            for i in range(2, len(encoded), 2)
        )
        return decoded if "@" in decoded else None
    except (ValueError, IndexError):
        return None


def _extract_json_values(obj: Any, key: str) -> List[str]:
    """Recursively collect all string values for `key` in a JSON structure."""
    results: List[str] = []
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], str):
            results.append(obj[key])
        for v in obj.values():
            results.extend(_extract_json_values(v, key))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_extract_json_values(item, key))
    return results


def _clean_phone(candidate: str) -> Optional[str]:
    """Reject dates and strings with too few/many digits; return the stripped string."""
    candidate = candidate.strip()
    if re.fullmatch(r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}", candidate):
        return None
    digits = re.sub(r"\D", "", candidate)
    if not 7 <= len(digits) <= 15:
        return None
    return candidate




def extract_contacts(
    html: str,
    text: str,
    country_code: str = "",
    phone_regex: str = "",
    site_domain: str = "",
    verify_mx: bool = False,
    email_min_score: int = 0,
) -> Dict[str, Any]:
    """Extract ALL contact info from a scraped page: every email, every phone,
    and the physical address.

    Returns a dict with:
      emails  : list[str] — all trustworthy addresses (on-domain/free first)
      phones  : list[str] — all valid unique numbers (E.164 where possible)
      address : str       — physical address (schema.org / <address>) or ""
      email   : str|None  — the single best email (back-compat)
      phone   : str|None  — the single best phone (back-compat)

    Sync (BeautifulSoup + optional DNS) — call off the event loop via
    asyncio.to_thread from async code.
    """
    soup = BeautifulSoup(html or "", "lxml")

    # === Emails (all of them) ================================================
    extractor = EmailExtractor(html or "", site_domain=site_domain)
    emails = extractor.all_addresses(verify_mx=verify_mx)
    # Keep a single "best" for back-compat / ranking display.
    best_email = extractor.best(verify_mx=verify_mx, min_score=email_min_score) \
        or (emails[0] if emails else None)

    # === Phones (all valid, deduped) =========================================
    if phone_regex:
        try:
            active_re = re.compile(phone_regex, re.IGNORECASE | re.MULTILINE)
        except re.error:
            active_re = _PHONE_RE_FALLBACK
    else:
        active_re = _PHONE_RE_FALLBACK

    phones: List[str] = []
    seen_phones: set = set()

    def _add_phone(candidate: str) -> None:
        cleaned = _clean_phone(candidate)
        if not cleaned:
            return
        formatted = _format_phone(cleaned, country_code)
        if formatted and formatted not in seen_phones:
            seen_phones.add(formatted)
            phones.append(formatted)

    # tel: links first (explicitly marked up; most reliable). URL-decode so a
    # link like tel:(212)%20752-1163 parses correctly instead of storing "%20".
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("tel:"):
            _add_phone(unquote(href[len("tel:"):]).strip())
    # Then every number matched in the visible text.
    for m in active_re.findall(text or ""):
        raw = m if isinstance(m, str) else m[0]
        _add_phone(raw)

    # Prefer a validated international (+…) number as the single "best".
    best_phone = (
        next((p for p in phones if p.startswith("+")), None)
        or (phones[0] if phones else None)
    )

    # === Physical address ====================================================
    address = _extract_address(soup, text)

    return {
        "emails": emails,
        "phones": phones,
        "address": address,
        "email": best_email,
        "phone": best_phone,
    }


# Street-type words that anchor a US/'/UK-style street address in free text.
_STREET_SUFFIX = (
    r"(?:street|st|avenue|ave|boulevard|blvd|road|rd|drive|dr|lane|ln|way|"
    r"place|pl|court|ct|square|sq|plaza|parkway|pkwy|highway|hwy|terrace|"
    r"suite|ste|floor|fl|unit)"
)
# "123 Main St, New York, NY 10001" — number + words + street-type + tail.
_ADDRESS_RE = re.compile(
    r"\d{1,6}\s+[A-Za-z0-9.\-'#, ]{2,45}?\b" + _STREET_SUFFIX +
    r"\b\.?[A-Za-z0-9.,\-#/'’ ]{0,70}",
    re.IGNORECASE,
)
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")


def _extract_address(soup: BeautifulSoup, text: str = "") -> str:
    """Extract a physical address, trying the most reliable sources first:
    schema.org JSON-LD → microdata → <address> tag → hCard → free-text pattern.
    Returns "" when none is found."""
    # 1) schema.org JSON-LD PostalAddress (most local-business sites)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        found = _find_postal_address(data)
        if found:
            return found
    # 2) HTML5 microdata (itemprop="streetAddress" …)
    md = _address_from_props(soup, "itemprop")
    if md:
        return md
    # 3) <address> element
    tag = soup.find("address")
    if tag:
        txt = " ".join(tag.get_text(" ", strip=True).split())
        if len(txt) >= 8:
            return txt
    # 4) hCard microformat (class="adr" / "street-address" …)
    hcard = _address_from_hcard(soup)
    if hcard:
        return hcard
    # 5) Free-text fallback: a street-address pattern in the visible text.
    return _address_from_text(text or soup.get_text(" "))


def _address_from_props(soup: BeautifulSoup, attr: str) -> str:
    """Assemble an address from elements tagged with schema address properties
    (works for both microdata `itemprop` and RDFa-ish `property`)."""
    parts: List[str] = []
    for prop in ("streetAddress", "addressLocality", "addressRegion", "postalCode"):
        el = soup.find(attrs={attr: re.compile(prop, re.IGNORECASE)})
        if el:
            val = el.get("content") or el.get_text(" ", strip=True)
            if val and val.strip():
                parts.append(val.strip())
    return ", ".join(dict.fromkeys(parts)) if len(parts) >= 2 else ""


def _address_from_hcard(soup: BeautifulSoup) -> str:
    """Assemble an address from hCard microformat classes."""
    adr = soup.find(class_=re.compile(r"\b(adr|address|vcard)\b", re.IGNORECASE))
    if not adr:
        return ""
    parts: List[str] = []
    for cls in ("street-address", "locality", "region", "postal-code"):
        el = adr.find(class_=re.compile(cls, re.IGNORECASE))
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                parts.append(t)
    if len(parts) >= 2:
        return ", ".join(dict.fromkeys(parts))
    # Fall back to the whole hCard block's text if it looks address-like.
    txt = " ".join(adr.get_text(" ", strip=True).split())
    return txt if (8 <= len(txt) <= 120 and _ZIP_RE.search(txt)) else ""


def _address_from_text(text: str) -> str:
    """Last-resort: find a street-address pattern in visible text. Prefers a
    match that also contains a ZIP/postcode (higher confidence)."""
    if not text:
        return ""
    matches = [m.group(0).strip(" .,-") for m in _ADDRESS_RE.finditer(text)]
    if not matches:
        return ""
    # Prefer a match that includes a postal code; else the first reasonable one.
    with_zip = [m for m in matches if _ZIP_RE.search(m)]
    best = " ".join((with_zip or matches)[0].split())
    # If it ends in a ZIP, trim any trailing prose after it ("… 10065 today" →
    # "… 10065") so the stored address is clean.
    zip_hits = list(_ZIP_RE.finditer(best))
    if zip_hits:
        best = best[: zip_hits[-1].end()].strip(" .,-")
    return best if 8 <= len(best) <= 120 else ""


def _assemble_postal(d: Dict[str, Any]) -> str:
    """Join the parts of a schema.org PostalAddress into one line."""
    keys = ("streetAddress", "addressLocality", "addressRegion",
            "postalCode", "addressCountry")
    parts: List[str] = []
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):          # addressCountry can be {name: "US"}
            v = v.get("name", "")
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return ", ".join(parts)


def _find_postal_address(obj: Any) -> str:
    """Recursively locate a PostalAddress (or an 'address' field) in JSON-LD."""
    if isinstance(obj, dict):
        t = obj.get("@type", "")
        is_postal = (t == "PostalAddress"
                     or (isinstance(t, list) and "PostalAddress" in t))
        if is_postal:
            assembled = _assemble_postal(obj)
            if assembled:
                return assembled
        if "address" in obj:
            addr = obj["address"]
            if isinstance(addr, str) and len(addr.strip()) >= 8:
                return addr.strip()
            nested = _find_postal_address(addr)
            if nested:
                return nested
        for v in obj.values():
            nested = _find_postal_address(v)
            if nested:
                return nested
    elif isinstance(obj, list):
        for item in obj:
            nested = _find_postal_address(item)
            if nested:
                return nested
    return ""


def _union_contacts(rows: List[Dict[str, Any]], field: str) -> List[str]:
    """Collect all unique non-N/A values of `field` (which may hold several
    values joined by CONTACT_SEP) across a set of stored page rows."""
    out: List[str] = []
    for r in rows:
        for v in (r.get(field) or "").split(CONTACT_SEP):
            v = v.strip()
            if v and v != "N/A" and v not in out:
                out.append(v)
    return out


def initialize_csv_storage_layer() -> None:
    """Create the CSV with headers if missing; repair a stale/empty header.

    If the file exists but its header doesn't match CSV_HEADERS (e.g. the older
    schema without `page_url`) and it holds no data rows, rewrite the header so
    the new per-page layout lines up. A file that already has data is left as-is.
    """
    if not os.path.exists(OUTPUT_CSV_FILE):
        logger.info("Initializing CSV archive: %s", OUTPUT_CSV_FILE)
        with open(OUTPUT_CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADERS)
        return
    with open(OUTPUT_CSV_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header = rows[0] if rows else []
    data_rows = rows[1:]
    if header != CSV_HEADERS and not any(any(c.strip() for c in r) for r in data_rows):
        logger.info("Upgrading empty CSV to new per-page schema (added page_url).")
        with open(OUTPUT_CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADERS)


def _lead_to_row(lead: Dict[str, Any]) -> List[str]:
    """Flatten a lead/page dict into a CSV row matching CSV_HEADERS order."""
    return [
        lead.get("company_name", "N/A"),
        lead.get("website_url", "N/A"),
        lead.get("page_url", lead.get("website_url", "N/A")),
        lead.get("page_title", ""),
        lead.get("meta_description", ""),
        lead.get("email", "N/A"),
        lead.get("phone_number", "N/A"),
        lead.get("physical_address", "N/A"),
        lead.get("scrape_source_method", "FAILED"),
        lead.get("date_added", ""),
        str(lead.get("page_text", "")),
        str(lead.get("raw_html", "")),
    ]


def _dedupe_shared_lines(
    page_records: List[Dict[str, Any]], page_lines: List[List[str]]
) -> None:
    """Strip boilerplate (menu/header/footer) that repeats across a site's pages.

    A line that appears on at least half of the pages is treated as shared
    template text. It's removed from every page EXCEPT the first (homepage),
    which keeps the full text — so the site's shared content is stored once
    instead of duplicated on every page. Modifies page_records in place.
    """
    n = len(page_records)
    if n < 3:  # too few pages for repetition to be a problem
        return

    from collections import Counter
    doc_freq: Counter = Counter()
    for lines in page_lines:
        for line in set(lines):
            doc_freq[line] += 1

    threshold = max(2, (n + 1) // 2)  # appears on >= ~half the pages
    shared = {line for line, freq in doc_freq.items() if freq >= threshold}
    if not shared:
        return

    # Homepage (index 0) keeps everything; subpages keep only unique lines.
    for i in range(1, n):
        unique = [line for line in page_lines[i] if line not in shared]
        page_records[i]["page_text"] = " ".join(unique)


def append_records_to_csv(records: List[Dict[str, Any]]) -> None:
    """Append a batch of page rows in one locked write, so all pages of a
    business land contiguously in the store (homepage first, then crawl order)."""
    if not records:
        return
    with _csv_lock:
        with open(OUTPUT_CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)  # quotes/escapes embedded commas + quotes
            for lead in records:
                writer.writerow(_lead_to_row(lead))
    logger.debug("Saved %d page row(s) for %s",
                 len(records), records[0].get("company_name"))


def load_cache() -> Dict[str, List[Dict[str, str]]]:
    """Return previously-analyzed businesses keyed by root website URL.

    Each value is the list of that site's stored page rows. Stands in for the
    MongoDB Atlas cache: a business already in the store is skipped (not
    re-crawled), and its pages are aggregated to re-qualify without re-scraping.
    """
    if not os.path.exists(OUTPUT_CSV_FILE):
        return {}
    cached: Dict[str, List[Dict[str, str]]] = {}
    with open(OUTPUT_CSV_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            site = row.get("website_url")
            if site and site != "N/A":
                # Key on the tracking-stripped URL so rows stored under different
                # srsltid tags collapse to one business and match this run's URL.
                cached.setdefault(_strip_tracking(site), []).append(row)
    return cached


def _cache_needs_recrawl(url: str, rows: List[Dict[str, str]]) -> bool:
    """Return True when a cached site is too shallow for its visible links.

    Older runs may have stored only the homepage. Without this guard, every
    future run treats that one row as complete and never crawls the site's
    contact/menu/service pages. We only refresh shallow entries when the cached
    homepage actually exposes more same-site crawl targets.
    """
    if len(rows) >= MIN_CACHED_PAGES_TO_TRUST:
        return False
    homepage = ""
    homepage_url = url
    for row in rows:
        page_url = row.get("page_url") or row.get("website_url") or url
        if _normalize_page_url(page_url) == _normalize_page_url(url):
            homepage = row.get("raw_html", "") or ""
            homepage_url = page_url
            break
    if not homepage and rows:
        homepage = rows[0].get("raw_html", "") or ""
        homepage_url = rows[0].get("page_url") or rows[0].get("website_url") or url
    if not homepage:
        return True
    existing = {
        _normalize_page_url(r.get("page_url") or r.get("website_url") or "")
        for r in rows
    }
    links = _extract_internal_links(homepage, homepage_url, _domain_key(url))
    new_links = [
        link for link in links
        if _normalize_page_url(link) not in existing
    ]
    return bool(new_links)


def _filter_new_page_records(
    page_records: List[Dict[str, Any]], cached_rows: Optional[List[Dict[str, str]]]
) -> List[Dict[str, Any]]:
    """Keep only newly discovered page rows when refreshing a shallow cache."""
    if not cached_rows:
        return page_records
    existing = {
        _normalize_page_url(r.get("page_url") or r.get("website_url") or "")
        for r in cached_rows
    }
    return [
        record for record in page_records
        if _normalize_page_url(record.get("page_url", "")) not in existing
    ]


# === Step 5: Lead qualification ============================================
def qualify_lead(
    page_text: str,
    exclude_keywords: List[str],
    include_keywords: List[str],
) -> Dict[str, Any]:
    """Score a lead's page text against the plan's keyword constraints.

    A lead is QUALIFIED when its text contains none of the exclude_keywords and
    (if include_keywords are given) at least one of them. Empty text cannot be
    assessed, so it is reported as 'no_content' rather than silently passing.
    """
    text = (page_text or "").lower()
    if not text.strip():
        return {"qualified": False, "reason": "no_content",
                "matched_exclude": [], "matched_include": []}

    matched_exclude = [k for k in (exclude_keywords or []) if k.lower() in text]
    matched_include = [k for k in (include_keywords or []) if k.lower() in text]

    if matched_exclude:
        return {"qualified": False, "reason": "excluded",
                "matched_exclude": matched_exclude, "matched_include": matched_include}
    if include_keywords and not matched_include:
        return {"qualified": False, "reason": "missing_required",
                "matched_exclude": [], "matched_include": []}
    return {"qualified": True, "reason": "passed",
            "matched_exclude": [], "matched_include": matched_include}


async def classify_relevance(
    industry: str, geo: str, name: str, page_text: str
) -> Dict[str, str]:
    """Step 5.5 — LLM relevance check (single business vs shop/aggregator).

    Runs off the event loop. Never raises: on any failure it returns 'match' so
    a classifier outage degrades to the old keyword-only behaviour, never
    silently dropping leads.
    """
    try:
        return await asyncio.to_thread(
            classify_business, industry or "", geo or "", name or "", page_text
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Relevance classification failed for %s: %s", name, exc)
        return {"category": "match", "reason": "classifier_unavailable"}


async def _evaluate_lead(
    industry: str, geo: str, name: str, combined: str,
    exclude_keywords: List[str], include_keywords: List[str],
) -> tuple[Dict[str, Any], Optional[Dict[str, str]]]:
    """Return (qualification, classification) for a business.

    When LEAD_FILTERING_ENABLED is False (current default), filtering is skipped
    entirely: the business passes as qualified and NO relevance LLM call is made,
    so discovery alone decides relevance. Flip the flag to re-enable filtering.
    """
    if not LEAD_FILTERING_ENABLED:
        passthrough = {"qualified": True, "reason": "filtering_disabled",
                       "matched_exclude": [], "matched_include": []}
        return passthrough, None

    qualification = qualify_lead(combined, exclude_keywords, include_keywords)
    classification = None
    if qualification.get("qualified"):
        classification = await classify_relevance(industry, geo, name, combined)
    return qualification, classification


# === Orchestration =========================================================
async def process_single_lead(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    target: Dict[str, Any],
    cache: Dict[str, List[Dict[str, str]]],
    exclude_keywords: List[str],
    include_keywords: List[str],
    industry: str = "",
    geo: str = "",
    country_code: str = "",
    phone_regex: str = "",
) -> Dict[str, Any]:
    """Cache-check, crawl the whole site, persist every page, then qualify the
    business across all of its pages combined (Step 5) and classify its
    relevance (Step 5.5: single business vs product shop / aggregator)."""
    name = target.get("title")
    # Canonicalize: strip srsltid/utm/etc so re-runs hit the cache instead of
    # re-scraping. The stripped URL still fetches fine (sites ignore those tags).
    url = _strip_tracking(target.get("website"))

    if not url:
        return {"company_name": name, "website_url": None, "status": "no_website"}

    # Cache check: a business already in the store is not re-crawled. Its stored
    # pages are aggregated and re-qualified against the (possibly new) keywords.
    cached_rows = cache.get(url)
    if cached_rows and not _cache_needs_recrawl(url, cached_rows):
        logger.info(
            "Cache HIT for %s (%d page(s)) — qualifying from cache.",
            url, len(cached_rows),
        )
        combined = " ".join(r.get("page_text", "") for r in cached_rows)
        email = CONTACT_SEP.join(_union_contacts(cached_rows, "email")) or "N/A"
        phone = CONTACT_SEP.join(_union_contacts(cached_rows, "phone_number")) or "N/A"
        qualification, classification = await _evaluate_lead(
            industry, geo, name, combined, exclude_keywords, include_keywords
        )
        return {"company_name": name, "website_url": url, "status": "cache_hit",
                "method": "CACHE", "pages": len(cached_rows), "email": email,
                "phone": phone, "qualification": qualification,
                "classification": classification}
    if cached_rows:
        logger.info(
            "Cache refresh for %s: only %d cached page(s), more internal links visible.",
            url, len(cached_rows),
        )

    # Scrape the homepage with the full two-tier scrape, then crawl the rest of
    # the site from it (internal pages fetched natively).
    async with semaphore:
        root = await execute_scavenger_scrape(session, url)
        if root["method"] == "FAILED" or not root["html"]:
            logger.warning("No content resolved for %s", url)
            return {"company_name": name, "website_url": url, "status": "failed",
                    "method": "FAILED"}
        pages = await crawl_site(session, url, root["html"], root["method"])

    # Build one row per page (keeping each page's lines for de-duplication).
    combined_parts: List[str] = []
    page_records: List[Dict[str, Any]] = []
    page_lines: List[List[str]] = []
    # Accumulate ALL unique contacts across the whole site (order-preserving).
    all_emails: List[str] = []
    all_phones: List[str] = []
    site_address = ""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # country_code and phone_regex come from the planner (passed in as args).
    for page in pages:
        fields = extract_page_fields(page["html"])
        page_text = fields["page_text"]
        if not page_text.strip():
            continue
        # Run off the event loop: BeautifulSoup parsing is CPU-bound and MX
        # verification does DNS I/O, neither of which should block other scrapes.
        contacts = await asyncio.to_thread(
            extract_contacts,
            page["html"], page_text, country_code, phone_regex,
            _domain_key(url), VERIFY_EMAIL_MX, EMAIL_MIN_SCORE,
        )
        page_emails = contacts["emails"]
        page_phones = contacts["phones"]
        for e in page_emails:
            if e not in all_emails:
                all_emails.append(e)
        for p in page_phones:
            if p not in all_phones:
                all_phones.append(p)
        if not site_address and contacts["address"]:
            site_address = contacts["address"]
        page_records.append(
            {
                "company_name": name,
                "website_url": url,
                "page_url": page["page_url"],
                "page_title": fields["page_title"],
                "meta_description": fields["meta_description"],
                "email": CONTACT_SEP.join(page_emails) if page_emails else "N/A",
                "phone_number": CONTACT_SEP.join(page_phones) if page_phones else "N/A",
                "physical_address": contacts["address"] or "N/A",
                "scrape_source_method": page["method"],
                "date_added": now_iso,
                "page_text": page_text,
                "raw_html": page["html"],
            }
        )
        page_lines.append(fields["lines"])
        combined_parts.append(page_text)

    # Site-wide aggregated contacts (joined) for the cache + result summary.
    email = CONTACT_SEP.join(all_emails) if all_emails else None
    phone = CONTACT_SEP.join(all_phones) if all_phones else None

    if not page_records:
        logger.warning("No readable content on any page of %s", url)
        return {"company_name": name, "website_url": url, "status": "failed",
                "method": root["method"]}

    # De-duplicate boilerplate: lines that repeat across most of a site's pages
    # (menu/header/footer) are stripped from every page EXCEPT the homepage, so
    # each row keeps only its unique content. The homepage keeps the full text,
    # so nothing is lost — just no longer repeated 20x.
    _dedupe_shared_lines(page_records, page_lines)

    # Evaluate BEFORE persisting. With filtering enabled, only real qualified
    # leads are stored; with it disabled, every scraped business is kept.
    combined = " ".join(combined_parts)
    qualification, classification = await _evaluate_lead(
        industry, geo, name, combined, exclude_keywords, include_keywords
    )
    is_lead = bool(
        qualification.get("qualified")
        and (classification or {}).get("category", "match") == "match"
    )

    if is_lead:
        records_to_append = _filter_new_page_records(page_records, cached_rows)
        append_records_to_csv(records_to_append)  # contiguous block, homepage first
        if cached_rows:
            cache[url] = cached_rows + [
                {
                    "website_url": record["website_url"],
                    "page_url": record["page_url"],
                    "page_text": record["page_text"],
                    "raw_html": record["raw_html"],
                    "email": record["email"],
                    "phone_number": record["phone_number"],
                    "physical_address": record["physical_address"],
                }
                for record in records_to_append
            ]
        else:
            cache[url] = [
                {
                    "website_url": record["website_url"],
                    "page_url": record["page_url"],
                    "page_text": record["page_text"],
                    "raw_html": record["raw_html"],
                    "email": record["email"],
                    "phone_number": record["phone_number"],
                    "physical_address": record["physical_address"],
                }
                for record in page_records
            ]
        logger.info(
            "Saved %d new page(s) for %s (%d total crawled)",
            len(records_to_append), name, len(page_records),
        )
    else:
        reason = (classification or {}).get("category") or qualification.get("reason")
        logger.info("Not stored (%s): %s", reason, name)

    return {"company_name": name, "website_url": url, "status": "scraped",
            "method": root["method"], "pages": len(page_records),
            "stored": is_lead, "text_len": len(combined),
            "email": email, "phone": phone,
            "qualification": qualification,
            "classification": classification}


def _is_lead(r: Dict[str, Any]) -> bool:
    """True only for a REAL qualified lead: keyword-qualified AND the relevance
    classifier judged it an actual matching business (category == "match"), not
    an aggregator / listicle / unrelated / wrong-location page."""
    return bool(
        r.get("qualification", {}).get("qualified")
        and (r.get("classification") or {}).get("category", "match") == "match"
    )


def _is_lead_count(results: List[Dict[str, Any]]) -> int:
    """Number of REAL qualified leads collected so far. This drives the discovery
    loop's stop condition, so it must count only genuine matches — otherwise the
    loop would stop after merely scraping N pages that then all get rejected."""
    return sum(1 for r in results if _is_lead(r))


async def run_pipeline(
    user_query: str, limit: Optional[int] = None, concurrency: int = 5
) -> Dict[str, Any]:
    """Run the full Phase 1 pipeline end to end and return a result summary.

    `limit` is optional and overrides the plan; when omitted, the count is taken
    from the planner's `result_limit` (which it reads from the query), defaulting
    to DEFAULT_RESULT_LIMIT.
    """
    # Quiet the intermittent DNS-blip tracebacks from aiohttp's shielded futures.
    try:
        asyncio.get_running_loop().set_exception_handler(_quiet_dns_exception_handler)
    except RuntimeError:  # pragma: no cover - not inside a loop (shouldn't happen)
        pass

    initialize_csv_storage_layer()
    cache = load_cache()
    logger.info("Loaded %d previously-analyzed leads from cache.", len(cache))

    summary: Dict[str, Any] = {
        "query": user_query,
        "plan": None,
        "limit": None,
        "discovered": 0,
        "results": [],
        "qualified": [],
        "counts": {},
        "qualified_count": 0,
        "error": None,
    }

    try:
        plan = await deconstruct_intent(user_query)
    except Exception as exc:  # noqa: BLE001 - degrade cleanly on LLM/network failure
        logger.critical("Intent deconstruction failed (LLM/network): %s", exc)
        summary["error"] = f"intent_failed: {exc}"
        return summary
    summary["plan"] = plan

    # Resolve the effective count: explicit override > plan > default.
    effective_limit = limit or plan.get("result_limit") or DEFAULT_RESULT_LIMIT
    effective_limit = max(1, int(effective_limit))
    summary["limit"] = effective_limit

    exclude_keywords = plan.get("exclude_keywords", []) or []
    include_keywords = plan.get("include_keywords", []) or []
    industry = plan.get("broad_industry", "") or ""
    geo = plan.get("geo_location", "") or ""
    country_code = plan.get("country_code", "") or ""
    phone_regex = plan.get("phone_regex", "") or ""
    if phone_regex:
        logger.info("Phone regex for %s: %s", country_code or geo, phone_regex)

    # Known businesses already in the store, keyed by registered domain.
    existing_by_domain: Dict[str, tuple] = {}
    for key, rows in cache.items():
        existing_by_domain.setdefault(_domain_key(key), (key, rows))

    # General vs specific (planner first, domain-in-query as fallback).
    search_type = (plan.get("search_type") or "general").lower()
    target_domain = (plan.get("target_domain") or "").strip().lower().removeprefix("www.")
    if not target_domain:
        detected = _detect_domain(user_query)
        if detected:
            target_domain, search_type = detected, "specific"
    summary["search_type"] = search_type

    results: List[Dict[str, Any]] = []
    semaphore = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(connector=_make_connector()) as session:
        query = plan.get("search_query") or f"{industry} in {geo}".strip()

        if search_type == "specific":
            # DB-FIRST: if we already have this business, return the stored record
            # (re-qualified against the current keywords) without re-scraping.
            if target_domain and target_domain in existing_by_domain:
                key, rows = existing_by_domain[target_domain]
                logger.info("Specific search: '%s' already in store — returning existing.",
                            target_domain)
                combined = " ".join(r.get("page_text", "") for r in rows)
                email = CONTACT_SEP.join(_union_contacts(rows, "email")) or "N/A"
                phone = CONTACT_SEP.join(_union_contacts(rows, "phone_number")) or "N/A"
                qualification, classification = await _evaluate_lead(
                    industry, geo, rows[0].get("company_name") or target_domain,
                    combined, exclude_keywords, include_keywords,
                )
                results = [{
                    "company_name": rows[0].get("company_name") or target_domain,
                    "website_url": key, "status": "existing", "method": "CACHE",
                    "pages": len(rows), "email": email, "phone": phone,
                    "qualification": qualification, "classification": classification,
                }]
                summary["discovered"] = 1
            else:
                # New specific target: scrape just that one site (or top-1 by name).
                if target_domain:
                    targets = [{"title": target_domain,
                                "website": f"https://{target_domain}/", "snippet": ""}]
                else:
                    targets, _ = await discover_targets(session, query, 1)
                summary["discovered"] = len(targets)
                for target in targets:
                    results.append(await process_single_lead(
                        session, semaphore, target, cache, exclude_keywords,
                        include_keywords, industry, geo,
                        country_code=country_code, phone_regex=phone_regex,
                    ))
        else:
            # GENERAL: keep discovering and scraping in batches of `concurrency`
            # until we have collected `effective_limit` SUCCESSFUL leads, or until
            # Serper runs out of results. This ensures "50 marinas" means 50
            # stored leads, not 50 attempts that might half-fail.
            state = _load_search_state()
            start_page = state.get(query, 1)
            current_page = start_page
            logger.info("General search — resuming from Serper page %d.", current_page)

            exclude_set = set(existing_by_domain)
            total_discovered = 0
            last_page = current_page
            wrapped = False   # have we already wrapped the cursor back to page 1?

            # Phase A — Google Maps (Places): the highest-yield source of REAL
            # individual businesses. Organic web search alone tends to surface
            # directories/listicles for location queries, so we lead with Places.
            place_targets = await discover_places(
                session, query,
                limit=effective_limit * 4,      # over-fetch; many won't qualify
                exclude_domains=exclude_set,
            )
            if place_targets:
                total_discovered += len(place_targets)
                for t in place_targets:
                    exclude_set.add(_domain_key(t["website"]))
                place_results = await asyncio.gather(*[
                    process_single_lead(
                        session, semaphore, target, cache, exclude_keywords,
                        include_keywords, industry, geo,
                        country_code=country_code, phone_regex=phone_regex,
                    )
                    for target in place_targets
                ])
                results.extend(place_results)
                logger.info(
                    "After Places: %d/%d real leads (%d businesses scraped).",
                    _is_lead_count(results), effective_limit, len(place_targets),
                )

            # Phase B — organic web + directory harvesting tops up to N, sweeping
            # progressively deeper Serper pages. Bounded so a query with few real
            # businesses can't spin forever.
            max_rounds = max(10, effective_limit * 3)
            rounds = 0
            while _is_lead_count(results) < effective_limit:
                rounds += 1
                if rounds > max_rounds:
                    logger.info(
                        "Hit discovery ceiling (%d rounds) — stopping with %d/%d leads.",
                        max_rounds, _is_lead_count(results), effective_limit,
                    )
                    break
                # Discover one batch worth of candidates from the current page.
                batch_targets, last_page = await discover_targets(
                    session, query,
                    limit=concurrency * 2,   # fetch a small batch at a time
                    exclude_domains=exclude_set,
                    start_page=current_page,
                )
                if not batch_targets:
                    # The saved cursor may have run past the end of Google's
                    # results. Wrap back to page 1 ONCE so an exhausted query
                    # can recover (e.g. after new businesses appear) instead of
                    # returning 0 forever.
                    if current_page > 1 and not wrapped:
                        wrapped = True
                        logger.info(
                            "Page %d exhausted — restarting this query from page 1.",
                            current_page,
                        )
                        current_page = 1
                        continue
                    logger.info(
                        "Serper returned no more new results. "
                        "Stopping (collected %d/%d leads).",
                        _is_lead_count(results), effective_limit,
                    )
                    break

                total_discovered += len(batch_targets)
                # Add this batch's domains to the exclude set so the next
                # iteration doesn't re-discover them.
                for t in batch_targets:
                    exclude_set.add(_domain_key(t["website"]))

                batch_results = await asyncio.gather(*[
                    process_single_lead(
                        session, semaphore, target, cache, exclude_keywords,
                        include_keywords, industry, geo,
                        country_code=country_code, phone_regex=phone_regex,
                    )
                    for target in batch_targets
                ])
                results.extend(batch_results)
                leads_so_far = _is_lead_count(results)
                logger.info(
                    "Leads: %d/%d | Serper pages used so far: %d-%d",
                    leads_so_far, effective_limit, start_page, last_page,
                )
                # Advance past the pages we just consumed.
                current_page = last_page + 1

            # Persist the cursor for the next run.
            state[query] = current_page
            _save_search_state(state)
            summary["serper_pages_used"] = f"{start_page}-{last_page}"
            summary["next_run_starts_page"] = current_page
            summary["discovered"] = total_discovered

            if not results:
                logger.critical(
                    "No NEW businesses found from page %d onwards.",
                    start_page,
                )
                summary["counts"] = {}
                return summary

    summary["processed"] = len(results)
    summary["results"] = results
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary["counts"] = counts

    # The deliverable: every candidate that passed (with filtering off, that's
    # all successfully scraped businesses). No truncation — we keep them all.
    qualified = [
        {
            "company_name": r.get("company_name"),
            "website_url": r.get("website_url"),
            "email": r.get("email") or "N/A",
            "phone": r.get("phone") or "N/A",
            "matched_include": r["qualification"].get("matched_include", []),
        }
        for r in results
        if _is_lead(r)
    ]
    summary["qualified"] = qualified
    summary["qualified_count"] = len(qualified)
    # Shortfall: real qualified leads found vs. the number the user asked for.
    # We never pad the count with weak/aggregator results — we report the gap.
    summary["shortfall"] = max(0, (summary.get("limit") or 0) - len(qualified))
    # Leads keyword-qualified but filtered out by the relevance classifier.
    summary["filtered_out"] = [
        {"company_name": r.get("company_name"),
         "category": (r.get("classification") or {}).get("category"),
         "reason": (r.get("classification") or {}).get("reason")}
        for r in results
        if r.get("qualification", {}).get("qualified") and not _is_lead(r)
    ]

    logger.info(
        "Phase 1 complete. %d qualified leads (of %d processed). Store: %s",
        len(qualified), len(results), OUTPUT_CSV_FILE,
    )
    # Record this query's scope so the data-quality report covers only the
    # latest query, not the whole cumulative store.
    _save_last_run(user_query, results)
    return summary


def print_summary(summary: Dict[str, Any]) -> None:
    """Render the pipeline's output return in a readable block."""
    print("\n" + "=" * 64)
    print("PHASE 1 OUTPUT RETURN")
    print("=" * 64)
    print(f"Query      : {summary['query']}")
    print(f"Search type: {summary.get('search_type', 'general')}")
    if summary.get("serper_pages_used"):
        print(f"Serper pages used : {summary['serper_pages_used']}  "
              f"| Next run starts at page {summary.get('next_run_starts_page')}")
    print(f"Plan       : {json.dumps(summary['plan'], ensure_ascii=False)}")
    print(
        f"Target leads: {summary.get('limit')}  |  Discovered : {summary['discovered']}"
        f"  |  Processed : {summary.get('processed', len(summary['results']))}"
    )
    print(f"Counts     : {summary['counts']}")
    print("-" * 64)
    for r in summary["results"]:
        method = r.get("method", "-")
        pages = r.get("pages")
        pages_tag = f" {pages}p" if pages else ""
        q = r.get("qualification") or {}
        cls = r.get("classification") or {}
        verdict = ""
        if q:
            if q.get("qualified"):
                cat = cls.get("category")
                if cat and cat != "match":
                    verdict = f"  [~ {cat}]"        # keyword-OK but not a real lead
                else:
                    verdict = "  [QUALIFIED]"
            else:
                reason = q.get("reason")
                hit = q.get("matched_exclude")
                verdict = f"  [x {reason}]" + (f" ({hit[0]})" if hit else "")
        print(f"  [{r['status']:<10}] {method:<7}{pages_tag:<5} {r.get('company_name')}{verdict}")
    print("-" * 64)
    filtered = summary.get("filtered_out", [])
    if filtered:
        print(f"FILTERED BY RELEVANCE ({len(filtered)}) — keyword-OK but not real leads:")
        for f in filtered:
            print(f"  ~ [{f.get('category')}] {f.get('company_name')} — {f.get('reason')}")
        print("-" * 64)
    target = summary.get("limit")
    got = summary.get("qualified_count", 0)
    print(f"QUALIFIED LEADS ({got}"
          + (f" of {target} requested" if target else "") + "):")
    for lead in summary.get("qualified", []):
        print(f"  • {lead['company_name']}")
        print(f"      site : {lead['website_url']}")
        print(f"      email: {lead['email']}  |  phone: {lead['phone']}")
    shortfall = summary.get("shortfall", 0)
    if shortfall:
        print("-" * 64)
        print(f"⚠  SHORTFALL: found {got} real qualified lead(s), {shortfall} short "
              f"of the {target} requested.")
        print("   Exhausted Google Maps (Places), deeper web pages, and directory")
        print("   harvesting without finding more genuine businesses. Not padding")
        print("   the count with directories/aggregators (they aren't real leads).")
        print("   Try: a broader area, different phrasing, or re-run later to sweep")
        print("   further pages.")
    print("=" * 64)
    print(f"Raw store  : {OUTPUT_CSV_FILE}\n")


def main() -> None:
    # Windows consoles default to cp1252 and crash on Unicode (e.g. a non-break
    # hyphen) when printing the summary. Force UTF-8 output where supported.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="AI BDM Phase 1 pipeline")
    parser.add_argument(
        "--query",
        type=str,
        default="give me 20 marinas in Miami with no smart monitoring tools",
        help="Natural-language lead query (include the count, e.g. 'give me 50 ...').",
    )
    parser.add_argument(
        "--concurrency", type=int, default=5, help="Concurrent scrape workers."
    )
    parser.add_argument(
        "--no-clean", action="store_true",
        help="Skip the data-quality clean/format step after scraping.",
    )
    parser.add_argument(
        "--no-linkedin", action="store_true",
        help="Skip finding each business's LinkedIn page after cleaning.",
    )
    args = parser.parse_args()

    # Stage 1 — collect: scrape + qualify into the raw store.
    summary = asyncio.run(
        run_pipeline(args.query, concurrency=args.concurrency)
    )
    print_summary(summary)

    # No real leads this run — nothing to clean or enrich. Skip gracefully
    # instead of crashing the downstream stages on a missing leads_clean.csv.
    if summary.get("qualified_count", 0) == 0:
        print("\n[clean] Skipped — no qualified leads to clean or enrich this run.")
        return

    # Stage 2 — clean + format: derive the governed dataset in the same command.
    # Imported lazily so data_pipeline (which imports this module) stays decoupled.
    if not args.no_clean:
        import os
        import data_pipeline
        print("\n[clean] Running data-quality pipeline...")
        data_pipeline.run(OUTPUT_CSV_FILE)

        # Stage 3 — enrich: add each business's LinkedIn page to the same sheet.
        if not args.no_linkedin and os.path.exists(data_pipeline.CLEAN_EXPORT):
            import linkedin_finder
            geo = (summary.get("plan") or {}).get("geo_location", "") or ""
            print("\n[linkedin] Finding LinkedIn pages...")
            try:
                asyncio.run(linkedin_finder.enrich(
                    data_pipeline.CLEAN_EXPORT, data_pipeline.CLEAN_EXPORT, geo
                ))
            except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
                logger.error("LinkedIn enrichment failed: %s", exc)
        elif not args.no_linkedin:
            print("[linkedin] Skipped — no leads_clean.csv was produced.")


if __name__ == "__main__":
    main()
