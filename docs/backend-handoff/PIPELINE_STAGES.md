# Pipeline Stages — Per-Stage Record

One record per stage, in the exact field order requested by the Laravel Backend Construction Handoff Guide's Appendix A2 template. This is the authoritative per-stage reference for backend integration; `docs/ARCHITECTURE.md` covers the same stages in narrative form (model settings, prompts, resilience reasoning) and is still the better read for *why* each stage behaves the way it does — this document exists so every A2 field is answered explicitly, including the ones ARCHITECTURE.md doesn't state as a labeled field (`Current status`, `Timeout`, `Checkpoint/cancellation`, `Test/fixture proving behavior`).

**Pipeline version**: reported as `git:<commit-sha>` — there is no separate internal version number; the git commit *is* the version identity (see `docs/HANDOFF_SUMMARY.md`'s Snapshot identity row for the current one).

---

## Stage 1 — Query Planning

- **Stage name and version**: Query Planning — versioned by git commit only (no per-stage semver).
- **Entry function/module**: `LLM_planner.plan_query()`, called from `phase1_pipeline.deconstruct_intent()`.
- **Current status**: Working.
- **Input schema and example**: one natural-language string. Example: `"find 3 dental clinics in Islamabad with no online booking"`.
- **Output schema and example**: JSON plan — `geo_location`, `broad_industry`, `search_query`, `result_limit`, `count_explicit`, `search_type` (`"general"`|`"specific"`), `target_domain`, `intent` (`"find"`|`"find_and_filter"`), `country_code`, `phone_regex`, `needs_tech_stack`, `needs_clarification`, `clarification_questions` (list), `exclude_keywords`, `include_keywords`, `reasoning`. See `examples/sample_request.json` / `examples/sample_response.json` for a full worked example.
- **External providers/libraries**: Groq (LLM) — `TaskType.INTENT_PLANNING`, primary `openai/gpt-oss-120b`, fallback `qwen/qwen3.6-27b`.
- **Durable and temporary side effects**: none — pure computation over the query string, no file/store writes.
- **Timeout and resource limits**: `timeout=30s` per LLM call attempt, `2` retries before falling back to the secondary model, `max_completion_tokens=3000`.
- **Retry and idempotency behavior**: retries the primary model up to its configured limit, then falls back to the secondary model. Naturally idempotent — re-planning the same query string produces an equivalent plan (LLM output is not byte-identical across calls, but the same real-world decisions are reached in practice; `temperature=0.0`).
- **Checkpoint/cancellation behavior**: **not implemented.** This stage runs to completion or fails; there is no mid-call cancellation hook. See `KNOWN_LIMITATIONS.md` in this folder.
- **Errors and retryability**: total failure (both primary and fallback fail) sets `summary["error"] = "intent_failed: ..."` and stops the pipeline — no partial plan is fabricated. Not currently retryable by the caller in a structured way (see the error-taxonomy gap in `KNOWN_LIMITATIONS.md`).
- **Test/fixture proving behavior**: `tests/unit/test_query_input.py` (request validation, `resolve_effective_limit()`). The planning LLM call itself is only exercised live, via `tests/integration/test_live_providers.py` (`pytest -m live`) — there is no offline/mocked test of `plan_query()`'s actual LLM output shape.
- **Known limitations and owner**: no non-LLM alternative implementation exists for this stage — see `KNOWN_LIMITATIONS.md`. No owner assigned beyond the Python team lead handling this handoff.

### Clarification layer (part of Stage 1)

If `needs_clarification` is true, the pipeline returns **before** Stage 2 or any paid API call runs. `main.py`'s `_plan_interactively()` prints the question(s), reads a typed terminal answer, and re-plans — capped at 3 rounds, then proceeds with the best-guess plan regardless.

---

## Stage 2 — Discovery & Scraping

- **Stage name and version**: Discovery & Scraping — versioned by git commit only.
- **Entry function/module**: `phase1_pipeline.run_pipeline()` main loop, calling `process_single_lead()` per candidate.
- **Current status**: Working.
- **Input schema and example**: the Stage 1 plan (see above).
- **Output schema and example**: one row per crawled page in `crawl_index.csv` (schema: `docs/DATA_CONTRACTS.md`), plus `summary["results"]` / `summary["qualified"]` lists in the returned summary dict.
- **External providers/libraries**: Serper.dev (discovery search + Places), native `requests`-style fetch, ScrapingBee (premium/JS-render fetch), `phase3.review_harvester` (Apify), `bi_providers.py` (social/decision-maker discovery), `tech_stack.py` (Stage 5, invoked inline), `storage_sync.py` (optional R2 sync).
- **Durable and temporary side effects**: commits `storage/<domain>/` artifacts and `crawl_index.csv` rows per business. Raw HTML is held in memory only, never written to disk (see `docs/ARCHITECTURE.md`'s pipeline diagram).
- **Timeout and resource limits**: `PREMIUM_MAX_CONCURRENCY = 2` concurrent ScrapingBee calls; overall scrape worker pool is `--concurrency` (CLI flag, default 5–10). Per-request fetch timeouts are set inside `phase1_pipeline.py`'s fetch helpers (native and premium tiers each have their own timeout constant).
- **Retry and idempotency behavior**: a per-domain circuit breaker opens the premium tier after 2 consecutive failures, falling back to native-fetch-only rather than retrying indefinitely. Re-running the same query does not re-scrape an already-committed business under the current `scoring_version`/industry/geo (see `docs/DATA_CONTRACTS.md`'s Re-run/idempotency section) — this is real idempotency at the *pipeline* level, not the job-request-level `Idempotency-Key` semantics the Laravel guide's Section 5 describes, which don't exist yet (see `KNOWN_LIMITATIONS.md`).
- **Checkpoint/cancellation behavior**: **not implemented.** A running discovery/scrape pass cannot be cancelled mid-run; there is no checkpoint to resume from if the process is killed (partial `storage/<domain>/` commits for already-finished businesses are retained, but the run itself doesn't track "resume from here").
- **Errors and retryability**: a genuinely empty provider response is distinguished from a request failure (network/timeout/non-200) — failures are never cached as false negatives, so the next run retries automatically. See `docs/DATA_CONTRACTS.md`'s cache-correctness table.
- **Test/fixture proving behavior**: `tests/unit/test_discovery_gate.py` (relevance scoring), `tests/mocked/test_cache_correctness.py` (transient-failure-vs-empty), `tests/mocked/test_review_escalation.py`, `tests/unit/test_windows_paths.py` (storage path handling).
- **Known limitations and owner**: `robots.txt` disallow rules are not enforced (fetched only for sitemap discovery) — see `KNOWN_LIMITATIONS.md`.

---

## Stage 3 — Route/Page Selection

- **Stage name and version**: Route/Page Selection — versioned by git commit only.
- **Entry function/module**: `route_planner.plan_routes()`.
- **Current status**: Working.
- **Input schema and example**: a committed domain's crawled pages (from `storage/<domain>/`) plus the original query.
- **Output schema and example**: `{"selected_pages": [{url, txt_path, priority, reason}], "confidence": "high"|"medium"|"low"}`.
- **External providers/libraries**: none for the common path (local BM25 + embedding search over already-crawled pages, via `page_retrieval.py`). Groq LLM only as a rare fallback (`TaskType.ROUTE_PLANNING`) when retrieval finds nothing usable.
- **Durable and temporary side effects**: none — reads already-committed page text, writes nothing new.
- **Timeout and resource limits**: LLM fallback path only: standard Tier 1 settings (`max_completion_tokens=500`). The common, non-LLM path has no explicit timeout (bounded by local index size, milliseconds to a few seconds).
- **Retry and idempotency behavior**: deterministic given the same crawled pages — re-running produces the same selection (no randomness in the search-first path).
- **Checkpoint/cancellation behavior**: not implemented — same as all other stages; not applicable in practice given this stage's short, bounded runtime.
- **Errors and retryability**: a domain with only 1 crawled page uses simple heuristics instead of building a full search index. No documented failure mode raises to the caller.
- **Test/fixture proving behavior**: **none.** No dedicated unit or mocked test exists for `route_planner.plan_routes()` today — flagged here rather than silently left out. Only indirectly exercised by whatever calls it during a live end-to-end run.
- **Known limitations and owner**: test coverage gap noted above; no owner assigned yet.

---

## Stage 4 — Final Reasoning

- **Stage name and version**: Final Reasoning — versioned by git commit only.
- **Entry function/module**: `final_reasoning.answer_query()`. Runs only when Stage 1's `intent` is `"find_and_filter"`.
- **Current status**: Working.
- **Input schema and example**: domain, the original query.
- **Output schema and example**: `{"answer": str, "source_pages": [...], "confidence": "high"|"medium"|"low"}`.
- **External providers/libraries**: Groq LLM — `TaskType.FINAL_REASONING`, `openai/gpt-oss-120b`, reasoning effort `medium`.
- **Durable and temporary side effects**: none — reads already-committed page text, returns an answer, writes nothing.
- **Timeout and resource limits**: `timeout=30s`, `max_completion_tokens=1500`, `temperature=0.15`.
- **Retry and idempotency behavior**: standard Tier 2 retry/fallback (see Stage 1). Idempotent in the same sense as Stage 1 — same inputs, equivalent (not byte-identical) output.
- **Checkpoint/cancellation behavior**: not implemented.
- **Errors and retryability**: retrieval failure or no page text available both degrade to a `"low"`-confidence empty result with a `reason` field — never raises to the caller.
- **Test/fixture proving behavior**: **none dedicated.** No unit/mocked test exercises `answer_query()` directly; only reachable via a live end-to-end run today. This is a real coverage gap, not an oversight being hidden.
- **Known limitations and owner**: same test-coverage gap as Stage 3; no non-LLM alternative exists for this stage (see `KNOWN_LIMITATIONS.md`).

---

## Stage 5 — Tech-Stack Detection

- **Stage name and version**: Tech-Stack Detection — versioned by git commit; vendored `wappalyzer` copy versioned separately, see `vendor/wappalyzer/README.md`.
- **Entry function/module**: `tech_stack.py`, entry point `analyze_raw_html()`. Runs only when Stage 1's `needs_tech_stack` is true.
- **Current status**: Working.
- **Input schema and example**: a domain's already-fetched HTML/headers (no new fetch for the integrated path).
- **Output schema and example**: `{"raw_wappalyzer": {...}, "normalized_tech_stack": {...}, "sales_signals": [...]}`.
- **External providers/libraries**: none over the network for the legacy/extended engines (pure local fingerprint matching); one DNS lookup and one `robots.txt` fetch as small, bounded extras.
- **Durable and temporary side effects**: writes `storage/<domain>/tech_stack.json`.
- **Timeout and resource limits**: bounded by the one DNS lookup + one small `robots.txt` fetch; no separate configured timeout constant beyond those two calls' own defaults.
- **Retry and idempotency behavior**: each of the 4 detection engines (legacy Wappalyzer, extended/cookie-DOM-JS, DNS, robots.txt) is wrapped independently — a failure in one only reduces signal count, never breaks the whole profile. Deterministic given the same input HTML.
- **Checkpoint/cancellation behavior**: not implemented; not meaningful given this stage's short runtime.
- **Errors and retryability**: no documented failure mode raises to the caller — degrades to fewer detected technologies instead.
- **Test/fixture proving behavior**: `tests/unit/test_wappalyzer_patches.py` (3 tests — package integrity, fingerprint compilation, patch idempotency). No dedicated test of `analyze_raw_html()`'s end-to-end merge logic against a real HTML fixture.
- **Known limitations and owner**: see the Wappalyzer package-reproducibility RESOLVED entry in `docs/KNOWN_LIMITATIONS.md` and `vendor/wappalyzer/README.md` for full incident history.

---

## Stage 6 — Evidence Index / Retrieval (RAG)

- **Stage name and version**: Evidence Index / Retrieval — versioned by git commit only. Skippable via `--no-rag`.
- **Entry function/module**: `rag/ingest_and_answer.run()`.
- **Current status**: Working.
- **Input schema and example**: already-committed page/review text from Stage 2.
- **Output schema and example**: a populated Chroma vector store (`rag/.chroma/`); ranked source chunks with citations returned to callers, not a synthesized answer.
- **External providers/libraries**: `sentence-transformers/all-MiniLM-L6-v2` — local embedding model, 384 dimensions, CPU by default (falls back to CUDA automatically if available). No network call. Chroma for local vector storage.
- **Durable and temporary side effects**: writes/updates `rag/.chroma/`.
- **Timeout and resource limits**: none explicit — bounded by local compute (embedding a few hundred to a few thousand chunks, seconds to low tens of seconds on CPU).
- **Retry and idempotency behavior**: re-running re-embeds from already-scraped text; no re-scraping needed. Deterministic given the same source text and model version.
- **Checkpoint/cancellation behavior**: not implemented. Rebuild process: delete `rag/.chroma/` and re-run — see `docs/RUNBOOK.md`'s cache-clearing table.
- **Errors and retryability**: no documented failure mode raises to the caller.
- **Test/fixture proving behavior**: **none dedicated.** No unit/mocked test exercises the embedding/retrieval pipeline directly.
- **Known limitations and owner**: domain-key consistency (bare domain vs. full URL) was a real bug, fixed — preserve this fix in any reimplementation (see `docs/DATA_CONTRACTS.md`). Test-coverage gap noted above.

---

## Stage 7 — Google Maps Enrichment

- **Stage name and version**: Google Maps Enrichment — versioned by git commit only. Skippable via `--no-maps`.
- **Entry function/module**: `phase3.google_maps.enrich()`.
- **Current status**: Working.
- **Input schema and example**: `leads_clean.csv`.
- **Output schema and example**: `leads_with_maps.csv` with `maps_rating`, `maps_rating_count`, `maps_category`, `maps_matched` columns added.
- **External providers/libraries**: Serper Places API.
- **Durable and temporary side effects**: writes `leads_with_maps.csv`; caches per-domain match result at `storage/<domain>/reviews/google_maps.json`.
- **Timeout and resource limits**: governed by the Serper.dev plan's own quota/timeout; not separately throttled by this codebase.
- **Retry and idempotency behavior**: a failed request (network/timeout/non-200) is never cached as a permanent no-match — retried on the next run. Matching hierarchy: address match → phone match (last-9-digit) → name-only fallback (only when Serper returns exactly one candidate, tagged `confidence: "low"`); `match_basis` records which tier confirmed each result.
- **Checkpoint/cancellation behavior**: not implemented.
- **Errors and retryability**: see cache-correctness table in `docs/DATA_CONTRACTS.md` — request failures are structurally distinct from genuine empty results.
- **Test/fixture proving behavior**: `tests/mocked/test_maps_matching.py` (12 tests — matching hierarchy + `find_place()` orchestration), `tests/mocked/test_cache_correctness.py` (transient-failure-vs-empty).
- **Known limitations and owner**: none beyond general Serper quota/availability risk.

---

## Stage 8 — Accuracy Audit

- **Stage name and version**: Accuracy Audit — versioned by git commit only; runs automatically as the last step of `main.py`, also runnable standalone.
- **Entry function/module**: `accuracy_check.py`.
- **Current status**: Working.
- **Input schema and example**: `leads_with_maps.csv` (or `leads_clean.csv` if Stage 7 was skipped) plus every `storage/<domain>/reviews/*.json`.
- **Output schema and example**: `accuracy_report.txt` — free-text, flag-prefixed (`[field]`, `[identity]`, `[review]`), not currently a structured/JSON report.
- **External providers/libraries**: none — pure rule-based checks.
- **Durable and temporary side effects**: writes `accuracy_report.txt` (overwritten each run, no history retained).
- **Timeout and resource limits**: none explicit — entirely local, read-only over already-committed data.
- **Retry and idempotency behavior**: never re-scrapes; safe and cheap to re-run after every query. Fully idempotent given the same input CSVs/JSON files.
- **Checkpoint/cancellation behavior**: not implemented; not meaningful given this stage's short, read-only runtime.
- **Errors and retryability**: no documented failure mode raises to the caller.
- **Test/fixture proving behavior**: `tests/smoke/test_offline_smoke.py` (accuracy-check functions against fixtures, including `tests/fixtures/leads_with_maps_sample.csv` and `tests/fixtures/fixture_storage/`).
- **Known limitations and owner**: Roman Urdu/code-switched review text isn't reliably language-detected (see `KNOWN_LIMITATIONS.md`); review-relevance grounding intentionally skips listing-based platforms (Google Reviews) — see `docs/ARCHITECTURE.md`'s Stage 8 for the reasoning.
