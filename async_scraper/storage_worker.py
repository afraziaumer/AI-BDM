"""Final pipeline stage: persist a ParsedPage. Default sink is a JSONL file
(via `aiofiles`, so the write doesn't block the event loop) — enough to
benchmark and inspect this engine standalone.

To integrate with the production pipeline, implement `PageSink` against
`storage.get_store()` (its `stage_page`/`buffer_index_row`/`commit_domain`
lifecycle) instead — the pipeline only depends on the `PageSink` protocol
below, never on JSONL specifically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import aiofiles

from .models import ParsedPage


class PageSink(Protocol):
    async def write(self, page: ParsedPage) -> None: ...
    async def close(self) -> None: ...


class JsonlSink:
    """One JSON object per line — trivially appendable from multiple
    concurrent workers without a lock, since each `write()` is a single
    `aiofiles` write of one already-newline-terminated line."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._file = None

    async def _ensure_open(self) -> None:
        if self._file is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = await aiofiles.open(self._path, "a", encoding="utf-8")

    async def write(self, page: ParsedPage) -> None:
        await self._ensure_open()
        row = {
            "url": page.url, "method": page.method.value,
            "status_code": page.status_code, "title": page.title,
            "word_count": page.word_count, "link_count": len(page.links),
            "text_preview": page.text[:500], "fetched_at": page.fetched_at,
        }
        await self._file.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def close(self) -> None:
        if self._file is not None:
            await self._file.close()
