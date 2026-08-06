"""Stage 2 — the fallback. ONE Playwright instance, ONE shared Chromium
process for the whole pipeline run, and a bounded pool of reusable
`BrowserContext`s (cookie/storage-isolated "profiles") checked out per job —
never a `browser.new_context()` per request and never `p.chromium.launch()`
per request. Only `context.new_page()` happens per job, and the PAGE (not
the context) is what gets closed after use.

Concurrency is enforced by the context pool itself: there are exactly
`playwright_concurrency` contexts, so at most that many renders run at once
with no separate semaphore needed — trying to check out a context when
none are free simply awaits until one is returned.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .config import ScraperConfig
from .models import FailureReason, FetchJob, FetchMethod, FetchResult

logger = logging.getLogger("async_scraper.browser")


class BrowserFetcher:
    def __init__(self, config: ScraperConfig) -> None:
        self._config = config
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context_pool: "asyncio.Queue[BrowserContext]" = asyncio.Queue()
        self._all_contexts: List[BrowserContext] = []

    async def __aenter__(self) -> "BrowserFetcher":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._config.headless)
        for _ in range(self._config.playwright_concurrency):
            ctx = await self._browser.new_context(
                user_agent=self._config.user_agents[0],
                ignore_https_errors=True,
            )
            self._all_contexts.append(ctx)
            self._context_pool.put_nowait(ctx)
        logger.info(
            "Playwright ready: 1 browser, %d reusable contexts",
            self._config.playwright_concurrency,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        for ctx in self._all_contexts:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    async def fetch(self, job: FetchJob) -> FetchResult:
        assert self._browser is not None, "use `async with BrowserFetcher(config) as f:`"
        context = await self._context_pool.get()
        start = time.monotonic()
        page = None
        try:
            page = await context.new_page()
            response = await page.goto(
                job.url,
                wait_until=self._config.playwright_wait_until,
                timeout=self._config.playwright_timeout_ms,
            )
            html = await page.content()
            status = response.status if response else None
            return FetchResult(
                job=job, method=FetchMethod.PLAYWRIGHT, html=html,
                status_code=status, elapsed_s=time.monotonic() - start,
            )
        except PlaywrightTimeoutError as exc:
            return FetchResult(
                job=job, method=FetchMethod.FAILED,
                elapsed_s=time.monotonic() - start,
                failure_reason=FailureReason.TIMEOUT, error_detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - a page/browser crash must not
            # take down the pipeline; surface it as a failed job instead.
            logger.warning("Playwright render failed for %s: %s", job.url, exc)
            return FetchResult(
                job=job, method=FetchMethod.FAILED,
                elapsed_s=time.monotonic() - start,
                failure_reason=FailureReason.BROWSER_ERROR, error_detail=str(exc),
            )
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
            # Context always goes back to the pool, even on failure — a
            # single bad page must not shrink the effective pool size.
            self._context_pool.put_nowait(context)
