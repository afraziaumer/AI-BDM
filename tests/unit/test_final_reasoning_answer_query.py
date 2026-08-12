"""End-to-end (mocked-LLM) tests for final_reasoning.answer_query() --
confidence/citation handling, previously untested in isolation (only the
Windows-path basename bug had coverage, see test_windows_paths.py).

Run: pytest -q tests/unit/test_final_reasoning_answer_query.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import final_reasoning as fr  # noqa: E402
import page_retrieval as pr  # noqa: E402


class _FakeStore:
    def __init__(self, rows, texts):
        self._rows = rows
        self._texts = texts

    def read_index(self):
        return self._rows

    def read_page_text(self, txt_path):
        return self._texts.get(txt_path, "")


def _hit(filename, url="https://example.org/pricing"):
    return pr.RetrievalHit(filename=filename, context={"url": url})


def _setup(monkeypatch, hits, llm_json, rows=None, texts=None):
    rows = rows or [{"domain": "example.org", "txt_path": f"storage\\example.org\\{h.filename}"} for h in hits]
    texts = texts or {r["txt_path"]: "Some real page content." for r in rows}
    monkeypatch.setattr(fr, "get_store", lambda: _FakeStore(rows, texts))
    monkeypatch.setattr(fr.pr, "hybrid_retrieve", lambda domain, query, top_k: hits)
    monkeypatch.setattr(fr.pr, "rerank", lambda hits_, query, top_n: hits_)
    monkeypatch.setattr(fr, "get_client", lambda: object())
    monkeypatch.setattr(fr, "call_llm", lambda *a, **kw: llm_json)


def test_no_retrievable_pages_returns_low_confidence_no_fabrication(monkeypatch):
    monkeypatch.setattr(fr.pr, "hybrid_retrieve", lambda domain, query, top_k: [])
    monkeypatch.setattr(fr.pr, "rerank", lambda hits, query, top_n: [])
    result = fr.answer_query("example.org", "does this business use a CRM?")
    assert result["confidence"] == "low"
    assert result["answer"] == ""
    assert result["reason"] == "no_retrievable_pages"


def test_no_page_text_available_returns_low_confidence_no_fabrication(monkeypatch):
    hits = [_hit("pricing.txt")]
    monkeypatch.setattr(fr.pr, "hybrid_retrieve", lambda domain, query, top_k: hits)
    monkeypatch.setattr(fr.pr, "rerank", lambda hits_, query, top_n: hits_)
    monkeypatch.setattr(fr, "get_store", lambda: _FakeStore([], {}))  # no rows -> no txt_path
    result = fr.answer_query("example.org", "does this business use a CRM?")
    assert result["confidence"] == "low"
    assert result["answer"] == ""
    assert result["reason"] == "no_page_text_available"


def test_valid_answer_returns_the_llm_s_confidence_field(monkeypatch):
    import json
    hits = [_hit("pricing.txt")]
    _setup(monkeypatch, hits, json.dumps({
        "answer": "Yes, they use a CRM called Acme CRM.",
        "cited_pages": ["pricing.txt"], "confidence": "high",
    }))
    result = fr.answer_query("example.org", "does this business use a CRM?")
    assert result["confidence"] == "high"
    assert result["answer"] == "Yes, they use a CRM called Acme CRM."
    assert result["source_pages"] == ["pricing.txt"]


def test_invalid_confidence_value_degrades_to_low(monkeypatch):
    """The LLM must never be trusted to emit a valid enum value -- an
    unrecognized confidence string is treated as the safest (lowest) tier,
    not silently passed through."""
    import json
    hits = [_hit("pricing.txt")]
    _setup(monkeypatch, hits, json.dumps({
        "answer": "Something.", "cited_pages": ["pricing.txt"], "confidence": "extremely-sure",
    }))
    result = fr.answer_query("example.org", "does this business use a CRM?")
    assert result["confidence"] == "low"


def test_missing_cited_pages_falls_back_to_all_source_filenames(monkeypatch):
    """A malformed/missing cited_pages must never silently drop all
    citations -- falls back to citing every source page that was actually
    shown to the model, so the answer is never presented as unsourced."""
    import json
    hits = [_hit("pricing.txt"), _hit("about.txt")]
    _setup(monkeypatch, hits, json.dumps({"answer": "Something.", "confidence": "medium"}))
    result = fr.answer_query("example.org", "does this business use a CRM?")
    assert set(result["source_pages"]) == {"pricing.txt", "about.txt"}


def test_llm_failure_degrades_to_low_confidence_not_a_crash(monkeypatch):
    hits = [_hit("pricing.txt")]
    rows = [{"domain": "example.org", "txt_path": "storage\\example.org\\pricing.txt"}]
    texts = {"storage\\example.org\\pricing.txt": "Some real page content."}
    monkeypatch.setattr(fr, "get_store", lambda: _FakeStore(rows, texts))
    monkeypatch.setattr(fr.pr, "hybrid_retrieve", lambda domain, query, top_k: hits)
    monkeypatch.setattr(fr.pr, "rerank", lambda hits_, query, top_n: hits_)
    monkeypatch.setattr(fr, "get_client", lambda: object())

    def boom(*a, **kw):
        raise RuntimeError("groq unavailable")
    monkeypatch.setattr(fr, "call_llm", boom)

    result = fr.answer_query("example.org", "does this business use a CRM?")
    assert result["confidence"] == "low"
    assert result["answer"] == ""
    assert "llm_unavailable" in result["reason"]
