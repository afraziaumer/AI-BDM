"""Generic, content-hash-keyed cache for LLM prompts — "never recompute an
identical prompt." Wired into `LLM_planner.call_llm()` centrally (not
re-implemented per call site) so EVERY task benefits automatically: intent
planning, moderation, and relevance classification are the highest-value
targets (this session's queries repeat near-identical prompts constantly),
but website classification and route planning benefit too whenever the same
domain is reprocessed.

No TTL — like `route_planner.py`'s page-index-hash cache and
`relevance_scoring`'s scoring-version cache, this is keyed on content, not
time: the SAME task + model + messages always deserves the SAME cached
answer, forever, until the task's config or the prompt itself changes (both
of which change the hash automatically).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ai_bdm.prompt_cache")

_CACHE_DIR = Path("storage") / ".prompt_cache"


def _cache_key(task: str, model: str, messages: List[Dict[str, str]]) -> str:
    blob = json.dumps({"task": task, "model": model, "messages": messages},
                       sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(task: str, model: str, messages: List[Dict[str, str]]) -> Optional[str]:
    """Return a previously-cached response text for this exact
    task+model+prompt, or None on a cache miss (or any read error — a
    corrupted cache entry must never break a real request, just force a
    fresh call)."""
    key = _cache_key(task, model, messages)
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("response")
    except (OSError, json.JSONDecodeError):
        return None


def set(task: str, model: str, messages: List[Dict[str, str]], response: str) -> None:
    """Persist a fresh response for this exact task+model+prompt. Best-effort
    — a write failure must never break the caller that just got a real
    answer, it just means this prompt isn't cached for next time."""
    key = _cache_key(task, model, messages)
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _CACHE_DIR / f"{key}.json"
        path.write_text(
            json.dumps({"task": task, "model": model, "response": response},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:  # noqa: BLE001 - caching is best-effort
        logger.warning("Failed to write prompt cache entry for task=%s: %s", task, exc)
