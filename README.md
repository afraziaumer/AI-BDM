# AI-BDM — AI-Powered B2B Lead Generation Pipeline

Given one natural-language query (e.g. *"find 3 dental clinics in Islamabad with no online booking"*), AI-BDM plans a search, discovers and scrapes real business websites, verifies each business is a genuine relevant match, harvests public reviews/mentions, detects each business's technology stack, builds a searchable evidence index grounded in the real scraped text, cross-verifies against Google Maps, and runs an automated accuracy audit over its own output.

This document is the canonical, exact-commands reference for running AI-BDM on a clean machine. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for what each pipeline stage does internally, [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for day-to-day operation, and [`docs/INTEGRATION_NOTES.md`](docs/INTEGRATION_NOTES.md) for how an external system (e.g. a Laravel backend) is meant to call into this project.

**Backend/Laravel integration reviewer — start at [`docs/backend-handoff/`](docs/backend-handoff/).** That folder is the required handoff structure per the Laravel Backend Construction Handoff Guide: a per-stage pipeline record (`PIPELINE_STAGES.md`), a consolidated provider/quota table (`PROVIDERS_AND_LIMITS.md`), storage/artifact contracts (`ARTIFACTS_AND_STORAGE.md`), backend-scoped known limitations (`KNOWN_LIMITATIONS.md`), and the latest test results (`TEST_RESULTS.txt`). The versioned job request/progress/result/error contract (Section 4.3–4.6 of that guide) is explicitly deferred — see that folder's `JOB_EXECUTION.md` and `schemas/README.md` for why, rather than treating their absence as an oversight.

## Runtime

- **Python**: 3.12 (developed and tested on 3.12.10). No `pyproject.toml`/version pin file exists yet — pin your virtual environment to 3.12 explicitly.
- **Operating system**: developed and tested on **Windows** (Windows 11). Nothing in the codebase is Windows-only by design, but several real, previously-live bugs were Windows-specific (backslash path separators, cp1252 console encoding) and are called out in [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — a Linux/macOS run has not been separately verified.
- **Architecture**: x86_64. No GPU is required; `sentence-transformers` runs the embedding model on CPU by default (falls back to CUDA automatically if available — logged as `"No device provided, using cuda:0"` when it is).

## Installation

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

```bash
# Linux/macOS (not separately verified, provided for completeness)
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Known post-install step (required)

`sentence-transformers` pulls in `pyarrow`/`datasets` as transitive dependencies. On Windows, importing either in the same process as the scraper causes an immediate, untrappable native segfault (not a Python exception — no amount of `try`/`except` guards against it). After installing requirements, run:

```powershell
pip uninstall -y pyarrow datasets
```

This is safe — nothing in this codebase imports either package directly.

### Wappalyzer (tech-stack detection) — vendored, no post-install step needed

`wappalyzer` is **not** pip-installed (deliberately not in `requirements.txt`). Confirmed live: PyPI currently serves a different, incompatible build under the `2.0.1` version string than what this codebase was built against, so a version pin alone doesn't guarantee reproducible behavior. Instead, a working, pre-patched copy (three real bugs fixed: BeautifulSoup parser, UTF-8 file encoding, a broken Symfony fingerprint regex) is committed directly at [`vendor/wappalyzer/`](vendor/wappalyzer/), and `tech_stack.py` adds `vendor/` to `sys.path` at import time so `import wappalyzer` always resolves to that copy. Nothing to install or patch after `pip install -r requirements.txt` — Step 5 (tech-stack detection) works out of the box. See [`vendor/wappalyzer/README.md`](vendor/wappalyzer/README.md) for provenance and the update procedure, and [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) for the full incident history.

## Entry points

| Command | What it runs |
|---|---|
| `python main.py --query "..."` | The full 8-step CLI pipeline (see below). This is the primary, supported entry point. |
| `python accuracy_check.py --geo "..."` | Re-runs Step 8 (accuracy audit) standalone against whatever was last committed, without re-scraping. Runs automatically as part of `main.py` already — this is for re-checking without a new query. |
| `python -m uvicorn api:app --reload --port 8000` | Optional FastAPI wrapper, versioned under `/api/v1`. **Partial** — exposes only Steps 1–2 (`POST /api/v1/pipeline/run`) plus read-only, cursor-paginated `/api/v1/leads` endpoints. Does not expose Steps 3–8. See [`docs/INTEGRATION_NOTES.md`](docs/INTEGRATION_NOTES.md). |

```powershell
python main.py --query "find 3 dental clinics in Islamabad with no online booking"
```

Flags:
- `--concurrency N` — parallel scrape workers for Step 2 (default 10)
- `--no-rag` — skip Step 6 (evidence chunking/retrieval)
- `--no-maps` — skip Step 7 (Google Maps enrichment)

Step 8 (accuracy check) always runs automatically at the end of `main.py` — there is no flag to skip it.

### Interactive clarification

If the query is missing something that would materially change the results (location too broad, category missing, ambiguous business segmentation, or no criteria at all), the pipeline stops **before any discovery/scraping happens**, prints the question(s) in the terminal, and waits for a typed answer in the same session — up to 3 rounds before proceeding with its best guess regardless. This is not a separate mode; it happens automatically as part of `python main.py --query "..."`.

## Initialization

No database migrations, seed data, or one-time downloads are required for the core pipeline. On first run, `sentence-transformers` downloads the `all-MiniLM-L6-v2` embedding model automatically (cached locally afterward — no action needed).

Optional, only if you use the standalone `async_scraper/` module (not used by `main.py`'s pipeline):
```powershell
playwright install chromium
```

## Verification

```powershell
pytest -q
```

Expected: all offline tests pass, no network calls, no API keys required. `pytest.ini` automatically excludes the separately-marked live tests (see below) — a bare `pytest` run never touches a real credential.

| Suite | What it proves | Live services? |
|---|---|---|
| `tests/smoke/` | Core modules import cleanly; Stage 8's accuracy audit runs end-to-end against fixtures | No |
| `tests/unit/` | Request validation, count/limit resolution, discovery-gate scoring, homepage-selection, Windows path handling, Wappalyzer patch verification | No |
| `tests/mocked/` | Review-escalation logic, transient-failure-vs-genuine-empty caching, Google Maps matching hierarchy — real logic, provider HTTP calls replaced with fakes | No (mocked) |
| `tests/integration/` | Real credential/quota validity for Groq, ScrapingBee, Apify, Serper | **Yes — run explicitly** |

No lint (`ruff`/`black`) or type-check (`mypy`/`pyright`) tooling is configured in this repository — stated explicitly rather than left to silent absence.

To run the live provider checks (only when you actually want to spend real quota verifying credentials — the Serper check costs 1 real search credit, the others are free account-info lookups):
```powershell
pytest -m live tests/integration
```

To verify the real, live end-to-end pipeline (requires real API keys in `.env` — see `.env.example`):
```powershell
python main.py --query "find 3 dental clinics in Islamabad with no online booking"
```
Expected output ends with a `PHASE 1 OUTPUT RETURN` block listing qualified leads, followed by `STEP 3` through `STEP 8` headers, and `accuracy_report.txt` being written.

## External services

| Service | Required? | Purpose |
|---|---|---|
| Groq (LLM) | **Required** | All planning/reasoning stages (Step 1 planning, clarification questions, Step 3 route-planning fallback, Step 4 final reasoning, relevance-scoring safety net, moderation) |
| Serper.dev | **Required** | Discovery search (Step 2), Google Maps/Places (Step 7), review-platform discovery searches |
| ScrapingBee (`.env` var `zenrows`) | Recommended | Premium/JS-render fetch tier for sites that block plain requests; native-only fetching still runs without it but many real sites will fail to scrape |
| Apify | Optional | Google Maps review text + Reddit mention harvesting; those two sources silently return nothing without it |
| GitHub / YouTube APIs | Optional | Social-presence enrichment; degrade to "not found" without them |
| Cloudflare R2 | Optional | Remote sync of each committed business's local storage folder; silently disabled without credentials |

Full variable names and defaults: [`.env.example`](.env.example).

## Resources

Observed on this development machine during real runs this session: single-digit percent CPU most of the time (I/O-bound — scraping/API calls dominate), a brief CPU spike loading the embedding model (a few seconds), under 1GB RAM for the pipeline process itself. A 3-business query typically completes in 2–10 minutes depending on how many sites need the premium fetch tier and how many review platforms match; this varies a lot with target-site responsiveness and is not a hard bound.

## Known issues

See [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) if present, or the "Known Limitations" section of `AI-BDM_Master_Progress_Report.docx` for the full, current list (Yelp/Trustpilot bot-protection, Dockwa JS-rendered reviews, Roman Urdu/code-switched language detection, ScrapingBee monthly quota).
