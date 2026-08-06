"""Step 3 of social/LinkedIn intelligence — turn a confirmed LinkedIn
company URL (see linkedin_discovery.py) into linkedin_company.json.

SCOPE, deliberately: this module does NOT scrape linkedin.com. LinkedIn's
ToS prohibits automated scraping, and most of the fields a company page
shows (employee list, founded year, specialties, follower count, logo/
banner) are only reliably visible to a logged-in viewer anyway — getting
them for real means either LinkedIn's own licensed API access or a
commercial data provider who has separately taken on that legal/technical
problem (Apollo, Clearbit, ZoomInfo, Proxycurl-style APIs...), not an
in-house scraper here.

What this DOES do: populate whatever Serper's search index already
surfaced about that URL (title, description snippet — the same "organic"
result data linkedin_discovery.py used to confirm the match) into the
schema below, and leave everything a snippet can't answer explicitly
`null` with `"requires_provider": true` rather than silently omitting it
or guessing.

Pluggable by design: `EnrichmentProvider` is the seam. `SnippetProvider`
(below) is the default/only implementation today. Swap in a real one later
by implementing the same interface — nothing else in the pipeline needs to
change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

from linkedin_discovery import LinkedInMatch

# Fields no snippet-only source can honestly answer. Present, always null,
# flagged — never fabricated, never silently dropped.
_PROVIDER_ONLY_FIELDS = (
    "industry", "specialties", "headquarters", "founded_year", "company_size",
    "phone", "email", "locations", "followers", "employee_count",
    "logo_url", "banner_url", "tagline",
)


class EnrichmentProvider(Protocol):
    """The seam a real LinkedIn data provider plugs into. `enrich` receives
    the confirmed match (URL + whatever Serper already returned) and the
    business's own domain, and returns a dict merged into the schema below
    — a real provider would fill in some/all of _PROVIDER_ONLY_FIELDS and
    flip `requires_provider` to False for the fields it actually supplied."""

    async def enrich(self, match: LinkedInMatch, website_domain: str) -> Dict[str, Any]: ...


@dataclass
class SnippetProvider:
    """Default provider: uses ONLY what's already in the confirmed Serper
    match (no new network calls, no scraping). Honest about its own limits
    — see _PROVIDER_ONLY_FIELDS."""

    async def enrich(self, match: LinkedInMatch, website_domain: str) -> Dict[str, Any]:
        return {
            "about_section": match.snippet or None,
            "website": website_domain,
        }


_FOLLOWER_RE = re.compile(r"([\d,.]+\s?[KMB]?)\s+followers", re.IGNORECASE)


def _try_extract_follower_count(snippet: str) -> Optional[str]:
    """Some Serper snippets for LinkedIn results DO include a follower count
    Google indexed directly off the page (e.g. "... 12,345 followers ...").
    Opportunistic, best-effort — absence just means null, not an error."""
    m = _FOLLOWER_RE.search(snippet or "")
    return m.group(1).strip() if m else None


async def enrich_linkedin_company(
    match: LinkedInMatch, website_domain: str,
    provider: Optional[EnrichmentProvider] = None,
) -> Dict[str, Any]:
    """Build linkedin_company.json. Never raises — a provider failure just
    means the snippet-only baseline is what gets stored."""
    provider = provider or SnippetProvider()
    result: Dict[str, Any] = {
        "url": match.url,
        "company_name": match.title or None,
        "description": match.snippet or None,
        "website": website_domain,
        "followers": _try_extract_follower_count(match.snippet),
        "match_confidence": round(match.confidence, 3),
        "enrichment_source": "search_snippet",
        "requires_provider": True,
    }
    for field_name in _PROVIDER_ONLY_FIELDS:
        result.setdefault(field_name, None)

    try:
        extra = await provider.enrich(match, website_domain)
    except Exception:  # noqa: BLE001 - enrichment is best-effort, never fatal
        extra = {}
    for key, value in extra.items():
        if value is not None:
            result[key] = value
    if isinstance(provider, SnippetProvider):
        result["requires_provider"] = any(
            result.get(f) is None for f in _PROVIDER_ONLY_FIELDS if f != "followers"
        )
    return result
