# Developer Handoff Summary

Filled-out submission template, combining both handoff guides' formats. Every value below is real, taken from this repository's actual code/config — none of it is a placeholder.

**Note on a bug found and fixed during this handoff — see `docs/KNOWN_LIMITATIONS.md`'s RESOLVED entry for the full story.** During this handoff's own fresh-install verification (2026-08-06), `wappalyzer==2.0.1`'s pin was found to not actually guarantee reproducible package contents — PyPI was serving a different, incompatible build under that same version string than what this development machine had. Fixed by vendoring a working, pre-patched copy directly into this repository at `vendor/wappalyzer/` and removing `wappalyzer` from `requirements.txt` entirely. Re-verified from a genuinely fresh install: Stage 5 (tech-stack detection) now works with zero manual post-install steps.

| Field | Value |
|---|---|
| Python version | 3.12 (developed and tested on 3.12.10) |
| Supported operating system(s) | Windows 11 (developed and tested). Not Windows-only by design, but not separately verified on Linux/macOS. |
| Dependency install command | `pip install -r requirements.txt` — see below for two required post-install steps |
| Post-install (required) | `pip uninstall -y pyarrow datasets` (Windows segfault avoidance) only. Wappalyzer (tech-stack detection) no longer needs a post-install patch step — it's vendored at `vendor/wappalyzer/` and works out of the box; see `docs/KNOWN_LIMITATIONS.md`. |
| Primary run command | `python main.py --query "find 3 dental clinics in Islamabad with no online booking"` |
| Main entry point | `main.py` (CLI, full 8-stage pipeline). `api.py` is a secondary, **partial** entry point — Stages 1–2 only, see `docs/INTEGRATION_NOTES.md`. |
| Offline smoke-test command | `pytest -q tests/smoke` (14 tests) — no network, no API keys |
| Full offline+mocked test command | `pytest -q` (62 tests — smoke + unit + mocked; live-provider tests are excluded by default via `pytest.ini`) |
| Opt-in live provider check | `pytest -m live tests/integration` — costs real quota/credit (documented per-test in the file itself); not part of any default run |
| Required external services | Groq (LLM — every planning/reasoning stage depends on this; there is no non-LLM alternative implementation today), Serper.dev (discovery + Maps) |
| Recommended external services | ScrapingBee (`.env` var `zenrows`) — premium/JS-render fetch tier |
| Optional external services | Apify (Google review text + Reddit mentions), GitHub API, YouTube Data API, Cloudflare R2 (remote sync) |
| Expected sample output | `examples/sample_request.json` → `examples/sample_response.json` (real schema, matches `api.py`'s current `POST /api/v1/pipeline/run` contract) |
| Storage/output locations | `storage/<domain>/` (per-business artifacts), `crawl_index.csv` (per-page index), `leads_clean.csv`/`leads_with_maps.csv` (per-business rollup), `accuracy_report.txt` (Stage 8 output), `rag/.chroma/` (evidence index) — full schema in `docs/DATA_CONTRACTS.md` |
| Backend/Laravel handoff structure | [`docs/backend-handoff/`](backend-handoff/) — the exact folder Section 7 of the Laravel Backend Construction Handoff Guide requires. Contains `PIPELINE_STAGES.md`, `PROVIDERS_AND_LIMITS.md`, `ARTIFACTS_AND_STORAGE.md`, `KNOWN_LIMITATIONS.md`, `TEST_RESULTS.txt`, and `fixtures/`. `JOB_EXECUTION.md` and `schemas/*.json` are present as deferral stubs, not silently missing — the versioned job request/progress/result/error contract (guide Section 4.3–4.6) is explicitly out of scope for this handoff pass by the Python team lead's decision. |
| Working pipeline stages | 8 of 8 confirmed on a genuinely fresh install as of 2026-08-06 (planning, discovery/scraping, routing, final reasoning, tech-stack detection, evidence retrieval, Maps enrichment, accuracy audit). Stage 5 (tech-stack detection) was briefly broken on a fresh install due to the Wappalyzer reproducibility issue noted above — now fixed via vendoring. See `docs/ARCHITECTURE.md` for exact per-stage contracts. |
| Partial stages | `api.py` (Stages 1–2 only, not 3–8 — a real, documented gap, not a secret one) |
| Placeholder stages | `bi_providers.py`'s Apify BI provider (`APIFY_ENABLED=false` by default, registered but not implemented) |
| Disabled/future stages | None currently disabled by flag; LLM-based outreach/email-drafting is explicitly out of scope (no such capability exists in this codebase at all — confirmed, not merely turned off) |
| Known failing tests | None. All 62 offline/mocked tests and all 4 live-provider tests pass, both on this development machine and on a genuinely fresh install (verified live 2026-08-06, after the Wappalyzer vendoring fix landed). |
| Known limitations | See `docs/KNOWN_LIMITATIONS.md` in full — headline items: Yelp/Trustpilot bot-protection unresolved, Dockwa JS-rendered reviews unresolved, Roman Urdu language-detection gap, ScrapingBee quota exhaustion risk, no non-LLM query-planning alternative exists |
| Correction to an earlier handoff draft | An earlier internal handoff document ("Python_Code_Handoff_Guide_Current_Non_LLM_System.docx") incorrectly stated no LLM was integrated. That was factually wrong — verified live throughout this handoff work (real Groq API calls, real cost tracking). A later, corrected document ("Python_Code_Handoff_and_Execution_Guide.docx") properly documents LLM usage as a normal, required, versioned part of the system, and this handoff follows that corrected version. |
| Snapshot identity | Repository: `https://github.com/chirp-io/AI-BDM` (delivery target), branch `main`, commit `46b653a62a6dfc9fba72594a6c1f4ec28285c578`. Also mirrored at `https://github.com/afraziaumer/AI-BDM`, branch `afrazia-updates`, same commit. |
| Archive (if a ZIP is needed instead of repo access) | Build fresh via `git archive --format=zip -o ai-bdm-handoff.zip HEAD` from the commit above — guarantees only git-tracked files are included, so no gitignored secret/generated file can leak in by construction. Independently re-verified from a genuinely fresh venv (fresh install, no manual post-install steps): full suite `pytest -q` — 62 passed, 4 deselected. This same fresh-install process is what originally surfaced the Wappalyzer package-reproducibility bug (see the RESOLVED entry in `docs/KNOWN_LIMITATIONS.md`) and, on a later fresh-install re-check, a second real gap (`pkg_resources` not guaranteed present) — both now fixed and re-verified. |
| Primary developer contact | afraz1a — afraziaumer@gmail.com |
