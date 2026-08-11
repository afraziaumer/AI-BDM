"""Tiny, dependency-light URL/domain helpers shared across the project.

Extracted out of phase1_pipeline.py specifically so route_planner.py,
final_reasoning.py, and route_filter.py can use them WITHOUT transitively
importing phase1_pipeline itself — phase1_pipeline pulls in wappalyzer (via
tech_stack), which segfaults if loaded into the same process as torch
(needed by route_planner/final_reasoning's retrieval layer). Keeping this
module's own dependencies to just `tldextract` and stdlib means importing it
never risks that conflict, so main.py can run Steps 3+/6 in a separate
process from Step 2 with a completely clean import graph.

phase1_pipeline.py itself still exposes `_domain_key`/`_strip_tracking`/
`TRACKING_PARAMS` under those exact names (aliased from here) so nothing
else in that file needs to change.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import tldextract

TRACKING_PARAMS = frozenset({
    "srsltid", "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "yclid",
    "mc_eid", "igshid", "_ga", "ref", "ref_src",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
})

_TLD = tldextract.TLDExtract(
    suffix_list_urls=(),
    cache_dir=os.getenv("TLDEXTRACT_CACHE", ".tldextract-cache"),
)


def domain_key(url_or_host: str) -> str:
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


def safe_domain_component(domain: str) -> str:
    """Neutralize a domain string for use as a single filesystem path segment.

    Real bug found while executing a professional QA test suite (SEC-003):
    domain_key() only strips slashes/traversal when its input is a proper
    URL -- a raw non-URL string like '..\\..\\windows\\system32' (or a
    LLM-planned `target_domain` value, which skips domain_key() entirely and
    is only strip/lower'd) passes straight through. On Windows,
    Path('storage') / '\\windows\\system32' resolves to C:\\Windows\\System32
    because a leading path separator makes pathlib treat it as rooted,
    escaping the intended storage directory entirely. Stripping every path
    separator and '..' segment guarantees the result is always a single
    plain segment.
    """
    domain = (domain or "").replace("\\", "/")
    parts = [p for p in domain.split("/") if p not in ("", ".", "..")]
    return "_".join(parts) or "_"


def strip_tracking(url: str) -> str:
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
