"""HTML -> ParsedPage. CPU-bound, so this stage's workers run the parse in a
thread (`asyncio.to_thread`) rather than on the event loop — a few hundred
KB of `lxml` parsing is enough to stall other coroutines for a noticeable
stretch if run inline.

Reuses this project's existing, already-tested cleaner
(`phase1_pipeline.clean_page_for_llm`) when available, since it already
handles cookie-banner/hidden-element stripping, heading/list/table
structure preservation, and mojibake repair — no reason to reimplement that
here. Falls back to a lean standalone extractor so this package stays
independently runnable (e.g. for benchmarking) without pulling in
phase1_pipeline's full dependency set (aiohttp, phonenumbers, tldextract...).
"""

from __future__ import annotations

import re
from typing import List
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .models import FetchResult, ParsedPage

try:
    from phase1_pipeline import clean_page_for_llm as _clean_page_for_llm
except Exception:  # noqa: BLE001 - optional integration, not a hard dependency
    _clean_page_for_llm = None

_WS_RE = re.compile(r"\s+")


def _lean_extract(html: str) -> tuple[str, str]:
    """Standalone fallback: title + visible text, no structure preservation."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = _WS_RE.sub(" ", soup.get_text(" ")).strip()
    return title, text


def _extract_links(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    domain = urlparse(base_url).netloc.lower().removeprefix("www.")
    seen: set = set()
    links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        if urlparse(absolute).netloc.lower().removeprefix("www.") != domain:
            continue
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
    return links


def parse(result: FetchResult) -> ParsedPage:
    """Pure function: FetchResult -> ParsedPage. Never raises — a parse
    failure produces an empty-but-valid ParsedPage so one bad page can't
    take down the storage stage."""
    if not result.ok:
        return ParsedPage(
            url=result.job.url, method=result.method, status_code=result.status_code,
        )
    try:
        if _clean_page_for_llm is not None:
            soup = BeautifulSoup(result.html, "lxml")
            cleaned = _clean_page_for_llm(soup, result.job.url)
            title, text = cleaned["page_title"], cleaned["text"]
            word_count = cleaned["word_count"]
        else:
            title, text = _lean_extract(result.html)
            word_count = len(text.split())
        links = _extract_links(result.html, result.job.url)
    except Exception:  # noqa: BLE001 - a malformed page yields an empty
        # ParsedPage, not a crashed worker.
        return ParsedPage(
            url=result.job.url, method=result.method, status_code=result.status_code,
        )
    return ParsedPage(
        url=result.job.url, method=result.method, status_code=result.status_code,
        title=title, text=text, links=links, word_count=word_count,
    )
