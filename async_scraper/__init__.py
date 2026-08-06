"""async_scraper — a standalone, benchmarkable high-concurrency scraping
engine: httpx fast path, async-Playwright fallback, asyncio.Queue pipeline.

See pipeline.py's module docstring for the architecture, and cli.py to run
it. Deliberately independent of phase1_pipeline.py's production crawler
(parser.py optionally reuses its HTML cleaner, nothing else does) so this
can be benchmarked and evaluated before any decision to integrate it.
"""

from .config import ScraperConfig
from .pipeline import Pipeline

__all__ = ["ScraperConfig", "Pipeline"]
