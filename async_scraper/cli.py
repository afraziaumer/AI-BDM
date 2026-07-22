"""Run the pipeline over a list of URLs and print a final metrics summary —
for benchmarking and manual verification.

Usage:
    ./env/bin/python -m async_scraper.cli --urls-file urls.txt
    ./env/bin/python -m async_scraper.cli --url https://example.com --url https://example.org
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from pathlib import Path
from typing import List

from .config import ScraperConfig
from .pipeline import Pipeline
from .storage_worker import JsonlSink


def _read_urls(args: argparse.Namespace) -> List[str]:
    urls: List[str] = list(args.url or [])
    if args.urls_file:
        text = Path(args.urls_file).read_text(encoding="utf-8")
        urls += [line.strip() for line in text.splitlines() if line.strip()]
    return urls


async def _main(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    urls = _read_urls(args)
    if not urls:
        raise SystemExit("No URLs given — pass --url (repeatable) or --urls-file.")

    config = ScraperConfig.from_env()
    if args.http_concurrency:
        config = ScraperConfig(**{**config.__dict__, "http_concurrency": args.http_concurrency})
    if args.playwright_concurrency:
        config = ScraperConfig(**{**config.__dict__, "playwright_concurrency": args.playwright_concurrency})

    sink = JsonlSink(args.out)
    pipeline = Pipeline(config, sink)

    start = time.monotonic()
    metrics = await pipeline.run(urls)
    elapsed = time.monotonic() - start

    print("\n" + "=" * 60)
    print("ASYNC SCRAPER — RUN SUMMARY")
    print("=" * 60)
    print(f"URLs requested     : {len(urls)}")
    print(f"Wall time          : {elapsed:.2f}s")
    print(f"Pages fetched      : {metrics.pages_fetched}")
    print(f"  via HTTP         : {metrics.http_success}")
    print(f"  via Playwright   : {metrics.playwright_success}")
    print(f"Escalations to PW  : {metrics.http_escalated}")
    print(f"Retries            : {metrics.retries}")
    print(f"Permanent failures : {metrics.failures}")
    print(f"Avg response time  : {metrics.avg_response_time_s:.3f}s")
    print(f"Throughput         : {len(urls) / elapsed:.2f} URLs/s")
    print(f"Output             : {args.out}")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="async_scraper benchmark runner")
    parser.add_argument("--url", action="append", help="A URL to fetch (repeatable).")
    parser.add_argument("--urls-file", help="Path to a newline-delimited URL list.")
    parser.add_argument("--out", default="async_scraper_output.jsonl", help="JSONL output path.")
    parser.add_argument("--http-concurrency", type=int, default=None)
    parser.add_argument("--playwright-concurrency", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
