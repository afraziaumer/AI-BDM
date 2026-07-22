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
import hashlib
import json
import logging
import os
import re
import socket
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, unquote, urljoin

import aiohttp
import phonenumbers
import tldextract
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from email_extractor import EmailExtractor

# Reuse the already-tested Step-1 planner instead of duplicating LLM logic.
from LLM_planner import plan_query, classify_business, generate_query_variations, moderate_query

# Generic, reusable per-page semantic summary (page type, meta, structure,
# content, anchors, metrics) — built once per crawled page, consumed by the
# Route Planner and, in the future, RAG/lead-scoring/tech-stack reasoning.
import page_intelligence

# Social/LinkedIn business intelligence (see each module's docstring for
# scope — none of these scrape linkedin.com or any other social platform
# directly; all read data already legitimately crawled, or Serper's search
# index, never a live fetch of a third-party social profile page).
import social_discovery
import linkedin_discovery
import linkedin_enrichment
import decision_maker_extractor
import schema_org_extractor
import public_search_decision_makers
import organization
import buying_committee
import business_intelligence
import github_enrichment
import youtube_enrichment

# Modular storage layer: cleaned page text + link metadata + per-page index.
# Local filesystem today; swap the backend in storage.get_store() for R2 later.
from storage import get_store

# Search-result triage (official website / discovery source to mine / noise)
# and the deduplicated discovery queue — independent of the crawler; new
# discovery-source types are added there, never here.
import discovery_classifier as dc

# Tech Stack Detection is an OPTIONAL processing step inside the crawl (see
# crawl_site below) — never a hard dependency of the crawler. If the module or
# its Wappalyzer dependency isn't installed, detection is silently skipped and
# the crawl behaves exactly as it did before this feature existed.
try:
    import tech_stack
except ImportError:
    tech_stack = None  # type: ignore[assignment]

# LLM Crawl Planner is likewise OPTIONAL: crawl_site falls back to queuing every
# homepage link (its pre-existing behavior) if this module or the LLM it uses
# is unavailable — a missing/failing planner can only mean less credit
# savings, never less recall.
try:
    import crawl_planner
except ImportError:
    crawl_planner = None  # type: ignore[assignment]

# Website Classifier is likewise OPTIONAL: crawl_site falls back to treating
# every site as an official business (its pre-existing behavior) if this
# module or the LLM it uses is unavailable — a missing/failing classifier can
# only mean wasted credits on an undetected directory, never a dropped lead.
try:
    import website_classifier
except ImportError:
    website_classifier = None  # type: ignore[assignment]

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

# Per-page crawl metadata index (NO page text / raw HTML — cleaned text lives in
# storage/<domain>/*.txt behind the storage layer; see storage.py).
CRAWL_INDEX_FILE = "crawl_index.csv"
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
# Always sweep a FIXED number of Google pages (MAX_SEARCH_PAGES), fetching results
# in chunks of N per page, where N = the number in the query (default 5). Every
# business found across those pages is scraped; the page count is fixed, not N.
DEFAULT_RESULT_LIMIT = 5         # N = chunk size (results requested per page)
MAX_SEARCH_PAGES = 3             # safety ceiling on how far discovery may page
                                  # to still reach N candidates (not a target)
SEARCH_RESULTS_PER_PAGE = 10     # fallback per-page chunk when N isn't provided
MAX_DIRECTORIES_TO_HARVEST = 6   # cap directory pages we crawl for outbound links
MAX_PLACES_PAGES = 8             # Google Maps (Places) pages to sweep for businesses
# General search keeps discovering (deeper Serper pages + mining discovery
# sources) across MULTIPLE rounds until N qualified leads are collected — not
# until N candidates have been attempted. This bounds the total pages consulted
# across ALL rounds in one run, so a query with genuinely few real businesses
# can't page into Google forever; the honest shortfall is reported instead.
MAX_TOTAL_SEARCH_PAGES_PER_RUN = 30
# When an explicit count was requested and the original query's discovery is
# exhausted, generate_query_variations() supplies fresh same-intent search
# queries (site: operators, industry synonyms, specific cities within the
# same requested location — never a different industry or country). This
# caps the total number of DISTINCT queries tried (original + variations) in
# one run, as an extra guard alongside MAX_TOTAL_SEARCH_PAGES_PER_RUN.
MAX_QUERY_VARIATIONS_TOTAL = 10
# A candidate whose scrape totally failed (no readable content from ANY tier,
# even after the retry/failover hardening in execute_scavenger_scrape) gets
# revisited up to this many times across LATER rounds before being given up
# on for good — only when aggressive_discovery, since single-pass mode has
# no later round to revisit within. Never lowers relevance standards; a
# candidate that keeps failing simply counts toward the honest shortfall.
MAX_SOFT_FAIL_RETRIES = 2
# Google Maps (Places) is a bonus source of real businesses run BEFORE the
# organic Google search. OFF by default so the result is exactly "all businesses
# across N Google pages" (set True to also mix in Google Maps results).
USE_PLACES_DISCOVERY = False
SERPER_TIMEOUT_S = 15            # discovery requests
NATIVE_FETCH_TIMEOUT_S = 10      # Tier-1 free request (fail fast, escalate/skip)
SCRAPEDO_TIMEOUT_S = 18          # Tier-2 proxied fetch (give up sooner = faster)
# NOTE: intra-site fetch batching was removed on purpose. The streaming crawl
# processes strictly ONE page at a time (fetch → clean → stage → free) so at
# most one page's raw HTML is ever in RAM per business being crawled.
# Scrape.do plans cap how many requests you may run AT ONCE. Exceeding it returns
# HTTP 429. This global limit keeps every proxied call under that cap (raise it
# if your plan allows more). Native Tier-1 fetches are NOT throttled by this.
SCRAPEDO_MAX_CONCURRENCY = 2
SCRAPEDO_MAX_RETRIES = 2         # retry a transient failure (429/5xx/timeout/
                                 # connection error) this many times before giving up
SCRAPEDO_BACKOFF_S = 3           # base backoff between retries, doubled each attempt
NATIVE_TRANSIENT_RETRIES = 1     # Tier-1: one quick retry on a connection-level
                                 # error only (a real HTTP response — WAF block,
                                 # 4xx/5xx — escalates to Tier-2 immediately instead;
                                 # retrying the same tier won't fix a block)
NATIVE_RETRY_BACKOFF_S = 1.5
# Cost optimization: by default Scrape.do fetches are CHEAP (raw HTML, no browser,
# ~1 credit). If a cheap fetch fails, retry ONCE with JS rendering + premium proxy
# — the expensive mode — only for sites that need it. Scrape.do bills only HTTP
# 200s, so the cheap misses are free, making this cost-safe. On the render call,
# images/CSS/fonts are blocked so the browser render stays fast.
SCRAPEDO_RENDER_FALLBACK = True  # retry a failed cheap fetch with JS rendering
SCRAPEDO_BLOCK_RESOURCES = True  # skip images/css/fonts on the render call (faster)
HTTP_OK = (200, 201)
HTTP_BLOCKED = (403, 429, 503)   # statuses that should escalate to Tier-2

# Whole-site crawl: follow EVERY same-domain internal link so no page is missed.
# The caps below are large SAFETY ceilings only — ordinary sites finish far below
# them; they exist so a crawler trap (infinite calendar/filter URLs) can't run
# forever. Raise them further if you truly need to cover a giant site.
MAX_PAGES_PER_SITE = 40          # cap pages per business (enough for real sites)
MAX_CRAWL_DEPTH = 50             # follow links to any depth (page cap bounds it)
# Safety ceiling on fetch ATTEMPTS per site (successful or failed).
MAX_FETCH_ATTEMPTS = 150
# Early-stop toggle. OFF by default so the crawler visits every USEFUL page and
# collects the MOST contacts possible (multi-location sites expose a different
# phone/email per page). It stays efficient via: skipping useless URLs
# (booking/query-string/blog), de-duplicating identical pages, and the
# MAX_PAGES_PER_SITE cap. Set True to instead stop as soon as one email+phone
# is found (fastest, fewest contacts).
STOP_WHEN_CONTACT_COMPLETE = False
MIN_PAGES_BEFORE_STOP = 5
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
# the LLM relevance classifier (Step 5.5, LLM_planner.classify_business). This
# is the ONLY stage in the whole pipeline that validates a candidate's actual
# industry + location match — discovery_classifier.py (Step 2/3) only screens
# noise/other-directories, and website_classifier.py (Layer 2) only screens
# directory-vs-official; neither is query-aware. Turned back ON after briefly
# being disabled: with it off, e.g. "5 salons in Spain" let through acxiom.com
# (a US data/martech company, mined off some directory's footer credit link)
# because literally nothing downstream ever checked industry/geo for ANY
# candidate. Flip to False only if you deliberately want zero relevance
# filtering again (every discovered business kept, unfiltered).
LEAD_FILTERING_ENABLED = True

# Website Classification (directory/aggregator detection, website_classifier.py)
# is ENABLED — this is the ONLY thing standing between "a directory/aggregator
# site (expat.com, locallista.com, a brand's salon-finder page...) gets crawled
# and stored as if it were itself the business" and a clean result set. It's
# independent of the other two flags below: it decides deep_crawl vs discard,
# NOT which pages to crawl (that's ENABLE_CRAWL_PLANNER) or whether a business
# passes a relevance check (that's LEAD_FILTERING_ENABLED). Flip to False only
# if you deliberately want directories treated as businesses.
ENABLE_WEBSITE_CLASSIFIER = True

# LLM Crawl Planner (crawl_planner.py) is ENABLED — trades some knowledge-base
# completeness (only the LLM-judged query-relevant pages get crawled, not
# every internal page) for a large reduction in per-business scrape time and
# Scrape.do credit usage: fewer pages fetched per site means fewer paid Tier-2
# calls, the slowest operation in the pipeline. Flip back to False to return
# to crawling every page that passes MAX_PAGES_PER_SITE/MAX_CRAWL_DEPTH/noise
# filtering, unchanged, for a more durable long-term knowledge base instead.
ENABLE_CRAWL_PLANNER = True

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
# ReDoS guards for the LLM-generated phone regex: reject an over-long pattern
# (fall back to the safe fixed one) and never scan more than a bounded slice of
# page text, so a pathological regex can't hang a worker on a big page.
MAX_PHONE_REGEX_LEN = 200
MAX_PHONE_SCAN_CHARS = 20000
# Substrings that mark a regex 'email' match as junk.
JUNK_EMAIL_HINTS = (
    "example.", "yourdomain", "domain.com", "email@", "sentry", "wixpress",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", "@2x", "u003e", "name@",
    "noreply@", "no-reply@", "donotreply@",
)

# Search-result triage (official website / discovery source to mine / noise)
# lives entirely in discovery_classifier.py — a module deliberately independent
# of the crawler, so new discovery-source types can be added there without
# touching this file. See discover_targets / discover_places below.

# Offline domain extractor (bundled public-suffix snapshot; no network fetch).
_TLD = tldextract.TLDExtract(
    suffix_list_urls=(),
    cache_dir=os.getenv("TLDEXTRACT_CACHE", ".tldextract-cache"),
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    # Strong (not "0.5") preference for English — content-negotiating sites
    # should serve their English variant instead of defaulting to whatever
    # the crawler's own network location implies. Sites that ignore
    # Accept-Language entirely (URL-path or GeoIP-based language selection,
    # or simply no English content at all) aren't affected by this header —
    # see FORCE_ENGLISH_GEO_CODE below for the GeoIP case, handled via
    # Scrape.do's proxy exit-country instead.
    "Accept-Language": "en-US,en;q=0.9",
}

# Alternate User-Agents tried on successive Scrape.do retries — a block can be
# fingerprint-based (a WAF flagging one specific UA string), so varying it on
# retry sometimes gets through where the original request was blocked outright.
_RETRY_USER_AGENTS = (
    BROWSER_HEADERS["User-Agent"],
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
)

# HTTP statuses worth retrying (rate-limited or a server-side hiccup) rather
# than treating as a permanent failure (a real 4xx like 404/403 is not retried
# — the same request will just fail the same way again).
_SCRAPEDO_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

# Scrape.do (Tier-2) exit-node country. Many sites pick their served language
# from the VISITOR'S apparent country (GeoIP), not the Accept-Language header
# — Accept-Language alone can't override that since it's a different signal.
# Forcing the proxy's exit IP to a native-English country makes those sites
# serve English the same way a real visitor browsing from the US would see.
# Sites with no English content at all are still unaffected either way.
FORCE_ENGLISH_GEO_CODE = "us"

ANTI_BOT_SIGNATURES = [
    "just a moment...",
    "checking your browser",
    "captcha",
    "ddos protection",
    "cf-browser-verification",
    "attention required",
]

# === Step 0: Query moderation ===============================================
async def moderate_user_query(user_prompt: str) -> Dict[str, Any]:
    """Block a policy-violating query BEFORE any resources (Serper, Scrape.do,
    the intent/route-planner LLM calls, storage) are spent on it.

    Delegates to LLM_planner.moderate_query (sync, with its own primary/
    fallback model failover, and fails OPEN on any error — see its
    docstring) and runs it off the event loop so we stay non-blocking.
    """
    result = await asyncio.to_thread(moderate_query, user_prompt)
    if not result["safe"]:
        logger.warning(
            "Query blocked by moderation (%s): %r — %s",
            result["category"], user_prompt, result["reason"],
        )
    return result


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
        # pure auth-ACTION endpoints — no content, no business/portal signal
        # beyond what /login itself already gives (kept crawlable: see
        # crawl_planner.py, which scores /login and /register as candidates —
        # a CRM/portal-detection query may specifically want them crawled).
        "/logout", "/log-out", "/forgot-password", "/reset-password",
        # blog / news / article sections — not contact-bearing, big noise + slow
        "/blog", "/blogs", "/news", "/article", "/articles", "/press",
        "/category", "/categories", "/page/",
        # booking / scheduling widgets — no contact info, endless variants
        # (careers/jobs are intentionally KEPT: they often list HR/office contacts)
        "/booking", "/book-", "/schedule", "/appointments",
    )
    if any(fragment in low for fragment in noisy_fragments):
        return True
    # Root-level blog posts have long, hyphen-heavy slugs (6+ words), e.g.
    # "/the-science-behind-tooth-movement-how-braces-really-work-5". Real service
    # pages are short ("/veneers", "/dental-implants"). 5+ hyphens ⇒ an article.
    last_segment = low.rstrip("/").rsplit("/", 1)[-1]
    if last_segment.count("-") >= 5:
        return True
    # Date-archive URLs like /2024/06/... are blog archives.
    if re.match(r"^/(19|20)\d{2}(/|$)", low):
        return True
    return False


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


def _save_last_run(
    query: str, results: List[Dict[str, Any]],
    industry: str = "", geo: str = "",
) -> None:
    """Record the latest query, its validated industry/geo, and the registered
    domains of the businesses it QUALIFIED, so the data-quality pipeline can
    scope its report to this run only.

    Only genuinely qualified leads (_is_lead) go into `domains` — a candidate
    that was merely attempted/discovered but rejected (wrong industry, wrong
    location, aggregator, ...) must never appear here, since data_pipeline.py
    treats every domain in this list as in-scope for the CURRENT query's
    output. `industry`/`geo` let data_pipeline.py additionally cross-check a
    cached commit was actually validated against THIS query's criteria, not
    some other query that happened to touch the same domain.

    Skipped when the run produced no qualified results so a failed/empty run
    never wipes the previous (still-valid) scope.
    """
    domains = sorted({
        _domain_key(r.get("website_url") or "")
        for r in results
        if r.get("website_url") and _is_lead(r)
    } - {""})
    if not domains:
        return
    payload = {
        "query": query,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "industry": industry,
        "geo": geo,
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
    country_code: str = "",
    location: str = "",
) -> List[Dict[str, Any]]:
    """Google Maps (Serper Places) discovery — real individual businesses.

    Organic web search for a query like "marinas in Argentina" returns
    directories and blog listicles, not the marinas themselves. The Places
    endpoint returns actual business listings (name, address, website), which is
    the right source when the user wants "N businesses in a location". Only
    listings that expose a website are returned, since we need a site to scrape.

    `country_code`/`location` are forwarded to Serper's `gl`/`location` params so
    results are actually geo-anchored — otherwise Google/Serper falls back to
    whatever region the query text alone implies (usually the server's own
    country), which silently returns the wrong place for any city name that
    exists in multiple countries (e.g. "Venice" -> Venice, FL instead of
    Venice, Italy, even when the plan correctly resolved country_code="IT").
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
        payload: Dict[str, Any] = {"q": query, "page": page}
        if country_code:
            payload["gl"] = country_code.lower()
        if location:
            payload["location"] = location
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
            domain = _domain_key(website)
            if not domain or domain in seen_hosts:
                continue
            result = dc.classify_search_result(website, item.get("title", "") or "")
            logger.info("Classified (Places) %s -> %s (%s)",
                        website, result.category.value, result.reason)
            if result.category != dc.ResultCategory.OFFICIAL:
                # A Places listing's own "website" field pointing at a
                # directory/aggregator is rare but happens — never queue it.
                continue
            seen_hosts.add(domain)
            targets.append({
                "title": item.get("title", "") or "",
                "website": website,
                "snippet": item.get("address", "") or "",
                "discovered_from": ["Google Maps"],
                "source_confidence": dc.Confidence.HIGH.value,
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
    per_page: int = SEARCH_RESULTS_PER_PAGE,
    max_pages: int = MAX_SEARCH_PAGES,
    country_code: str = "",
    location: str = "",
) -> tuple[List[Dict[str, Any]], int]:
    """Paginated Serper web search starting at `start_page`.

    Returns (targets, last_page_fetched). The caller persists `last_page_fetched`
    so the next run resumes from `last_page_fetched + 1` instead of page 1,
    progressively working deeper into Google results across repeated queries.
    `exclude_domains` seeds the dedup set so already-stored businesses are skipped.

    `country_code`/`location` are forwarded to Serper's `gl`/`location` params —
    without them, Google/Serper geo-anchors purely from server IP + query text,
    which silently returns the wrong place for any city name that exists in
    multiple countries (e.g. "salons in Venice" -> Venice, FL, not Venice,
    Italy, even though the plan's own country_code correctly says "IT").
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
    # (url, source_name, confidence) — discovery sources to MINE, not discard.
    directories: List[tuple] = []
    noise_count = 0
    seen_hosts: Set[str] = set(exclude_domains or ())
    page = start_page
    last_page = start_page

    while len(targets) < limit and page <= start_page + max_pages - 1:
        # "num" is the per-page count (Serper allows up to 100), so each page
        # returns up to `per_page` businesses; we sweep `max_pages` pages.
        payload: Dict[str, Any] = {"q": query, "num": max(1, min(per_page, 100)), "page": page}
        if country_code:
            payload["gl"] = country_code.lower()
        if location:
            payload["location"] = location
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
            domain = _domain_key(link)
            if not domain or domain in seen_hosts:
                continue

            result = dc.classify_search_result(link, title)
            logger.info("Classified %s -> %s (%s)", link, result.category.value, result.reason)

            if result.category == dc.ResultCategory.NOISE:
                noise_count += 1
                continue
            if result.category == dc.ResultCategory.DISCOVERY_SOURCE:
                directories.append((link, result.source_name, result.confidence))
                continue

            # OFFICIAL
            seen_hosts.add(domain)
            targets.append({
                "title": title, "website": link, "snippet": item.get("snippet", ""),
                "discovered_from": ["Google Search"],
                "source_confidence": dc.Confidence.MEDIUM.value,
            })
            if len(targets) >= limit:
                break
        page += 1

    direct_count = len(targets)

    # Mine discovery sources for individual businesses — highest-confidence
    # sources first (Google Maps/associations before random directories).
    directories.sort(key=lambda d: dc.confidence_rank(d[2]), reverse=True)
    mined_count = 0
    sources_mined = 0
    for directory_url, source_name, confidence in directories[:MAX_DIRECTORIES_TO_HARVEST]:
        if len(targets) >= limit:
            break
        businesses = await dc.extract_businesses_from_source(
            session, directory_url, source_name or directory_url, confidence,
            seen_hosts, needed=limit - len(targets),
        )
        sources_mined += 1
        mined_count += len(businesses)
        targets.extend(b.as_target() for b in businesses)

    logger.info(
        "Discovery: %d sites (%d direct, %d mined from %d/%d discovery source(s)) | "
        "%d noise rejected | pages %d-%d",
        len(targets), direct_count, mined_count, sources_mined, len(directories),
        noise_count, start_page, last_page,
    )
    return targets, last_page


# === Step 3: Two-tier scavenger scrape =====================================
# Global throttle: never run more than SCRAPEDO_MAX_CONCURRENCY proxied requests
# at once, so we stay under the Scrape.do plan's concurrent-request cap (429s).
_scrapedo_semaphore = asyncio.Semaphore(SCRAPEDO_MAX_CONCURRENCY)


async def _scrapedo_fetch(
    session: aiohttp.ClientSession, target_url: str, render: bool
) -> Tuple[str, Dict[str, str], List[str]]:
    """One Scrape.do proxy fetch. Returns (html, headers); html is "" on failure.

    render=False → cheap: raw HTML through the proxy, no browser (~1 credit).
    render=True  → expensive: runs a headless browser (JS) via a premium proxy
                   for hard/SPA sites, with images/CSS/fonts blocked so it's fast.
    Concurrency-throttled and retried with exponential backoff on ANY transient
    failure — rate-limited (429), a server-side hiccup (5xx), or a network-level
    timeout/connection error — not just 429. Each retry also rotates the
    forwarded User-Agent (see _RETRY_USER_AGENTS), in case the block is
    fingerprint-based rather than rate/volume-based. A genuine non-transient
    HTTP status (403/404/...) is NOT retried — the same request would just fail
    the same way again. Scrape.do bills only HTTP 200s, so failed cheap
    attempts cost nothing. Headers are Scrape.do's own response headers
    (best-effort passthrough), not guaranteed identical to the origin's — still
    useful bonus signal for tech detection when present.

    Forces English content two ways: `geoCode` pins the proxy's exit IP to
    FORCE_ENGLISH_GEO_CODE (defeats GeoIP-based language selection — the site
    sees a US visitor, same as it would for a real one), and `customHeaders`
    forwards our own Accept-Language header instead of Scrape.do's default
    (defeats content-negotiation-based language selection). Sites with no
    English content at all are unaffected by either — there's nothing to
    switch to.
    """
    params: Dict[str, str] = {
        "token": SCRAPEDO_API_KEY, "url": target_url,
        "geoCode": FORCE_ENGLISH_GEO_CODE,
        "customHeaders": "true",
    }
    if render:
        params["render"] = "true"       # execute JavaScript (headless browser)
        params["super"] = "true"        # premium residential proxies
        if SCRAPEDO_BLOCK_RESOURCES:
            params["blockResources"] = "true"   # skip images/css/fonts → faster
    mode = "render" if render else "plain"

    for attempt in range(SCRAPEDO_MAX_RETRIES + 1):
        forwarded_headers = {
            "Accept-Language": BROWSER_HEADERS["Accept-Language"],
            "User-Agent": _RETRY_USER_AGENTS[attempt % len(_RETRY_USER_AGENTS)],
        }
        retry_reason: Optional[str] = None
        try:
            async with _scrapedo_semaphore:       # cap concurrent proxied calls
                async with session.get(
                    SCRAPEDO_URL, params=params, headers=forwarded_headers,
                    timeout=aiohttp.ClientTimeout(total=SCRAPEDO_TIMEOUT_S),
                ) as resp:
                    if resp.status == 200:
                        return (await _read_html(resp), dict(resp.headers),
                                resp.headers.getall("Set-Cookie", []))
                    if resp.status in _SCRAPEDO_TRANSIENT_STATUS:
                        retry_reason = f"status={resp.status}"
                    else:
                        logger.error("Scrape.do %s failed (status=%s): %s",
                                     mode, resp.status, target_url)
                        return "", {}, []
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            retry_reason = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - unexpected, not worth retrying
            logger.error("Scrape.do %s unexpected error for %s: %s", mode, target_url, repr(exc))
            return "", {}, []

        if attempt >= SCRAPEDO_MAX_RETRIES:
            logger.error("Scrape.do %s exhausted %d attempt(s) for %s (%s)",
                         mode, attempt + 1, target_url, retry_reason)
            return "", {}, []
        wait = SCRAPEDO_BACKOFF_S * (2 ** attempt)
        logger.warning(
            "Scrape.do %s transient failure (%s) — retrying in %ss with a "
            "different User-Agent (attempt %d/%d): %s",
            mode, retry_reason, wait, attempt + 2, SCRAPEDO_MAX_RETRIES + 1, target_url,
        )
        await asyncio.sleep(wait)
    return "", {}, []


async def execute_scavenger_scrape(
    session: aiohttp.ClientSession, target_url: str
) -> Dict[str, Any]:
    """Tier-1 native fetch; on block/error fail over to Tier-2 Scrape.do.

    Returns {"html", "method", "headers", "status_code", "cookies"}. Headers
    AND raw Set-Cookie header strings are captured from the SAME response
    already being fetched (no extra request) so downstream tech-stack
    detection (Wappalyzer) can use them without a second call to the site —
    `cookies` uses `.getall("Set-Cookie", [])` specifically because a plain
    `dict(resp.headers)` collapses multiple same-named headers to just the
    last one, silently dropping every cookie but one.

    A connection-level error (DNS blip, reset, timeout) gets one quick retry on
    Tier-1 itself before escalating — cheap insurance against a purely
    transient network hiccup. A real HTTP response (WAF block, 4xx/5xx)
    escalates to Tier-2 immediately instead — retrying the identical Tier-1
    request wouldn't change the outcome.
    """
    # --- Tier 1: low-cost native request (one quick retry on connection error) ---
    for attempt in range(NATIVE_TRANSIENT_RETRIES + 1):
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
                    return {"html": body, "method": "NATIVE", "headers": dict(resp.headers),
                            "status_code": resp.status,
                            "cookies": resp.headers.getall("Set-Cookie", [])}
                logger.warning(
                    "Tier-1 blocked (status=%s, waf=%s): %s",
                    resp.status, waf_detected, target_url,
                )
                # A genuine 404 means the page simply doesn't exist — Scrape.do would
                # 404 too, so don't waste a proxy credit/30s escalating it. (If the
                # 404 came WITH an anti-bot signature it may be a faked block, so we
                # still escalate that case.)
                if resp.status == 404 and not waf_detected:
                    return {"html": "", "method": "FAILED", "headers": {},
                            "status_code": 404, "cookies": []}
                break  # a real (blocked/waf'd) response -> escalate now, don't retry Tier-1
        except Exception as exc:  # noqa: BLE001 - connection-level; worth one quick retry
            if attempt >= NATIVE_TRANSIENT_RETRIES:
                logger.warning("Tier-1 connection failed for %s (giving up on Tier-1): %s",
                               target_url, exc)
                break
            logger.warning("Tier-1 connection failed for %s — retrying in %ss: %s",
                           target_url, NATIVE_RETRY_BACKOFF_S, exc)
            await asyncio.sleep(NATIVE_RETRY_BACKOFF_S)

    # --- Tier 2: Scrape.do — cheap proxy first, JS-render fallback on failure ---
    if not SCRAPEDO_API_KEY:
        logger.error("Scrape.do token missing; cannot escalate %s", target_url)
        return {"html": "", "method": "FAILED", "headers": {}, "status_code": None, "cookies": []}

    logger.info("Tier-2 Scrape.do escalation: %s", target_url)
    # 2a — cheap: raw HTML through the proxy, no browser (cheapest, ~1 credit).
    html, headers, cookies = await _scrapedo_fetch(session, target_url, render=False)
    if html:
        logger.info("Tier-2 success (plain): %s", target_url)
        # Scrape.do bills (and therefore returns HTML) only for HTTP 200s.
        return {"html": html, "method": "SCRAPEDO", "headers": headers,
                "status_code": 200, "cookies": cookies}

    # 2b — fallback: only sites that failed cheap get the expensive JS render +
    # premium proxy (images/css/fonts blocked for speed). Cost-safe: the cheap
    # miss above was free, and Scrape.do bills only successful (200) requests.
    if SCRAPEDO_RENDER_FALLBACK:
        html, headers, cookies = await _scrapedo_fetch(session, target_url, render=True)
        if html:
            logger.info("Tier-2 success (render): %s", target_url)
            return {"html": html, "method": "SCRAPEDO_RENDER", "headers": headers,
                    "status_code": 200, "cookies": cookies}

    return {"html": "", "method": "FAILED", "headers": {}, "status_code": None, "cookies": []}


# === Step 3.5: Whole-site crawl ============================================
def _normalize_page_url(url: str) -> str:
    """Canonical form for de-dup: drop fragment, lowercase host, trim trailing /."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{host}{path}{query}"


def _extract_internal_link_pairs(
    soup: BeautifulSoup, base_url: str, root_domain: str
) -> List[Dict[str, str]]:
    """Return same-domain, crawlable links as [{'url', 'anchor'}].

    Runs on the RAW page soup, before any cleaning, so no hyperlink information
    is lost when nav/footer clutter is later stripped. URLs are resolved,
    normalized, deduped; unsupported schemes (mailto:, tel:, javascript:, #…)
    and asset/query/noise URLs are skipped. Anchor text is kept for Step 3.
    """
    pairs: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        # Resolve relative links exactly as a browser would.
        href = urljoin(base_url, href)
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        if _domain_key(href) != root_domain:       # same business only
            continue
        if parsed.path.lower().endswith(SKIP_EXTENSIONS):  # assets, not pages
            continue
        # Query-string URLs are almost always booking/filter/pagination variants
        # (e.g. ?market=nyc&studio=chelsea) — endless, no unique contact info.
        if parsed.query:
            continue
        if _is_internal_crawl_noise(parsed.netloc.lower().removeprefix("www."),
                                    parsed.path):
            continue
        norm = _normalize_page_url(href)
        if norm in seen:
            continue
        seen.add(norm)
        pairs.append({"url": href,
                      "anchor": a.get_text(" ", strip=True)[:120]})
    return pairs


def _extract_external_link_pairs(
    soup: BeautifulSoup, base_url: str, root_domain: str
) -> List[Dict[str, str]]:
    """Return off-domain http(s) links as [{'url', 'anchor'}] — mirrors
    `_extract_internal_link_pairs` but for the complement (external) set.
    Runs on the SAME raw soup pass, before cleaning strips `<a>` tags, so it
    costs one extra `find_all` rather than a second HTML parse.

    Two, unrelated consumers: page_intelligence's context JSON only needs
    `len(...)` (external_links metric); social_discovery.py needs the actual
    url+anchor pairs to recognize and normalize social-platform profile
    links (mailto:/tel: contact links are handled separately by
    extract_contacts, not here)."""
    pairs: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        href = urljoin(base_url, href)
        if urlparse(href).scheme not in ("http", "https"):
            continue
        if _domain_key(href) == root_domain:
            continue
        if href in seen:
            continue
        seen.add(href)
        pairs.append({"url": href, "anchor": a.get_text(" ", strip=True)[:120]})
    return pairs


def _extract_internal_links(html: str, base_url: str, root_domain: str) -> List[str]:
    """Return same-domain, crawlable page links found in `html`."""
    soup = BeautifulSoup(html, "lxml")
    return [p["url"] for p in _extract_internal_link_pairs(soup, base_url, root_domain)]


# === Language handling: prefer a site's own declared English variant =======
# General, not hardcoded per domain — reads the SAME standard mechanism every
# properly internationalized site already exposes for browsers/search engines:
# <link rel="alternate" hreflang="xx" href="..."> in <head>. If a site declares
# no such alternates at all, none of this changes anything (Requirement 4:
# crawl the default/original language as before).
def _parse_language_alternates(html: str, base_url: str) -> Dict[str, str]:
    """{hreflang code (lowercased) -> absolute url} for every alternate the
    homepage declares, from <link rel="alternate" hreflang="..." href="...">
    tags in <head>. "x-default" is skipped (it's a fallback marker, not a
    language). Best-effort: never raises, empty dict on any parse failure."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 - a bad parse just means no alternates found
        return {}
    alternates: Dict[str, str] = {}
    for link in soup.find_all("link", hreflang=True):
        rel = link.get("rel") or []
        if isinstance(rel, str):
            rel = [rel]
        if not any("alternate" in r.lower() for r in rel):
            continue
        code = (link.get("hreflang") or "").strip().lower()
        href = (link.get("href") or "").strip()
        if not code or not href or code == "x-default":
            continue
        alternates[code] = urljoin(base_url, href)
    return alternates


def _select_english_alternate(alternates: Dict[str, str]) -> Optional[str]:
    """The declared English variant's URL, e.g. from hreflang "en", "en-us",
    "en-GB" — any code whose primary subtag is "en". None if the site
    declares no English alternate at all."""
    for code, url in alternates.items():
        if code.split("-")[0] == "en":
            return url
    return None


def _other_language_path_prefixes(alternates: Dict[str, str], domain: str) -> Set[str]:
    """Path prefixes (e.g. "/it/", "/fr/") belonging to the site's OWN
    non-English declared alternates — used to keep the crawl from wandering
    into other locales once the English variant has been selected. Only
    considers alternates on the SAME registered domain (an hreflang pointing
    at a different domain/regional site is out of scope here)."""
    prefixes: Set[str] = set()
    for code, url in alternates.items():
        if code.split("-")[0] == "en":
            continue
        if _domain_key(url) != domain:
            continue
        path = urlparse(url).path.strip("/")
        if not path:
            continue
        prefixes.add(f"/{path.split('/')[0]}/")
    return prefixes


def _drop_other_language_links(urls: List[str], other_lang_prefixes: Set[str]) -> List[str]:
    """Filter out URLs that fall under one of the site's OWN other-language
    path prefixes (see _other_language_path_prefixes) — a no-op list copy
    when no other-language prefixes were found (the common case: most sites
    have no declared alternates at all)."""
    if not other_lang_prefixes:
        return urls
    kept = []
    for u in urls:
        path = urlparse(u).path
        if any(path == p.rstrip("/") or path.startswith(p) for p in other_lang_prefixes):
            continue
        kept.append(u)
    return kept


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


# Bounded text sample kept in RAM for the LLM relevance classifier. Keyword
# qualification runs on each FULL page before its text is freed, so only the
# classifier works from a sample.
CLASSIFY_SAMPLE_MAX_CHARS = 60_000
_SAMPLE_HOME_CHARS = 8_000       # homepage contribution to the sample
_SAMPLE_PAGE_CHARS = 1_500       # every other page's contribution


async def crawl_site(
    session: aiohttp.ClientSession,
    root_url: str,
    root_html: str,
    root_method: str,
    *,
    root_headers: Optional[Dict[str, str]] = None,
    root_status_code: Optional[int] = None,
    root_cookies: Optional[List[str]] = None,
    company_name: str = "",
    country_code: str = "",
    phone_regex: str = "",
    exclude_keywords: Optional[List[str]] = None,
    include_keywords: Optional[List[str]] = None,
    user_query: str = "",
    industry: str = "",
) -> Dict[str, Any]:
    """STREAMING contact-first crawl of one business site.

    Discovery order is unchanged (sitemap/robots contact probes first, then
    internal links, contact-ish first; two-tier native→Scrape.do fetch; depth
    and fetch-budget bounds). What changed is the data flow: pages are processed
    strictly ONE AT A TIME —

        fetch → extract hyperlinks (raw soup) → clean for LLM →
        stage .txt via the storage layer → buffer index/link metadata →
        free raw HTML + cleaned text → next page

    Raw HTML and page text never accumulate in RAM. Only lightweight metadata
    survives the loop: contact lists, keyword hits (checked against each FULL
    page before it is freed), and a bounded classification sample. Pages are
    staged; the caller commits or discards the whole domain after qualification.

    Tech Stack Detection (Wappalyzer) runs on the HOMEPAGE'S raw HTML while it
    is still in memory below — the SAME response already fetched here, never a
    second request — and is staged alongside the pages so a rejected business's
    profile is discarded with everything else. This happens unconditionally for
    every newly-crawled business; only surfacing it to a user is intent-gated
    (see tech_stack.get_stored_profile, called elsewhere at query time).
    """
    store = get_store()
    domain = _domain_key(root_url)
    exclude_keywords = exclude_keywords or []
    include_keywords = include_keywords or []
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    pages_kept = 0
    emails: List[str] = []
    phones: List[str] = []
    address = ""
    matched_exclude: Set[str] = set()
    matched_include: Set[str] = set()
    sample_parts: List[str] = []
    sample_chars = 0
    seen_sigs: Set[str] = set()          # content signatures → skip dup pages
    home_lines: Optional[Set[str]] = None  # homepage boilerplate for dedupe
    discovered_urls: Set[str] = set()    # every page/link URL seen (tech profile's
                                          # login/portal path-hint check, at the end)
    external_links: List[Dict[str, str]] = []  # site-wide, for social_discovery.py:
                                          # every off-domain link + its source page +
                                          # anchor text (social profile URLs are
                                          # never same-domain, so these never show
                                          # up in link_pairs/links.json at all)
    decision_makers: List[Dict[str, Any]] = []  # site-wide, for decision_maker_extractor.py
                                          # (this domain's website + schema.org sources —
                                          # contact_page/linkedin/search sources are added
                                          # later, post-commit, in process_single_lead)
    schema_organization: Optional[Dict[str, Any]] = None  # first Organization schema.org
                                          # node found on any page, if any

    async def _process_one(
        page_url: str, html: str, method: str, status_code: Optional[int] = None
    ) -> List[str]:
        """Process ONE page end-to-end and stage it. Returns its internal links.

        Everything heavy (soup, html, text) is local to this call and freed on
        return; only the enclosing lightweight accumulators are updated.
        """
        nonlocal pages_kept, address, sample_chars, home_lines, schema_organization
        soup = BeautifulSoup(html, "lxml")
        # Hyperlinks FIRST, from the raw soup, so cleaning can't lose them.
        link_pairs = _extract_internal_link_pairs(soup, page_url, domain)
        link_urls = [p["url"] for p in link_pairs]
        # Same raw-soup pass, before cleaning strips <a> tags — feeds both
        # page_intelligence's context JSON metric (len(...)) and
        # social_discovery.py's site-wide social-link scan (the pairs
        # themselves, tagged with their source page below).
        external_pairs = _extract_external_link_pairs(soup, page_url, domain)
        external_link_count = len(external_pairs)
        external_links.extend(
            {"page_url": page_url, "url": p["url"], "anchor": p["anchor"]}
            for p in external_pairs
        )
        # Decision-maker source #2 (schema.org JSON-LD) — MUST run before
        # clean_page_for_llm below, which strips <script> tags (that's where
        # JSON-LD lives) as part of turning the page into LLM-ready text.
        schema_data = schema_org_extractor.extract_schema_org(soup, page_url)
        if schema_data["people"]:
            decision_makers.extend(schema_data["people"])
        if schema_data["organization"] and schema_organization is None:
            schema_organization = schema_data["organization"]
        # Tracked regardless of whether this page's content is kept — a login/
        # portal path is itself a signal for tech profiling, even off a thin or
        # duplicate-content page. Cheap: just strings, not page content.
        discovered_urls.add(page_url)
        discovered_urls.update(link_urls)
        cleaned = clean_page_for_llm(soup, page_url)
        del soup
        text = cleaned["text"]
        if not text.strip():
            return link_urls

        # Duplicate-content page (same page at two URLs) → skip.
        sig = " ".join(text.lower().split())[:2000]
        if sig in seen_sigs:
            return link_urls
        seen_sigs.add(sig)

        # Boilerplate dedupe: the homepage keeps its full text; later pages drop
        # lines the homepage already has (menu/header/footer), keeping headings.
        lines = cleaned["lines"]
        if home_lines is None:
            home_lines = set(lines)
        else:
            lines = [ln for ln in lines
                     if ln.startswith(("#", "-")) or ln not in home_lines]
            text = "\n".join(lines)

        # Contacts come from the RAW html (mailto:, schema.org, obfuscation…),
        # before it is freed. CPU/DNS-bound → off the event loop.
        contacts = await asyncio.to_thread(
            extract_contacts, html, text, country_code, phone_regex,
            domain, VERIFY_EMAIL_MX, EMAIL_MIN_SCORE,
        )
        for e in contacts["emails"]:
            if e not in emails:
                emails.append(e)
        for p in contacts["phones"]:
            if p not in phones:
                phones.append(p)
        if not address and contacts["address"]:
            address = contacts["address"]

        # Keyword qualification on the FULL page text (exact — no sampling).
        low = text.lower()
        matched_exclude.update(k for k in exclude_keywords if k.lower() in low)
        matched_include.update(k for k in include_keywords if k.lower() in low)

        # Bounded sample for the LLM relevance classifier.
        if sample_chars < CLASSIFY_SAMPLE_MAX_CHARS:
            take = _SAMPLE_HOME_CHARS if pages_kept == 0 else _SAMPLE_PAGE_CHARS
            part = text[:take]
            sample_parts.append(part)
            sample_chars += len(part)

        # This is THE homepage of the crawl (first page kept) regardless of
        # its URL path — e.g. a site's English variant may live at "/en/",
        # not "/", once the language-alternate switch above has run. Deciding
        # by position (not by an empty URL path) keeps page_name_for/_page_type
        # correct for route_planner's homepage lookup either way.
        is_home_page = pages_kept == 0
        page_type = "home" if is_home_page else _page_type(page_url)

        # Stage the cleaned text + metadata NOW (not after the crawl finishes).
        page_name = "home" if is_home_page else store.page_name_for(page_url)
        txt_path = store.stage_page(domain, page_name, text)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        store.buffer_index_row(domain, {
            "company_name": company_name,
            "domain": domain,
            "website_url": root_url,
            "page_url": page_url,
            "page_title": cleaned["page_title"],
            "meta_description": cleaned["meta_description"],
            "email": CONTACT_SEP.join(contacts["emails"]) if contacts["emails"] else "N/A",
            "phone_number": CONTACT_SEP.join(contacts["phones"]) if contacts["phones"] else "N/A",
            "physical_address": contacts["address"] or "N/A",
            "txt_path": txt_path,
            "crawl_status": "ok",
            "http_status": method,          # scrape tier (NATIVE/SCRAPEDO)
            "content_length": str(len(text)),
            "word_count": str(cleaned["word_count"]),
            "timestamp": now_iso,
            "page_type": page_type,
        })
        store.buffer_links(domain, page_url, link_pairs)
        # Master per-page manifest entry — see storage.py's module docstring.
        # Written incrementally (one entry per page) so page_index.json stays
        # current throughout a long crawl, not just at the very end.
        store.stage_page_index_entry(domain, {
            "page_name": page_name,
            "url": page_url,
            "storage_path": txt_path,
            "http_status": status_code,
            "word_count": cleaned["word_count"],
            "content_length": len(text),
            "last_crawled": now_iso,
            "content_hash": content_hash,
            "page_type": page_type,
        })
        # Generic "Page Intelligence" context JSON (see page_intelligence.py) —
        # NOT router-specific: reused by Route Planning, RAG, lead scoring,
        # etc. `txt_path`'s stem is the ACTUAL deduped filename stage_page
        # picked (e.g. "about-2"), so "<stem>_context.json" reliably pairs
        # with "<stem>.txt" even when two URLs collided on the same name.
        # `anchors` starts empty here — filled by enrich_anchors() once the
        # whole site (and its link graph) is known, right after commit.
        try:
            stem = Path(txt_path).stem
            context = page_intelligence.build_page_context(
                url=page_url, page_name=stem,
                page_title=cleaned["page_title"],
                meta_description=cleaned["meta_description"],
                canonical_url=cleaned.get("canonical_url", ""),
                og_title=cleaned.get("og_title", ""),
                og_description=cleaned.get("og_description", ""),
                lines=lines, word_count=cleaned["word_count"],
                internal_links=len(link_urls), external_links=external_link_count,
                is_home=is_home_page,
            )
            store.stage_page_context(domain, stem, context)
            # Decision-maker extraction (Step 4, sources #1/#3): only on
            # pages already classified as team/about/contact/support — see
            # decision_maker_extractor.py's module docstring for why this is
            # scoped to the business's OWN site rather than LinkedIn, and why
            # it's precision-gated to these page types rather than scanned
            # on every page.
            page_type_for_dm = context.get("page_type")
            if page_type_for_dm in ("team", "about"):
                people = decision_maker_extractor.extract_decision_makers(
                    page_url, text, company_name, external_pairs,
                )
                decision_makers.extend(people)
            elif page_type_for_dm in ("contact", "support"):
                people = decision_maker_extractor.extract_contact_page_people(
                    page_url, text, external_pairs,
                )
                decision_makers.extend(people)
        except Exception as exc:  # noqa: BLE001 - best-effort, never breaks the crawl
            logger.warning("Page Intelligence context build failed for %s: %s", page_url, exc)
        pages_kept += 1
        return link_urls

    # --- Language handling: prefer the site's OWN declared English variant,
    # if any (Requirements 1-5). Runs BEFORE the homepage is staged, so if an
    # English alternate exists, its content — not the default-locale
    # homepage's — becomes "home.txt". If the site declares no alternates at
    # all, root_url/root_html are untouched and the crawl proceeds exactly as
    # before (original/default language). Best-effort: any failure here just
    # means the crawl continues on whatever homepage was already fetched.
    other_lang_prefixes: Set[str] = set()
    try:
        alternates = _parse_language_alternates(root_html, root_url)
        english_url = _select_english_alternate(alternates)
        if english_url and _normalize_page_url(english_url) != _normalize_page_url(root_url):
            logger.info("%s declares an English variant (%s) — switching to it.",
                        domain, english_url)
            fetched = await execute_scavenger_scrape(session, english_url)
            if fetched.get("html"):
                root_url = english_url
                root_html = fetched["html"]
                root_method = fetched.get("method", root_method)
                root_headers = fetched.get("headers") or root_headers
                root_status_code = fetched.get("status_code")
                alternates = _parse_language_alternates(root_html, root_url)
            else:
                logger.warning("English variant declared for %s but fetch failed "
                               "(%s) — keeping the original homepage.",
                               domain, english_url)
        other_lang_prefixes = _other_language_path_prefixes(alternates, domain)
    except Exception as exc:  # noqa: BLE001 - language handling is best-effort
        logger.warning("Language-alternate handling failed for %s: %s", domain, exc)

    # --- Homepage (already fetched by the caller's two-tier scrape, or just
    # switched above to the site's own English variant) ---------------------
    home_link_urls = _drop_other_language_links(
        await _process_one(root_url, root_html, root_method, root_status_code),
        other_lang_prefixes,
    )

    # Tech Stack Detection: Wappalyzer runs on the SAME raw HTML above, still
    # in memory, before it is freed on the next line — never a second request.
    # Unconditional (collection isn't gated by query intent); best-effort, so a
    # detection failure can never break the crawl. Assembled into the full
    # profile at the end of this function, once every page has been discovered
    # (stronger login/customer-portal signal than the homepage alone gives).
    tech_raw: Dict[str, Dict[str, Any]] = {}
    rule_based_detections: List[Dict[str, Any]] = []
    if tech_stack is not None:
        try:
            tech_raw = await asyncio.to_thread(
                tech_stack.analyze_raw_html, domain, root_html, root_headers, root_cookies,
            )
        except Exception as exc:  # noqa: BLE001 - detection is best-effort
            logger.warning("Tech-stack detection failed for %s: %s", domain, exc)
        try:
            rule_based_detections = await asyncio.to_thread(
                tech_stack.run_rule_based_detection, root_html,
            )
        except Exception as exc:  # noqa: BLE001 - detection is best-effort
            logger.warning("Rule-based tech detection failed for %s: %s", domain, exc)

    # Website Classification: is this homepage an individual business's own
    # site, or a directory/aggregator/marketplace/travel-guide/review/listing
    # site that merely LISTS many businesses? Runs on the SAME raw HTML above,
    # still in memory, using the tech-stack signals just collected. Must run
    # BEFORE the crawl planner — a directory is never deep-crawled at all, so
    # planning a deep crawl for one would be pure waste. Fails OPEN to
    # deep_crawl on any error/unavailability (module missing, LLM down, bad
    # response) — exactly the pre-existing behavior — so a classifier outage
    # can only ever waste credits on an undetected directory, never drop a
    # real business.
    classification = None
    if ENABLE_WEBSITE_CLASSIFIER and website_classifier is not None:
        try:
            classification = await asyncio.to_thread(
                website_classifier.classify_homepage,
                domain, root_url, root_html, tech_raw, user_query,
            )
        except Exception as exc:  # noqa: BLE001 - classification is best-effort
            logger.warning("Website classification failed for %s: %s", domain, exc)

    if classification is not None and not classification.deep_crawl:
        # Directory/aggregator/informational site: never deep-crawled, never
        # stored as a business. Mine its homepage for real business links
        # instead and hand them back so the caller can queue each as its OWN
        # crawl job (own storage, own crawl planner, own tech profile).
        discovered_businesses = await asyncio.to_thread(
            website_classifier.mine_homepage_businesses,
            root_html, root_url, domain, classification,
        )
        logger.info(
            "[Classifier] %s classified as %s (confidence=%d%%, action=%s) — "
            "extracting businesses instead of deep-crawling. %d business(es) found.",
            domain, classification.category, classification.confidence,
            classification.action, len(discovered_businesses),
        )
        del root_html
        return {
            "domain": domain,
            "pages_kept": pages_kept,
            "attempts": 0,
            "emails": emails,
            "phones": phones,
            "address": address,
            "matched_exclude": sorted(matched_exclude),
            "matched_include": sorted(matched_include),
            "sample_text": " ".join(sample_parts),
            "is_directory": True,
            "classification": {
                "category": classification.category,
                "confidence": classification.confidence,
                "action": classification.action,
                "method": classification.method,
                "reasoning": classification.reasoning,
            },
            "discovered_businesses": discovered_businesses,
        }

    # LLM Crawl Planner: decide which of the homepage's own links are worth
    # fetching for THIS query, within a small budget — instead of queuing every
    # link (the old behavior, which is exactly what wasted Scrape.do credits on
    # hundreds of blog/product pages). Runs on the SAME raw HTML above, still in
    # memory. Falls back to the full, unfiltered link set on ANY failure
    # (module missing, LLM down, bad response) so a planner outage can only
    # ever mean less credit savings for this site, never less recall.
    planned_urls = home_link_urls
    if ENABLE_CRAWL_PLANNER and crawl_planner is not None:
        try:
            planned = await asyncio.to_thread(
                crawl_planner.plan_site_crawl,
                domain, root_url, root_html, user_query, industry,
                exclude_keywords, include_keywords,
            )
            if planned:
                planned_urls = planned
        except Exception as exc:  # noqa: BLE001 - planning is best-effort
            logger.warning("Crawl planning failed for %s, falling back to full "
                           "link set: %s", domain, exc)

    del root_html  # the raw homepage HTML is not needed past this point

    visited: Set[str] = {_normalize_page_url(root_url)}
    # High-value contact pages first (sitemap/robots/probe), then the crawl
    # planner's selected homepage links (or all of them, if planning fell back).
    # Both are filtered against the site's OWN other-language path prefixes (if
    # any were declared) so the crawl never wanders into another locale.
    priority_urls = _drop_other_language_links(
        await _discover_contact_urls(session, root_url, domain), other_lang_prefixes,
    )
    queue: List[tuple[str, int]] = (
        [(u, 1) for u in priority_urls]
        + [(u, 1) for u in sorted(
            _drop_other_language_links(planned_urls, other_lang_prefixes),
            key=_contact_priority,
        )]
    )

    head = 0
    attempts = 0
    while (
        head < len(queue)
        and pages_kept < MAX_PAGES_PER_SITE
        and attempts < MAX_FETCH_ATTEMPTS
    ):
        # STRICTLY one page at a time (mandatory streaming): fetch, process,
        # free, then move on. No intra-site fetch batching — the single-HTML-
        # in-RAM guarantee outranks the concurrency speedup here.
        page_url, depth = queue[head]
        head += 1
        norm = _normalize_page_url(page_url)
        if norm in visited or depth > MAX_CRAWL_DEPTH:
            continue
        visited.add(norm)

        attempts += 1
        fetched = await execute_scavenger_scrape(session, page_url)
        html = fetched.get("html") or ""
        method = fetched.get("method", "FAILED")
        status_code = fetched.get("status_code")
        del fetched
        if not html:
            continue
        inner_links = _drop_other_language_links(
            await _process_one(page_url, html, method, status_code), other_lang_prefixes,
        )
        del html  # raw HTML freed immediately after processing

        if depth < MAX_CRAWL_DEPTH:
            for link in sorted(inner_links, key=_contact_priority):
                if _normalize_page_url(link) not in visited:
                    queue.append((link, depth + 1))

        # Once we have a reachable contact (email AND phone) and covered the
        # priority pages, stop — the rest is noise.
        if (STOP_WHEN_CONTACT_COMPLETE and emails and phones
                and pages_kept >= MIN_PAGES_BEFORE_STOP):
            logger.info("Contact found for %s after %d page(s) — stopping crawl early.",
                        domain, pages_kept)
            break

    logger.info(
        "Crawled %d page(s) of %s (%d fetches, %d priority candidates) — streamed to staging",
        pages_kept, domain, attempts, len(priority_urls),
    )

    # Assemble the full tech profile NOW — every page this crawl discovered is
    # known at this point, giving login/customer-portal detection its strongest
    # signal (a nav link 2 clicks deep still counts, even if never fetched).
    # Staged alongside the pages: commit_domain/discard_domain move or delete
    # it together with everything else, no separate lifecycle to maintain.
    if tech_stack is not None and (tech_raw or rule_based_detections):
        try:
            profile = tech_stack.build_website_profile(
                domain, tech_raw, homepage_url=root_url, fetch_method=root_method,
                discovered_urls=discovered_urls, rule_based=rule_based_detections,
            )
            profile["sales_signals"] = tech_stack.generate_sales_signals_from_profile(profile)
            store.stage_tech_profile(domain, profile["capabilities"], profile)
            logger.info(
                "Tech stack profiled for %s: %d Wappalyzer technolog(ies), "
                "%d rule-engine finding(s).",
                domain, len(tech_raw), len(rule_based_detections),
            )
        except Exception as exc:  # noqa: BLE001 - never break the crawl over this
            logger.warning("Failed to stage tech profile for %s: %s", domain, exc)

    # Social profile discovery (Step 1) — free (no new network requests, just
    # filtering links already collected above) and site-wide (a business's
    # social links can appear on any page, not just the homepage), so this
    # runs unconditionally, same as tech-stack profiling above. LinkedIn
    # DISCOVERY (Step 2, costs real Serper API calls) is deliberately NOT
    # run here — it's gated to qualified/committed businesses only, in
    # process_single_lead, right after commit_domain.
    try:
        social_profiles = social_discovery.extract_social_links(external_links)
        store.stage_social_profiles(domain, social_profiles)
    except Exception as exc:  # noqa: BLE001 - never break the crawl over this
        logger.warning("Failed to stage social profiles for %s: %s", domain, exc)

    if decision_makers:
        try:
            store.stage_decision_makers(domain, {"people": decision_makers})
        except Exception as exc:  # noqa: BLE001 - never break the crawl over this
            logger.warning("Failed to stage decision makers for %s: %s", domain, exc)

    return {
        "domain": domain,
        "pages_kept": pages_kept,
        "attempts": attempts,
        "emails": emails,
        "phones": phones,
        "address": address,
        "matched_exclude": sorted(matched_exclude),
        "matched_include": sorted(matched_include),
        "sample_text": " ".join(sample_parts),
        "external_links": external_links,
        "decision_makers": decision_makers,
        "schema_organization": schema_organization,
    }


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


# CSS that hides an element outright — its text is invisible to a visitor.
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
# class/id tokens that mark cookie banners, consent walls and ad slots. Matched
# against whole tokens (not substrings) so e.g. "download" never matches "ad".
_NOISE_TOKENS = frozenset({
    "cookie", "cookies", "cookie-banner", "cookie-consent", "cookie-notice",
    "cookiebar", "consent", "consent-banner", "gdpr", "gdpr-banner",
    "ad", "ads", "advert", "advertisement", "adsense", "ad-slot", "adslot",
    "ad-banner", "sponsored", "sponsor-banner",
})
# Structural tags that never carry readable page content.
_STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe",
               "canvas", "embed", "object", "head")

_PAGE_TYPE_HINTS: Dict[str, tuple] = {
    "contact": ("contact", "kontakt", "contacto", "impressum", "get-in-touch"),
    "about": ("about", "team", "who-we-are", "our-story", "company"),
    "services": ("service", "berth", "moorings", "facilities", "offering"),
    "products": ("product", "shop", "store", "pricing", "plans"),
    "blog": ("blog", "news", "article", "press", "insights"),
    "legal": ("privacy", "terms", "legal", "policy", "disclaimer"),
}


def _page_type(url: str) -> str:
    """Best-effort page classification from the URL path alone."""
    path = urlparse(url).path.strip("/").lower()
    if not path:
        return "home"
    for ptype, hints in _PAGE_TYPE_HINTS.items():
        if any(h in path for h in hints):
            return ptype
    return "other"


def _has_noise_token(tag) -> bool:
    """True if a tag's class/id tokens identify it as cookie/consent/ad chrome."""
    tokens = set(tag.get("class") or [])
    if tag.get("id"):
        tokens.add(tag["id"])
    return any(t.lower() in _NOISE_TOKENS for t in tokens)


def clean_page_for_llm(soup: BeautifulSoup, page_url: str = "") -> Dict[str, Any]:
    """Clean one parsed page into LLM-ready structured text.

    Removes CSS/JS, hidden elements, cookie/consent banners and ad slots.
    Preserves the title, meta description, H1–H6 headings (as `#`-prefixed
    lines), paragraphs, list items (as `- ` lines), tables (flattened to
    `cell | cell` rows), anchor text, and the page's reading order. Nav/footer
    TEXT is kept (that's where contacts live) but its links are captured
    separately by `_extract_internal_link_pairs` before this runs.

    Mutates `soup` (the caller is about to free it anyway). Returns
    {page_title, meta_description, canonical_url, og_title, og_description,
    text, lines, word_count}.
    """
    empty = {"page_title": "", "meta_description": "", "canonical_url": "",
             "og_title": "", "og_description": "",
             "text": "", "lines": [], "word_count": 0}
    if soup is None:
        return empty

    # Capture title + meta/OG description + canonical URL before stripping the
    # <head> — page_intelligence's context JSON reuses these (no second parse).
    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = (
        soup.find("meta", attrs={"name": "description"})
        or soup.find("meta", attrs={"property": "og:description"})
    )
    meta_desc = (meta.get("content", "") if meta else "").strip()
    canonical_tag = soup.find("link", rel="canonical")
    canonical_url = (canonical_tag.get("href", "") if canonical_tag else "").strip()
    og_title_tag = soup.find("meta", attrs={"property": "og:title"})
    og_title = (og_title_tag.get("content", "") if og_title_tag else "").strip()
    og_desc_tag = soup.find("meta", attrs={"property": "og:description"})
    og_description = (og_desc_tag.get("content", "") if og_desc_tag else "").strip()

    # Replace Cloudflare-obfuscated email placeholders with the real address so
    # "[email protected]" doesn't pollute the text and the email is recoverable.
    for el in soup.find_all(attrs={"data-cfemail": True}):
        real = _decode_cf_email(el.get("data-cfemail", ""))
        if real:
            el.replace_with(real)

    # 1) Non-visible / non-content tags.
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    # 2) Hidden elements (inline style / hidden attr / aria-hidden).
    for tag in soup.find_all(style=_HIDDEN_STYLE_RE):
        tag.decompose()
    for tag in soup.find_all(attrs={"hidden": True}):
        tag.decompose()
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()
    # 3) Cookie banners / consent walls / ad slots by class-or-id token.
    for tag in soup.find_all(_has_noise_token):
        tag.decompose()

    # 4) Structure markers, injected in place so reading order is preserved.
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            text = h.get_text(" ", strip=True)
            h.replace_with(f"\n{'#' * level} {text}\n" if text else "\n")
    for li in soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        li.replace_with(f"\n- {text}\n" if text else "\n")
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(("td", "th"))]
        row = " | ".join(c for c in cells if c)
        tr.replace_with(f"\n{row}\n" if row else "\n")

    # 5) Serialize: one line per block, mojibake-repaired, blanks and
    #    consecutive duplicates dropped.
    lines: List[str] = []
    for raw_line in soup.get_text(separator="\n").split("\n"):
        line = _fix_mojibake(" ".join(raw_line.split()))
        if line and (not lines or line != lines[-1]):
            lines.append(line)

    text = "\n".join(lines)
    return {
        "page_title": _fix_mojibake(title),
        "meta_description": _fix_mojibake(meta_desc),
        "canonical_url": canonical_url,
        "og_title": _fix_mojibake(og_title),
        "og_description": _fix_mojibake(og_description),
        "text": text,
        "lines": lines,
        "word_count": len(text.split()),
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
    # ReDoS guard: only trust a short LLM-generated regex; anything oversized
    # falls back to the safe fixed pattern.
    if phone_regex and len(phone_regex) <= MAX_PHONE_REGEX_LEN:
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
    # Then every number matched in the visible text. Scan only a bounded slice
    # so a pathological regex can't run away on a huge page (ReDoS guard).
    for m in active_re.findall((text or "")[:MAX_PHONE_SCAN_CHARS]):
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


# Characters that make a spreadsheet treat a cell as a FORMULA. Scraped content
# is untrusted, so any cell starting with one of these is prefixed with a quote
# to neutralize CSV/formula injection when an export is opened in Excel/Sheets.
# Shared by the CSV exporters (data_pipeline, linkedin_finder).
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> str:
    """Neutralize spreadsheet formula injection in an untrusted cell value."""
    s = "" if value is None else str(value)
    if s and s[0] in _CSV_INJECTION_PREFIXES:
        return "'" + s
    return s


def load_cache() -> Dict[str, List[Dict[str, str]]]:
    """Return previously-crawled businesses keyed by root website URL.

    Reads the per-page crawl index through the storage layer (metadata only —
    the cleaned text lives in storage/<domain>/*.txt). A business already in
    the index is skipped (not re-crawled) and re-qualified from its stored
    pages, streamed one at a time.
    """
    cached: Dict[str, List[Dict[str, str]]] = {}
    for row in get_store().read_index():
        site = row.get("website_url")
        if site and site != "N/A":
            # Key on the tracking-stripped URL so rows stored under different
            # srsltid tags collapse to one business and match this run's URL.
            cached.setdefault(_strip_tracking(site), []).append(row)
    return cached


def _cache_needs_recrawl(url: str, rows: List[Dict[str, str]]) -> bool:
    """Return True when a cached site is too shallow for its discovered links.

    Uses the domain's persisted link metadata (links.json, written at commit
    time) instead of re-parsing raw HTML: if the site exposed internal pages
    that were never stored, the entry is stale — refresh it. A full recrawl
    replaces the domain's stored pages and index rows.
    """
    if len(rows) >= MIN_CACHED_PAGES_TO_TRUST:
        return False
    links_by_page = get_store().read_links(_domain_key(url))
    if not links_by_page:
        return True    # no link metadata — old/incomplete entry
    stored = {
        _normalize_page_url(r.get("page_url") or r.get("website_url") or "")
        for r in rows
    }
    return any(
        _normalize_page_url(link.get("url", "")) not in stored
        for page_links in links_by_page.values()
        for link in page_links
    )


def _scan_stored_pages(
    rows: List[Dict[str, str]],
    exclude_keywords: List[str],
    include_keywords: List[str],
) -> Dict[str, Any]:
    """Re-qualify a cached site by streaming its stored .txt pages ONE at a time.

    Mirrors the live crawl's aggregation: exact keyword hits on each full page,
    plus a bounded classification sample — never the whole site's text in RAM.
    """
    store = get_store()
    matched_exclude: Set[str] = set()
    matched_include: Set[str] = set()
    sample_parts: List[str] = []
    sample_chars = 0
    has_text = False
    for i, row in enumerate(rows):
        text = store.read_page_text(row.get("txt_path", ""))
        if not text:
            continue
        has_text = True
        low = text.lower()
        matched_exclude.update(k for k in exclude_keywords or [] if k.lower() in low)
        matched_include.update(k for k in include_keywords or [] if k.lower() in low)
        if sample_chars < CLASSIFY_SAMPLE_MAX_CHARS:
            take = _SAMPLE_HOME_CHARS if i == 0 else _SAMPLE_PAGE_CHARS
            part = text[:take]
            sample_parts.append(part)
            sample_chars += len(part)
        del text
    return {
        "matched_exclude": sorted(matched_exclude),
        "matched_include": sorted(matched_include),
        "sample_text": " ".join(sample_parts),
        "has_text": has_text,
    }


# === Step 5: Lead qualification ============================================
def qualify_lead(
    matched_exclude: List[str],
    matched_include: List[str],
    include_keywords: List[str],
    has_text: bool = True,
) -> Dict[str, Any]:
    """Build the qualification verdict from keyword hits aggregated page by page.

    Same rules as ever: a lead is QUALIFIED when no exclude keyword was seen and
    (if include keywords are given) at least one include keyword was. The hits
    are collected by the streaming crawl, which checks each FULL page's text
    before freeing it — exact matching with no whole-site text in RAM. A site
    with no readable text is 'no_content' rather than silently passing.
    """
    if not has_text:
        return {"qualified": False, "reason": "no_content",
                "matched_exclude": [], "matched_include": []}
    if matched_exclude:
        return {"qualified": False, "reason": "excluded",
                "matched_exclude": list(matched_exclude),
                "matched_include": list(matched_include)}
    if include_keywords and not matched_include:
        return {"qualified": False, "reason": "missing_required",
                "matched_exclude": [], "matched_include": []}
    return {"qualified": True, "reason": "passed",
            "matched_exclude": [], "matched_include": list(matched_include)}


async def classify_relevance(
    industry: str, geo: str, name: str, page_text: str
) -> Dict[str, str]:
    """Step 5.5 — LLM relevance check (industry/location match, single business
    vs shop/aggregator).

    Runs off the event loop. This is the ONLY stage that validates industry and
    location, so it must fail CLOSED: on any failure (both models in call_llm
    exhausted their retries, malformed response, etc.) it returns "unknown" —
    NOT "match". "unknown" is treated by _is_lead() as not-a-lead, same as any
    other non-"match" category. A classifier outage must never silently turn
    into "this is a valid lead"; it must simply mean fewer qualified leads
    (an honest shortfall), never a wrong one in the output.
    """
    try:
        return await asyncio.to_thread(
            classify_business, industry or "", geo or "", name or "", page_text
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Relevance classification unavailable for %s (both models failed: "
            "%s) — treating as not-a-lead, not as a match.", name, exc,
        )
        return {"category": "unknown", "reason": "classifier_unavailable"}


async def _evaluate_lead(
    industry: str, geo: str, name: str,
    matched_exclude: List[str], matched_include: List[str],
    include_keywords: List[str], sample_text: str, has_text: bool = True,
) -> tuple[Dict[str, Any], Optional[Dict[str, str]]]:
    """Return (qualification, classification) for a business.

    Fed by streamed aggregates: exact keyword hits collected page-by-page during
    the crawl (or from stored pages), plus a bounded text sample for the LLM
    relevance classifier — the whole site's text is never held in memory.

    When LEAD_FILTERING_ENABLED is False, filtering is skipped entirely: the
    business passes as qualified and NO relevance LLM call is made, so discovery
    alone decides relevance. Flip the flag to re-enable filtering.
    """
    if not LEAD_FILTERING_ENABLED:
        passthrough = {"qualified": True, "reason": "filtering_disabled",
                       "matched_exclude": [], "matched_include": []}
        return passthrough, None

    qualification = qualify_lead(matched_exclude, matched_include,
                                 include_keywords, has_text)
    classification = None
    if qualification.get("qualified"):
        classification = await classify_relevance(industry, geo, name, sample_text)
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
    user_query: str = "",
) -> Dict[str, Any]:
    """Cache-check, STREAM-crawl the whole site (one page at a time, each page
    staged to storage the moment it's cleaned), then qualify the business from
    the crawl's aggregates (Step 5) and classify its relevance (Step 5.5).
    Qualified businesses are committed to final storage; rejected ones
    (aggregator / unrelated / spam) have their staged files deleted."""
    name = target.get("title")
    # Canonicalize: strip srsltid/utm/etc so re-runs hit the cache instead of
    # re-scraping. The stripped URL still fetches fine (sites ignore those tags).
    url = _strip_tracking(target.get("website"))

    if not url:
        return {"company_name": name, "website_url": None, "status": "no_website"}

    store = get_store()
    domain = _domain_key(url)

    # Cache check: a business already in the index is not re-crawled. Its stored
    # pages are streamed one at a time and re-qualified against the (possibly
    # new) keywords — metadata comes from the index, text from the .txt files.
    cached_rows = cache.get(url)
    if cached_rows and not _cache_needs_recrawl(url, cached_rows):
        logger.info(
            "Cache HIT for %s (%d page(s)) — qualifying from stored pages.",
            url, len(cached_rows),
        )
        scan = _scan_stored_pages(cached_rows, exclude_keywords, include_keywords)
        email = CONTACT_SEP.join(_union_contacts(cached_rows, "email")) or "N/A"
        phone = CONTACT_SEP.join(_union_contacts(cached_rows, "phone_number")) or "N/A"
        qualification, classification = await _evaluate_lead(
            industry, geo, name,
            scan["matched_exclude"], scan["matched_include"],
            include_keywords, scan["sample_text"], scan["has_text"],
        )
        if LEAD_FILTERING_ENABLED and not (
            qualification.get("qualified")
            and (classification or {}).get("category", "match") == "match"
        ):
            # A previously-committed domain that no longer qualifies under
            # this query's industry/geo/keywords (or was wrongly committed
            # before this fix existed) must not linger in final storage where
            # a later query could resurface it as a "clean" cached lead.
            store.retire_domain(domain)
            cache.pop(url, None)
            logger.info(
                "Discarded stale commit for %s — no longer qualifies (%s).",
                domain, (classification or {}).get("category")
                or qualification.get("reason"),
            )
        return {"company_name": name, "website_url": url, "status": "cache_hit",
                "method": "CACHE", "pages": len(cached_rows), "email": email,
                "phone": phone, "qualification": qualification,
                "classification": classification}
    if cached_rows:
        logger.info(
            "Cache refresh for %s: only %d cached page(s), more internal links "
            "visible — recrawling (commit will replace the stored copy).",
            url, len(cached_rows),
        )

    # Scrape the homepage with the full two-tier scrape, then stream-crawl the
    # rest of the site from it. Every page is cleaned and staged to storage
    # immediately; nothing heavy survives the crawl.
    try:
        async with semaphore:
            root = await execute_scavenger_scrape(session, url)
            if root["method"] == "FAILED" or not root["html"]:
                logger.warning("No content resolved for %s", url)
                return {"company_name": name, "website_url": url,
                        "status": "failed", "method": "FAILED"}
            root_method = root["method"]
            root_headers = root.get("headers", {})
            root_status_code = root.get("status_code")
            root_cookies = root.get("cookies")
            crawl = await crawl_site(
                session, url, root.pop("html"), root_method,
                root_headers=root_headers,
                root_status_code=root_status_code,
                root_cookies=root_cookies,
                company_name=name or "",
                country_code=country_code, phone_regex=phone_regex,
                exclude_keywords=exclude_keywords,
                include_keywords=include_keywords,
                user_query=user_query, industry=industry,
            )
    except Exception:
        # A crawl abandoned mid-flight must not leave staged files behind.
        store.discard_domain(domain)
        raise

    if crawl.get("is_directory"):
        # Directory/aggregator/informational site: never a business in its
        # own right, so its staged homepage is always discarded — never
        # committed, regardless of qualify_lead. Its discovered_businesses
        # are surfaced so the caller (run_pipeline) can queue each of them as
        # its OWN crawl job.
        store.discard_domain(domain)
        logger.info(
            "Not stored: %s — classified as %s, %d business(es) queued "
            "for their own crawl.",
            name, crawl["classification"]["category"],
            len(crawl.get("discovered_businesses", [])),
        )
        return {"company_name": name, "website_url": url, "status": "directory",
                "method": root_method, "classification": crawl["classification"],
                "discovered_businesses": crawl.get("discovered_businesses", [])}

    if not crawl["pages_kept"]:
        logger.warning("No readable content on any page of %s", url)
        store.discard_domain(domain)
        return {"company_name": name, "website_url": url, "status": "failed",
                "method": root_method}

    # Site-wide aggregated contacts (joined) for the result summary.
    email = CONTACT_SEP.join(crawl["emails"]) if crawl["emails"] else None
    phone = CONTACT_SEP.join(crawl["phones"]) if crawl["phones"] else None

    # Evaluate BEFORE committing. With filtering enabled, only real qualified
    # leads are kept; rejected businesses' staged pages are deleted.
    qualification, classification = await _evaluate_lead(
        industry, geo, name,
        crawl["matched_exclude"], crawl["matched_include"],
        include_keywords, crawl["sample_text"],
    )
    is_lead = bool(
        qualification.get("qualified")
        and (classification or {}).get("category", "match") == "match"
    )

    if is_lead:
        store.commit_domain(domain, extra_fields={
            "relevance_category": (classification or {}).get("category", ""),
            "relevance_reason": (classification or {}).get("reason", ""),
            "validated_industry": industry or "",
            "validated_geo": geo or "",
        })
        cache[url] = [r for r in store.read_index() if r.get("domain") == domain]
        logger.info(
            "Committed %d page(s) for %s -> storage/%s/",
            crawl["pages_kept"], name, domain,
        )
        # Back-fill each page's incoming-anchor evidence now that the whole
        # site's link graph is final (links.json only exists post-commit) —
        # best-effort, never blocks/breaks a successful commit.
        try:
            page_intelligence.enrich_anchors(domain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Anchor enrichment failed for %s: %s", domain, exc)

        # Social/LinkedIn fallback discovery (Step 2) — gated to committed/
        # qualified leads only, since (unlike social-link extraction above)
        # this costs real Serper API calls; never worth spending on a
        # business that just got discarded. Cached: only runs for whichever
        # platforms social_profiles.json doesn't already have a URL for — a
        # future run against the same committed domain skips straight past
        # this, satisfying "reuse cached data, don't repeat discovery."
        social_profiles: Dict[str, Any] = {}
        li_data: Optional[Dict[str, Any]] = None
        try:
            social_profiles = store.read_social_profiles(domain) or {}
            if not social_profiles.get("linkedin"):
                match = await linkedin_discovery.discover_linkedin_company(
                    session, name or "", domain,
                )
                if match:
                    social_profiles["linkedin"] = match.url
                    store.write_social_profiles_now(domain, social_profiles)
                    li_data = await linkedin_enrichment.enrich_linkedin_company(match, domain)
                    store.write_linkedin_company_now(domain, li_data)
                    logger.info(
                        "LinkedIn company page found for %s: %s (confidence=%.2f)",
                        domain, match.url, match.confidence,
                    )
            else:
                li_data = store.read_linkedin_company(domain)

            # Same fallback engine, generalized to Facebook/Instagram/X (see
            # linkedin_discovery.discover_profile_via_search's docstring —
            # one search/confidence implementation, not four near-copies).
            # Still never scrapes any of these platforms directly.
            _FALLBACK_PLATFORMS = (
                ("facebook", "facebook.com"),
                ("instagram", "instagram.com"),
                ("x", "x.com"),
            )
            for platform, host in _FALLBACK_PLATFORMS:
                if social_profiles.get(platform):
                    continue
                queries = [f'site:{host} "{name}"'] if name else []
                queries.append(f"site:{host} {domain}")
                match = await linkedin_discovery.discover_profile_via_search(
                    session, queries, name or "",
                    lambda link, h=host: h in link,
                )
                if match:
                    social_profiles[platform] = match.url
                    logger.info(
                        "%s profile found for %s: %s (confidence=%.2f)",
                        platform, domain, match.url, match.confidence,
                    )
            store.write_social_profiles_now(domain, social_profiles)
        except Exception as exc:  # noqa: BLE001 - best-effort, never blocks a commit
            logger.warning("Social/LinkedIn discovery failed for %s: %s", domain, exc)

        # GitHub/YouTube enrichment — real public APIs (Step 3's ONLY
        # platforms with a genuine ToS-compliant public API for this data;
        # see github_enrichment.py / youtube_enrichment.py docstrings).
        github_profile: Optional[Dict[str, Any]] = None
        youtube_profile: Optional[Dict[str, Any]] = None
        try:
            if social_profiles.get("github"):
                github_profile = await github_enrichment.enrich_github(
                    session, social_profiles["github"],
                )
            if social_profiles.get("youtube"):
                youtube_profile = await youtube_enrichment.enrich_youtube(
                    session, social_profiles["youtube"],
                )
        except Exception as exc:  # noqa: BLE001 - best-effort, never blocks a commit
            logger.warning("GitHub/YouTube enrichment failed for %s: %s", domain, exc)

        # Decision makers (Step 4) — merge sources #1/#2 (already staged
        # during the crawl: website team/about/contact pages + schema.org)
        # with source #4 (public search), used ONLY as a last resort when
        # nothing named was found on-site, then annotate with likely
        # business-problem ownership (Step 7) and build the org chart
        # (Step 6) and consolidated summary (Step 8).
        try:
            people: List[Dict[str, Any]] = list(crawl.get("decision_makers") or [])
            if not any(p.get("name") for p in people):
                fallback_people = await public_search_decision_makers.discover_via_public_search(
                    session, domain,
                )
                people.extend(fallback_people)

            people = buying_committee.annotate_buying_committee(people)
            store.write_decision_makers_now(domain, {"people": people})

            org = organization.build_organization(people)
            store.write_organization_now(domain, org)

            bi_summary = business_intelligence.build_business_intelligence_summary(
                domain, social_profiles, li_data, people, org,
                github_profile, youtube_profile,
            )
            store.write_business_intelligence_now(domain, bi_summary)
            logger.info(
                "Business intelligence assembled for %s: %d decision-maker(s), "
                "%d department(s), %d social platform(s).",
                domain, org.get("total_people", 0),
                len(org.get("department_counts") or {}),
                bi_summary.get("social", {}).get("platform_count", 0),
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, never blocks a commit
            logger.warning("Business intelligence assembly failed for %s: %s", domain, exc)
    else:
        store.discard_domain(domain)
        reason = (classification or {}).get("category") or qualification.get("reason")
        logger.info("Not stored (%s): %s — staged pages discarded.", reason, name)

    return {"company_name": name, "website_url": url, "status": "scraped",
            "method": root_method, "pages": crawl["pages_kept"],
            "stored": is_lead, "text_len": len(crawl["sample_text"]),
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

    orphaned = get_store().cleanup_orphaned_staging()
    if orphaned:
        logger.info(
            "Cleaned up %d orphaned staging director(ies) left by a previous "
            "unclean shutdown: %s", len(orphaned), orphaned,
        )

    cache = load_cache()
    logger.info("Loaded %d previously-analyzed leads from the crawl index.", len(cache))

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
        "blocked": False,
        "block_reason": "",
    }

    # Step 0: moderation gate — runs before ANY other resource (Serper,
    # Scrape.do, the intent/route-planner LLM calls, storage) is touched, so
    # a policy-violating query costs at most this one small check, not a
    # full discovery+scrape+classify run.
    moderation = await moderate_user_query(user_query)
    if not moderation["safe"]:
        summary["blocked"] = True
        summary["block_reason"] = f"{moderation['category']}: {moderation['reason']}"
        summary["error"] = "blocked_by_moderation"
        return summary

    try:
        plan = await deconstruct_intent(user_query)
    except Exception as exc:  # noqa: BLE001 - degrade cleanly on LLM/network failure
        logger.critical("Intent deconstruction failed (LLM/network): %s", exc)
        summary["error"] = f"intent_failed: {exc}"
        return summary
    summary["plan"] = plan

    # Resolve the effective count: explicit override > plan (only if the user's
    # OWN text stated a number) > default. `aggressive_discovery` gates whether
    # the general-search round-loop below is allowed to page Serper repeatedly
    # and mine directories across multiple rounds to reach the count, or must
    # stay to a single, cheap, predictable pass — see the loop below. A caller-
    # supplied `limit` counts as explicit (it's an intentional override).
    count_explicit = bool(plan.get("count_explicit"))
    aggressive_discovery = bool(limit) or count_explicit
    effective_limit = limit or (plan.get("result_limit") if count_explicit else None) or DEFAULT_RESULT_LIMIT
    effective_limit = max(1, int(effective_limit))
    summary["limit"] = effective_limit
    summary["discovery_mode"] = "aggressive" if aggressive_discovery else "single_pass"

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
                scan = _scan_stored_pages(rows, exclude_keywords, include_keywords)
                email = CONTACT_SEP.join(_union_contacts(rows, "email")) or "N/A"
                phone = CONTACT_SEP.join(_union_contacts(rows, "phone_number")) or "N/A"
                qualification, classification = await _evaluate_lead(
                    industry, geo, rows[0].get("company_name") or target_domain,
                    scan["matched_exclude"], scan["matched_include"],
                    include_keywords, scan["sample_text"], scan["has_text"],
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
                    targets, _ = await discover_targets(
                        session, query, 1, country_code=country_code, location=geo,
                    )
                summary["discovered"] = len(targets)
                for target in targets:
                    r = await process_single_lead(
                        session, semaphore, target, cache, exclude_keywords,
                        include_keywords, industry, geo,
                        country_code=country_code, phone_regex=phone_regex,
                        user_query=user_query,
                    )
                    results.append(r)
                    if r.get("status") == "directory":
                        # The requested "site" turned out to be a directory,
                        # not a business — process the real businesses it
                        # pointed to instead of returning nothing.
                        discovered = r.get("discovered_businesses", [])
                        logger.info(
                            "%s classified as %s — processing its %d discovered "
                            "business(es) directly.", r.get("website_url"),
                            r["classification"]["category"], len(discovered),
                        )
                        for biz in discovered[:effective_limit]:
                            results.append(await process_single_lead(
                                session, semaphore, biz, cache, exclude_keywords,
                                include_keywords, industry, geo,
                                country_code=country_code, phone_regex=phone_regex,
                                user_query=user_query,
                            ))
        else:
            # GENERAL: keep discovering — deeper Serper pages, plus mining any
            # discovery sources (directories/associations/etc.) encountered —
            # across MULTIPLE rounds, accumulating candidates in a deduplicated
            # queue and crawling newly-queued businesses each round, UNTIL
            # `effective_limit` REAL QUALIFIED leads are collected. This is the
            # actual goal ("50 marinas" means 50 qualified companies), not "50
            # search results attempted". Bounded by MAX_TOTAL_SEARCH_PAGES_PER_RUN,
            # tracked independently of any single query's own Serper page cursor
            # (query-variation expansion below may switch queries entirely) — a
            # query with few genuine businesses can't page forever; the honest
            # shortfall is reported instead of padding the count.
            original_query = query
            state = _load_search_state()
            start_page = state.get(query, 1)
            current_page = start_page
            original_final_page = current_page
            logger.info("General search — resuming from Serper page %d.", current_page)

            exclude_set = set(existing_by_domain)
            attempted: Set[str] = set()
            soft_fail_counts: Dict[str, int] = {}
            results_index_by_domain: Dict[str, int] = {}
            queue = dc.DiscoveryQueue()
            total_discovered = 0
            last_page = current_page
            round_num = 0
            exhausted = False
            total_pages_used = 0
            tried_queries: List[str] = [query]
            pending_variations: List[str] = []
            queries_used: List[str] = [query]

            # Phase A — Google Maps (Places): a fast source of REAL individual
            # businesses, queued once upfront (Places sweeps its own pages
            # internally, up to MAX_PLACES_PAGES — not part of the round loop).
            if USE_PLACES_DISCOVERY:
                place_targets = await discover_places(
                    session, query, limit=effective_limit * 2,
                    exclude_domains=exclude_set,
                    country_code=country_code, location=geo,
                )
                total_discovered += len(place_targets)
                for t in place_targets:
                    queue.add(dc.QueuedBusiness(
                        website=t["website"], title=t.get("title", ""),
                        snippet=t.get("snippet", ""),
                        discovered_from=list(t.get("discovered_from") or ["Google Maps"]),
                        confidence=dc.Confidence(
                            t.get("source_confidence", dc.Confidence.HIGH.value)
                        ),
                    ))
                logger.info("Places discovery queued %d business(es).", len(place_targets))

            # Phase B — organic search + discovery-source mining. When the
            # user gave an explicit count, this exhausts every reasonable
            # option before giving up: deeper Serper pages, more mining, a
            # later revisit for candidates that only failed to SCRAPE (not
            # ones actively rejected for relevance), and finally LLM-generated
            # same-intent query variations once the original query itself
            # runs dry — "50 marinas" means make every reasonable effort to
            # find 50 qualified companies, never by fabricating one or
            # loosening industry/location relevance. When no count was given,
            # stick to exactly ONE round: a single, cheap, predictable pass
            # over the first page of results, returning whatever high-
            # confidence matches that naturally turns up rather than paging
            # Serper or expanding the search just to hit the default-5 quota.
            while (
                _is_lead_count(results) < effective_limit
                and total_pages_used < MAX_TOTAL_SEARCH_PAGES_PER_RUN
                and (aggressive_discovery or round_num == 0)
            ):
                if exhausted:
                    # Single-pass mode never gets here with anything useful to
                    # do (round_num == 0 already ends the loop above), so this
                    # expansion path only ever runs for an explicit count.
                    if not (aggressive_discovery and _is_lead_count(results) < effective_limit):
                        break
                    if not pending_variations and len(tried_queries) < MAX_QUERY_VARIATIONS_TOTAL:
                        try:
                            pending_variations = await asyncio.to_thread(
                                generate_query_variations,
                                user_query, industry, geo, tried_queries,
                            )
                        except Exception as exc:  # noqa: BLE001 - expansion is best-effort
                            logger.warning("Query-variation generation failed: %s", exc)
                            pending_variations = []
                        if pending_variations:
                            logger.info(
                                "Query %r exhausted (%d/%d qualified so far) — "
                                "expanding discovery with %d same-intent variation(s): %s",
                                query, _is_lead_count(results), effective_limit,
                                len(pending_variations), pending_variations,
                            )
                    if not pending_variations:
                        logger.info(
                            "All discovery strategies exhausted for %r — no more "
                            "query variations available (collected %d/%d qualified "
                            "leads; %d distinct query/queries tried).",
                            user_query, _is_lead_count(results), effective_limit,
                            len(tried_queries),
                        )
                        break
                    query = pending_variations.pop(0)
                    tried_queries.append(query)
                    queries_used.append(query)
                    variation_state = _load_search_state()
                    current_page = variation_state.get(query, 1)
                    exhausted = False

                round_num += 1
                pending = queue.pending(attempted)

                # Top up the queue only if it can't already cover this round.
                if len(pending) < effective_limit:
                    page_before_call = current_page
                    batch_targets, last_page = await discover_targets(
                        session, query,
                        limit=effective_limit,
                        exclude_domains=exclude_set | attempted | queue.domains(),
                        start_page=current_page,
                        per_page=effective_limit,
                        max_pages=MAX_SEARCH_PAGES if aggressive_discovery else 1,
                        country_code=country_code, location=geo,
                    )
                    total_discovered += len(batch_targets)
                    total_pages_used += max(1, last_page - page_before_call + 1)
                    current_page = last_page + 1
                    if query == original_query:
                        original_final_page = current_page
                    if not batch_targets:
                        exhausted = True
                    for t in batch_targets:
                        queue.add(dc.QueuedBusiness(
                            website=t["website"], title=t.get("title", ""),
                            snippet=t.get("snippet", ""),
                            discovered_from=list(t.get("discovered_from") or ["Google Search"]),
                            confidence=dc.Confidence(
                                t.get("source_confidence", dc.Confidence.MEDIUM.value)
                            ),
                        ))
                    pending = queue.pending(attempted)

                if not pending:
                    logger.info(
                        "Discovery exhausted at round %d for query %r — no more "
                        "candidates yet (collected %d/%d qualified leads).",
                        round_num, query, _is_lead_count(results), effective_limit,
                    )
                    exhausted = True
                    continue  # top-of-loop check decides: expand query, or stop

                still_needed = max(effective_limit - _is_lead_count(results), 1)
                batch = pending[:still_needed]
                for b in batch:
                    attempted.add(dc.domain_key(b.website))
                batch_results = await asyncio.gather(*[
                    process_single_lead(
                        session, semaphore, b.as_target(), cache, exclude_keywords,
                        include_keywords, industry, geo,
                        country_code=country_code, phone_regex=phone_regex,
                        user_query=user_query,
                    )
                    for b in batch
                ])

                # Soft-failure revisit: a candidate whose scrape totally failed
                # (no readable content from ANY tier, even after the retry/
                # failover hardening in execute_scavenger_scrape) is un-attempted
                # so queue.pending() offers it again in a LATER round — it might
                # have been a purely transient outage. A candidate actually
                # REJECTED (wrong industry/location/directory/keyword exclusion)
                # is never revisited — only ones that never got a verdict at all.
                # Bounded by MAX_SOFT_FAIL_RETRIES so a permanently-broken site
                # doesn't loop forever; it then simply counts toward the shortfall.
                for r in batch_results:
                    if r.get("status") != "failed":
                        continue
                    domain = dc.domain_key(r.get("website_url") or "")
                    if not domain:
                        continue
                    soft_fail_counts[domain] = soft_fail_counts.get(domain, 0) + 1
                    if aggressive_discovery and soft_fail_counts[domain] < MAX_SOFT_FAIL_RETRIES:
                        attempted.discard(domain)
                        logger.info(
                            "%s failed to scrape (attempt %d/%d) — will retry in "
                            "a later round if still needed.",
                            domain, soft_fail_counts[domain], MAX_SOFT_FAIL_RETRIES,
                        )

                # A revisited candidate's earlier "failed" row is REPLACED by
                # this round's outcome rather than accumulating a duplicate
                # entry for the same business.
                for r in batch_results:
                    domain = dc.domain_key(r.get("website_url") or "")
                    if domain and domain in results_index_by_domain:
                        results[results_index_by_domain[domain]] = r
                    else:
                        results.append(r)
                        if domain:
                            results_index_by_domain[domain] = len(results) - 1

                # A "directory" result mined real business links from its
                # homepage instead of being deep-crawled itself — queue each
                # one as its OWN crawl job (own storage, own crawl planner),
                # exactly like any other discovered business. Picked up
                # automatically next round by the same pending()/attempted set.
                mined_count = 0
                for r in batch_results:
                    if r.get("status") != "directory":
                        continue
                    for biz in r.get("discovered_businesses", []):
                        if queue.add(dc.QueuedBusiness(
                            website=biz["website"], title=biz.get("title", ""),
                            snippet=biz.get("snippet", ""),
                            discovered_from=list(biz.get("discovered_from") or []),
                            confidence=dc.Confidence(
                                biz.get("source_confidence", dc.Confidence.MEDIUM.value)
                            ),
                        )):
                            mined_count += 1
                if mined_count:
                    logger.info(
                        "Round %d: mined %d new business(es) from directory site(s) "
                        "encountered this round.", round_num, mined_count,
                    )

                logger.info(
                    "Round %d (%r): attempted %d business(es) -> %d/%d qualified "
                    "leads so far (queue: %d total, %d unattempted).",
                    round_num, query, len(batch), _is_lead_count(results),
                    effective_limit, len(queue), len(queue.pending(attempted)),
                )

            # Persist the cursor for the ORIGINAL query only — variations are a
            # this-run-only expansion, not separately resumed on a future run.
            state = _load_search_state()
            state[original_query] = original_final_page
            _save_search_state(state)
            summary["serper_pages_used"] = f"{start_page}-{original_final_page}"
            summary["next_run_starts_page"] = original_final_page
            summary["queries_used"] = queries_used
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
        "Phase 1 complete. %d qualified leads (of %d processed). Index: %s",
        len(qualified), len(results), CRAWL_INDEX_FILE,
    )
    # Record this query's scope so the data-quality report covers only the
    # latest query, not the whole cumulative store.
    _save_last_run(user_query, results, industry=industry, geo=geo)
    return summary


def print_summary(summary: Dict[str, Any]) -> None:
    """Render the pipeline's output return in a readable block."""
    print("\n" + "=" * 64)
    print("PHASE 1 OUTPUT RETURN")
    print("=" * 64)
    if summary.get("blocked"):
        # User-facing message is deliberately short and generic -- the
        # detailed category/reason is still captured in summary["block_reason"]
        # and the warning log for internal review, but isn't surfaced here
        # (avoids handing back a roadmap for rephrasing around the filter).
        print(f"Query      : {summary['query']}")
        print("Status     : This request violates our usage policy and was not processed.")
        print("=" * 64)
        return
    print(f"Query      : {summary['query']}")
    print(f"Search type: {summary.get('search_type', 'general')}")
    if summary.get("serper_pages_used"):
        print(f"Serper pages used : {summary['serper_pages_used']}  "
              f"| Next run starts at page {summary.get('next_run_starts_page')}")
    print(f"Plan       : {json.dumps(summary['plan'], ensure_ascii=False)}")
    print(
        f"Target leads: {summary.get('limit')} "
        f"({summary.get('discovery_mode', 'aggressive')})  "
        f"|  Discovered : {summary['discovered']}"
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
    print(f"Crawl index: {CRAWL_INDEX_FILE}  |  Cleaned pages: storage/<domain>/*.txt\n")


async def _run_tech_stack_batch(ts_module, domains: Set[str]) -> Dict[str, Any]:
    """Run tech_stack.get_stored_profile() for each domain concurrently.

    get_stored_profile() ALWAYS checks storage first — every domain here was
    just crawled in Stage 1 above, which already ran Wappalyzer on its
    homepage and staged the result, so this is normally a pure disk read (only
    a rare fallback scan makes a blocking request, hence still offloaded via
    asyncio.to_thread). This module never shares the crawler's aiohttp session.
    """
    domains = list(domains)
    profiles = await asyncio.gather(
        *[asyncio.to_thread(ts_module.get_stored_profile, d) for d in domains]
    )
    return dict(zip(domains, profiles))


async def _run_route_planner_batch(rp_module, domains: Set[str]) -> Dict[str, Any]:
    """Run route_planner.plan_routes() for each domain concurrently.

    plan_routes() reads the committed crawl index + a few cleaned .txt previews
    and makes one Groq call per site, all synchronous — so each is offloaded
    via asyncio.to_thread so N sites plan in parallel rather than serially.
    """
    domains = list(domains)
    plans = await asyncio.gather(
        *[asyncio.to_thread(rp_module.plan_routes, d) for d in domains]
    )
    return dict(zip(domains, plans))


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
    parser.add_argument(
        "--no-route-planner", action="store_true",
        help="Skip the LLM Route Planner (high_intent_pages) after scraping.",
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

    # Stage 1.5 — Tech Stack Detection: ONLY if this query's intent needs it
    # (LLM_planner sets plan["needs_tech_stack"]). Runs after Stage 1 has
    # already discovered/crawled every qualified business; it never triggers a
    # crawl itself, it only consumes what Stage 1 already produced.
    tech_stacks: Dict[str, str] = {}
    plan = summary.get("plan") or {}
    if plan.get("needs_tech_stack"):
        import json as _json
        import tech_stack as ts
        domains = {
            _domain_key(q["website_url"]) for q in summary.get("qualified", [])
            if q.get("website_url")
        }
        print("\n" + "#" * 64)
        print(f"STEP 4 — TECH STACK DETECTION  ({len(domains)} business(es))")
        print("#" * 64)
        profiles = asyncio.run(_run_tech_stack_batch(ts, domains))
        for domain, profile in profiles.items():
            tech_stacks[domain] = _json.dumps(profile, ensure_ascii=False, default=str)
            print(f"\n■ {domain}")
            if profile.get("error"):
                print(f"    error: {profile['error']}")
                continue
            normalized = profile.get("normalized_tech_stack", {})
            for bucket in ts.NORMALIZED_BUCKETS:
                if normalized.get(bucket):
                    names = ", ".join(t["name"] for t in normalized[bucket])
                    print(f"    {bucket:<14}: {names}")
            for s in profile.get("sales_signals", []):
                print(f"    [{s['confidence']:.2f}] {s['signal']} "
                      f"-> {', '.join(s['recommended_services'])}")

    # Stage 1.6 — LLM Route Planner: pick each qualified business's most
    # valuable pages (high_intent_pages) from the cleaned .txt files Phase 1
    # already produced. Consumes stored data only (no crawl); the LLM decides
    # from lightweight page previews, never full text. Independent of intent —
    # always useful, unlike tech-stack which is gated.
    routes: Dict[str, str] = {}
    if not args.no_route_planner:
        import json as _json
        import route_planner as rp
        rp_domains = {
            _domain_key(q["website_url"]) for q in summary.get("qualified", [])
            if q.get("website_url")
        }
        print("\n" + "#" * 64)
        print(f"STEP 5 — LLM ROUTE PLANNER  ({len(rp_domains)} business(es))")
        print("#" * 64)
        plans = asyncio.run(_run_route_planner_batch(rp, rp_domains))
        for domain, plan_result in plans.items():
            pages = plan_result.get("selected_pages", [])
            routes[domain] = _json.dumps(pages, ensure_ascii=False, default=str)
            print(f"\n■ {domain}  (confidence: {plan_result.get('confidence', '?')})")
            for p in pages:
                print(f"    [{p['priority']}] {p['filename']} — {p['reason']}")

    # Stage 2 — clean + format: derive the governed dataset in the same command.
    # Imported lazily so data_pipeline (which imports this module) stays decoupled.
    if not args.no_clean:
        import os
        import data_pipeline
        print("\n[clean] Running data-quality pipeline...")
        data_pipeline.run(tech_stacks=tech_stacks, routes=routes)

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
