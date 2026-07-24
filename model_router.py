"""Centralized Model Router — the ONE place that decides which Groq model,
reasoning effort, token budget, temperature, timeout, and retry count a task
gets. No component should hardcode a model name or a reasoning-effort string
directly; every LLM call in this codebase goes through
`LLM_planner.call_llm(..., task=TaskType.X)`, which resolves its settings
here via `select_model(task)`.

Two-tier intelligence architecture:

  Tier 1 (lightweight)  gpt-oss-20b   reasoning=low     — intent planning,
                                                           website classification,
                                                           route planning,
                                                           query rewriting,
                                                           JSON extraction
  Tier 2 (heavy)        gpt-oss-120b  reasoning=medium  — final reasoning over
                                                           retrieved pages,
                                                           multi-document/complex
                                                           analysis
  Fail-safe             qwen/qwen3.6-27b                — universal emergency
                                                           backup for BOTH tiers,
                                                           only on failure of the
                                                           primary model.

Tier 2 is never the default for a task — a task is only assigned Tier 2 in
TASK_CONFIG if it genuinely needs deeper reasoning (see FINAL_REASONING).
Never "upgrade" mid-request: a Tier 1 task that fails falls straight to Qwen,
never to gpt-oss-120b — escalating to a bigger model on a transient failure
is not what the fallback is for, and would silently multiply cost exactly
where this router exists to prevent that.

IMPORTANT model quirk (verified live against the Groq API, not assumed):
`qwen/qwen3.6-27b` does NOT accept `reasoning_effort="low"/"medium"/"high"`
(the gpt-oss family's vocabulary) — it only accepts `"none"` or `"default"`.
`"default"` burns as many reasoning tokens as gpt-oss-120b at "high" (~240
tokens on a trivial task, all invisible chain-of-thought); `"none"` is fast,
cheap, and produces clean JSON. Every TaskConfig's `fallback_reasoning_effort`
is therefore always `"none"` — never reuse the primary model's reasoning
string when calling Qwen.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict

GPT_OSS_20B = "openai/gpt-oss-20b"
GPT_OSS_120B = "openai/gpt-oss-120b"
QWEN_FAILSAFE = "qwen/qwen3.6-27b"


class TaskType(str, Enum):
    INTENT_PLANNING = "intent_planning"
    WEBSITE_CLASSIFICATION = "website_classification"
    ROUTE_PLANNING = "route_planning"          # rare now — only the
                                                 # retrieval-failed fallback
                                                 # path in route_planner.py
    QUERY_REWRITE = "query_rewrite"             # generate_query_variations
    JSON_EXTRACTION = "json_extraction"         # moderation, relevance safety-net
    FINAL_REASONING = "final_reasoning"         # reasoning over retrieved pages
                                                 # (page_retrieval.py + final_reasoning.py)
    # Declared per the model capability map but no caller uses them yet —
    # reserved for future tasks that genuinely need them, not dead code to
    # trim: a task type existing here costs nothing until something asks
    # select_model() for it.
    SUMMARY = "summary"
    MULTI_DOCUMENT_REASONING = "multi_document_reasoning"
    COMPLEX_ANALYSIS = "complex_analysis"


@dataclass(frozen=True)
class TaskConfig:
    primary_model: str
    fallback_model: str
    reasoning_effort: str            # valid for the PRIMARY model's family
    fallback_reasoning_effort: str   # valid for the FALLBACK model's family
    max_completion_tokens: int
    temperature: float
    timeout_s: float
    max_retries: int


# Token budgets below carry over this session's already-LIVE-measured
# per-task sizes (see LLM_planner.py's previous MODERATION_MAX_TOKENS /
# PLANNER_MAX_TOKENS / RELEVANCE_MAX_TOKENS / QUERY_VARIATIONS_MAX_TOKENS) —
# not re-guessed. FINAL_REASONING is the one genuinely new task, sized for
# reasoning over 3-5 full page texts (a few thousand characters of prompt).
TASK_CONFIG: Dict[TaskType, TaskConfig] = {
    TaskType.INTENT_PLANNING: TaskConfig(
        GPT_OSS_20B, QWEN_FAILSAFE, "low", "none", 1000, 0.0, 20.0, 2,
    ),
    TaskType.WEBSITE_CLASSIFICATION: TaskConfig(
        GPT_OSS_20B, QWEN_FAILSAFE, "low", "none", 300, 0.0, 15.0, 2,
    ),
    TaskType.ROUTE_PLANNING: TaskConfig(
        GPT_OSS_20B, QWEN_FAILSAFE, "low", "none", 500, 0.0, 15.0, 2,
    ),
    TaskType.QUERY_REWRITE: TaskConfig(
        GPT_OSS_20B, QWEN_FAILSAFE, "low", "none", 400, 0.1, 15.0, 2,
    ),
    TaskType.JSON_EXTRACTION: TaskConfig(
        GPT_OSS_20B, QWEN_FAILSAFE, "low", "none", 350, 0.0, 15.0, 2,
    ),
    TaskType.FINAL_REASONING: TaskConfig(
        GPT_OSS_120B, QWEN_FAILSAFE, "medium", "none", 1500, 0.15, 30.0, 2,
    ),
    TaskType.SUMMARY: TaskConfig(
        GPT_OSS_20B, QWEN_FAILSAFE, "low", "none", 400, 0.1, 15.0, 2,
    ),
    TaskType.MULTI_DOCUMENT_REASONING: TaskConfig(
        GPT_OSS_120B, QWEN_FAILSAFE, "high", "none", 2000, 0.2, 30.0, 2,
    ),
    TaskType.COMPLEX_ANALYSIS: TaskConfig(
        GPT_OSS_120B, QWEN_FAILSAFE, "high", "none", 2000, 0.2, 30.0, 2,
    ),
}


# Centralized model capability map — documents which tier/tasks each model
# is meant for (observability/introspection; TASK_CONFIG above is what
# actually drives select_model(), this is the human-readable mirror of it
# per-model instead of per-task).
MODEL_CONFIG: Dict[str, Dict[str, object]] = {
    GPT_OSS_20B: {
        "type": "lightweight",
        "tasks": [t.value for t, c in TASK_CONFIG.items() if c.primary_model == GPT_OSS_20B],
        "reasoning": "low",
    },
    GPT_OSS_120B: {
        "type": "heavy_reasoning",
        "tasks": [t.value for t, c in TASK_CONFIG.items() if c.primary_model == GPT_OSS_120B],
        "reasoning": "medium/high",
    },
    QWEN_FAILSAFE: {
        "type": "failsafe",
        "tasks": ["ALL_TASKS"],
        "reasoning": "none",
    },
}

# Rough, static $/1M-token estimates for the METRICS cost_estimate field —
# good enough for relative "which task is expensive" comparison, not a
# billing system. Source: Groq's published per-model pricing at time of
# writing; update here if Groq's pricing changes, nowhere else.
_COST_PER_MILLION_TOKENS: Dict[str, float] = {
    GPT_OSS_20B: 0.10,
    GPT_OSS_120B: 0.50,
    QWEN_FAILSAFE: 0.20,
}


def select_model(task: TaskType) -> TaskConfig:
    """The one place every LLM call site asks "what should I use for this
    task?" — never hardcode a model/reasoning-effort/token-budget directly."""
    return TASK_CONFIG[task]


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Rough dollar estimate for one call, for the METRICS log line. Unknown
    models cost 0 rather than raising — a missing price entry must never
    break a real request over a logging nicety."""
    rate = _COST_PER_MILLION_TOKENS.get(model, 0.0)
    return round((prompt_tokens + completion_tokens) * rate / 1_000_000, 6)
