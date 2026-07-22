"""All performance/behavior knobs for the async scraping engine, in one place.

Every field has a sane default; every field is overridable via environment
variable (see `ScraperConfig.from_env`) without editing code — the same
`.env`-driven convention the rest of this project already uses
(LLM_planner.get_client, tech_stack.py, etc.).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Tuple


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# A small rotation pool, not an exhaustive fingerprint database — anti-bot
# posture beyond this (TLS/JA3 impersonation, residential proxy rotation)
# belongs to a dedicated fetch backend (see http_fetcher.py's module
# docstring for the curl_cffi upgrade path), not to this config file.
DEFAULT_USER_AGENTS: Tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)


@dataclass(frozen=True)
class ScraperConfig:
    # --- concurrency -------------------------------------------------------
    http_concurrency: int = 100      # Stage 1 (fast path) in-flight requests
    playwright_concurrency: int = 15  # Stage 2 (browser) in-flight renders
    parser_concurrency: int = 20     # CPU-bound HTML parsing workers
    storage_concurrency: int = 5     # sink writers

    # --- per-domain politeness ----------------------------------------------
    # Global concurrency is not per-site concurrency — see architecture notes
    # in pipeline.py. Independent of the pool sizes above.
    per_domain_concurrency: int = 3
    request_delay_s: float = 0.0     # extra fixed delay between requests to
                                      # the SAME domain, on top of the semaphore

    # --- timeouts / retries --------------------------------------------------
    http_timeout_s: float = 15.0
    playwright_timeout_ms: int = 20_000
    max_retries: int = 3
    retry_backoff_base_s: float = 1.0     # backoff = base * 2^attempt
    retry_backoff_max_s: float = 20.0

    # --- browser ---------------------------------------------------------------
    headless: bool = True
    playwright_wait_until: str = "domcontentloaded"  # cheaper than "networkidle"

    # --- identity ----------------------------------------------------------------
    user_agents: Tuple[str, ...] = field(default_factory=lambda: DEFAULT_USER_AGENTS)

    # --- content-sufficiency gate (Stage 1 -> Stage 2 escalation) -------------
    min_text_chars: int = 400            # below this, treat as "thin" -> escalate
    min_text_to_html_ratio: float = 0.02  # text_len / html_len

    # --- metrics -------------------------------------------------------------
    metrics_interval_s: float = 5.0

    @classmethod
    def from_env(cls) -> "ScraperConfig":
        return cls(
            http_concurrency=_int_env("HTTP_CONCURRENCY", 100),
            playwright_concurrency=_int_env("PLAYWRIGHT_CONCURRENCY", 15),
            parser_concurrency=_int_env("PARSER_CONCURRENCY", 20),
            storage_concurrency=_int_env("STORAGE_CONCURRENCY", 5),
            per_domain_concurrency=_int_env("PER_DOMAIN_CONCURRENCY", 3),
            request_delay_s=_float_env("REQUEST_DELAY", 0.0),
            http_timeout_s=_float_env("TIMEOUT", 15.0),
            playwright_timeout_ms=_int_env("PLAYWRIGHT_TIMEOUT_MS", 20_000),
            max_retries=_int_env("MAX_RETRIES", 3),
            retry_backoff_base_s=_float_env("RETRY_BACKOFF_BASE_S", 1.0),
            retry_backoff_max_s=_float_env("RETRY_BACKOFF_MAX_S", 20.0),
            headless=_bool_env("HEADLESS", True),
            min_text_chars=_int_env("MIN_TEXT_CHARS", 400),
            min_text_to_html_ratio=_float_env("MIN_TEXT_TO_HTML_RATIO", 0.02),
            metrics_interval_s=_float_env("METRICS_INTERVAL_S", 5.0),
        )
