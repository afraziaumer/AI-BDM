"""Structured, low-overhead metrics: counters mutated in-line by workers
(no locks needed — CPython's GIL makes `int +=` atomic enough for counters
that only need to be approximately right in a log line), plus a background
task that logs a snapshot every `metrics_interval_s`.

Deliberately NOT a Prometheus exporter here — that belongs at the
integration layer once this engine is wired into the production pipeline
(see the companion architecture doc's §12). This module is the thing a
Prometheus exporter would read from.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict

logger = logging.getLogger("async_scraper.metrics")


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.monotonic)

    pages_fetched: int = 0
    http_success: int = 0
    http_escalated: int = 0        # Stage 1 -> Stage 2 handoffs
    playwright_launches: int = 0
    playwright_success: int = 0
    retries: int = 0
    failures: int = 0

    _response_time_total_s: float = 0.0
    _response_time_count: int = 0

    def record_response_time(self, elapsed_s: float) -> None:
        self._response_time_total_s += elapsed_s
        self._response_time_count += 1

    @property
    def avg_response_time_s(self) -> float:
        if not self._response_time_count:
            return 0.0
        return self._response_time_total_s / self._response_time_count

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def pages_per_second(self) -> float:
        el = self.elapsed_s
        return self.pages_fetched / el if el > 0 else 0.0

    def queue_snapshot(self, queues: Dict[str, "asyncio.Queue"]) -> Dict[str, int]:
        return {name: q.qsize() for name, q in queues.items()}

    def log_snapshot(self, queues: Dict[str, "asyncio.Queue"]) -> None:
        q = self.queue_snapshot(queues)
        logger.info(
            "metrics | fetched=%d rps=%.2f http_ok=%d escalated=%d "
            "pw_launches=%d pw_ok=%d retries=%d failures=%d avg_rt=%.3fs | "
            "queues=%s",
            self.pages_fetched, self.pages_per_second, self.http_success,
            self.http_escalated, self.playwright_launches,
            self.playwright_success, self.retries, self.failures,
            self.avg_response_time_s, q,
        )

    async def run_periodic_logger(
        self, queues: Dict[str, "asyncio.Queue"], interval_s: float,
        stop_event: "asyncio.Event",
    ) -> None:
        """Runs as its own task inside the pipeline's TaskGroup; exits
        cleanly when `stop_event` is set (see pipeline.py's shutdown)."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                self.log_snapshot(queues)
        self.log_snapshot(queues)  # final snapshot on shutdown
