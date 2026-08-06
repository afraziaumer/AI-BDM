"""Phase 3's own tiny cache — plain JSON files, no dependency on storage.py.

Mirrors the "cache first, own data second, external call last" rule from the
project plan: before any platform module spends a Serper/scrape call on a
business, it calls `load()` here first. Deliberately NOT built on top of
storage.py's PageStore ABC (staging/commit lifecycle) — Phase 3 always runs
AFTER a domain is already committed by Phase 1, so it only ever needs plain
read/write of its own small JSON blobs, the same arm's-length relationship
rag/store.py already has with the main crawler's storage.

File layout: storage/<domain>/reviews/<platform>.json
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from phase3.config import REVIEWS_SUBDIR, STORAGE_ROOT


def _path(domain: str, platform: str) -> Path:
    return Path(STORAGE_ROOT) / domain / REVIEWS_SUBDIR / f"{platform}.json"


def load(domain: str, platform: str, max_age_days: Optional[float] = 7) -> Optional[Dict[str, Any]]:
    """Return the cached record for domain/platform, or None if missing/stale.

    `max_age_days=None` disables the freshness check (cache never expires).
    """
    p = _path(domain, platform)
    if not p.exists():
        return None
    if max_age_days is not None:
        age_days = (time.time() - p.stat().st_mtime) / 86400
        if age_days > max_age_days:
            return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None  # corrupt/unreadable cache -> treat as a miss, not a crash


def save(domain: str, platform: str, record: Dict[str, Any]) -> None:
    """Write domain/platform's record, creating storage/<domain>/reviews/ if needed."""
    p = _path(domain, platform)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
