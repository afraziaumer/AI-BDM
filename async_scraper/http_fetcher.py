"""Stage 1 — the fast path. A single shared `httpx.AsyncClient` (connection
pooling + HTTP keep-alive + automatic gzip/deflate/br decompression are all
built into httpx's client — there is nothing to configure by hand for those
beyond installing `brotli`/`brotlicffi` if you want `br` on top of gzip).

Why httpx over curl_cffi: httpx is already installed in this project's venv
(zero new dependency) and is the right default fetch client. The one thing
httpx genuinely can't do that curl_cffi can is TLS/JA3 fingerprint
impersonation (curl_cffi mimics a real browser's TLS ClientHello; httpx's
handshake looks like "a Python script" to fingerprint-based anti-bot
systems). If a specific target consistently 403s Stage 1 but a real browser
gets through, that's the concrete signal to add curl_cffi as an alternate
Stage-1 backend for those domains — not a reason to swap the default
wholesale. `HttpFetcher` is written against a narrow protocol precisely so
that swap is a new class, not a rewrite.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import httpx

from .config import ScraperConfig
from .models import FailureReason, FetchJob, FetchMethod, FetchResult

logger = logging.getLogger("async_scraper.http")

# Retried: transient. NOT retried: everything else (permanent 4xx, or a
# non-error response that's just thin/JS-rendered — that's the validator's
# job, via escalation to Stage 2, not a retry of Stage 1).
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpFetcher:
    """Owns ONE `httpx.AsyncClient` for the whole pipeline run — created
    once in `__aenter__`, reused by every job. Creating a new client per
    request is the single most common way to accidentally throw away
    connection pooling and keep-alive in an async scraper."""

    def __init__(self, config: ScraperConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "HttpFetcher":
        limits = httpx.Limits(
            max_connections=self._config.http_concurrency * 2,
            max_keepalive_connections=self._config.http_concurrency,
        )
        timeout = httpx.Timeout(self._config.http_timeout_s)
        self._client = httpx.AsyncClient(
            http2=False,  # see module note: enable only if targets benefit from multiplexing
            limits=limits,
            timeout=timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(self._config.user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def fetch(self, job: FetchJob) -> FetchResult:
        """One attempt. Retry orchestration (backoff, re-enqueue) lives in
        the pipeline worker, not here — this method is a pure single try."""
        assert self._client is not None, "use `async with HttpFetcher(config) as f:`"
        start = time.monotonic()
        try:
            resp = await self._client.get(job.url, headers=self._headers())
        except httpx.TimeoutException as exc:
            return FetchResult(
                job=job, method=FetchMethod.FAILED,
                elapsed_s=time.monotonic() - start,
                failure_reason=FailureReason.TIMEOUT, error_detail=str(exc),
            )
        except httpx.HTTPError as exc:
            return FetchResult(
                job=job, method=FetchMethod.FAILED,
                elapsed_s=time.monotonic() - start,
                failure_reason=FailureReason.CONNECTION, error_detail=str(exc),
            )

        elapsed = time.monotonic() - start
        if resp.status_code >= 400:
            return FetchResult(
                job=job, method=FetchMethod.FAILED, status_code=resp.status_code,
                elapsed_s=elapsed, failure_reason=FailureReason.HTTP_ERROR,
                error_detail=f"HTTP {resp.status_code}",
            )
        return FetchResult(
            job=job, method=FetchMethod.HTTP, html=resp.text,
            status_code=resp.status_code, headers=dict(resp.headers),
            elapsed_s=elapsed,
        )

    @staticmethod
    def is_retryable(result: FetchResult) -> bool:
        if result.failure_reason in (FailureReason.TIMEOUT, FailureReason.CONNECTION):
            return True
        if result.status_code in _RETRYABLE_STATUS:
            return True
        return False
