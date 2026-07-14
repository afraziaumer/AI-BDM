"""Ingest REAL scraped data from the storage layer (storage/ + crawl_index.csv)
into the RAG knowledge base. This is the production ingestion path — it reads
exactly what phase1_pipeline.py's streaming crawler committed, via the SAME
storage.PageStore abstraction the crawler itself writes through (see
storage.py's docstring: "future components (routing, embeddings, RAG, search)
should read THIS, not scan the filesystem").

This is the one file in rag/ that reaches outside the package, and that is
deliberate: storage.py is a generic filesystem/CSV abstraction (no HTML
parsing, no scraper internals) — it is the documented, intended hand-off point
between Developer A's crawl output and any downstream consumer, RAG included.
Nothing else in rag/ needs to change; this adapter just turns each committed
crawl_index.csv row into the one shape the rest of the package already
understands: SourceDoc(url, title, content, domain).

Run (from the project root, C:\\AI-BDM, so both `rag` and `storage` resolve):
  python -m rag.ingest_from_storage
  python -m rag.ingest_from_storage --domain sachardental.com
  python -m rag.ingest_from_storage --limit 20
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterator, Optional
from urllib.parse import urlparse

from collections import Counter

from .contract import SourceDoc

DEFAULT_LEADS_JSON = "leads_clean.json"


def _strip_repeated_boilerplate(page_contents: List[str]) -> List[str]:
    """Strip lines that appear verbatim on 2+ of this SAME business's own
    high-intent pages (site nav bars, footers, cookie banners, "select a
    state" dropdown lists, etc. -- chrome the site repeats on every page).

    Such a line carries no distinguishing signal for any query (every page of
    the business has it) and, left in, does two kinds of damage: it pads out
    a chunk and dilutes any genuinely distinctive fact merged in next to it,
    and -- if it happens to literally contain a query word (e.g. a "New York"
    entry in a repeated state-picker list) -- it can win a keyword bonus that
    has nothing to do with the business itself.

    A line unique to just one page is always kept, no matter how short.
    Nothing here is hardcoded to any specific word/phrase -- "boilerplate" is
    detected purely by repetition across this business's own pages.
    """
    if len(page_contents) < 2:
        return page_contents  # nothing to compare against -- keep as-is

    line_counts: Counter = Counter()
    per_page_lines: List[List[str]] = []
    for content in page_contents:
        lines = [ln.strip() for ln in content.splitlines()]
        per_page_lines.append(lines)
        # Count each distinct line once per page, so a line repeated several
        # times WITHIN one page doesn't look cross-page-repeated by itself.
        for ln in {l for l in lines if l}:
            line_counts[ln] += 1

    cleaned = []
    for lines in per_page_lines:
        kept = [ln for ln in lines if not ln or line_counts[ln] < 2]
        cleaned.append("\n".join(kept))
    return cleaned


def _home_page_titles(store) -> Dict[str, str]:
    """The title of each domain's OWN home/root page (the URL with the fewest
    path segments), used as reliable per-business context for chunk titles.

    Deliberately NOT leads_clean.json's `company_name` field -- that can be
    corrupted upstream (observed: a raw URL for one business, an unrelated
    subpage's title for another). A business's actual scraped home-page title
    comes from the same reliable source as every other page_title and has
    proven trustworthy in practice.
    """
    best: Dict[str, tuple[int, str]] = {}
    for row in store.read_index():
        domain = (row.get("domain") or "").strip()
        title = (row.get("page_title") or "").strip()
        url = row.get("page_url") or ""
        if not domain or not title:
            continue
        # Segment COUNT, not "slashes remaining after stripping" -- the latter
        # is 0 for BOTH the true root ("/") and any single-segment page
        # ("/about", "/new-york/"), making them tie and picking whichever the
        # index happens to list first. Segment count correctly makes the root
        # (0 segments) strictly shorter than any single-segment page (1).
        path = urlparse(url).path.strip("/")
        path_len = 0 if not path else path.count("/") + 1
        if domain not in best or path_len < best[domain][0]:
            best[domain] = (path_len, title)
    return {d: t for d, (_, t) in best.items()}


def _chunk_title(page_title: str, home_title: str) -> str:
    """Combine a page's own title with its business's home-page title, e.g.
    "Safe Harbor - Global leader in waterfront lifestyle — Vessel Service
    Excellence". A page titled just "Service" or "Superyachts" gives every one
    of its chunks that generic title as their ONLY context otherwise, so a
    chunk about, say, a payment app never gets the business-identifying
    context a home-page chunk gets for free -- purely because of which page
    it happened to be scraped from, not because it's less relevant.
    """
    page_title = (page_title or "").strip()
    home_title = (home_title or "").strip()
    if not home_title:
        return page_title
    if not page_title or page_title.lower() == home_title.lower():
        return home_title
    return f"{home_title} — {page_title}"


def iter_source_docs_from_high_intent(
    leads_json_path: str = DEFAULT_LEADS_JSON,
    domain: Optional[str] = None,
) -> Iterator[SourceDoc]:
    """Yield one SourceDoc per HIGH-INTENT page ONLY — not every scraped page.

    Reads leads_clean.json (written by data_pipeline.py). Each business's
    `high_intent_pages` field was populated by Step 3's LLM route planner
    (route_planner.py) with the small set of pages it judged most likely to
    answer the user's query (e.g. contact, about, pricing) — typically 2-6
    pages, versus potentially dozens of raw crawled pages per business. This
    is the embedding source: a 40-page site contributes only its handful of
    high-intent pages, not all 40, to the RAG store.

    Businesses with no high_intent_pages (Step 3 didn't run for this query, or
    found nothing) are skipped entirely — there is nothing "high intent" to
    embed for them; use iter_source_docs_from_storage() if you need every page.

    Each high_intent_pages entry already carries its own `txt_path` (set by
    route_planner.py from the same crawl_index.csv row), so the cleaned text
    is read directly — no separate index lookup needed for the content itself.
    `page_title` is not part of a route entry, so it's looked up once from
    crawl_index.csv (via storage.read_index()) for a better chunk title than
    the bare filename.
    """
    import storage  # project-root module; resolves when run from C:\AI-BDM

    p = Path(leads_json_path)
    if not p.exists():
        return
    try:
        businesses = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return

    store = storage.get_store()
    title_by_url = {row.get("page_url", ""): row.get("page_title", "")
                    for row in store.read_index()}
    home_titles = _home_page_titles(store)

    for biz in businesses:
        biz_domain = (biz.get("domain") or "").strip()
        if domain and biz_domain != domain:
            continue
        pages = biz.get("high_intent_pages")
        if not pages:
            continue  # Step 3 found nothing high-intent for this business

        # Read every high-intent page's raw text FIRST (not yielded one at a
        # time), so cross-page boilerplate -- text repeated verbatim across
        # this business's own pages -- can be stripped before chunking.
        raw_pages = []
        for page in pages:
            txt_path = page.get("txt_path", "")
            url = page.get("url", "")
            if not txt_path:
                continue
            content = store.read_page_text(txt_path)
            if not content.strip():
                continue
            raw_pages.append((url, content))
        if not raw_pages:
            continue

        cleaned_contents = _strip_repeated_boilerplate([c for _, c in raw_pages])
        for (url, _), cleaned in zip(raw_pages, cleaned_contents):
            if not cleaned.strip():
                continue  # this page turned out to be entirely boilerplate
            yield SourceDoc(
                url=url,
                title=_chunk_title(title_by_url.get(url, ""),
                                  home_titles.get(biz_domain) or biz.get("company_name", "")),
                content=cleaned,
                domain=biz_domain,
            )


def iter_source_docs_from_storage(
    domain: Optional[str] = None,
) -> Iterator[SourceDoc]:
    """Yield one SourceDoc per committed page in crawl_index.csv.

    `domain` optionally restricts ingestion to a single business (matches the
    index's own `domain` column, e.g. "sachardental.com") — useful for
    re-ingesting just one site after a fresh crawl instead of the whole index.

    Reads via storage.get_store() (not open() on the CSV directly) so this
    adapter keeps working unchanged if the storage backend ever moves off the
    local filesystem (e.g. to Cloudflare R2) — see storage.py's docstring.
    """
    import storage  # project-root module; resolves when run from C:\AI-BDM

    store = storage.get_store()
    for row in store.read_index():
        row_domain = (row.get("domain") or "").strip()
        if domain and row_domain != domain:
            continue
        txt_path = row.get("txt_path", "")
        content = store.read_page_text(txt_path) if txt_path else ""
        if not content.strip():
            continue  # nothing to chunk/embed for this page
        yield SourceDoc(
            url=row.get("page_url", "") or row.get("website_url", ""),
            title=row.get("page_title", ""),
            content=content,
            domain=row_domain,   # trust crawl_index.csv's own computed domain
        )


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="Ingest committed crawl output into the RAG knowledge base. "
                    "By default only HIGH-INTENT pages (from leads_clean.json) "
                    "are embedded, not every scraped page."
    )
    parser.add_argument("--domain", default=None,
                        help="Only ingest this business's pages (e.g. sachardental.com).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max pages to ingest (0 = all).")
    parser.add_argument("--all-pages", action="store_true",
                        help="Ingest EVERY committed page (crawl_index.csv), not just "
                             "the high-intent ones from leads_clean.json.")
    parser.add_argument("--leads-json", default=DEFAULT_LEADS_JSON,
                        help="Path to leads_clean.json (only used without --all-pages).")
    args = parser.parse_args()

    from .pipeline import RagPipeline
    pipe = RagPipeline()

    if args.all_pages:
        source = iter_source_docs_from_storage(domain=args.domain)
    else:
        source = iter_source_docs_from_high_intent(
            leads_json_path=args.leads_json, domain=args.domain
        )

    pages = 0
    total_chunks = 0
    seen_domains: set = set()
    for doc in source:
        total_chunks += pipe.ingest(doc)
        pages += 1
        seen_domains.add(doc.domain)
        if pages % 20 == 0:
            print(f"  ingested {pages} pages / {total_chunks} chunks...")
        if args.limit and pages >= args.limit:
            break

    mode = "all committed pages" if args.all_pages else "high-intent pages only"
    print(f"[ingest] done ({mode}): {pages} pages across {len(seen_domains)} "
          f"business(es) -> {total_chunks} chunks. Store now holds {pipe.store.count()} chunks.")
    if seen_domains:
        print("  businesses:", ", ".join(sorted(seen_domains)))


if __name__ == "__main__":
    main()
