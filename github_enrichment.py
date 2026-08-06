"""GitHub enrichment — the one social platform in this feature set with a
genuinely public, ToS-compliant, unauthenticated REST API for the exact data
we want (public org/user profile: bio, public repo count, followers,
location, blog/website link). This is a real API call, not a scrape — no
login wall, no bot-detection to work around, official and documented
(https://docs.github.com/en/rest/users, /orgs).

Unauthenticated calls are rate-limited to 60/hour per IP by GitHub — fine
for occasional enrichment; set GITHUB_TOKEN in .env to raise that to 5000/hour
if this is ever run at volume (same "flexible env key" convention as every
other credential in this project — see LLM_planner.get_client).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger("ai_bdm.github_enrichment")

GITHUB_API_BASE = "https://api.github.com"
_TIMEOUT_S = 10


def _github_handle_from_url(url: str) -> Optional[str]:
    """'https://github.com/stripe' -> 'stripe'. None for a repo/blob/issue
    URL (path with more than one segment) — this module enriches an ORG/USER
    profile, not a specific repository."""
    path = urlparse(url).path.strip("/")
    segments = [s for s in path.split("/") if s]
    if len(segments) != 1:
        return None
    return segments[0]


def _auth_headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN") or os.getenv("github_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def enrich_github(session: aiohttp.ClientSession, github_url: str) -> Optional[Dict[str, Any]]:
    """Fetch public org (falling back to user) profile data for a GitHub
    URL already discovered by social_discovery.py. Returns None on any
    failure (rate-limited, not found, network error) — never raises,
    never fabricates data the API didn't actually return."""
    handle = _github_handle_from_url(github_url)
    if not handle:
        return None

    headers = _auth_headers()
    for kind, endpoint in (("organization", f"orgs/{handle}"), ("user", f"users/{handle}")):
        try:
            async with session.get(
                f"{GITHUB_API_BASE}/{endpoint}", headers=headers,
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S),
            ) as resp:
                if resp.status == 404:
                    continue  # try the other endpoint kind
                if resp.status == 403:
                    logger.warning("GitHub API rate-limited enriching %s", github_url)
                    return None
                if resp.status != 200:
                    logger.warning("GitHub API error %s enriching %s", resp.status, github_url)
                    return None
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001 - network layer, degrade quietly
            logger.warning("GitHub API request failed for %s: %s", github_url, exc)
            return None

        return {
            "url": github_url,
            "handle": handle,
            "type": kind,
            "name": data.get("name"),
            "bio": data.get("bio") or data.get("description"),
            "company": data.get("company"),
            "location": data.get("location"),
            "email": data.get("email"),
            "website": data.get("blog") or None,
            "public_repos": data.get("public_repos"),
            "followers": data.get("followers"),
            "following": data.get("following") if kind == "user" else None,
            "avatar_url": data.get("avatar_url"),
            "verified": bool(data.get("is_verified")) if kind == "organization" else None,
            "source": "github_api",
        }
    return None
