"""YouTube enrichment — the other platform with a genuine, ToS-compliant
public API (YouTube Data API v3: https://developers.google.com/youtube/v3),
unlike the scrape-or-nothing situation for LinkedIn/Facebook/Instagram/X.

Requires an API key (YOUTUBE_API_KEY or youtube_api_key in .env — same
flexible-env-key convention as every other credential here). Without one,
this degrades cleanly to returning None everywhere — never crashes the
pipeline, never falls back to scraping youtube.com as a substitute.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger("ai_bdm.youtube_enrichment")

_API_BASE = "https://www.googleapis.com/youtube/v3"
_TIMEOUT_S = 10


def _api_key() -> Optional[str]:
    return os.getenv("YOUTUBE_API_KEY") or os.getenv("youtube_api_key")


def _channel_ref_from_url(url: str) -> Optional[tuple]:
    """('id', 'UC...') | ('forHandle', '@name') | ('forUsername', 'name'),
    matching the three URL shapes social_discovery.py already normalizes to
    (see its youtube path validation: /channel/UC..., /@handle, /c or /user)."""
    path = urlparse(url).path.strip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    if segments[0] == "channel" and len(segments) > 1:
        return ("id", segments[1])
    if segments[0].startswith("@"):
        return ("forHandle", segments[0])
    if segments[0] in ("c", "user") and len(segments) > 1:
        return ("forUsername", segments[1])
    return None


async def enrich_youtube(session: aiohttp.ClientSession, youtube_url: str) -> Optional[Dict[str, Any]]:
    """Fetch public channel snippet+statistics for a YouTube URL already
    discovered by social_discovery.py. None if no API key is configured, the
    channel isn't resolvable, or the request fails — never raises."""
    api_key = _api_key()
    if not api_key:
        logger.info("No YOUTUBE_API_KEY configured — skipping YouTube enrichment for %s", youtube_url)
        return None

    ref = _channel_ref_from_url(youtube_url)
    if not ref:
        return None
    param_name, param_value = ref

    params = {
        "part": "snippet,statistics",
        param_name: param_value,
        "key": api_key,
    }
    try:
        async with session.get(
            f"{_API_BASE}/channels", params=params,
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                logger.warning("YouTube API error %s enriching %s", resp.status, youtube_url)
                return None
            data = await resp.json()
    except Exception as exc:  # noqa: BLE001 - network layer, degrade quietly
        logger.warning("YouTube API request failed for %s: %s", youtube_url, exc)
        return None

    items = data.get("items") or []
    if not items:
        return None
    item = items[0]
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})

    def _int_or_none(v: Any) -> Optional[int]:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "url": youtube_url,
        "channel_id": item.get("id"),
        "name": snippet.get("title"),
        "description": snippet.get("description"),
        "subscriber_count": (
            None if stats.get("hiddenSubscriberCount") else _int_or_none(stats.get("subscriberCount"))
        ),
        "video_count": _int_or_none(stats.get("videoCount")),
        "view_count": _int_or_none(stats.get("viewCount")),
        "thumbnail_url": (snippet.get("thumbnails", {}).get("default", {}) or {}).get("url"),
        "source": "youtube_api",
    }
