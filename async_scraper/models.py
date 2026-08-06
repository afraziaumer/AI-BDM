"""Typed data passed between pipeline stages (see pipeline.py).

Every stage takes one of these in and produces the next one out — the
pipeline never passes bare dicts between queues, so a typo in a field name
is a type-checker error, not a silent `None` at 2am.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
from urllib.parse import urlsplit


class FetchMethod(str, Enum):
    HTTP = "http"          # Stage 1 (fast path) satisfied the request
    PLAYWRIGHT = "playwright"  # Stage 2 (browser) was needed
    FAILED = "failed"


class FailureReason(str, Enum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    HTTP_ERROR = "http_error"          # non-2xx after retries
    BROWSER_ERROR = "browser_error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def registered_domain(url: str) -> str:
    """Best-effort registrable-ish domain (host minus 'www.') — good enough
    for per-domain concurrency bucketing without pulling in tldextract."""
    host = urlsplit(url).netloc.lower()
    host = host.split("@")[-1].split(":")[0]  # strip userinfo/port
    return host.removeprefix("www.")


@dataclass
class FetchJob:
    """One URL to fetch. `attempt` is mutated in place as it's retried —
    the SAME job object flows back into the http queue on a retryable
    failure, carrying its incremented attempt count."""
    url: str
    attempt: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)
    domain: str = ""

    def __post_init__(self) -> None:
        if not self.domain:
            self.domain = registered_domain(self.url)


@dataclass
class FetchResult:
    """Output of Stage 1 or Stage 2 — a URL's raw HTML (or a terminal
    failure). `needs_render` is set by the validator (see validator.py) and
    read by the pipeline to route into the Playwright queue."""
    job: FetchJob
    method: FetchMethod
    html: str = ""
    status_code: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    elapsed_s: float = 0.0
    needs_render: bool = False
    failure_reason: Optional[FailureReason] = None
    error_detail: str = ""

    @property
    def ok(self) -> bool:
        return self.method != FetchMethod.FAILED and bool(self.html)


@dataclass
class ParsedPage:
    """Output of the parser stage — what the storage stage persists."""
    url: str
    method: FetchMethod
    status_code: Optional[int]
    title: str = ""
    text: str = ""
    links: List[str] = field(default_factory=list)
    word_count: int = 0
    fetched_at: float = field(default_factory=time.time)
