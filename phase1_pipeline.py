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
import sys
import threading
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import aiohttp
import tldextract
from bs4 import BeautifulSoup
from dotenv import load_dotenv

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

# --- Configuration ---------------------------------------------------------
load_dotenv()

SERPER_API_KEY = os.getenv("serper") or os.getenv("SERPER_API_KEY")
ZENROWS_API_KEY = os.getenv("zenrows") or os.getenv("ZENROWS_API_KEY")

OUTPUT_CSV_FILE = "scavenger_leads_cache.csv"

SERPER_SEARCH_URL = "https://google.serper.dev/search"
ZENROWS_URL = "https://api.zenrows.com/v1/"

# Tunable operational parameters (kept as named constants, not magic numbers).
DEFAULT_RESULT_LIMIT = 20        # used when the query/plan specifies no count
MAX_SEARCH_PAGES = 12            # cap on Serper web-search pagination
SEARCH_RESULTS_PER_PAGE = 10     # Serper returns ~10 organic results per page
MAX_DIRECTORIES_TO_HARVEST = 6   # cap directory pages we crawl for outbound links
SERPER_TIMEOUT_S = 15            # discovery requests
NATIVE_FETCH_TIMEOUT_S = 12      # Tier-1 free request
ZENROWS_TIMEOUT_S = 45           # Tier-2 proxied + JS render (slower by design)
HTTP_OK = (200, 201)
HTTP_BLOCKED = (403, 429, 503)   # statuses that should escalate to Tier-2

# Whole-site crawl: from each business homepage, follow same-domain internal
# links and scrape every page (capped so a large site can't run forever).
MAX_PAGES_PER_SITE = 20          # hard cap on pages scraped per business
MAX_CRAWL_DEPTH = 2              # homepage = depth 0; how many link-hops deep

# Over-discovery: the requested count is a target of QUALIFIED leads, not raw
# candidates. Since shops/aggregators/failures get filtered out, pull a larger
# candidate pool and stop once enough real leads are collected.
CANDIDATE_OVERFETCH = 3          # discover up to this many candidates per wanted lead
MAX_CANDIDATE_POOL = 60          # absolute cap on candidates processed (safety)

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

# Contact extraction.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{7,}\d")
# Substrings that mark a regex 'email' as junk (assets, placeholders, trackers).
JUNK_EMAIL_HINTS = (
    "example.", "yourdomain", "domain.com", "email@", "sentry", "wixpress",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", "@2x", "u003e", "name@",
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
)

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
_TLD = tldextract.TLDExtract(suffix_list_urls=())

# Path fragments and title patterns that mark a result as an article/listicle/
# directory page rather than a single business homepage.
ARTICLE_PATH_HINTS = (
    "/blog", "/news", "/article", "/articles", "/guide", "/guides",
    "/region/", "/browse/", "/explore", "/wiki/", "/category/", "/list",
    "/directory", "/directories",
)
ARTICLE_TITLE_RE = re.compile(
    r"^\s*(\d+\s|the\s+best\b|best\b|top\s+\d+|a\s+guide\b|guide\s+to\b|"
    r"ultimate\s+guide\b)",
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
    "scrape_source_method",  # "NATIVE" or "ZENROWS"
    "page_text",             # cleaned main content (nav/footer/cookie removed)
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
    """True if host is a known directory/social domain (not a business site)."""
    return any(host == d or host.endswith("." + d) for d in AGGREGATOR_DOMAINS)


def _domain_key(url_or_host: str) -> str:
    """Registered domain (e.g. 'help.predictwind.com' -> 'predictwind.com').

    Used to de-duplicate so a company's many subdomains count as one business.
    """
    ext = _TLD.extract_str(url_or_host)
    return ext.registered_domain.lower() or ext.domain.lower()


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


async def discover_targets(
    session: aiohttp.ClientSession, query: str, limit: int
) -> List[Dict[str, Any]]:
    """Discover candidate business websites via paginated Serper web search.

    Google web search (unlike Maps Places) returns the business's own website
    directly and paginates, so we can scale to the requested `limit`. Results
    are de-duplicated by host and filtered against known aggregator/social
    domains. Each target: {title, website, snippet}.
    """
    if not SERPER_API_KEY:
        logger.error("Serper key missing (set `serper` or `SERPER_API_KEY` in .env).")
        return []

    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    logger.info("Discovery query: %r (target %d businesses)", query, limit)

    targets: List[Dict[str, Any]] = []
    directories: List[str] = []  # listicle/directory pages to harvest if short
    seen_hosts: Set[str] = set()
    page = 1
    while len(targets) < limit and page <= MAX_SEARCH_PAGES:
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
            break

        for item in organic:
            link = item.get("link", "")
            title = item.get("title", "") or ""
            parsed = urlparse(link)
            host = parsed.netloc.lower().removeprefix("www.")
            domain = _domain_key(link)
            if not host or domain in seen_hosts or _is_aggregator(host):
                continue
            # Directories/listicles are a SOURCE of leads, not the lead itself —
            # stash them to harvest outbound links from if we come up short.
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

    # Top up from directories: follow their outbound links to real businesses.
    for directory_url in directories[:MAX_DIRECTORIES_TO_HARVEST]:
        if len(targets) >= limit:
            break
        harvested = await harvest_directory(
            session, directory_url, seen_hosts, needed=limit - len(targets)
        )
        targets.extend(harvested)

    logger.info(
        "Discovery collected %d business sites (%d direct, %d harvested from %d directories).",
        len(targets), direct_count, len(targets) - direct_count, len(directories),
    )
    return targets


# === Step 3: Two-tier scavenger scrape =====================================
async def execute_scavenger_scrape(
    session: aiohttp.ClientSession, target_url: str
) -> Dict[str, Any]:
    """Tier-1 native fetch; on block/error fail over to Tier-2 ZenRows."""
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

    # --- Tier 2: ZenRows residential proxy + JS render ---
    if not ZENROWS_API_KEY:
        logger.error("ZenRows key missing; cannot escalate %s", target_url)
        return {"html": "", "method": "FAILED"}

    logger.info("Tier-2 ZenRows escalation: %s", target_url)
    params = {
        "apikey": ZENROWS_API_KEY,
        "url": target_url,
        "js_render": "true",
        "premium_proxy": "true",
    }
    try:
        async with session.get(
            ZENROWS_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=ZENROWS_TIMEOUT_S),
        ) as resp:
            if resp.status == 200:
                body = await _read_html(resp)
                logger.info("Tier-2 success: %s", target_url)
                return {"html": body, "method": "ZENROWS"}
            logger.error("Tier-2 ZenRows failed (status=%s): %s", resp.status, target_url)
            return {"html": "", "method": "FAILED"}
    except Exception as exc:  # noqa: BLE001
        logger.error("Tier-2 ZenRows error for %s: %s", target_url, exc)
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
    base = urlparse(base_url)
    links: List[str] = []
    seen: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        # Resolve relative links ("/about", "services/") against the page URL.
        if href.startswith("//"):
            href = f"{base.scheme}:{href}"
        elif href.startswith("/"):
            href = f"{base.scheme}://{base.netloc}{href}"
        elif not href.lower().startswith("http"):
            href = f"{base.scheme}://{base.netloc}/{href.lstrip('./')}"
        parsed = urlparse(href)
        if _domain_key(href) != root_domain:       # same business only
            continue
        if parsed.path.lower().endswith(SKIP_EXTENSIONS):  # assets, not pages
            continue
        if _is_utility_link(parsed.netloc.lower().removeprefix("www."),
                            parsed.path, a.get_text(strip=True)):
            continue
        norm = _normalize_page_url(href)
        if norm in seen:
            continue
        seen.add(norm)
        links.append(href)
    return links


async def crawl_site(
    session: aiohttp.ClientSession,
    root_url: str,
    root_html: str,
    root_method: str,
) -> List[Dict[str, str]]:
    """Breadth-first crawl of one business site, starting from an already-fetched
    homepage. Internal pages are fetched natively (cheap); the expensive two-tier
    scrape was already spent on the homepage. Returns one entry per page:
    {page_url, html, method}, capped at MAX_PAGES_PER_SITE / MAX_CRAWL_DEPTH.
    """
    root_domain = _domain_key(root_url)
    pages: List[Dict[str, str]] = [
        {"page_url": root_url, "html": root_html, "method": root_method}
    ]
    visited: Set[str] = {_normalize_page_url(root_url)}

    # Seed the queue with the homepage's internal links at depth 1.
    queue: List[tuple[str, int]] = [
        (link, 1) for link in _extract_internal_links(root_html, root_url, root_domain)
    ]
    head = 0
    while head < len(queue) and len(pages) < MAX_PAGES_PER_SITE:
        page_url, depth = queue[head]
        head += 1
        norm = _normalize_page_url(page_url)
        if norm in visited or depth > MAX_CRAWL_DEPTH:
            continue
        visited.add(norm)

        html = await _native_get(session, page_url)
        if not html:
            continue
        pages.append({"page_url": page_url, "html": html, "method": "NATIVE"})

        if depth < MAX_CRAWL_DEPTH:
            for link in _extract_internal_links(html, page_url, root_domain):
                if _normalize_page_url(link) not in visited:
                    queue.append((link, depth + 1))

    logger.info("Crawled %d page(s) of %s", len(pages), root_domain)
    return pages


# === Step 4: Local CSV storage =============================================
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

    page_text = " ".join(soup.get_text(separator=" ").split())
    return {
        "page_title": title,
        "meta_description": meta_desc,
        "page_text": page_text,
    }


def extract_clean_text(html: str) -> str:
    """Back-compat shim: just the cleaned body text (see extract_page_fields)."""
    return extract_page_fields(html)["page_text"]


def _valid_email(candidate: str) -> bool:
    """Reject asset filenames, placeholders and tracker pseudo-emails."""
    low = candidate.lower()
    return "@" in low and not any(h in low for h in JUNK_EMAIL_HINTS)


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


def _clean_phone(candidate: str) -> Optional[str]:
    """Normalize-validate a phone candidate; reject dates and non-phone noise."""
    candidate = candidate.strip()
    # Reject obvious dates like 09.27.2024 or 27-09-2024.
    if re.fullmatch(r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}", candidate):
        return None
    digits = re.sub(r"\D", "", candidate)
    if not 9 <= len(digits) <= 15:  # real phone numbers fall in this range
        return None
    return candidate


def extract_contacts(html: str, text: str) -> Dict[str, Optional[str]]:
    """Best-effort email + phone extraction from a scraped page.

    Email: Cloudflare-decoded address first, then mailto:, then regex.
    Phone: a "+"-prefixed number shown in the text wins (that's the salon's real
    line), then a tel: link, then any other number — so booking-widget toll-free
    tel: links don't shadow the real local number. Returns {email, phone}.
    """
    email: Optional[str] = None
    phone: Optional[str] = None
    soup = BeautifulSoup(html or "", "lxml")

    # --- Email: Cloudflare-protected -> mailto: -> regex over visible text. ---
    for el in soup.find_all(attrs={"data-cfemail": True}):
        real = _decode_cf_email(el.get("data-cfemail", ""))
        if real and _valid_email(real):
            email = real
            break
    if email is None:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                cand = href[len("mailto:"):].split("?")[0].strip()
                if _valid_email(cand):
                    email = cand
                    break
    if email is None:
        for match in EMAIL_RE.findall(text or ""):
            if _valid_email(match):
                email = match
                break

    # --- Phone: prefer a "+"-format number in the text, then tel:, then any. ---
    text_phones = [
        c for c in (_clean_phone(m) for m in PHONE_RE.findall(text or "")) if c
    ]
    phone = next((p for p in text_phones if p.lstrip().startswith("+")), None)
    if phone is None:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("tel:"):
                cand = _clean_phone(href[len("tel:"):])
                if cand:
                    phone = cand
                    break
    if phone is None and text_phones:
        phone = text_phones[0]

    return {"email": email, "phone": phone}


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
        str(lead.get("page_text", "")),
    ]


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


def reorganize_csv() -> Dict[str, int]:
    """Tidy the store so it's easy to read: drop duplicate pages, group every
    business's pages into one contiguous block (homepage first), and sort the
    businesses alphabetically. Backs the file up first. Returns row counts.
    """
    if not os.path.exists(OUTPUT_CSV_FILE):
        return {"before": 0, "after": 0, "businesses": 0}

    with open(OUTPUT_CSV_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    before = len(rows)

    # Dedupe by canonical (site, page); keep the last (most recent) copy.
    deduped: Dict[tuple, Dict[str, str]] = {}
    for row in rows:
        site = _strip_tracking(row.get("website_url", "") or "")
        page = _strip_tracking(row.get("page_url", "") or site)
        deduped[(site, page)] = row

    # Group pages under their business.
    groups: Dict[str, List[Dict[str, str]]] = {}
    for (site, _page), row in deduped.items():
        groups.setdefault(site, []).append(row)

    def _page_sort_key(row: Dict[str, str]) -> tuple:
        # Homepage (empty/root path) sorts first, then alphabetical by URL.
        page = _strip_tracking(row.get("page_url", "") or "")
        path = urlparse(page).path.rstrip("/")
        return (path != "", path, page)

    ordered_sites = sorted(
        groups, key=lambda s: (groups[s][0].get("company_name") or "").lower()
    )

    after = 0
    with open(OUTPUT_CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for site in ordered_sites:
            for row in sorted(groups[site], key=_page_sort_key):
                out = {k: row.get(k, "") for k in CSV_HEADERS}
                # Normalize stored URLs too, so the file matches the cache keys.
                out["website_url"] = site
                out["page_url"] = _strip_tracking(out.get("page_url", "") or site)
                writer.writerow(out)
                after += 1

    logger.info(
        "Reorganized store: %d -> %d rows across %d businesses (deduped %d).",
        before, after, len(groups), before - after,
    )
    return {"before": before, "after": after, "businesses": len(groups)}


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
    if url in cache:
        rows = cache[url]
        logger.info("Cache HIT for %s (%d page(s)) — qualifying from cache.", url, len(rows))
        combined = " ".join(r.get("page_text", "") for r in rows)
        email = next((r.get("email") for r in rows
                      if r.get("email") not in (None, "", "N/A")), "N/A")
        phone = next((r.get("phone_number") for r in rows
                      if r.get("phone_number") not in (None, "", "N/A")), "N/A")
        qualification = qualify_lead(combined, exclude_keywords, include_keywords)
        classification = None
        if qualification.get("qualified"):
            classification = await classify_relevance(industry, geo, name, combined)
        return {"company_name": name, "website_url": url, "status": "cache_hit",
                "method": "CACHE", "pages": len(rows), "email": email,
                "phone": phone, "qualification": qualification,
                "classification": classification}

    # Scrape the homepage with the full two-tier scrape, then crawl the rest of
    # the site from it (internal pages fetched natively).
    async with semaphore:
        root = await execute_scavenger_scrape(session, url)
        if root["method"] == "FAILED" or not root["html"]:
            logger.warning("No content resolved for %s", url)
            return {"company_name": name, "website_url": url, "status": "failed",
                    "method": "FAILED"}
        pages = await crawl_site(session, url, root["html"], root["method"])

    # Build one row per page, then write them as a single contiguous block.
    combined_parts: List[str] = []
    page_records: List[Dict[str, Any]] = []
    email = phone = None
    for page in pages:
        fields = extract_page_fields(page["html"])
        page_text = fields["page_text"]
        if not page_text.strip():
            continue
        contacts = extract_contacts(page["html"], page_text)
        email = email or contacts["email"]
        phone = phone or contacts["phone"]
        page_records.append(
            {
                "company_name": name,
                "website_url": url,
                "page_url": page["page_url"],
                "page_title": fields["page_title"],
                "meta_description": fields["meta_description"],
                "email": contacts["email"] or "N/A",
                "phone_number": contacts["phone"] or "N/A",
                "physical_address": "N/A",  # web-search discovery has no address
                "scrape_source_method": page["method"],
                "page_text": page_text,
            }
        )
        combined_parts.append(page_text)

    if not page_records:
        logger.warning("No readable content on any page of %s", url)
        return {"company_name": name, "website_url": url, "status": "failed",
                "method": root["method"]}

    # Qualify + classify BEFORE persisting, so only real salon leads are stored.
    # Product shops, aggregators, unrelated sites and keyword-excluded businesses
    # are crawled but NOT written to the database.
    combined = " ".join(combined_parts)
    qualification = qualify_lead(combined, exclude_keywords, include_keywords)
    classification = None
    if qualification.get("qualified"):
        classification = await classify_relevance(industry, geo, name, combined)
    is_lead = bool(
        qualification.get("qualified")
        and (classification or {}).get("category", "match") == "match"
    )

    if is_lead:
        append_records_to_csv(page_records)  # contiguous block, homepage first
        cache[url] = [{"website_url": url, "page_text": combined,
                       "email": email or "N/A", "phone_number": phone or "N/A"}]
        logger.info("Saved %d page(s) for %s", len(page_records), name)
    else:
        reason = (classification or {}).get("category") or qualification.get("reason")
        logger.info("Not stored (%s): %s", reason, name)

    return {"company_name": name, "website_url": url, "status": "scraped",
            "method": root["method"], "pages": len(page_records),
            "stored": is_lead, "text_len": len(combined),
            "email": email, "phone": phone, "qualification": qualification,
            "classification": classification}


async def run_pipeline(
    user_query: str, limit: Optional[int] = None, concurrency: int = 5
) -> Dict[str, Any]:
    """Run the full Phase 1 pipeline end to end and return a result summary.

    `limit` is optional and overrides the plan; when omitted, the count is taken
    from the planner's `result_limit` (which it reads from the query), defaulting
    to DEFAULT_RESULT_LIMIT.
    """
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

    semaphore = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession() as session:
        query = plan.get("search_query") or (
            f"{plan.get('broad_industry', '')} in {plan.get('geo_location', '')}".strip()
        )
        # Over-discover: pull a larger candidate pool because filtering will
        # drop shops/aggregators/failures before we reach the wanted count.
        pool_size = min(effective_limit * CANDIDATE_OVERFETCH, MAX_CANDIDATE_POOL)
        targets = await discover_targets(session, query, pool_size)
        summary["discovered"] = len(targets)
        if not targets:
            logger.critical("No candidate businesses found. Halting pipeline.")
            return summary

        exclude_keywords = plan.get("exclude_keywords", []) or []
        include_keywords = plan.get("include_keywords", []) or []
        industry = plan.get("broad_industry", "") or ""
        geo = plan.get("geo_location", "") or ""

        def _is_lead(r: Dict[str, Any]) -> bool:
            return bool(
                r.get("qualification", {}).get("qualified")
                and (r.get("classification") or {}).get("category", "match") == "match"
            )

        # Process the pool in concurrency-sized batches, stopping as soon as we
        # have `effective_limit` real leads (so we don't scrape more than needed).
        logger.info(
            "Targeting %d qualified leads from a pool of %d candidates.",
            effective_limit, len(targets),
        )
        results: List[Dict[str, Any]] = []
        leads_found = 0
        idx = 0
        while idx < len(targets) and leads_found < effective_limit:
            batch = targets[idx : idx + concurrency]
            idx += len(batch)
            batch_results = await asyncio.gather(*[
                process_single_lead(
                    session, semaphore, target, cache, exclude_keywords,
                    include_keywords, industry, geo,
                )
                for target in batch
            ])
            results.extend(batch_results)
            leads_found += sum(1 for r in batch_results if _is_lead(r))
            logger.info(
                "Progress: %d/%d qualified leads after %d candidates processed.",
                leads_found, effective_limit, len(results),
            )

        if leads_found < effective_limit:
            logger.warning(
                "Candidate pool exhausted: found %d of %d wanted leads "
                "(only %d real businesses existed in this niche).",
                leads_found, effective_limit, leads_found,
            )

    summary["processed"] = len(results)
    summary["results"] = results
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary["counts"] = counts

    # Step 5 + 5.5 — the deliverable: leads that passed keyword qualification
    # AND were classified as a real single business ("match"), not a product
    # shop / aggregator / unrelated site. Truncate to the requested count (the
    # final batch may have pushed us a lead or two past the target).
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
    ][:effective_limit]
    summary["qualified"] = qualified
    summary["qualified_count"] = len(qualified)
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
    return summary


def print_summary(summary: Dict[str, Any]) -> None:
    """Render the pipeline's output return in a readable block."""
    print("\n" + "=" * 64)
    print("PHASE 1 OUTPUT RETURN")
    print("=" * 64)
    print(f"Query      : {summary['query']}")
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
    print(f"QUALIFIED LEADS ({summary.get('qualified_count', 0)}):")
    for lead in summary.get("qualified", []):
        print(f"  • {lead['company_name']}")
        print(f"      site : {lead['website_url']}")
        print(f"      email: {lead['email']}  |  phone: {lead['phone']}")
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
        "--reorganize", action="store_true",
        help="Tidy the CSV store (dedupe + group by business + sort) and exit.",
    )
    args = parser.parse_args()

    if args.reorganize:
        stats = reorganize_csv()
        print(
            f"Store reorganized: {stats['before']} -> {stats['after']} rows "
            f"across {stats['businesses']} businesses "
            f"(removed {stats['before'] - stats['after']} duplicates)."
        )
        return

    summary = asyncio.run(
        run_pipeline(args.query, concurrency=args.concurrency)
    )
    print_summary(summary)


if __name__ == "__main__":
    main()
