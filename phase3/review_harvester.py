"""Category-aware public-review discovery through Serper and ZenRows.

Each committed business is checked against the five review platforms mapped to
its category. Results are cached independently under
storage/<domain>/reviews/<platform>.json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from phase3 import store
from phase3.business_category import categorize_domain
from phase3.config import (APIFY_API_TOKEN, APIFY_MAX_REVIEWS_ATTEMPT, APIFY_REDDIT_RUN_SYNC_URL,
    APIFY_REDDIT_TIMEOUT_S, APIFY_RUN_SYNC_URL, APIFY_TIMEOUT_S, DEFAULT_CONCURRENCY,
    MAX_REVIEWS_PER_PLATFORM, REVIEW_CACHE_DAYS, REVIEW_MAX_PAGES, SERPER_API_KEY,
    SERPER_SEARCH_URL, SERPER_TIMEOUT_S, ZENROWS_API_KEY)

logger = logging.getLogger("Phase3ReviewHarvester")

# Stable storage key + official host(s). Every category has the five sources
# supplied for it; lookup is restricted to these hosts.
CATEGORY_PLATFORMS: Dict[str, Sequence[tuple[str, Sequence[str]]]] = {
    "Health & Medical": (("google_reviews", ("google.com/maps",)), ("healthgrades", ("healthgrades.com",)), ("ratemds", ("ratemds.com",)), ("webmd_physician_directory", ("doctor.webmd.com",)), ("zocdoc", ("zocdoc.com",))),
    "Beauty & Aesthetics": (("realself", ("realself.com",)), ("yelp", ("yelp.com",)), ("google_reviews", ("google.com/maps",)), ("fresha", ("fresha.com",)), ("treatwell", ("treatwell.com",))),
    "Automotive": (("dealerrater", ("dealerrater.com",)), ("cars_com", ("cars.com",)), ("edmunds", ("edmunds.com",)), ("kelley_blue_book", ("kbb.com",)), ("google_reviews", ("google.com/maps",))),
    "Marinas & Boating": (("marinas_com", ("marinas.com",)), ("dockwa", ("dockwa.com",)), ("activecaptain", ("activecaptain.garmin.com", "garmin.com")), ("navily", ("navily.com",)), ("google_reviews", ("google.com/maps",))),
    "Tourism & Travel": (("tripadvisor", ("tripadvisor.com",)), ("google_reviews", ("google.com/maps",)), ("booking_com", ("booking.com",)), ("expedia", ("expedia.com",)), ("getyourguide", ("getyourguide.com",))),
    "Hospitality & Hotels": (("booking_com", ("booking.com",)), ("tripadvisor", ("tripadvisor.com",)), ("expedia", ("expedia.com",)), ("hotels_com", ("hotels.com",)), ("google_reviews", ("google.com/maps",))),
    "Restaurants & Dining": (("yelp", ("yelp.com",)), ("tripadvisor", ("tripadvisor.com",)), ("google_reviews", ("google.com/maps",)), ("opentable", ("opentable.com",)), ("zomato", ("zomato.com",))),
    "Cafes & Bakeries": (("google_reviews", ("google.com/maps",)), ("yelp", ("yelp.com",)), ("tripadvisor", ("tripadvisor.com",)), ("facebook_reviews", ("facebook.com",)), ("zomato", ("zomato.com",))),
    "Fitness & Gyms": (("classpass", ("classpass.com",)), ("mindbody", ("mindbodyonline.com", "mindbody.io")), ("yelp", ("yelp.com",)), ("google_reviews", ("google.com/maps",)), ("gymcatch", ("gymcatch.com",))),
    "Education & Training": (("google_reviews", ("google.com/maps",)), ("trustpilot", ("trustpilot.com",)), ("course_report", ("coursereport.com",)), ("switchup", ("switchup.org",)), ("yelp", ("yelp.com",))),
    "Legal Services": (("avvo", ("avvo.com",)), ("martindale_hubbell", ("martindale.com",)), ("lawyers_com", ("lawyers.com",)), ("justia", ("justia.com",)), ("google_reviews", ("google.com/maps",))),
    "Financial Services": (("trustpilot", ("trustpilot.com",)), ("wallethub", ("wallethub.com",)), ("nerdwallet", ("nerdwallet.com",)), ("google_reviews", ("google.com/maps",)), ("bbb", ("bbb.org",))),
    "Real Estate": (("zillow", ("zillow.com",)), ("realtor_com", ("realtor.com",)), ("homes_com", ("homes.com",)), ("ratemyagent", ("ratemyagent.com",)), ("google_reviews", ("google.com/maps",))),
    "Construction & Contractors": (("angi", ("angi.com",)), ("homeadvisor", ("homeadvisor.com",)), ("houzz", ("houzz.com",)), ("thumbtack", ("thumbtack.com",)), ("google_reviews", ("google.com/maps",))),
    "Home Services": (("angi", ("angi.com",)), ("homeadvisor", ("homeadvisor.com",)), ("thumbtack", ("thumbtack.com",)), ("porch", ("porch.com",)), ("yelp", ("yelp.com",))),
    "Retail & Shopping": (("trustpilot", ("trustpilot.com",)), ("google_reviews", ("google.com/maps",)), ("sitejabber", ("sitejabber.com",)), ("reviews_io", ("reviews.io",)), ("resellerratings", ("resellerratings.com",))),
    "IT & Software Services": (("g2", ("g2.com",)), ("capterra", ("capterra.com",)), ("gartner_peer_insights", ("gartner.com/reviews",)), ("trustradius", ("trustradius.com",)), ("software_advice", ("softwareadvice.com",))),
    "Marketing & Advertising": (("clutch", ("clutch.co",)), ("designrush", ("designrush.com",)), ("upcity", ("upcity.com",)), ("goodfirms", ("goodfirms.co",)), ("sortlist", ("sortlist.com",))),
    "Manufacturing & Industrial": (("thomasnet", ("thomasnet.com",)), ("industrynet", ("industrynet.com",)), ("mfg_com", ("mfg.com",)), ("google_reviews", ("google.com/maps",)), ("trustpilot", ("trustpilot.com",))),
    "Logistics & Transportation": (("freightwaves_ratings", ("ratings.freightwaves.com",)), ("carriersource", ("carriersource.io",)), ("transport_reviews", ("transportreviews.com",)), ("google_reviews", ("google.com/maps",)), ("trustpilot", ("trustpilot.com",))),
    "Event Planning": (("weddingwire", ("weddingwire.com",)), ("the_knot", ("theknot.com",)), ("eventective", ("eventective.com",)), ("bark", ("bark.com",)), ("google_reviews", ("google.com/maps",))),
    "Pet Services": (("rover", ("rover.com",)), ("wag", ("wagwalking.com",)), ("bringfido", ("bringfido.com",)), ("yelp", ("yelp.com",)), ("google_reviews", ("google.com/maps",))),
    "Professional Services & Consulting": (("clutch", ("clutch.co",)), ("goodfirms", ("goodfirms.co",)), ("trustpilot", ("trustpilot.com",)), ("google_reviews", ("google.com/maps",)), ("upcity", ("upcity.com",))),
    "Entertainment & Recreation": (("tripadvisor", ("tripadvisor.com",)), ("google_reviews", ("google.com/maps",)), ("yelp", ("yelp.com",)), ("facebook_reviews", ("facebook.com",)), ("fever", ("feverup.com",))),
}
_WORDS = re.compile(r"[a-z0-9]{3,}")
_IGNORE = {"the", "and", "for", "with", "inc", "llc", "ltd", "company", "services", "service"}


def platforms_for_category(category: str) -> Sequence[tuple[str, Sequence[str]]]:
    return CATEGORY_PLATFORMS.get(category, ())


def _tokens(text: str) -> set[str]:
    return {word for word in _WORDS.findall((text or "").lower()) if word not in _IGNORE}


def _parse_site_spec(spec: str) -> tuple[str, str]:
    """Split a CATEGORY_PLATFORMS entry into (host, path_prefix).

    "healthgrades.com" -> ("healthgrades.com", "") — host-only, any path on
    that host counts. "google.com/maps" -> ("google.com", "/maps") — the host
    must ALSO have a path starting with /maps, which is what rejects
    play.google.com/store/... or docs.google.com/... (both real google.com
    subdomains, neither a Maps listing) — the exact bug that let a Play Store
    game page get harvested as a dental clinic's "Google review."
    """
    host, _, path = spec.partition("/")
    return host.lower(), (f"/{path}".rstrip("/") if path else "")


def _host_matches(url: str, hosts: Iterable[str]) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    path = parsed.path or "/"
    for spec in hosts:
        expected_host, path_prefix = _parse_site_spec(spec)
        if host == expected_host or host.endswith("." + expected_host):
            if not path_prefix or path.startswith(path_prefix):
                return True
    return False


async def _discover(session: aiohttp.ClientSession, name: str, hosts: Sequence[str], geo: str) -> Optional[str]:
    if not SERPER_API_KEY or not name:
        return None
    # Google's own `site:` operator only reliably filters by domain, not by
    # path — so the SEARCH stays domain-scoped (site:google.com, not
    # site:google.com/maps, which Google may just ignore past the domain).
    # The path restriction (google.com/maps) is enforced afterward, on the
    # actual returned URLs, by _host_matches — that check is deterministic
    # and ours to trust; relying on Google to honor a path-scoped site:
    # filter would not be.
    search_host, _ = _parse_site_spec(hosts[0])
    query = f'site:{search_host} "{name}" {geo}'
    logger.info("Searching %s for %r: %s", search_host, name, query)
    try:
        async with session.post(SERPER_SEARCH_URL, json={"q": query, "num": 5}, headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=SERPER_TIMEOUT_S)) as response:
            results = (await response.json()).get("organic", []) if response.status == 200 else []
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("Search failed for %s (%r): %s", search_host, name, exc)
        return None
    wanted = _tokens(name)
    for result in results:
        link = result.get("link", "")
        evidence = f"{result.get('title', '')} {result.get('snippet', '')}"
        if _host_matches(link, hosts) and (not wanted or wanted & _tokens(evidence)):
            logger.info("Confident match on %s: %s", search_host, link)
            return link
    logger.info("No confident match on %s for %r (%d result(s) checked)", search_host, name, len(results))
    return None


_NATIVE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_NATIVE_TIMEOUT_S = 10
_ANTI_BOT_SIGNATURES = (
    "just a moment...", "checking your browser", "cf-browser-verification",
    "attention required", "captcha", "ddos protection",
)


async def _native_fetch(session: aiohttp.ClientSession, url: str) -> str:
    """Cheap direct GET — the same Tier-1-first idea as phase1_pipeline's
    scraper (try a plain request before spending a premium-scraper credit).
    Returns "" on any non-200, anti-bot page, or network error so the caller
    falls back to the premium tier."""
    logger.info("Native fetch: %s", url)
    try:
        async with session.get(
            url, headers=_NATIVE_HEADERS,
            timeout=aiohttp.ClientTimeout(total=_NATIVE_TIMEOUT_S),
        ) as response:
            if response.status != 200:
                logger.info("Native fetch blocked (status=%s): %s", response.status, url)
                return ""
            html = await response.text(errors="replace")
            if any(sig in html.lower() for sig in _ANTI_BOT_SIGNATURES):
                logger.info("Native fetch hit an anti-bot page: %s", url)
                return ""
            logger.info("Native fetch success: %s", url)
            return html
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.info("Native fetch connection error (%s): %s", exc, url)
        return ""


async def _premium_fetch(session: aiohttp.ClientSession, url: str) -> str:
    """Phase 1's own premium-scraper tier (its `PremiumScraper` class),
    reused directly instead of reimplemented — same retry/backoff on
    transient failures, same User-Agent rotation per retry, same
    cheap-plain-fetch-first-then-JS-render-fallback staging Phase 1 already
    relies on for hard/Cloudflare-protected sites. This is the exact
    mechanism Phase 1 itself falls back to when Tier-1 native fetch is
    blocked (see phase1_pipeline.py's `_tier2_premium_fetch`).

    Lazy import (not at module top) to avoid a circular import: phase1_pipeline
    imports phase3.review_harvester, so review_harvester importing
    phase1_pipeline at module load time would be phase1_pipeline ->
    phase3.review_harvester -> phase1_pipeline (see phase3/config.py's
    docstring for the same rule applied to phase3.google_maps). By the time
    THIS function actually runs, phase1_pipeline has always already finished
    importing (it's the one that called into review_harvester in the first
    place), so the import here is instant, not a fresh load.
    """
    from phase1_pipeline import PremiumScraper
    from phase1_pipeline import ZENROWS_API_KEY as PHASE1_PREMIUM_KEY
    if not PHASE1_PREMIUM_KEY:
        logger.info("Premium scraper not configured (no API key) — skipping for %s", url)
        return ""
    logger.info("Premium-scraper escalation (plain): %s", url)
    html, _, _ = await PremiumScraper.fetch(session, url, render=False)
    if html:
        logger.info("Premium-scraper success (plain): %s", url)
        return html
    logger.info("Premium-scraper escalation (JS-render): %s", url)
    html, _, _ = await PremiumScraper.fetch(session, url, render=True)
    if html:
        logger.info("Premium-scraper success (render): %s", url)
    else:
        logger.info("Premium-scraper failed (both tiers): %s", url)
    return html


def _review(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    text = item.get("reviewBody") or item.get("description") or item.get("text") or item.get("review")
    if not isinstance(text, str) or len(text.strip()) < 8:
        return None
    rating = item.get("reviewRating") or item.get("rating")
    author = item.get("author", "")
    return {"text": " ".join(text.split()), "rating": (rating.get("ratingValue") if isinstance(rating, dict) else rating) or "", "author": (author.get("name") if isinstance(author, dict) else author) or "", "date": item.get("datePublished") or item.get("date") or ""}


def _walk_json(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        result = _review(value)
        if result:
            yield result
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def extract_reviews(html: str) -> List[Dict[str, Any]]:
    """Extract only explicitly exposed public review fields; never guess."""
    soup = BeautifulSoup(html or "", "html.parser")
    reviews: List[Dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            reviews.extend(_walk_json(json.loads(script.get_text(strip=True))))
        except (TypeError, ValueError):
            continue
    for node in soup.select('[itemprop="reviewBody"], [data-testid*="review"] p, .review-content, .review-text'):
        text = " ".join(node.get_text(" ", strip=True).split())
        if len(text) >= 8:
            reviews.append({"text": text, "rating": "", "author": "", "date": ""})
    unique, seen = [], set()
    for value in reviews:
        if value["text"].lower() not in seen:
            seen.add(value["text"].lower())
            unique.append(value)
        if len(unique) >= MAX_REVIEWS_PER_PLATFORM:
            break
    return unique


def _page_url(url: str, page: int) -> str:
    """Best-effort page-N URL for a listing (page 1 is the listing itself).
    A generic "?page=N"/"&page=N" guess — right for many platforms, a harmless
    no-op (re-fetches the same page 1 content) for those that ignore it; the
    zero-new-reviews check in enrich_platform stops pagination the moment a
    "page" turns out not to be a real one, so a wrong guess never runs the
    fetch count up to the REVIEW_MAX_PAGES cap for nothing."""
    if page <= 1:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={page}"


async def _fetch_one(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
    """Native-first, premium-scraper-fallback fetch of one page — same tiering
    Phase 1 uses. Returns (html, method); html is "" on failure, method is
    "" / "native" / "premium"."""
    html = await _native_fetch(session, url)
    if html:
        return html, "native"
    html = await _premium_fetch(session, url)
    if html:
        return html, "premium"
    return "", ""


async def _discover_google_maps(session: aiohttp.ClientSession, name: str, address: str,
                                 geo: str, phone: str = "") -> tuple[str, Dict[str, Any]]:
    """Google Maps listings are NOT indexed by organic web search the way
    every other platform here is — a bare site:google.com search only ever
    surfaces unrelated Play Store/Docs/Drive pages, never an actual Maps
    place (confirmed live: it returned a mobile game). The only reliable way
    to find one is Serper's dedicated Places API, address-OR-phone-matched
    exactly like phase3.google_maps.find_place() already does for the
    separate Maps enrichment step — reused here rather than reimplemented,
    so there's one source of truth for "is this really the business we
    searched for." Phone is an equally-trusted, independent verification
    path (see find_place's docstring) — used when no address is on file
    rather than just giving up.

    Returns (listing_url, extra_record_fields). listing_url is "" on no
    confident match. extra_record_fields carries the real rating/review-count
    signal even when review TEXT can't be scraped afterward (Google Maps is a
    JS-rendered SPA that doesn't expose review text in static HTML —
    rating/count are the reliably-gettable part, per the project's own
    "cheap tier first" plan).
    """
    from phase3.google_maps import find_place
    logger.info("Places API lookup: %r %s (address on file: %s, phone on file: %s)",
                name, geo, bool(address), bool(phone))
    place = await find_place(session, name, address, geo, phone)
    if not place.get("matched") or not place.get("cid"):
        logger.info("No verified Google Maps match for %r (%s)",
                    name, place.get("reason", "unknown"))
        # transient_failure means the Serper request itself failed rather
        # than genuinely finding no listing -- propagated so the caller
        # doesn't cache a network blip as a permanent miss (see
        # phase3.google_maps._serper_places's docstring).
        return "", ({"transient_failure": True} if place.get("transient_failure") else {})
    logger.info("Google Maps match (via %s, confidence=%s): %s (rating=%s, %s review(s))",
                place.get("match_basis", "?"), place.get("confidence", "high"),
                place.get("title"), place.get("rating"), place.get("rating_count"))
    return (f"https://www.google.com/maps?cid={place['cid']}",
            {"maps_rating": place.get("rating"), "maps_rating_count": place.get("rating_count"),
             "maps_match_basis": place.get("match_basis", ""),
             "maps_match_confidence": place.get("confidence", "high")})


async def _fetch_apify_google_reviews(
    session: aiohttp.ClientSession, listing_url: str
) -> List[Dict[str, Any]]:
    """Real Google Maps review TEXT — the one thing no direct fetch tier
    (native, premium-scraper plain, premium-scraper JS-render) can ever get,
    confirmed live: Google Maps' review panel is virtualized (never present
    in any page source) and full browsing requires a signed-in Google
    session, which this project will not automate.

    Apify's compass/Google-Maps-Reviews-Scraper actor runs the actual
    scraping on Apify's own infrastructure as a paid (here, free-tier)
    service — this project never touches Google's page directly for review
    text, only Apify's own API, given the SAME address/phone/name-verified
    listing URL _discover_google_maps already produced (no re-verification
    needed here; that already happened).

    `run-sync-get-dataset-items` blocks until the run finishes and returns
    the scraped rows directly — simpler than the async run-then-poll
    pattern, and fine at this review count (MAX_REVIEWS_PER_PLATFORM caps
    it well under Apify's own sync-endpoint time limit).

    Not every raw review has written text (star-only ratings get filtered
    out below), so requesting exactly MAX_REVIEWS_PER_PLATFORM from Apify
    usually yields fewer than that with actual text. To still land on the
    MAX_REVIEWS_PER_PLATFORM target, this escalates: if the first run comes
    up short AND Apify returned as many raw reviews as asked for (a sign
    there are more out there to fetch), it re-runs asking for more, doubling
    each time up to APIFY_MAX_REVIEWS_ATTEMPT. It stops early the moment
    Apify returns fewer raw reviews than requested — that means the
    business's review pool is exhausted and asking again would just re-fetch
    the same reviews at extra cost.
    """
    if not APIFY_API_TOKEN:
        logger.info("Apify not configured (no API token) — skipping Google review text for %s",
                    listing_url)
        return []

    request_count = MAX_REVIEWS_PER_PLATFORM
    reviews: List[Dict[str, Any]] = []
    while True:
        logger.info("Apify Google Reviews run: %s (up to %d reviews)",
                    listing_url, request_count)
        payload = {
            "startUrls": [{"url": listing_url}],
            "maxReviews": request_count,
            "reviewsSort": "newest",
        }
        try:
            async with session.post(
                APIFY_RUN_SYNC_URL, params={"token": APIFY_API_TOKEN}, json=payload,
                timeout=aiohttp.ClientTimeout(total=APIFY_TIMEOUT_S),
            ) as response:
                if response.status not in (200, 201):
                    body = (await response.text())[:300]
                    logger.warning("Apify Google Reviews run failed (status=%s) for %s: %s",
                                   response.status, listing_url, body)
                    break
                items = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Apify Google Reviews run error for %s: %s", listing_url, exc)
            break

        items = items or []
        reviews = []
        for item in items:
            text = (item.get("text") or "").strip()
            if not text:
                continue  # a star-only rating with no written text -- nothing to chunk/RAG
            reviews.append({
                "text": text,
                "rating": item.get("stars") or "",
                "author": item.get("name") or "",
                "date": item.get("publishedAtDate") or item.get("publishAt") or "",
            })
        logger.info("Apify Google Reviews: %d review(s) with text out of %d raw for %s",
                    len(reviews), len(items), listing_url)

        if len(reviews) >= MAX_REVIEWS_PER_PLATFORM:
            reviews = reviews[:MAX_REVIEWS_PER_PLATFORM]
            break
        if len(items) < request_count:
            break  # business has no more reviews to give -- asking again would just repeat this run
        if request_count >= APIFY_MAX_REVIEWS_ATTEMPT:
            break  # hit the spend/time ceiling for this business
        request_count = min(request_count * 2, APIFY_MAX_REVIEWS_ATTEMPT)
        logger.info("Apify Google Reviews: only %d/%d with text -- retrying with maxReviews=%d for %s",
                    len(reviews), MAX_REVIEWS_PER_PLATFORM, request_count, listing_url)

    return reviews


async def _fetch_apify_reddit_mentions(
    session: aiohttp.ClientSession, name: str, geo: str
) -> Optional[List[Dict[str, Any]]]:
    """Reddit discussion mentioning this business, found by a direct keyword
    search (business name + geo) via Apify's trudax/reddit-scraper-lite
    actor -- not a Serper site:reddit.com lookup followed by a fetch, the
    pattern every other platform uses. Reddit itself is the search surface
    here, on Apify's own infrastructure, because reddit.com blocks every
    direct fetch tier this project has (confirmed live: native 403,
    api.reddit.com/old.reddit.com 403, ScrapingBee plain hits a
    bot-verification wall, ScrapingBee JS-render times out).

    Returns both posts and their top-level comments that matched the search,
    normalized to the same {text, rating, author, date} shape every other
    platform's `reviews` list uses (plus `url`/`community`, which no other
    platform has, for traceability) -- `rating` is always empty since Reddit
    has no star rating, but the shared shape is what lets
    rag/ingest_reviews.py embed this identically to every other platform's
    output with zero changes.

    Returns `None` (not `[]`) when the Apify request itself fails (network
    error, timeout, non-200) -- distinct from a genuine zero-result search,
    so the caller doesn't cache a transient network blip as a permanent "no
    mentions found" (observed live: a DNS blip during the request produced
    exactly that false-negative before this distinction existed).
    """
    if not APIFY_API_TOKEN:
        logger.info("Apify not configured (no API token) — skipping Reddit search for %r", name)
        return []
    if not name:
        return []
    query = f"{name} {geo}".strip()
    payload = {
        "searches": [query],
        "searchPosts": True,
        "searchComments": True,
        "sort": "relevance",
        "maxItems": MAX_REVIEWS_PER_PLATFORM,
        "maxPostCount": MAX_REVIEWS_PER_PLATFORM,
        "maxComments": MAX_REVIEWS_PER_PLATFORM,
        "skipUserPosts": True,
        "skipCommunity": True,
    }
    logger.info("Apify Reddit search: %r (up to %d items)", query, MAX_REVIEWS_PER_PLATFORM)
    try:
        async with session.post(
            APIFY_REDDIT_RUN_SYNC_URL, params={"token": APIFY_API_TOKEN}, json=payload,
            timeout=aiohttp.ClientTimeout(total=APIFY_REDDIT_TIMEOUT_S),
        ) as response:
            if response.status not in (200, 201):
                body = (await response.text())[:300]
                logger.warning("Apify Reddit search failed (status=%s) for %r: %s",
                               response.status, query, body)
                return None  # request failed -- distinct from a genuine zero-result search
            items = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("Apify Reddit search error for %r: %s", query, exc)
        return None  # ditto -- caller must not cache this as "no mentions found" forever

    mentions: List[Dict[str, Any]] = []
    for item in items or []:
        title = (item.get("title") or "").strip()
        body = (item.get("body") or "").strip()
        text = f"{title}\n{body}".strip() if title and body else (title or body)
        if not text:
            continue  # e.g. a media-only post with no title/body text
        mentions.append({
            "text": text,
            "rating": "",
            "author": item.get("username") or "",
            "date": item.get("createdAt") or "",
            "url": item.get("url") or item.get("link") or "",
            "community": item.get("communityName") or "",
        })
    logger.info("Apify Reddit search: %d mention(s) with text for %r", len(mentions), query)
    return mentions


async def enrich_platform(session: aiohttp.ClientSession, domain: str, name: str, address: str,
                           geo: str, platform: str, hosts: Sequence[str],
                           phone: str = "") -> Dict[str, Any]:
    cached = store.load(domain, platform, max_age_days=REVIEW_CACHE_DAYS)
    if cached is not None:
        logger.info("[%s] %s: cache hit (%d review(s) cached) — skipping re-scrape",
                    domain, platform, cached.get("review_count_extracted", 0))
        return cached
    logger.info("[%s] %s: checking (no cache) ...", domain, platform)
    if platform == "reddit":
        # Reddit has no single canonical "listing" page like every other
        # platform (a profile/review page found via Serper's site: search) --
        # it's scattered posts/comments across communities, found directly by
        # keyword search instead. See _fetch_apify_reddit_mentions.
        mentions = await _fetch_apify_reddit_mentions(session, name, geo)
        transient_failure = mentions is None
        mentions = mentions or []
        record = {
            "platform": platform, "business_name": name, "matched": bool(mentions),
            "listing_url": "", "reviews": mentions, "review_count_extracted": len(mentions),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if mentions:
            record["fetch_method"] = "apify"
        elif transient_failure:
            record["reason"] = "Apify Reddit request failed (network/timeout) -- not cached, will retry next run"
        elif not APIFY_API_TOKEN:
            record["reason"] = "Apify is not configured (no API token)"
        else:
            record["reason"] = "no Reddit mentions found"
        logger.info("[%s] %s: done -> %d mention(s) extracted", domain, platform, len(mentions))
        # A transient request failure must not get cached as a permanent "no
        # mentions found" -- same principle as _discover_google_maps below.
        if not transient_failure:
            store.save(domain, platform, record)
        return record
    if platform == "google_reviews":
        listing, extra_fields = await _discover_google_maps(session, name, address, geo, phone)
    else:
        listing = await _discover(session, name, hosts, geo)
        extra_fields = {}
    transient_failure = extra_fields.pop("transient_failure", False)
    record: Dict[str, Any] = {"platform": platform, "business_name": name, "matched": bool(listing), "listing_url": listing or "", "reviews": [], "review_count_extracted": 0, "checked_at": datetime.now(timezone.utc).isoformat()}
    record.update(extra_fields)
    if not listing:
        record["reason"] = "no confident public listing found"
        logger.info("[%s] %s: no confident listing found", domain, platform)
    elif platform == "google_reviews":
        # Google Maps review text is unreachable by any generic fetch tier
        # (see _fetch_apify_google_reviews's docstring) -- Apify replaces the
        # whole native/premium fetch+extract loop for this platform only.
        logger.info("[%s] %s: listing found -> %s", domain, platform, listing)
        reviews = await _fetch_apify_google_reviews(session, listing)
        record["pages_fetched"] = 1 if reviews or APIFY_API_TOKEN else 0
        if reviews:
            record["fetch_method"] = "apify"
            record["reviews"] = reviews
            record["review_count_extracted"] = len(reviews)
        elif not APIFY_API_TOKEN:
            record["reason"] = "listing found but Apify is not configured (no API token)"
        else:
            record["reason"] = "listing found but Apify returned no review text"
        logger.info("[%s] %s: done -> %d review(s) extracted", domain, platform,
                    record["review_count_extracted"])
    else:
        logger.info("[%s] %s: listing found -> %s", domain, platform, listing)
        reviews: List[Dict[str, Any]] = []
        seen_texts: set = set()
        fetch_method = ""
        pages_fetched = 0
        for page_num in range(1, REVIEW_MAX_PAGES + 1):
            html, method = await _fetch_one(session, _page_url(listing, page_num))
            if not html:
                logger.info("[%s] %s: page %d unreachable — stopping pagination",
                            domain, platform, page_num)
                break  # this page (and any further one) isn't reachable — stop
            fetch_method = fetch_method or method
            pages_fetched += 1
            new_count = 0
            for r in extract_reviews(html):
                key = r["text"].lower()
                if key in seen_texts:
                    continue
                seen_texts.add(key)
                reviews.append(r)
                new_count += 1
                if len(reviews) >= MAX_REVIEWS_PER_PLATFORM:
                    break
            logger.info("[%s] %s: page %d (%s) -> %d new review(s), %d total",
                        domain, platform, page_num, method, new_count, len(reviews))
            if len(reviews) >= MAX_REVIEWS_PER_PLATFORM:
                break
            if new_count == 0:
                break  # no new reviews on this page -> no real extra page here, stop
        record["pages_fetched"] = pages_fetched
        if reviews:
            record["fetch_method"] = fetch_method
            record["reviews"] = reviews
            record["review_count_extracted"] = len(reviews)
        elif pages_fetched == 0:
            record["reason"] = ("native fetch and premium scraper could not retrieve the listing"
                                if ZENROWS_API_KEY else
                                "native fetch blocked and ZENROWS_API_KEY is not configured")
        else:
            record["reason"] = "listing found but no public review text was exposed"
        logger.info("[%s] %s: done -> %d review(s) extracted across %d page(s)",
                    domain, platform, record["review_count_extracted"], pages_fetched)
    # A transient Serper failure (google_reviews only) must not get cached as
    # a permanent "no listing" -- see _discover_google_maps/_serper_places.
    if not transient_failure:
        store.save(domain, platform, record)
    return record


async def enrich_business(domain: str, name: str, industry: str = "", geo: str = "",
                           page_title: str = "", address: str = "",
                           phone: str = "") -> Dict[str, Any]:
    """Categorize one committed business and scrape its five mapped sources.

    `address`/`phone` (the business's own already-scraped contact details,
    when known) are only used by the google_reviews platform's Places-API
    verification — either one independently confirms a match (see
    _discover_google_maps / phase3.google_maps.find_place). Every other
    platform is unaffected. `phone` matters most for a business with no
    address on file — the only alternative to giving up on Google Reviews
    entirely for it.
    """
    category = categorize_domain(domain, industry, name, page_title)
    # Reddit isn't a category-specific review site -- discussion of any
    # business, any category, can turn up there -- so it's appended
    # unconditionally instead of coming from CATEGORY_PLATFORMS.
    selected: List[tuple[str, Sequence[str]]] = list(platforms_for_category(category))
    selected.append(("reddit", ()))
    logger.info("[%s] category=%s -> checking %d platform(s): %s",
                domain, category, len(selected), ", ".join(p for p, _ in selected))
    # Category-specific platforms all need Serper for discovery; reddit only
    # needs Apify (direct keyword search, no site: lookup) -- so a missing
    # Serper key narrows the list to reddit instead of blocking everything.
    if not SERPER_API_KEY:
        selected = [item for item in selected if item[0] == "reddit"]
    if not SERPER_API_KEY and not APIFY_API_TOKEN:
        return {
            "category": category, "checked": 0, "matched": 0, "reviews": 0,
            "reason": "SERPER_API_KEY and APIFY_API_TOKEN are not configured",
        }
    sem = asyncio.Semaphore(min(DEFAULT_CONCURRENCY, 2))
    async with aiohttp.ClientSession() as session:
        async def one(item: tuple[str, Sequence[str]]) -> Dict[str, Any]:
            async with sem:
                return await enrich_platform(session, domain, name, address, geo,
                                              item[0], item[1], phone)
        records = await asyncio.gather(*(one(item) for item in selected))
    matched = sum(bool(x.get("matched")) for x in records)
    review_count = sum(int(x.get("review_count_extracted", 0)) for x in records)
    matched_sites = [x["platform"] for x in records if x.get("matched")]
    sites_tag = f" ({', '.join(matched_sites)})" if matched_sites else ""
    logger.info("[%s] review harvest complete: %d/%d site(s) matched%s, %d review(s) extracted",
                domain, matched, len(records), sites_tag, review_count)
    return {"category": category, "checked": len(records), "matched": matched, "reviews": review_count,
            "matched_sites": matched_sites, "platforms": records}
