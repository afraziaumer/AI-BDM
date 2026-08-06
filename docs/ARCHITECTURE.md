# AI-BDM Pipeline Architecture

One `python main.py --query "..."` run executes 8 stages in sequence. This document is the per-stage contract required for external integration (e.g. converting this into a job a Laravel backend can call): what each stage receives, produces, which ones call an LLM and with what settings, and how each behaves when a dependency fails.

Centralized model routing lives in `model_router.py` — no component hardcodes a model name or reasoning-effort string directly; every LLM call goes through `LLM_planner.call_llm(..., task=TaskType.X)`, which resolves settings via `model_router.select_model(task)`. Two tiers exist: Tier 1 (`openai/gpt-oss-20b`, low effort — fast/cheap classification-style calls) and Tier 2 (`openai/gpt-oss-120b`, medium effort — calls that require genuine reasoning). `qwen/qwen3.6-27b` is the universal emergency fallback for **both** tiers, used only when the primary model's call fails (network/rate-limit/malformed-JSON) — a task is never silently escalated from Tier 1 to Tier 2 on failure, only to the fallback.

---

## Stage 1 — Query Planning

**Identity**: `LLM_planner.plan_query()`, called from `phase1_pipeline.deconstruct_intent()`.

**Contract**:
- Input: one natural-language string (the user's query).
- Output: a JSON plan — `geo_location`, `broad_industry`, `search_query`, `result_limit`, `count_explicit`, `search_type` ("general"|"specific"), `target_domain`, `intent` ("find"|"find_and_filter"), `country_code`, `phone_regex`, `needs_tech_stack`, `needs_clarification`, `clarification_questions` (list), `exclude_keywords`, `include_keywords`, `reasoning`.

**Model settings**: `TaskType.INTENT_PLANNING` — primary `openai/gpt-oss-120b`, reasoning effort `medium`, `max_completion_tokens=3000`, `temperature=0.0`, `timeout=30s`, `2` retries before falling back to `qwen/qwen3.6-27b` (reasoning effort `none` for the fallback — Qwen doesn't accept the gpt-oss reasoning-effort vocabulary).

Originally Tier 1 (fast/cheap); moved to Tier 2 because this stage does more than structured extraction — it also judges whether a request is specific enough to run or needs a clarifying question first (see "Clarification layer" below), which needed genuine reasoning consistency that Tier 1 measurably lacked (same-shape queries returned different clarification decisions).

**Prompts**: `LLM_planner.PLANNER_SYSTEM_PROMPT` (inline in `LLM_planner.py`, not an external template file) — one system prompt plus one worked example (`PLANNER_EXAMPLE`). Not currently versioned beyond git history; no separate `prompts/` directory exists.

**Tools/data**: none — pure LLM reasoning over the query string, no external calls.

**Resilience**: on total failure (both primary and fallback model calls fail), `phase1_pipeline.run_pipeline()` returns `summary["error"] = "intent_failed: ..."` and the pipeline stops — no partial plan is fabricated.

**Evidence**: not applicable — this stage produces a plan, not a claim about a business.

**Performance**: single LLM call, typically under 2 seconds. Cost varies with reasoning-effort token usage; a real observed cost this session: `$0.0002–$0.0007` per call.

### Clarification layer (part of Stage 1)

If `needs_clarification` is true, `phase1_pipeline.run_pipeline()` returns immediately — **before Stage 2 (discovery/scraping) or any paid API call runs**. `main.py`'s `_plan_interactively()` prints the question(s), reads a typed answer via `input()` in the same terminal session, restates the exact question next to the answer (not just appended as a bare sentence — restating is required for the model to correctly resolve what the answer refers to on re-plan), and re-runs Stage 1. Capped at 3 rounds; after the cap or an empty answer, proceeds with that round's best-guess plan regardless (every field is always populated even when `needs_clarification` is true).

---

## Stage 2 — Discovery & Scraping

**Identity**: `phase1_pipeline.run_pipeline()` main loop, calling `process_single_lead()` per candidate.

**Contract**:
- Input: the Stage 1 plan.
- Output: per-candidate commit to local storage (`storage/<domain>/`) plus a row in `crawl_index.csv`; `summary["results"]`/`summary["qualified"]` lists.

**Model settings**: two LLM calls per candidate, both Tier 1 (`gpt-oss-20b`, low effort): `TaskType.WEBSITE_CLASSIFICATION` (`max_completion_tokens=300`) for the relevance-gate classification, and `TaskType.JSON_EXTRACTION` (`max_completion_tokens=350`) as `relevance_scoring.py`'s safety-net check on a borderline score. Discovery itself (finding candidate URLs) is not an LLM call — it's a Serper web search.

**Tools/data**: Serper.dev (discovery search), native HTTP fetch (Tier 1 fetch), ScrapingBee (Tier 2 fetch, JS-render/anti-bot escalation), `phase3.review_harvester` (category-aware review/Reddit harvesting), `bi_providers.BusinessIntelligenceManager` (social/decision-maker discovery), `tech_stack.py` (Wappalyzer-based detection, see Stage 5), `storage_sync.py` (optional R2 sync, post-commit).

**Resilience**: a hybrid relevance gate scores each candidate (industry/location/schema.org/description signals) before committing to a full crawl — cheap rejection before expensive work. A per-domain circuit breaker opens the premium (ScrapingBee) fetch tier after 2 consecutive failures for that domain, falling back to native-fetch-only for its remaining pages rather than retrying indefinitely. Review/Maps lookups distinguish a genuinely empty provider response (cached, since it's a real "no data" answer) from a request failure (network/timeout/non-200 — **never** cached, so the next run retries instead of trusting a false negative forever — this was a real bug found and fixed twice this session, once for Google Maps, once for Reddit).

**Evidence**: each committed business gets `storage/<domain>/reviews/*.json` (one file per review platform, each with `matched`, `listing_url`, `reviews[]`, `review_count_extracted`, `reason` on a miss), `storage/<domain>/*.txt` (cleaned page text), `storage/<domain>/*_context.json` (per-page structured context).

**Performance**: dominated by network I/O (scraping, provider APIs), not compute. Highly variable — a 3-business query typically takes 2–10 minutes depending on how many candidates need the premium fetch tier.

---

## Stage 3 — Route/Page Selection

**Identity**: `route_planner.plan_routes()`.

**Contract**:
- Input: a committed domain's crawled pages (from `storage/<domain>/`) plus the original query.
- Output: `{"selected_pages": [{url, txt_path, priority, reason}], "confidence": "high"|"medium"|"low"}`.

**Model settings**: primarily **not** an LLM call — search-first retrieval (BM25 + embedding similarity over the site's own already-crawled pages, via `page_retrieval.py`) picks the top pages deterministically. An LLM call (`TaskType.ROUTE_PLANNING`, Tier 1, `gpt-oss-20b`, `max_completion_tokens=500`) only fires as a fallback when retrieval finds nothing usable — rare in practice (logged: "1 page(s) -> 1 candidate(s)" style search-first selection is the common path).

**Resilience**: a domain with only 1 crawled page uses simple heuristics (URL/title pattern matching) rather than running a full search index for a single document.

**Performance**: the search-first path builds a local BM25 + embedding index per domain (a few hundred milliseconds to a few seconds depending on page count); no LLM cost in the common case.

---

## Stage 4 — Final Reasoning

**Identity**: `final_reasoning.answer_query()`. Runs **only** when Stage 1's `intent` is `"find_and_filter"` (i.e. the query has a specific ask beyond plain discovery, e.g. "...with no CRM").

**Contract**:
- Input: domain, the original query.
- Output: `{"answer": str, "source_pages": [...], "confidence": "high"|"medium"|"low"}`. On any failure, returns a low-confidence empty result rather than raising.

**Model settings**: `TaskType.FINAL_REASONING` — Tier 2, `openai/gpt-oss-120b`, reasoning effort `medium`, `max_completion_tokens=1500`, `temperature=0.15`, `timeout=30s`. The only stage that reads full page TEXT and lets the model synthesize a natural-language answer — every earlier stage only classifies/extracts structured JSON from smaller inputs.

**Prompts**: inline `_SYSTEM_PROMPT` in `final_reasoning.py`. Explicitly instructed to answer only from the given excerpts and say "not enough evidence" rather than guess.

**Resilience**: retrieval failure or no page text available both degrade to a `"low"`-confidence empty result with a `reason` field (`no_retrievable_pages` / `no_page_text_available`) — never raises to the caller.

**Evidence**: `source_pages` cites the exact filenames the answer was grounded in; the answer is required to say "not enough evidence" rather than fabricate when the excerpts don't support a claim.

---

## Stage 5 — Tech-Stack Detection

**Identity**: `tech_stack.py`. Runs only when Stage 1's `needs_tech_stack` is true (explicit CRM/CMS/framework/redesign-style questions — not merely mentioning a technology as a keyword filter).

**Contract**: input is a domain's already-fetched HTML/headers (no new fetch for the integrated path); output is `{"raw_wappalyzer": {...}, "normalized_tech_stack": {...}, "sales_signals": [...]}`.

**Model settings**: none — purely rule/fingerprint-based, no LLM call.

**Tools/data**: four detection engines merged into one result: (1) the legacy Wappalyzer engine (HTML/script/meta/headers), (2) an extended engine (cookies/DOM/inline-JS), (3) a DNS-record lookup, (4) a `robots.txt` fetch. Uses the `wappalyzer` package's fingerprint database (1,270 real technology signatures after `tech_stack._ensure_corrected_tech_db()` corrects a data-loading bug in the installed package — see `docs/KNOWN_LIMITATIONS.md`).

**Resilience**: each of the 4 engines is wrapped independently — a failure in one only means fewer signals, never a broken profile. All 3 of the non-legacy engines were silently failing on Windows before a UTF-8-encoding fix this session (see `docs/KNOWN_LIMITATIONS.md`).

---

## Stage 6 — Evidence Index / Retrieval (RAG)

**Identity**: `rag/ingest_and_answer.run()`. Skippable via `--no-rag`.

**Contract**: embeds both website high-intent-page content and harvested review content into a Chroma vector store, in two independently-ranked categories (never merged — a large website's chunks can't drown out a business's review signal or vice versa).

**Model settings**: no LLM call for retrieval itself — `sentence-transformers/all-MiniLM-L6-v2` (local embedding model, CPU/CUDA) for chunk embeddings; hybrid BM25 + semantic scoring for ranking. This is **non-generative search** — it returns ranked source chunks with citations, not a synthesized answer (that's Stage 4's job).

**Tools/data**: Chroma (local vector store, `rag/.chroma/`), the same page/review text already committed by Stage 2.

**Resilience**: negation-aware evidence scoring distinguishes "has a CRM" from "doesn't have a CRM" from "CRM planned for next year" rather than pure keyword matching. Domain-key consistency (bare domain vs. full URL) was a real bug found and fixed this session — see `docs/KNOWN_LIMITATIONS.md`'s note and preserve this fix in any reimplementation.

---

## Stage 7 — Google Maps Enrichment

**Identity**: `phase3.google_maps.enrich()`. Skippable via `--no-maps`.

**Contract**: input is `leads_clean.csv`; output is `leads_with_maps.csv` with `maps_rating`, `maps_rating_count`, `maps_category`, `maps_matched` columns added.

**Model settings**: none — Serper Places API lookup plus deterministic matching logic, no LLM call.

**Tools/data**: Serper Places API. Matching hierarchy: address match → phone match (last-9-digit comparison) → name-only fallback (only when Serper returns exactly one candidate, tagged `confidence: "low"`). `match_basis` on every result records which tier confirmed it.

**Resilience**: a failed Serper request (network/timeout/non-200) is distinguished from a genuine empty response and is **never** cached as a permanent "no match" — a real bug (confirmed live: a business with a verified match minutes earlier got silently locked into "no match" by one bad network moment) fixed this session.

---

## Stage 8 — Accuracy Audit

**Identity**: `accuracy_check.py`, runs automatically as the last step of `main.py` (also runnable standalone).

**Contract**: input is `leads_with_maps.csv` (or `leads_clean.csv` if Stage 7 was skipped) plus every `storage/<domain>/reviews/*.json`; output is `accuracy_report.txt`.

**Model settings**: none — pure rule-based checks, no LLM call.

**Three checks**:
1. **Field validity** — phone/email/address format sanity, ratings within 0–5.
2. **Cross-source identity consistency** — word-overlap between the business's own name and its name on Google Maps / each matched review platform, catching wrong-business matches.
3. **Review-relevance grounding** — for search-based sources (Reddit only — listing-based platforms like Google Reviews are deliberately excluded, since a review posted directly on a business's own confirmed page has no reason to restate the business's own name), checks each mention references the business name or geo. Skips non-English text (language-detected) rather than false-flagging it — see `docs/KNOWN_LIMITATIONS.md` for the Roman-Urdu gap this doesn't solve.

**Resilience**: entirely read-only over already-committed data — never re-scrapes, safe/cheap to re-run after every query.

---

## Cross-cutting: what requires an LLM vs. what doesn't

| Requires LLM | Does not require LLM |
|---|---|
| Stage 1 (query planning + clarification) | Stage 2's actual scraping/fetching (discovery search itself is not an LLM call) |
| Stage 2's relevance-gate classification + safety-net check | Stage 3's common-case search-first retrieval |
| Stage 3's rare retrieval-failure fallback | Stage 5 (tech-stack detection) |
| Stage 4 (final reasoning) — only runs for `find_and_filter` queries | Stage 6 (evidence retrieval — embeddings are local, not generative) |
| | Stage 7 (Maps enrichment) |
| | Stage 8 (accuracy audit) |

There is currently **no non-LLM alternative implementation** for Stage 1 — it is the only path that turns a natural-language query into a structured plan. Building a rule-based replacement would be new engineering work, not a relabeling exercise.
