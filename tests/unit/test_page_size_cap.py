"""Tests for phase1_pipeline._read_html()'s bounded read -- real gap found
live (professional QA suite, SEC-008 "oversized webpage"): NATIVE_FETCH_
TIMEOUT_S/PREMIUM_TIMEOUT_S bound how long a fetch may run, but the old
`raw = await resp.read()` buffered the ENTIRE response body regardless of
size -- a malicious/misbehaving server streaming a very large body quickly
enough would still be fully buffered into memory before any timeout fired.
Fixed by reading in bounded 64KB chunks up to MAX_PAGE_BYTES.

Run: pytest -q tests/unit/test_page_size_cap.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import phase1_pipeline as p1  # noqa: E402


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    def iter_chunked(self, size):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


class _FakeResponse:
    def __init__(self, chunks, charset="utf-8"):
        self.content = _FakeContent(chunks)
        self.charset = charset


def _read(chunks) -> str:
    return asyncio.run(p1._read_html(_FakeResponse(chunks)))


def test_normal_small_page_reads_in_full():
    body = "<html><body>Hello world</body></html>".encode("utf-8")
    assert _read([body]) == body.decode("utf-8")


def test_page_under_the_cap_across_multiple_chunks_reads_in_full():
    text = "<html>" + ("x" * 1000) + "</html>"
    body = text.encode("utf-8")
    chunks = [body[i:i + 100] for i in range(0, len(body), 100)]
    assert _read(chunks) == text


def test_oversized_page_is_truncated_not_fully_buffered():
    """The actual defect: a page well over MAX_PAGE_BYTES must stop being
    read once the cap is hit, not buffer everything the server sends."""
    chunk = b"a" * 65536
    num_chunks_over_cap = (p1.MAX_PAGE_BYTES // 65536) + 5
    chunks = [chunk] * num_chunks_over_cap
    result = _read(chunks)
    # Truncated to (at most, allowing for the chunk that pushed it over) one
    # extra chunk past the cap -- never the full ~5 chunks over.
    assert len(result.encode("utf-8")) <= p1.MAX_PAGE_BYTES + 65536
    assert len(result.encode("utf-8")) < len(chunk) * num_chunks_over_cap


def test_truncation_still_decodes_cleanly_not_a_crash():
    """Truncating mid-multi-byte-UTF-8-character must not raise -- falls
    back to charset-tolerant decode rather than crashing the whole fetch."""
    # A multi-byte UTF-8 character straddling a chunk boundary at the cap.
    filler = ("x" * (p1.MAX_PAGE_BYTES - 65536 + 65530)).encode("utf-8")
    straddling = "héllo wörld €€€".encode("utf-8")  # multi-byte chars
    chunks = [filler[i:i + 65536] for i in range(0, len(filler), 65536)] + [straddling]
    result = _read(chunks)  # must not raise
    assert isinstance(result, str)
