"""Tiny, dependency-light timestamp helper shared across the project.

Every persisted/returned timestamp in this codebase should go through
`utc_now_iso()` rather than a bare `datetime.now(timezone.utc).isoformat()`
call. Python's `isoformat()` on a UTC-aware datetime renders the offset as
`+00:00` (e.g. `2026-08-06T12:00:00+00:00`); the Laravel Backend
Construction Handoff Guide's Section 5 convention is the literal `Z` suffix
(`2026-08-06T12:00:00Z`) instead -- both are valid ISO-8601, this just picks
one form and keeps it consistent everywhere a timestamp is generated.

Stdlib-only, like domain_utils.py, so it's safe to import from any module
in this codebase without pulling in wappalyzer/torch's segfault-prone
import chain (see domain_utils.py's docstring for why that matters).
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso(timespec: str = "seconds") -> str:
    """Current UTC time as ISO-8601 with a literal `Z` suffix, e.g.
    `2026-08-06T12:00:00Z` (timespec="seconds", the default)."""
    return datetime.now(timezone.utc).isoformat(timespec=timespec).replace("+00:00", "Z")
