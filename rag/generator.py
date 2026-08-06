"""Step 5 — Generation (the "G" in RAG).

Ask the LLM to answer using ONLY the retrieved chunks.

Strategy:
  - Build a prompt: system rule + retrieved chunks (with URLs) + question.
  - Instruct: answer only from context; if it's not there, say so; cite URLs.
    This is what prevents hallucination.

Self-contained: this module creates its own Groq client and does NOT import any
of the scraper's modules, so the rag/ package stays independent until we merge.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from . import config
from .contract import Chunk

_SYSTEM = (
    "You are a business-intelligence assistant. Answer the question using ONLY "
    "the CONTEXT provided (extracted from the company's own website). If the "
    "answer is not in the context, reply exactly: \"Not stated on the website.\" "
    "Be concise and factual. Do not invent details."
)

_FALLBACK_MODEL = "openai/gpt-oss-120b"


def _get_client():
    """Create a Groq client from the .env key. Lazy so importing is cheap."""
    from dotenv import load_dotenv
    from groq import Groq
    load_dotenv()
    api_key = os.getenv("groq_llm_apikey1") or os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing Groq API key. Put `groq_llm_apikey1=...` in .env "
            "or set GROQ_API_KEY."
        )
    return Groq(api_key=api_key)


def build_prompt(question: str, chunks: List[Chunk]) -> str:
    """Assemble the user prompt from the question + retrieved chunks."""
    blocks = [f"[{i}] Source: {c.url}\n{c.text}" for i, c in enumerate(chunks, 1)]
    context = "\n\n".join(blocks) if blocks else "(no context)"
    return f"CONTEXT:\n{context}\n\nQUESTION: {question}\n\nANSWER:"


def answer(question: str, chunks: List[Chunk]) -> Dict[str, Any]:
    """Generate a grounded answer from the retrieved chunks."""
    client = _get_client()
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": build_prompt(question, chunks)},
    ]
    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL, messages=messages, temperature=0,
        )
    except Exception:  # noqa: BLE001 - fall back to the larger model
        resp = client.chat.completions.create(
            model=_FALLBACK_MODEL, messages=messages, temperature=0,
        )
    text = resp.choices[0].message.content or ""
    sources = list(dict.fromkeys(c.url for c in chunks if c.url))
    return {"answer": text.strip(), "sources": sources}


def explain_chunk_match(question: str, chunk: Chunk, score: float) -> Dict[str, Any]:
    """Ask the LLM whether the highest-matched chunk helps answer the query."""
    client = _get_client()
    system = (
        "You are a RAG evaluation assistant. Use only the provided chunk. "
        "Decide whether the chunk helps answer the user's query, then explain "
        "the chunk's relevance in 4 to 5 short lines. Be direct and do not "
        "invent anything outside the chunk."
    )
    user = (
        f"QUERY:\n{question}\n\n"
        f"MATCH_SCORE:\n{score:.4f}\n\n"
        f"CHUNK_SOURCE:\nTitle: {chunk.title}\nURL: {chunk.url}\n"
        f"Chunk number: {chunk.chunk_no}\n\n"
        f"CHUNK_TEXT:\n{chunk.text}\n\n"
        "TASK:\n"
        "1. Say whether this chunk helps answer the query: Yes, Partially, or No.\n"
        "2. Explain in 4 to 5 short lines what the chunk says with respect to "
        "the query.\n"
        "3. If the chunk does not contain enough information, say what is missing."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL, messages=messages, temperature=0,
        )
    except Exception:  # noqa: BLE001 - fall back to the larger model
        resp = client.chat.completions.create(
            model=_FALLBACK_MODEL, messages=messages, temperature=0,
        )
    text = resp.choices[0].message.content or ""
    return {
        "analysis": text.strip(),
        "source": chunk.url,
        "title": chunk.title,
        "chunk_no": chunk.chunk_no,
        "score": score,
    }
