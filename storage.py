"""
Modular storage layer for the Lead Scravenger stage.

The streaming crawler writes cleaned page text, per-page link metadata, and a
per-page metadata index THROUGH this interface — it never touches the filesystem
directly. Today the interface is backed by the local filesystem; the same
interface can be backed by Cloudflare R2 later WITHOUT changing any crawler
logic. Swap the implementation returned by `get_store()` and nothing upstream
changes.

Local layout:
    storage/
        <domain>/
            <page>.txt           cleaned, LLM-ready page text (one file per page)
            links.json           per-page extracted internal links (for Step 3)
            tech_stack.json      normalized tech capabilities (see tech_stack.py)
            website_profile.json full tech-intelligence profile (see tech_stack.py)
            crawl_plan.json      cached LLM crawl plan (see crawl_planner.py)
    crawl_index.csv           per-page metadata index (NO page text, NO raw HTML)

Staging & commit
----------------
Pages are streamed to a temporary per-domain area first (`storage/.staging/`).
The business is committed to final storage only if it qualifies; if it is
rejected (aggregator / unrelated / spam) its staged files and buffered metadata
are discarded. On R2 this maps cleanly to a staging key-prefix that is
server-side copied to the final prefix on commit and deleted on discard — so the
crawler's commit/discard calls stay identical.

Memory
------
Only lightweight metadata (index rows, link lists) is buffered in RAM, and only
for the ONE business currently being crawled. Cleaned text is flushed to storage
per page and dropped from memory immediately; raw HTML never reaches this layer.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

# Per-page metadata columns for the crawl index. Deliberately excludes page TEXT
# and raw HTML — the cleaned text lives only in the .txt files. Contacts and
# title/meta ARE metadata (and the footer text they come from is stripped from
# the cleaned text), so they are captured here for the per-business rollup.
INDEX_COLUMNS: List[str] = [
    "company_name",
    "domain",
    "website_url",      # root site (groups all pages of one business)
    "page_url",         # the specific page this row indexes
    "page_title",
    "meta_description",
    "email",
    "phone_number",
    "physical_address",
    "txt_path",         # local path (or R2 key) of the cleaned text
    "crawl_status",     # ok | empty | failed
    "http_status",      # HTTP code or scrape method fallback
    "content_length",   # characters of cleaned text
    "word_count",
    "timestamp",        # ISO-8601 UTC
    "page_type",        # home | contact | about | services | products | blog | legal | other
]


class PageStore(ABC):
    """Storage interface the crawler depends on. Implement once per backend."""

    # --- filename helper (shared; deterministic across backends) -----------
    @staticmethod
    def page_name_for(url: str) -> str:
        """Deterministic, filesystem/key-safe base name for a page URL.

        Homepage -> "home"; "/about/team" -> "about-team". Bounded length.
        """
        path = urlsplit(url).path.strip("/")
        if not path:
            return "home"
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", path).strip("-").lower()
        return (name or "home")[:120]

    # --- streaming writes (staging) ----------------------------------------
    @abstractmethod
    def stage_page(self, domain: str, page_name: str, text: str) -> str:
        """Persist one cleaned page to the staging area. Returns its store path."""

    @abstractmethod
    def buffer_index_row(self, domain: str, row: Dict[str, str]) -> None:
        """Buffer one per-page metadata row (flushed to the index on commit)."""

    @abstractmethod
    def buffer_links(self, domain: str, page_url: str, links: List[Dict[str, str]]) -> None:
        """Buffer one page's extracted internal links (written on commit)."""

    @abstractmethod
    def stage_tech_profile(
        self, domain: str, capabilities: Dict, full_profile: Dict
    ) -> None:
        """Stage tech_stack.json (capabilities) + website_profile.json (full)
        alongside this domain's staged pages. Promoted/discarded together with
        them by commit_domain/discard_domain — no separate lifecycle needed."""

    @abstractmethod
    def stage_crawl_plan(self, domain: str, plan: Dict) -> None:
        """Stage crawl_plan.json (see crawl_planner.py) alongside this domain's
        staged pages. Promoted/discarded together with them."""

    # --- lifecycle ---------------------------------------------------------
    @abstractmethod
    def commit_domain(self, domain: str) -> None:
        """Promote staged pages to final storage; write links + index rows."""

    @abstractmethod
    def discard_domain(self, domain: str) -> None:
        """Delete staged pages and buffered metadata for a rejected business."""

    @abstractmethod
    def cleanup_orphaned_staging(self) -> List[str]:
        """Delete any staged domain left over from a previous unclean shutdown
        (killed process, crash outside the commit/discard try-block, etc.).

        A domain's staging directory only ever outlives its own crawl if the
        process died before reaching commit_domain/discard_domain — a normal
        run always calls one or the other for every domain it touches. Call
        this once, before a new run starts crawling anything. Returns the
        domain names that were cleaned up.
        """

    # --- reads (cache, rollup, Step 3) -------------------------------------
    @abstractmethod
    def has_domain(self, domain: str) -> bool:
        """True if the domain is already committed to final storage."""

    @abstractmethod
    def read_index(self) -> List[Dict[str, str]]:
        """Return all committed per-page index rows."""

    @abstractmethod
    def read_page_text(self, txt_path: str) -> str:
        """Return the cleaned text for a stored page (by its index txt_path)."""

    @abstractmethod
    def read_links(self, domain: str) -> Dict[str, List[Dict[str, str]]]:
        """Return committed {page_url: [links]} for a domain (for Step 3)."""

    @abstractmethod
    def read_tech_profile(self, domain: str) -> Optional[Dict]:
        """Return the committed website_profile.json for a domain, or None."""

    @abstractmethod
    def write_tech_profile_now(
        self, domain: str, capabilities: Dict, full_profile: Dict
    ) -> None:
        """Write a tech profile straight to FINAL storage for an already-
        committed domain (query-time backfill scan — not part of an in-
        progress crawl, so there is no staging/commit decision to make)."""

    @abstractmethod
    def read_crawl_plan(self, domain: str) -> Optional[Dict]:
        """Return the committed crawl_plan.json for a domain, or None if this
        domain was never planned (or its committed run predates this feature)."""


class LocalPageStore(PageStore):
    """Filesystem-backed PageStore. Swap for an R2-backed one later."""

    def __init__(self, root: str = "storage", index_path: str = "crawl_index.csv") -> None:
        self.root = Path(root)
        self.staging_root = self.root / ".staging"
        self.index_path = Path(index_path)
        # Buffered per-domain metadata (lightweight, one business at a time).
        self._index_buffers: Dict[str, List[Dict[str, str]]] = {}
        self._link_buffers: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
        self._staged_names: Dict[str, set] = {}
        self._lock = threading.Lock()

    # -- paths --
    def _staging_dir(self, domain: str) -> Path:
        return self.staging_root / domain

    def _final_dir(self, domain: str) -> Path:
        return self.root / domain

    def _unique_name(self, domain: str, page_name: str) -> str:
        """Avoid collisions when two URLs map to the same base name."""
        used = self._staged_names.setdefault(domain, set())
        candidate = page_name
        n = 2
        while candidate in used:
            candidate = f"{page_name}-{n}"
            n += 1
        used.add(candidate)
        return candidate

    # -- streaming writes --
    def stage_page(self, domain: str, page_name: str, text: str) -> str:
        with self._lock:
            name = self._unique_name(domain, page_name)
        d = self._staging_dir(domain)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.txt").write_text(text, encoding="utf-8")
        # Final path recorded in the index (where the file will live post-commit).
        return str(self._final_dir(domain) / f"{name}.txt")

    def buffer_index_row(self, domain: str, row: Dict[str, str]) -> None:
        with self._lock:
            self._index_buffers.setdefault(domain, []).append(row)

    def buffer_links(self, domain: str, page_url: str, links: List[Dict[str, str]]) -> None:
        with self._lock:
            self._link_buffers.setdefault(domain, {})[page_url] = links

    def stage_tech_profile(
        self, domain: str, capabilities: Dict, full_profile: Dict
    ) -> None:
        d = self._staging_dir(domain)
        d.mkdir(parents=True, exist_ok=True)
        self._write_tech_profile_files(d, capabilities, full_profile)

    def stage_crawl_plan(self, domain: str, plan: Dict) -> None:
        d = self._staging_dir(domain)
        d.mkdir(parents=True, exist_ok=True)
        (d / "crawl_plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    # -- lifecycle --
    def commit_domain(self, domain: str) -> None:
        staged = self._staging_dir(domain)
        final = self._final_dir(domain)
        # Move staged text files into final storage (replace any prior copy).
        if staged.exists():
            if final.exists():
                shutil.rmtree(final)
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(final))
        with self._lock:
            links = self._link_buffers.pop(domain, {})
            rows = self._index_buffers.pop(domain, [])
            self._staged_names.pop(domain, None)
        if links:
            final.mkdir(parents=True, exist_ok=True)
            (final / "links.json").write_text(
                json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        self._append_index(rows)

    def discard_domain(self, domain: str) -> None:
        staged = self._staging_dir(domain)
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        with self._lock:
            self._index_buffers.pop(domain, None)
            self._link_buffers.pop(domain, None)
            self._staged_names.pop(domain, None)

    def cleanup_orphaned_staging(self) -> List[str]:
        if not self.staging_root.exists():
            return []
        orphaned = [d.name for d in self.staging_root.iterdir() if d.is_dir()]
        for name in orphaned:
            shutil.rmtree(self.staging_root / name, ignore_errors=True)
        with self._lock:
            for name in orphaned:
                self._index_buffers.pop(name, None)
                self._link_buffers.pop(name, None)
                self._staged_names.pop(name, None)
        return orphaned

    # -- index i/o --
    def _append_index(self, rows: List[Dict[str, str]]) -> None:
        if not rows:
            return
        with self._lock:
            new_file = not self.index_path.exists() or self.index_path.stat().st_size == 0
            with open(self.index_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=INDEX_COLUMNS, extrasaction="ignore")
                if new_file:
                    writer.writeheader()
                for row in rows:
                    writer.writerow({c: row.get(c, "") for c in INDEX_COLUMNS})

    # -- reads --
    def has_domain(self, domain: str) -> bool:
        d = self._final_dir(domain)
        return d.exists() and any(d.glob("*.txt"))

    def read_index(self) -> List[Dict[str, str]]:
        if not self.index_path.exists():
            return []
        with open(self.index_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def read_page_text(self, txt_path: str) -> str:
        try:
            return Path(txt_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def read_links(self, domain: str) -> Dict[str, List[Dict[str, str]]]:
        f = self._final_dir(domain) / "links.json"
        if not f.exists():
            return {}
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def read_tech_profile(self, domain: str) -> Optional[Dict]:
        f = self._final_dir(domain) / "website_profile.json"
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def write_tech_profile_now(
        self, domain: str, capabilities: Dict, full_profile: Dict
    ) -> None:
        d = self._final_dir(domain)
        d.mkdir(parents=True, exist_ok=True)
        self._write_tech_profile_files(d, capabilities, full_profile)

    def read_crawl_plan(self, domain: str) -> Optional[Dict]:
        f = self._final_dir(domain) / "crawl_plan.json"
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_tech_profile_files(
        directory: Path, capabilities: Dict, full_profile: Dict
    ) -> None:
        (directory / "tech_stack.json").write_text(
            json.dumps(capabilities, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        (directory / "website_profile.json").write_text(
            json.dumps(full_profile, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )


# --- Singleton accessor: the ONE place to swap the backend for R2 later. -----
_STORE: Optional[PageStore] = None


def get_store() -> PageStore:
    """Return the process-wide PageStore. Replace the constructor here to move
    from local disk to Cloudflare R2 — no crawler code changes."""
    global _STORE
    if _STORE is None:
        _STORE = LocalPageStore(
            root=os.getenv("STORAGE_ROOT", "storage"),
            index_path=os.getenv("CRAWL_INDEX", "crawl_index.csv"),
        )
    return _STORE
