"""The pipeline itself: fixed-size worker pools connected by `asyncio.Queue`s,
supervised by an `asyncio.TaskGroup`. No stage calls another stage's code
directly — every hand-off is a `queue.put`, so any stage can be slower or
faster than its neighbors without blocking them (the queue absorbs the
difference; see `queue_snapshot` in metrics.py for watching that in
practice).

    fetch_queue        --http_workers-->      (sufficient?)
                                                 |          \\
                                          parse_queue    playwright_queue
                                                 ^               |
                                                 +---playwright_workers
                                                 |
                                          parser_workers
                                                 |
                                          storage_queue
                                                 |
                                          storage_workers

Concurrency model, deliberately not one-semaphore-per-stage:
  - Stage worker COUNT is the concurrency limit for that stage (the
    standard asyncio producer/consumer pattern) — `http_concurrency`,
    `parser_concurrency`, `storage_concurrency` workers are spawned, each
    processing one job at a time in a loop.
  - Playwright concurrency is enforced by browser_fetcher.py's context POOL
    size, not a separate semaphore — the pool IS the semaphore (checking out
    a context blocks exactly like acquiring one). Two independent
    concurrency controls for the same resource is a real footgun (they can
    drift out of sync); one mechanism, used consistently, isn't.
  - Per-domain politeness genuinely needs a semaphore, because it's keyed by
    a job's domain rather than by which worker happens to run it — that's
    the one place `asyncio.Semaphore` is the right tool, and it's what's
    used below.

Completion tracking uses an explicit pending-job counter + `asyncio.Event`,
not `queue.join()`. `Queue.join()` returns as soon as its CURRENT item count
reaches zero — with multiple chained queues, a downstream queue that starts
empty (playwright_queue, before Stage 1 has escalated anything into it)
would report "done" immediately, even though work is still going to arrive.
counting completions at the unit-of-original-work level sidesteps that.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional

from .browser_fetcher import BrowserFetcher
from .config import ScraperConfig
from .http_fetcher import HttpFetcher
from .metrics import Metrics
from .models import FetchJob, FetchMethod, FetchResult, ParsedPage
from .parser import parse
from .storage_worker import PageSink
from .validator import check_sufficiency

logger = logging.getLogger("async_scraper.pipeline")


class Pipeline:
    def __init__(self, config: ScraperConfig, sink: PageSink) -> None:
        self.config = config
        self.sink = sink
        self.metrics = Metrics()

        self.fetch_queue: "asyncio.Queue[FetchJob]" = asyncio.Queue()
        self.playwright_queue: "asyncio.Queue[FetchJob]" = asyncio.Queue()
        self.parse_queue: "asyncio.Queue[FetchResult]" = asyncio.Queue()
        self.storage_queue: "asyncio.Queue[ParsedPage]" = asyncio.Queue()

        self._domain_locks: Dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(config.per_domain_concurrency)
        )
        self._pending = 0
        self._done_event = asyncio.Event()
        self._stop_metrics = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Completion tracking
    # ------------------------------------------------------------------ #
    def _mark_terminal(self) -> None:
        """Call exactly once per original URL, when it reaches a terminal
        state (stored, success or permanent failure). Retries/escalation
        between stages do NOT call this — they're the same unit of work
        moving between queues, not new work."""
        self._pending -= 1
        if self._pending <= 0:
            self._done_event.set()

    async def _delayed_requeue(self, job: FetchJob, queue: "asyncio.Queue[FetchJob]", delay: float) -> None:
        await asyncio.sleep(delay)
        queue.put_nowait(job)

    def _backoff(self, attempt: int) -> float:
        return min(
            self.config.retry_backoff_base_s * (2 ** attempt),
            self.config.retry_backoff_max_s,
        )

    # ------------------------------------------------------------------ #
    # Stage 1 workers
    # ------------------------------------------------------------------ #
    async def _http_worker(self, fetcher: HttpFetcher) -> None:
        while True:
            job = await self.fetch_queue.get()
            domain_lock = self._domain_locks[job.domain]
            async with domain_lock:
                result = await fetcher.fetch(job)
                if self.config.request_delay_s:
                    await asyncio.sleep(self.config.request_delay_s)

            self.metrics.record_response_time(result.elapsed_s)

            if result.ok:
                sufficiency = check_sufficiency(result.html, self.config)
                if sufficiency.sufficient:
                    self.metrics.pages_fetched += 1
                    self.metrics.http_success += 1
                    await self.parse_queue.put(result)
                    continue
                logger.debug("Escalating %s to Playwright: %s", job.url, sufficiency.reason)
                self.metrics.http_escalated += 1
                await self.playwright_queue.put(job)
                continue

            if HttpFetcher.is_retryable(result) and job.attempt < self.config.max_retries:
                job.attempt += 1
                self.metrics.retries += 1
                delay = self._backoff(job.attempt)
                asyncio.create_task(self._delayed_requeue(job, self.fetch_queue, delay))
                continue

            # Non-retryable, or retries exhausted, or a connection-level
            # failure: give the browser a shot before giving up entirely —
            # matches the requirement "HTTP request fails -> Playwright".
            self.metrics.http_escalated += 1
            await self.playwright_queue.put(job)

    # ------------------------------------------------------------------ #
    # Stage 2 workers
    # ------------------------------------------------------------------ #
    async def _playwright_worker(self, fetcher: BrowserFetcher) -> None:
        while True:
            job = await self.playwright_queue.get()
            self.metrics.playwright_launches += 1
            domain_lock = self._domain_locks[job.domain]
            async with domain_lock:
                result = await fetcher.fetch(job)

            self.metrics.record_response_time(result.elapsed_s)

            if result.ok:
                self.metrics.pages_fetched += 1
                self.metrics.playwright_success += 1
                await self.parse_queue.put(result)
                continue

            if job.attempt < self.config.max_retries:
                job.attempt += 1
                self.metrics.retries += 1
                delay = self._backoff(job.attempt)
                asyncio.create_task(self._delayed_requeue(job, self.playwright_queue, delay))
                continue

            # Terminal failure — still flows through parse/storage so it's
            # recorded (as an empty ParsedPage), never silently dropped.
            self.metrics.failures += 1
            await self.parse_queue.put(result)

    # ------------------------------------------------------------------ #
    # Parse + storage workers
    # ------------------------------------------------------------------ #
    async def _parser_worker(self) -> None:
        while True:
            result = await self.parse_queue.get()
            # CPU-bound (lxml parsing) — off the event loop, so a big page
            # doesn't stall every other coroutine mid-parse.
            page = await asyncio.to_thread(parse, result)
            await self.storage_queue.put(page)

    async def _storage_worker(self) -> None:
        while True:
            page = await self.storage_queue.get()
            try:
                await self.sink.write(page)
            except Exception as exc:  # noqa: BLE001 - a sink failure must not
                # wedge the pipeline; the job is still terminal either way.
                logger.error("Storage write failed for %s: %s", page.url, exc)
            finally:
                self._mark_terminal()

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    async def run(self, urls: List[str]) -> Metrics:
        self._pending = len(urls)
        if self._pending == 0:
            return self.metrics
        for url in urls:
            self.fetch_queue.put_nowait(FetchJob(url=url))

        queues_by_name = {
            "fetch": self.fetch_queue, "playwright": self.playwright_queue,
            "parse": self.parse_queue, "storage": self.storage_queue,
        }

        async with HttpFetcher(self.config) as http_fetcher, \
                   BrowserFetcher(self.config) as browser_fetcher:
            async with asyncio.TaskGroup() as tg:
                workers = []
                workers += [
                    tg.create_task(self._http_worker(http_fetcher))
                    for _ in range(self.config.http_concurrency)
                ]
                workers += [
                    tg.create_task(self._playwright_worker(browser_fetcher))
                    for _ in range(self.config.playwright_concurrency)
                ]
                workers += [
                    tg.create_task(self._parser_worker())
                    for _ in range(self.config.parser_concurrency)
                ]
                workers += [
                    tg.create_task(self._storage_worker())
                    for _ in range(self.config.storage_concurrency)
                ]
                metrics_task = tg.create_task(
                    self.metrics.run_periodic_logger(
                        queues_by_name, self.config.metrics_interval_s, self._stop_metrics,
                    )
                )

                await self._done_event.wait()
                self._stop_metrics.set()
                # Workers loop forever by design (`while True: queue.get()`)
                # — cancellation is the correct, expected way to stop them
                # once every job has reached a terminal state. TaskGroup
                # treats a child's CancelledError as normal shutdown, not a
                # failure that cancels its siblings.
                for w in workers:
                    w.cancel()
                await asyncio.sleep(0)  # let cancellations land before exiting

        await self.sink.close()
        return self.metrics
