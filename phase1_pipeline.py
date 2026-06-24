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
from urllib.parse import urlparse

import aiohttp
import tldextract
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Reuse the already-tested Step-1 planner instead of duplicating LLM logic.
from LLM_planner import plan_query

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
    "website_url",
    "email",
    "phone_number",
    "physical_address",
    "scrape_source_method",  # "NATIVE" or "ZENROWS"
    "page_text",             # clean visible text (markup/scripts stripped)
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


async def _native_get(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Cheap, best-effort native GET (no ZenRows). Returns HTML or None."""
    try:
        async with session.get(
            url, headers=BROWSER_HEADERS,
            timeout=aiohttp.ClientTimeout(total=NATIVE_FETCH_TIMEOUT_S),
        ) as resp:
            if resp.status in HTTP_OK:
                return await resp.text()
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
            body = await resp.text()
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
                body = await resp.text()
                logger.info("Tier-2 success: %s", target_url)
                return {"html": body, "method": "ZENROWS"}
            logger.error("Tier-2 ZenRows failed (status=%s): %s", resp.status, target_url)
            return {"html": "", "method": "FAILED"}
    except Exception as exc:  # noqa: BLE001
        logger.error("Tier-2 ZenRows error for %s: %s", target_url, exc)
        return {"html": "", "method": "FAILED"}


# === Step 4: Local CSV storage =============================================
def extract_clean_text(html: str) -> str:
    """Strip markup, scripts and styling; return collapsed visible text.

    Keeps the store readable and small, and yields exactly the content Step 5
    needs for exclude/include keyword matching.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


def _valid_email(candidate: str) -> bool:
    """Reject asset filenames, placeholders and tracker pseudo-emails."""
    low = candidate.lower()
    return "@" in low and not any(h in low for h in JUNK_EMAIL_HINTS)


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

    Prefers explicit mailto:/tel: links (most reliable), then falls back to
    regex over the visible text. Returns {"email": ..., "phone": ...}.
    """
    email: Optional[str] = None
    phone: Optional[str] = None

    soup = BeautifulSoup(html or "", "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:") and email is None:
            cand = href[len("mailto:"):].split("?")[0].strip()
            if _valid_email(cand):
                email = cand
        elif low.startswith("tel:") and phone is None:
            cand = _clean_phone(href[len("tel:"):])
            if cand:
                phone = cand
        if email and phone:
            break

    if email is None:
        for match in EMAIL_RE.findall(text or ""):
            if _valid_email(match):
                email = match
                break

    if phone is None:
        for match in PHONE_RE.findall(text or ""):
            cleaned = _clean_phone(match)
            if cleaned:
                phone = cleaned
                break

    return {"email": email, "phone": phone}


def initialize_csv_storage_layer() -> None:
    """Create the CSV with headers if it does not exist yet."""
    if not os.path.exists(OUTPUT_CSV_FILE):
        logger.info("Initializing CSV archive: %s", OUTPUT_CSV_FILE)
        with open(OUTPUT_CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADERS)


def append_lead_record_to_csv(lead: Dict[str, Any]) -> None:
    """Append one lead row. Lock-guarded; stores clean page text only."""
    page_text = str(lead.get("page_text", ""))
    with _csv_lock:
        with open(OUTPUT_CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
            # csv.writer already quotes/escapes embedded commas and quotes.
            csv.writer(f).writerow(
                [
                    lead.get("company_name", "N/A"),
                    lead.get("website_url", "N/A"),
                    lead.get("email", "N/A"),
                    lead.get("phone_number", "N/A"),
                    lead.get("physical_address", "N/A"),
                    lead.get("scrape_source_method", "FAILED"),
                    page_text,
                ]
            )
    logger.info("Saved lead: %s", lead.get("company_name"))


def load_cache() -> Dict[str, Dict[str, str]]:
    """Return previously-analyzed leads keyed by website URL (CSV dev cache).

    Stands in for the MongoDB Atlas cache lookup. Holds the full row (including
    page_text and contacts) so cache hits can still be qualified in Step 5
    without re-scraping.
    """
    if not os.path.exists(OUTPUT_CSV_FILE):
        return {}
    cached: Dict[str, Dict[str, str]] = {}
    with open(OUTPUT_CSV_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row.get("website_url")
            if url and url != "N/A":
                cached[url] = row
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


# === Orchestration =========================================================
async def process_single_lead(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    target: Dict[str, Any],
    cache: Dict[str, Dict[str, str]],
    exclude_keywords: List[str],
    include_keywords: List[str],
) -> Dict[str, Any]:
    """Cache-check, scrape, extract contacts, persist, then qualify (Step 5)."""
    name = target.get("title")
    url = target.get("website")

    if not url:
        return {"company_name": name, "website_url": None, "status": "no_website"}

    # Step 2.1 — cache check (CSV dev cache substitutes for MongoDB Atlas).
    if url in cache:
        logger.info("Cache HIT for %s — qualifying from cache.", url)
        row = cache[url]
        qualification = qualify_lead(
            row.get("page_text", ""), exclude_keywords, include_keywords
        )
        return {"company_name": name, "website_url": url, "status": "cache_hit",
                "method": "CACHE", "email": row.get("email"),
                "phone": row.get("phone_number"), "qualification": qualification}

    async with semaphore:
        result = await execute_scavenger_scrape(session, url)

    if result["method"] != "FAILED" and result["html"]:
        page_text = extract_clean_text(result["html"])
        contacts = extract_contacts(result["html"], page_text)
        append_lead_record_to_csv(
            {
                "company_name": name,
                "website_url": url,
                "email": contacts["email"] or "N/A",
                "phone_number": contacts["phone"] or "N/A",
                "physical_address": "N/A",  # web-search discovery has no address
                "scrape_source_method": result["method"],
                "page_text": page_text,
            }
        )
        cache[url] = {"website_url": url, "email": contacts["email"] or "N/A",
                      "phone_number": contacts["phone"] or "N/A", "page_text": page_text}
        qualification = qualify_lead(page_text, exclude_keywords, include_keywords)
        return {"company_name": name, "website_url": url, "status": "scraped",
                "method": result["method"], "text_len": len(page_text),
                "email": contacts["email"], "phone": contacts["phone"],
                "qualification": qualification}

    logger.warning("No content resolved for %s", url)
    return {"company_name": name, "website_url": url, "status": "failed",
            "method": "FAILED"}


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
        targets = await discover_targets(session, query, effective_limit)
        summary["discovered"] = len(targets)
        if not targets:
            logger.critical("No candidate businesses found. Halting pipeline.")
            return summary

        exclude_keywords = plan.get("exclude_keywords", []) or []
        include_keywords = plan.get("include_keywords", []) or []

        logger.info("Processing %d discovered businesses.", len(targets))
        tasks = [
            process_single_lead(
                session, semaphore, target, cache, exclude_keywords, include_keywords
            )
            for target in targets
        ]
        results = await asyncio.gather(*tasks)

    summary["results"] = results
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary["counts"] = counts

    # Step 5 — the deliverable: leads that passed qualification.
    qualified = [
        {
            "company_name": r.get("company_name"),
            "website_url": r.get("website_url"),
            "email": r.get("email") or "N/A",
            "phone": r.get("phone") or "N/A",
            "matched_include": r["qualification"].get("matched_include", []),
        }
        for r in results
        if r.get("qualification", {}).get("qualified")
    ]
    summary["qualified"] = qualified
    summary["qualified_count"] = len(qualified)

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
    print(f"Limit      : {summary.get('limit')}  |  Discovered : {summary['discovered']}")
    print(f"Counts     : {summary['counts']}")
    print("-" * 64)
    for r in summary["results"]:
        method = r.get("method", "-")
        q = r.get("qualification") or {}
        verdict = ""
        if q:
            if q.get("qualified"):
                verdict = "  [QUALIFIED]"
            else:
                reason = q.get("reason")
                hit = q.get("matched_exclude")
                verdict = f"  [x {reason}]" + (f" ({hit[0]})" if hit else "")
        print(f"  [{r['status']:<10}] {method:<7} {r.get('company_name')}{verdict}")
    print("-" * 64)
    print(f"QUALIFIED LEADS ({summary.get('qualified_count', 0)}):")
    for lead in summary.get("qualified", []):
        print(f"  • {lead['company_name']}")
        print(f"      site : {lead['website_url']}")
        print(f"      email: {lead['email']}  |  phone: {lead['phone']}")
    print("=" * 64)
    print(f"Raw store  : {OUTPUT_CSV_FILE}\n")


def main() -> None:
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
    args = parser.parse_args()

    summary = asyncio.run(
        run_pipeline(args.query, concurrency=args.concurrency)
    )
    print_summary(summary)


if __name__ == "__main__":
    main()
