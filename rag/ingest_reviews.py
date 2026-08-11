"""Ingest Phase 3's harvested review data (storage/<domain>/reviews/<platform>.json,
written by phase3.review_harvester) into the RAG knowledge base as its own
source_type="review" chunks, kept separate from a business's own website
chunks (source_type="website", the chunker's default) so retrieval can rank
and report each pool independently — see rag/top_matches.py's `source_type`
filter and rag/ingest_and_answer.py's two-category output.

Mirrors rag/ingest_from_storage.py's role: the one adapter file that reaches
outside rag/ (here, into phase3.config for the storage path layout) to turn
someone else's committed output into the one shape the rest of the package
already understands, SourceDoc.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, List

from .contract import SourceDoc


def iter_source_docs_from_reviews(domains: List[str]) -> Iterator[SourceDoc]:
    """Yield one SourceDoc per platform's harvested reviews, for each domain
    in `domains`. Only a platform with at least one extracted review TEXT
    contributes a doc — an unmatched listing or a matched-but-empty one
    (see review_harvester.py's `reason` field) has nothing to embed.
    """
    from domain_utils import safe_domain_component
    from phase3.config import REVIEWS_SUBDIR, STORAGE_ROOT

    for domain in domains:
        reviews_dir = Path(STORAGE_ROOT) / safe_domain_component(domain) / REVIEWS_SUBDIR
        if not reviews_dir.is_dir():
            continue
        for platform_file in sorted(reviews_dir.glob("*.json")):
            try:
                record = json.loads(platform_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            reviews = record.get("reviews") or []
            if not reviews:
                continue
            platform = record.get("platform") or platform_file.stem
            business_name = record.get("business_name") or domain
            body = "\n\n".join(
                f"{r.get('author') or 'Anonymous'} ({r.get('rating') or 'n/a'}): {r.get('text', '')}"
                for r in reviews if r.get("text")
            )
            if not body.strip():
                continue
            yield SourceDoc(
                url=record.get("listing_url") or f"{platform}:{domain}",
                title=f"{business_name} — {platform} reviews",
                content=body,
                domain=domain,
                source_type="review",
            )
