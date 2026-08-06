# Runbook

Day-to-day operation, cache/state layout, and troubleshooting for real failure modes observed running this pipeline.

## Running a query

```powershell
python main.py --query "find 3 dental clinics in Islamabad with no online booking"
```

If the pipeline stops and prints numbered questions under `"A bit more detail would help before I search:"`, type an answer at the `Your answer:` prompt and press Enter — it continues automatically in the same terminal session. No scraping happens before this resolves.

Re-running Step 8 (accuracy audit) alone, without a new query:
```powershell
python accuracy_check.py --geo "Islamabad"
```

## Wappalyzer (tech-stack detection)

No post-install step needed — `wappalyzer` is vendored directly into `vendor/wappalyzer/` (a working, pre-patched copy) rather than pip-installed, so Stage 5 works immediately after `pip install -r requirements.txt`. See `docs/KNOWN_LIMITATIONS.md`'s RESOLVED entry for why this changed from a required patch step to a vendored dependency.

`scripts/patch_wappalyzer.py` still exists, but only as the tool used to re-vendor a newer upstream release in the future — it is not something a developer needs to run after a normal install:

```powershell
python scripts/patch_wappalyzer.py
```

Idempotent — safe to run repeatedly. Prints `PATCHED`/`SKIP` per fix and ends with a verification pass (compile-tests all 1,270 technology fingerprints, confirms the config module loads). See `vendor/wappalyzer/README.md` for the full re-vendoring procedure.

## Clearing cache / state safely

The pipeline caches aggressively so a re-run of the same query doesn't repeat expensive work. To force a genuinely fresh run, remove **only** what you intend to reset:

| To reset | Remove |
|---|---|
| Everything (full reset) | `storage/`, `crawl_index.csv`, `last_run.json`, `search_state.json`, `site_classifications.json`, `leads_clean.*`, `leads_with_maps.csv`, `leads_quarantine.csv`, `rag/.chroma/`, `__pycache__/` dirs, `.tldextract-cache/` |
| One business only | Delete `storage/<domain>/` and its rows in `crawl_index.csv` — do not touch other domains' folders |
| One review platform's cache for one business | Delete `storage/<domain>/reviews/<platform>.json` (e.g. `reddit.json`) — every other platform and the rest of the business's data is untouched |
| Google Maps match cache only | Delete `storage/<domain>/reviews/google_maps.json` |
| RAG/evidence index only | Delete `rag/.chroma/` — the next `main.py` run re-embeds from already-scraped page/review text, no re-scraping needed |

**Always confirm scope before deleting** — this pipeline treats a cache-clear as a real, sometimes-costly action (re-scraping and re-calling paid APIs), not a trivial reset.

## Interpreting `accuracy_report.txt`

Each flag is prefixed `[field]`, `[identity]`, or `[review]` (see `docs/ARCHITECTURE.md`'s Stage 8). Flags are informational, not automatic filters — a flagged review still gets embedded into the evidence index; the report exists to tell you what's worth a manual look, not to silently drop data.

## Known failure modes and how to read them

| Log message | Meaning | Action |
|---|---|---|
| `"Monthly API calls limit reached: 1000"` (from ScrapingBee) | Your premium-fetch-tier quota is exhausted for the billing period. Native-only fetching continues; sites that need JS-render/anti-bot bypass will fail until the plan resets or is upgraded. | Not a code bug — wait for reset or upgrade the plan. |
| `Cannot connect to host ... [getaddrinfo failed]` | DNS resolution failure — observed repeatedly on the development machine, unrelated to this codebase. | Retry; if it's a real outage, check the machine's DNS resolver (e.g. point at `8.8.8.8`/`1.1.1.1`). |
| `Rate limit reached for model ... on tokens per minute (TPM)` (from Groq) | The LLM provider's per-minute token quota was hit. The pipeline already retries and falls back to the secondary model automatically — this is visible in the log, not a failure requiring action, unless it happens on every call. | If persistent, check the Groq account's plan/quota. |
| `"Premium scraper failed 2 consecutive time(s) for <domain> — opening circuit breaker"` | Expected behavior, not an error — stops retrying a domain that's reliably failing, switches to native-fetch-only for its remaining pages. | No action needed. |
| `UnicodeEncodeError: 'charmap' codec can't encode character ...` | Windows console defaults to cp1252; LLM-generated text (em-dashes, smart quotes) can't print. Already fixed in `main.py` (stdout/stderr reconfigured to UTF-8 at startup) — should not occur when running via `main.py`. If it appears in a script you wrote yourself, add `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` near the top. | Fixed in the supported entry point; only relevant if writing new ad-hoc scripts. |
| Any `google_maps`/`reddit` review file with `"reason": "... request failed ..."` and no `matched` | A transient failure was correctly **not** cached (see `docs/ARCHITECTURE.md` Stage 2/7 resilience notes) — the next run will retry automatically. | No action needed; re-run to retry. |

## Logging & observability

Current, honest state — not a target design:

- Standard library `logging`, one named logger per module (e.g. `Phase1Engine`, `ai_bdm.main`, `ai_bdm.llm_planner`), `StreamHandler` to stdout/stderr, level `INFO` by default. Format is `%(asctime)s [%(levelname)s] %(message)s` — timestamp, level, and message; the module name is only implicit in which logger emitted it, not a separate structured field.
- **Gap vs. a production-service expectation**: there is no job/correlation ID, no per-stage/duration field, and no structured (JSON) log output today — every log line is free-text, not machine-parseable. A Laravel-facing service wrapper would need to inject its own correlation ID (e.g. via `logging.LoggerAdapter` or a `contextvars`-based filter) rather than assume one already exists here.
- **Retry/backoff, documented per stage in `docs/ARCHITECTURE.md`**: LLM calls (`LLM_planner.call_llm`) retry the primary model up to its configured limit, then fall back to `qwen/qwen3.6-27b`; the premium (ScrapingBee) fetch tier opens a per-domain circuit breaker after 2 consecutive failures rather than retrying indefinitely; Google Maps/review-provider request failures are distinguished from genuine empty results and are never cached as false negatives (see `docs/DATA_CONTRACTS.md`'s cache-correctness table).
- **Secrets**: no code path logs a raw API key, bearer token, or `.env` value — provider clients receive credentials directly from `os.environ`/`python-dotenv`, never through a logged string. Not independently audited line-by-line; this reflects the pattern used throughout, not a guarantee.
- **Tracing/error reporting**: none is integrated (no Sentry/OpenTelemetry/etc.) — there is nothing to disable in local development because nothing external-facing is enabled by default.

## Windows-specific notes

- Stored file paths in `crawl_index.csv` use backslash separators (`storage\<domain>\home.txt`) — code that splits on `/` alone to get a basename will silently fail to match anything on Windows (this was a real, previously-shipped bug in `final_reasoning.py`, now fixed by splitting on `[\\/]` instead). Any new code reading these paths should do the same.
- Object storage (R2) keys always use forward slashes regardless of host OS — `storage_sync.py` uses `Path.as_posix()` when building remote keys for exactly this reason (a real bug, found and fixed this session, that broke folder hierarchy in the actual bucket on Windows).
