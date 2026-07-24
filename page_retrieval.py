"""Search-first retrieval layer — the LLM should never inspect every page of
a website again. This module is what `route_planner.py` (page selection) and
`final_reasoning.py` (query answering) both search BEFORE any LLM sees
anything.

Pipeline (all local, no external database, no LLM call anywhere in here):

    committed <page>_context.json files (page_intelligence.py's output —
        title, meta description, H1/H2s, intro/outro, anchor text, page_type)
        -> build_index()      tokenize + embed a compact per-page "searchable
                               blob" built from that context (NEVER the full
                               .txt), cache under storage/<domain>/search_index/
        -> hybrid_retrieve()  BM25 keyword search + embedding cosine search,
                               merged and deduped -> top ~10 candidates
        -> rerank()           deterministic weighted scoring (BM25, embedding
                               similarity, page_type tier, title/H1 overlap,
                               anchor relevance, intro overlap) -> top 3-5

Only AFTER rerank() narrows to 3-5 filenames does anything read the actual
.txt files (that's `final_reasoning.py`'s job, not this module's).

Embeddings reuse `rag.embedder.Embedder` (already-installed
sentence-transformers wrapper from the separate, decoupled `rag/` package —
a one-directional dependency: this module depends on a rag/ utility, rag/
itself never depends on anything here or in the rest of the scraper). BM25
uses `rank_bm25.BM25Okapi`, a new lightweight dependency — a full vector-DB
system (what `rag/` uses for deep, chunk-level Q&A) is overkill for a single
site's ~40 short page-context documents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
from rank_bm25 import BM25Okapi

# rag.embedder.Embedder only sets HF_HUB_OFFLINE=1 AFTER its first successful
# model load, so a cold start always attempts a live Hugging Face Hub
# reachability check first, even when the weights are already cached locally
# (confirmed: ~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2
# exists) — on a firewalled/offline machine that first call fails outright.
# Set it here, BEFORE importing/constructing the embedder, so this module
# never depends on live network access for an already-cached model. This
# does not modify rag/'s own file (its "don't import the scraper" rule is
# about rag/ depending on us, not the reverse — see module docstring).
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from rag.embedder import Embedder
from storage import get_store

logger = logging.getLogger("ai_bdm.page_retrieval")

# Bumped whenever the searchable-blob construction, tokenization, or
# embedding model changes, so a cached index computed under an older version
# is never silently reused (same pattern as route_planner.PLANNER_VERSION /
# relevance_scoring.SCORING_VERSION).
RETRIEVAL_VERSION = 1

DEFAULT_HYBRID_TOP_K = 10
DEFAULT_RERANK_TOP_N = 5

# Merge weights for combining BM25 + embedding scores into one retrieval
# score (both normalized to [0, 1] first, so these are directly comparable).
_BM25_WEIGHT = 0.5
_EMBEDDING_WEIGHT = 0.5

# Reranker signal weights (sum to 100) — deterministic, no LLM.
_RERANK_WEIGHTS = {
    "retrieval_score": 40,   # the merged BM25+embedding score from hybrid_retrieve
    "page_type_tier": 20,    # reuses route_planner's HIGH/MEDIUM/LOW_PRIORITY_HINTS
    "title_overlap": 15,
    "anchor_overlap": 15,
    "intro_overlap": 10,
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_embedder: Optional[Embedder] = None


def _get_embedder() -> Embedder:
    """Lazy singleton — the sentence-transformers model is expensive to
    load, cheap to reuse (same rationale as rag/embedder.py's own docstring)."""
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


def _tokenize(text: str) -> List[str]:
    """Simple lowercase alphanumeric tokenizer for BM25 — no stemming/
    stopword removal needed at this corpus size (a few dozen short
    documents per site); BM25's own term-frequency weighting already
    down-weights generic words that appear on every page."""
    return _TOKEN_RE.findall((text or "").lower())


def _searchable_blob(context: Dict[str, Any]) -> str:
    """The ONE searchable representation of a page — built from
    page_intelligence.py's context JSON, never the full .txt. Title and H1
    are repeated (once directly, once implicitly weighted by BM25 term
    frequency) since they're the strongest signal of what a page is about."""
    meta = context.get("meta") or {}
    structure = context.get("structure") or {}
    content = context.get("content") or {}
    parts = [
        meta.get("title") or "",
        meta.get("description") or "",
        meta.get("og_title") or "",
        structure.get("h1") or "",
        " ".join(structure.get("h2s") or []),
        content.get("intro") or "",
        content.get("outro") or "",
        " ".join(context.get("anchors") or []),
    ]
    return " ".join(p for p in parts if p)


def _page_index_hash(domain: str) -> str:
    """Fingerprint of the domain's committed page_index.json — the SAME
    hashing approach as route_planner._page_index_hash (kept as an
    independent copy, not a shared import, to avoid a module-level circular
    import between route_planner.py and this module — route_planner calls
    INTO this module, so this module must not import route_planner at
    import time)."""
    page_index = get_store().read_page_index(domain)
    if not page_index:
        return ""
    blob = json.dumps(page_index, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class PageDoc:
    """One page's committed context, ready to search."""
    filename: str            # e.g. "pricing_context.json" -> "pricing"
    context: Dict[str, Any]
    blob: str


def _load_page_docs(domain: str) -> List[PageDoc]:
    store = get_store()
    docs: List[PageDoc] = []
    for context_filename in store.list_page_context_filenames(domain):
        context = store.read_page_context(domain, context_filename)
        if not context:
            continue
        stem = context_filename[: -len("_context.json")] if context_filename.endswith(
            "_context.json"
        ) else context_filename
        docs.append(PageDoc(filename=f"{stem}.txt", context=context, blob=_searchable_blob(context)))
    return docs


def build_index(domain: str) -> bool:
    """Ensure this domain's local search index is up to date. Returns True
    if a usable index exists afterward (freshly built or already valid),
    False if there's nothing to index (no committed pages/context yet).

    Cheap no-op on repeat calls: skipped entirely if the cached index's
    page_index_hash + RETRIEVAL_VERSION already match — the SAME
    "compute once, reuse forever" pattern as route_planner's own cache.
    """
    store = get_store()
    current_hash = _page_index_hash(domain)
    if not current_hash:
        return False

    cached = store.read_search_index(domain)
    if cached and cached.get("index_meta", {}).get("page_index_hash") == current_hash \
            and cached["index_meta"].get("retrieval_version") == RETRIEVAL_VERSION:
        return True  # already current, nothing to rebuild

    docs = _load_page_docs(domain)
    if not docs:
        return False

    embeddings = np.array(_get_embedder().embed([d.blob for d in docs]), dtype="float32")
    bm25_corpus = {
        "filenames": [d.filename for d in docs],
        "tokens": [_tokenize(d.blob) for d in docs],
    }
    index_meta = {
        "page_index_hash": current_hash,
        "retrieval_version": RETRIEVAL_VERSION,
        "page_count": len(docs),
    }
    store.write_search_index_now(domain, bm25_corpus, embeddings, index_meta)
    logger.info("Built local search index for %s: %d page(s).", domain, len(docs))
    return True


@dataclass
class RetrievalHit:
    filename: str
    context: Dict[str, Any]
    bm25_score: float = 0.0
    embedding_score: float = 0.0
    retrieval_score: float = 0.0
    rerank_score: float = 0.0
    reasons: List[str] = field(default_factory=list)


def _normalize(scores: Sequence[float]) -> List[float]:
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-9:
        return [1.0 if hi > 0 else 0.0 for _ in scores]
    return [(s - lo) / (hi - lo) for s in scores]


def hybrid_retrieve(
    domain: str, query: str, top_k: int = DEFAULT_HYBRID_TOP_K,
) -> List[RetrievalHit]:
    """BM25 keyword search + embedding semantic search over this domain's
    page contexts, merged into one normalized-score ranking. Returns up to
    `top_k` candidates, richest signal first. Empty list if there's no
    index (caller should fall back to the pre-existing LLM/heuristic path)."""
    store = get_store()
    if not build_index(domain):
        return []
    index = store.read_search_index(domain)
    if not index:
        return []

    bm25_corpus = index["bm25_corpus"]
    filenames: List[str] = bm25_corpus["filenames"]
    tokens: List[List[str]] = bm25_corpus["tokens"]
    embeddings: np.ndarray = index["embeddings"]
    if not filenames:
        return []

    docs_by_filename = {d.filename: d for d in _load_page_docs(domain)}

    bm25 = BM25Okapi(tokens)
    query_tokens = _tokenize(query)
    bm25_scores = bm25.get_scores(query_tokens) if query_tokens else np.zeros(len(filenames))

    embedder = _get_embedder()
    query_vec = np.array(embedder.embed_one(query), dtype="float32")
    # Embeddings are pre-normalized (rag.embedder.Embedder normalizes on
    # encode), so cosine similarity is a plain dot product.
    embedding_scores = embeddings @ query_vec if len(embeddings) else np.zeros(len(filenames))

    bm25_norm = _normalize(list(bm25_scores))
    embedding_norm = _normalize(list(embedding_scores))

    hits: List[RetrievalHit] = []
    for i, filename in enumerate(filenames):
        doc = docs_by_filename.get(filename)
        if doc is None:
            continue
        b, e = bm25_norm[i], embedding_norm[i]
        hits.append(RetrievalHit(
            filename=filename, context=doc.context,
            bm25_score=b, embedding_score=e,
            retrieval_score=_BM25_WEIGHT * b + _EMBEDDING_WEIGHT * e,
        ))

    hits.sort(key=lambda h: h.retrieval_score, reverse=True)
    return hits[:top_k]


def _word_overlap(query_terms: set, text: str) -> float:
    if not query_terms or not text:
        return 0.0
    text_terms = set(_tokenize(text))
    if not text_terms:
        return 0.0
    return len(query_terms & text_terms) / len(query_terms)


def rerank(
    hits: List[RetrievalHit], query: str, top_n: int = DEFAULT_RERANK_TOP_N,
) -> List[RetrievalHit]:
    """Deterministic weighted reranking — no LLM. Combines the retrieval
    score with page_type tier (reusing route_planner's existing rule-based
    priority hints, via a lazy import to avoid a module-level circular
    import — route_planner.py imports THIS module, so this can't import
    route_planner at module load time), plus direct query-term overlap
    against title/H1, anchor text, and the intro snippet."""
    from route_planner import (  # lazy: see docstring
        HIGH_PRIORITY_HINTS, MEDIUM_PRIORITY_HINTS, LOW_PRIORITY_HINTS, IGNORE_HINTS,
    )

    query_terms = set(_tokenize(query))

    def _tier_score(context: Dict[str, Any]) -> float:
        page_type = (context.get("page_type") or "").lower().replace(" ", "-")
        tokens = set(page_type.split("-")) if page_type else set()
        if tokens & IGNORE_HINTS:
            return 0.0
        if tokens & HIGH_PRIORITY_HINTS:
            return 1.0
        if tokens & MEDIUM_PRIORITY_HINTS:
            return 0.5
        if tokens & LOW_PRIORITY_HINTS:
            return 0.15
        return 0.3  # unknown page_type — neither clearly valuable nor noise

    for hit in hits:
        context = hit.context
        meta = context.get("meta") or {}
        structure = context.get("structure") or {}
        content = context.get("content") or {}

        tier = _tier_score(context)
        title_text = " ".join([meta.get("title") or "", structure.get("h1") or ""])
        title_overlap = _word_overlap(query_terms, title_text)
        anchor_overlap = _word_overlap(query_terms, " ".join(context.get("anchors") or []))
        intro_overlap = _word_overlap(query_terms, content.get("intro") or "")

        hit.rerank_score = (
            _RERANK_WEIGHTS["retrieval_score"] * hit.retrieval_score
            + _RERANK_WEIGHTS["page_type_tier"] * tier
            + _RERANK_WEIGHTS["title_overlap"] * title_overlap
            + _RERANK_WEIGHTS["anchor_overlap"] * anchor_overlap
            + _RERANK_WEIGHTS["intro_overlap"] * intro_overlap
        )
        hit.reasons = [
            f"retrieval={hit.retrieval_score:.2f}", f"tier={tier:.2f}",
            f"title_overlap={title_overlap:.2f}", f"anchor_overlap={anchor_overlap:.2f}",
            f"intro_overlap={intro_overlap:.2f}",
        ]

    hits.sort(key=lambda h: h.rerank_score, reverse=True)
    return hits[:top_n]
