"""Step 1 of social/LinkedIn intelligence — discover a business's OFFICIAL
social profiles from links already found on its own crawled pages.

Input is `crawl_site`'s `external_links` (site-wide, every off-domain
<a href> + its source page + anchor text — see phase1_pipeline.py's
`_extract_external_link_pairs`). No new network requests: this is a pure
filter/normalize pass over data the crawl already collected.

Distinct from discovery_classifier.py's NOISE_BRANDS: that list says "never
treat facebook.com/instagram.com/etc as a business's OWN homepage" for
general business discovery (a Facebook page found via Serper is not a
website to crawl). Here the goal is the opposite — a business's real,
already-crawled website legitimately links to its own social profiles, and
we want to find and keep those links. The two modules are not in tension:
one says "don't crawl these as if they were the business," the other says
"do record these as evidence the business has them."
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Each platform: (canonical schema key, host suffixes that identify it).
_PLATFORM_HOSTS: List[tuple] = [
    ("linkedin", ("linkedin.com",)),
    ("facebook", ("facebook.com", "fb.com")),
    ("instagram", ("instagram.com",)),
    ("x", ("twitter.com", "x.com")),
    ("youtube", ("youtube.com", "youtu.be")),
    ("tiktok", ("tiktok.com",)),
    ("pinterest", ("pinterest.com",)),
    ("threads", ("threads.net",)),
    ("github", ("github.com",)),
    ("gitlab", ("gitlab.com",)),
    ("discord", ("discord.com", "discord.gg")),
    ("reddit", ("reddit.com",)),
    ("medium", ("medium.com",)),
    ("whatsapp", ("wa.me", "api.whatsapp.com", "whatsapp.com")),
    ("telegram", ("t.me", "telegram.me", "telegram.org")),
]
_KNOWN_PLATFORM_HOSTS = frozenset(h for _, hosts in _PLATFORM_HOSTS for h in hosts)

# Path fragments that mark a link as a share/intent/login/widget/CDN URL,
# not an actual profile — checked against the URL path (lowercased).
_NOISE_PATH_HINTS = (
    "/sharer", "/share.php", "/dialog/share", "/sharing/share-offsite",
    "/shareArticle".lower(), "/intent/", "/share/",
    "/login", "/uas/login", "/accounts/login", "/login.php", "/signin",
    "/oauth", "/authorize", "/embed", "/plugins/", "/widgets/",
    "/pin/create", "/pin/", "?url=", "/i/flow/login",
)
# Generic root/near-empty paths ("just facebook.com/") are almost always a
# misconfigured "follow us" icon whose href was never actually set, not a
# real profile.
_MIN_MEANINGFUL_PATH_SEGMENTS = 1

# LinkedIn paths that are real business profile pages (not personal /in/
# profiles, not login/help paths) — see linkedin_discovery.py for the
# person-profile ("/in/") equivalent used by decision_maker_extractor.py.
_LINKEDIN_COMPANY_PATH_RE = re.compile(r"^/(company|school|showcase)/", re.IGNORECASE)


@dataclass
class SocialLink:
    platform: str
    url: str
    source_page: str
    anchor: str


def _host_platform(host: str) -> Optional[str]:
    host = host.lower().removeprefix("www.")
    for platform, hosts in _PLATFORM_HOSTS:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return platform
    return None


# Path segments that mark a URL as a specific POST/TWEET/PIN permalink
# rather than the profile itself (e.g. "/CabrilloMarina/status/12345",
# "/reel/abc123/", "/p/abc123/") — found via real testing: a business's own
# tweet, shared as a link on their site, was captured as its status-permalink
# instead of the handle. A profile URL never contains one of these.
_PERMALINK_SEGMENTS = frozenset({
    "status", "statuses", "posts", "post", "p", "reel", "reels", "photo",
    "photos", "videos", "video", "watch", "pin", "comments", "hashtag",
})
# Platforms whose canonical profile path is exactly one segment (the
# handle) — anything with a second segment is a permalink into that
# profile's content, not the profile. YouTube is handled separately below
# (its own profile shapes legitimately use 2 segments, e.g. "/channel/UC...").
_SINGLE_SEGMENT_PLATFORMS = frozenset({
    "facebook", "instagram", "x", "tiktok", "pinterest", "threads",
    "github", "gitlab", "reddit", "medium", "discord",
})


def _is_noise_url(platform: str, parsed) -> bool:
    path = parsed.path.lower()
    if any(hint in path for hint in _NOISE_PATH_HINTS):
        return True
    segments = [s for s in path.split("/") if s]
    if len(segments) < _MIN_MEANINGFUL_PATH_SEGMENTS:
        return True  # bare "facebook.com/" etc.
    if any(seg in _PERMALINK_SEGMENTS for seg in segments):
        return True  # a specific post/tweet/pin, not the profile itself
    if platform in _SINGLE_SEGMENT_PLATFORMS and len(segments) > 1:
        return True
    if platform == "youtube" and len(segments) > 1 and segments[0] not in (
        "channel", "c", "user"
    ):
        return True
    if platform == "linkedin" and not (
        _LINKEDIN_COMPANY_PATH_RE.match(parsed.path) or path.startswith("/in/")
    ):
        # Some other linkedin.com path (help, legal, jobs listing, feed...) —
        # not a company (or person) profile at all.
        return True
    return False


def _normalize(platform: str, url: str) -> str:
    """Canonical form: scheme+host lowercase, no query/fragment, no
    trailing slash — so the same profile linked with different tracking
    params from different pages collapses to one entry."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"https://{host}{path}"


def extract_social_links(external_links: List[Dict[str, str]]) -> Dict[str, Any]:
    """Build the social_profiles.json schema from a crawl's collected
    external links. Never raises — a malformed URL is just skipped.

    Returns {"linkedin": url|None, "facebook": ..., ..., "other": [urls]}.
    When a platform has multiple distinct candidate profiles (rare — most
    sites link to exactly one), the one linked from the most distinct pages
    wins (a footer link repeated site-wide outranks a one-off in a blog
    post body).
    """
    candidates: Dict[str, Dict[str, set]] = {}  # platform -> {url: {source pages}}

    for link in external_links:
        url = link.get("url", "")
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if not parsed.netloc:
            continue
        platform = _host_platform(parsed.netloc)
        if platform is None:
            continue
        if _is_noise_url(platform, parsed):
            continue
        canonical = _normalize(platform, url)
        candidates.setdefault(platform, {}).setdefault(canonical, set()).add(
            link.get("page_url", "")
        )

    result: Dict[str, Any] = {p: None for p, _ in _PLATFORM_HOSTS}
    for platform, urls_seen_from in candidates.items():
        best_url = max(urls_seen_from, key=lambda u: len(urls_seen_from[u]))
        result[platform] = best_url
    # "other": recognized platforms above are the ones this module actively
    # normalizes/dedupes; anything outside that list would need its own
    # noise-filtering rules to trust (see _is_noise_url) rather than being
    # guessed at generically, so it's intentionally always empty here.
    result["other"] = []
    return result
