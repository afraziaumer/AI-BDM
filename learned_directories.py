"""Auto-growing list of domains the pipeline has already confidently
determined are directories/aggregators/OTAs, not single businesses.

Complements discovery_classifier.DISCOVERY_SOURCE_REGISTRY (a hand-curated,
hardcoded list of well-known brands) rather than replacing it: the
hardcoded list gives instant, zero-fetch recognition for brands we already
know about; this module lets the pipeline LEARN new ones on its own, so a
domain only ever needs to be discovered-and-classified as a directory ONCE
(via website_classifier.classify_homepage()'s existing, non-hardcoded
structural scoring) before every future query excludes it directly at
Serper search time — never fetched, never classified again.

Persisted as a flat JSON file so it survives across process restarts, same
pattern as last_run.json / search_state.json.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Dict, Set

logger = logging.getLogger("ai_bdm.learned_directories")

LEARNED_DIRECTORIES_FILE = "learned_directories.json"

# Search-time -site: exclusions are capped -- an unbounded, ever-growing
# list would make the Serper query itself unwieldy (and Google's own
# handling of very long queries with many operators is not something to
# rely on), and this is ADDED ON TOP of the hardcoded registry (already
# 50+ brands as of this writing -- see phase1_pipeline._append_directory_
# exclusions), so the learned side is kept deliberately modest. Newest-
# first ordering (see load_for_exclusion) means the cap naturally favors
# recently-learned domains over ones learned long ago, which also self-
# corrects if a domain's classification was ever a one-off mistake -- it
# just ages out of the active exclusion set. No hard data yet on exactly
# how many -site: operators Serper/Google tolerate gracefully in one
# query; this is a deliberately conservative starting point, tunable if
# real usage shows it should be higher.
MAX_EXCLUSIONS_PER_QUERY = 15

_lock = threading.Lock()


def _load_raw() -> Dict[str, Dict[str, object]]:
    try:
        with open(LEARNED_DIRECTORIES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def record_learned_directory(domain: str, reason: str = "") -> None:
    """Persist `domain` as a known directory/aggregator. Safe to call
    repeatedly for the same domain (just refreshes its reason/timestamp).
    Best-effort: a write failure is logged, never raised -- learning is a
    nice-to-have optimization, never allowed to break the actual crawl."""
    domain = (domain or "").strip().lower()
    if not domain:
        return
    try:
        with _lock:
            data = _load_raw()
            data[domain] = {"reason": reason, "learned_at": time.time()}
            tmp_path = LEARNED_DIRECTORIES_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, LEARNED_DIRECTORIES_FILE)
        logger.info("[LearnedDirectories] Recorded %s as a known directory (%s).", domain, reason)
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        logger.warning("[LearnedDirectories] Failed to record %s: %s", domain, exc)


def load_for_exclusion(limit: int = MAX_EXCLUSIONS_PER_QUERY) -> Set[str]:
    """Domains to exclude from search results, most-recently-learned
    first, capped at `limit`."""
    try:
        data = _load_raw()
    except Exception as exc:  # noqa: BLE001 - never block a search over this
        logger.warning("[LearnedDirectories] Failed to load: %s", exc)
        return set()
    ranked = sorted(data.items(), key=lambda kv: kv[1].get("learned_at", 0), reverse=True)
    return {domain for domain, _meta in ranked[:limit]}
