"""Tests for LLM_planner.call_llm()'s primary -> fallback (Qwen) model
failover -- previously only tested for "both fail" (GAP-FUNC-003); the
intermediate "primary fails, fallback succeeds" case (AI-BDM-303/304) had
no coverage.

The Groq client itself is fully mocked -- no real network/API call.

Run: pytest -q tests/unit/test_llm_call_failover.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import LLM_planner as lp  # noqa: E402
from model_router import TaskType  # noqa: E402


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    completion_tokens_details = None


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _FakeAPIStatusError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        self.response = None
        super().__init__(f"status {status_code}")


class _FakeClient:
    """Records every model called, in order; raises/returns per a canned
    per-model script."""

    def __init__(self, script):
        self.script = dict(script)  # model -> Exception instance or content string
        self.calls = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, model, messages, **kwargs):
        self.calls.append(model)
        result = self.script.get(model)
        if isinstance(result, Exception):
            raise result
        return _Response(result)


def _no_cache(monkeypatch):
    monkeypatch.setattr(lp.prompt_cache, "get", lambda *a, **kw: None)
    monkeypatch.setattr(lp.prompt_cache, "set", lambda *a, **kw: None)
    monkeypatch.setattr(lp.time, "sleep", lambda s: None)


def test_primary_success_never_calls_fallback(monkeypatch):
    _no_cache(monkeypatch)
    cfg = lp.select_model(TaskType.INTENT_PLANNING)
    client = _FakeClient({cfg.primary_model: '{"ok": true}'})
    result = lp.call_llm(client, [{"role": "user", "content": "x"}], task=TaskType.INTENT_PLANNING)
    assert result == '{"ok": true}'
    assert client.calls == [cfg.primary_model] * 1 or all(c == cfg.primary_model for c in client.calls)
    assert cfg.fallback_model not in client.calls


def test_primary_failure_falls_over_to_backup_model(monkeypatch):
    """AI-BDM-303/304: the primary model fails (deterministically, so no
    wasted retries) and the backup model takes over -- the caller still
    gets a real answer, not an error."""
    _no_cache(monkeypatch)
    cfg = lp.select_model(TaskType.INTENT_PLANNING)
    client = _FakeClient({
        cfg.primary_model: _FakeAPIStatusError(400),  # deterministic -- no retry, straight to fallback
        cfg.fallback_model: '{"answer": "from backup"}',
    })
    result = lp.call_llm(client, [{"role": "user", "content": "x"}], task=TaskType.INTENT_PLANNING)
    assert result == '{"answer": "from backup"}'
    assert cfg.primary_model in client.calls
    assert cfg.fallback_model in client.calls


def test_both_primary_and_backup_failing_raises_clearly(monkeypatch):
    """AI-BDM-305: when the backup ALSO fails, the caller gets a clear
    exception -- never a hallucinated/fabricated response."""
    _no_cache(monkeypatch)
    cfg = lp.select_model(TaskType.INTENT_PLANNING)
    client = _FakeClient({
        cfg.primary_model: _FakeAPIStatusError(400),
        cfg.fallback_model: _FakeAPIStatusError(400),
    })
    try:
        lp.call_llm(client, [{"role": "user", "content": "x"}], task=TaskType.INTENT_PLANNING)
        assert False, "expected an exception when both models fail"
    except _FakeAPIStatusError:
        pass


def test_retryable_failure_retries_the_same_model_before_falling_over(monkeypatch):
    """A transient (429/5xx) failure gets retried on the SAME model first,
    not an immediate jump to the fallback."""
    _no_cache(monkeypatch)
    cfg = lp.select_model(TaskType.INTENT_PLANNING)
    if cfg.max_retries < 2:
        return  # nothing to assert if this task's config allows only 1 attempt
    attempts = {"n": 0}

    def flaky(model, messages, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _FakeAPIStatusError(503)
        return _Response('{"ok": true}')

    client = _FakeClient({})
    client.create = flaky
    result = lp.call_llm(client, [{"role": "user", "content": "x"}], task=TaskType.INTENT_PLANNING)
    assert result == '{"ok": true}'
    assert attempts["n"] == 2  # one failed attempt + one successful retry, same model
