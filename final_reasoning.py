"""The "LLM Reasoning -> Final Result" stage — the ONLY place in the search-
first architecture where an LLM sees actual page TEXT, and only after
`page_retrieval.py` has already narrowed a whole site down to the 3-5 pages
most likely to answer the user's specific question. Nothing before this
point ever loads a .txt file's full content.

Genuinely new capability (not a replacement of anything): the pre-existing
route_planner.py only ever picked pages and stored them as metadata — it
never actually answered the user's question. That capability previously
only existed in the separate, decoupled `rag/` package's `generator.answer()`
(chunk-level Q&A over a global Chroma index). This is the per-business,
per-query equivalent wired into the main pipeline, reusing this session's
own retrieval layer (page_retrieval.py) and the centralized model router
(model_router.TaskType.FINAL_REASONING — gpt-oss-120b, deeper reasoning,
since this is the one task that actually has to synthesize an answer from
real page content instead of just classifying/extracting structured JSON).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from LLM_planner import call_llm, get_client
from model_router import TaskType
from phase1_pipeline import _domain_key
from storage import get_store

import page_retrieval as pr

logger = logging.getLogger("ai_bdm.final_reasoning")

MAX_CHARS_PER_PAGE = 2000  # bounds prompt size across up to 5 pages


def _txt_path_by_filename(domain: str) -> Dict[str, str]:
    """filename ('pricing.txt') -> stored txt_path, from the committed crawl
    index — the same lookup route_planner.py's retrieval integration uses."""
    lookup: Dict[str, str] = {}
    for row in get_store().read_index():
        if _domain_key(row.get("domain") or row.get("website_url") or "") != domain:
            continue
        txt_path = row.get("txt_path") or ""
        if txt_path:
            lookup.setdefault(txt_path.rsplit("/", 1)[-1], txt_path)
    return lookup


_SYSTEM_PROMPT = """\
You are a business research assistant. You are given a specific question \
about ONE business and excerpts from the {n} pages of its own website judged \
most likely to answer it. Answer using ONLY what these excerpts actually say \
-- never guess or use outside knowledge about the business.

If the excerpts answer the question, give a direct, concise answer (2-4 \
sentences) and cite which page(s) it came from. If the excerpts do NOT \
contain enough information to answer confidently, say so plainly instead of \
guessing -- an honest "not enough evidence" is far more useful than a \
plausible-sounding fabrication.

Respond with ONLY valid JSON of exactly this form:
{{"answer": "...", "cited_pages": ["pricing.txt"], "confidence": "high"}}
confidence must be one of "high" (the excerpts directly answer this),
"medium" (partial/indirect evidence), "low" (little to no relevant evidence
found, the answer is a best guess or a clear "not found").
"""


def answer_query(domain: str, query: str, top_n: int = 5) -> Dict[str, Any]:
    """Retrieve the top pages for `query` (page_retrieval's hybrid retrieval
    + reranker, no LLM), read their actual page text, and make ONE LLM call
    to synthesize a grounded answer.

    Returns {"answer": str, "source_pages": [...], "confidence": str}. On any
    failure (no retrievable pages, LLM unavailable) returns a "low"-confidence
    empty-evidence result rather than raising — a research helper failing
    should degrade to "couldn't find an answer," never crash the caller.
    """
    domain = _domain_key(domain)
    try:
        hits = pr.hybrid_retrieve(domain, query, top_k=pr.DEFAULT_HYBRID_TOP_K)
        top = pr.rerank(hits, query, top_n=top_n)
    except Exception as exc:  # noqa: BLE001 - degrade, never crash the caller
        logger.warning("Retrieval failed for %s while answering %r: %s", domain, query, exc)
        top = []

    if not top:
        return {"answer": "", "source_pages": [], "confidence": "low",
                "reason": "no_retrievable_pages"}

    txt_paths = _txt_path_by_filename(domain)
    store = get_store()
    sources: List[Dict[str, str]] = []
    for hit in top:
        txt_path = txt_paths.get(hit.filename, "")
        text = store.read_page_text(txt_path)[:MAX_CHARS_PER_PAGE] if txt_path else ""
        if not text:
            continue
        sources.append({"filename": hit.filename, "url": hit.context.get("url", ""), "text": text})

    if not sources:
        return {"answer": "", "source_pages": [], "confidence": "low",
                "reason": "no_page_text_available"}

    excerpt_blob = "\n\n".join(
        f"--- {s['filename']} ({s['url']}) ---\n{s['text']}" for s in sources
    )
    user_content = f"QUESTION: {query}\n\n{excerpt_blob}"

    try:
        client = get_client()
        text = call_llm(
            client,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT.format(n=len(sources))},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            task=TaskType.FINAL_REASONING,
        )
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - degrade, never crash the caller
        logger.warning("Final reasoning LLM call failed for %s (%s): %s", domain, query, exc)
        return {"answer": "", "source_pages": [s["filename"] for s in sources],
                "confidence": "low", "reason": f"llm_unavailable:{exc}"}

    confidence = str(data.get("confidence", "")).strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    cited = data.get("cited_pages")
    cited_pages = [c for c in cited if isinstance(c, str)] if isinstance(cited, list) else []

    return {
        "answer": str(data.get("answer", "")).strip(),
        "source_pages": cited_pages or [s["filename"] for s in sources],
        "confidence": confidence,
    }
