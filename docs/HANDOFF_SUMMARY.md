# Developer Handoff Summary

Filled-out submission template, combining both handoff guides' formats. Every value below is real, taken from this repository's actual code/config — none of it is a placeholder.

**Read `docs/KNOWN_LIMITATIONS.md`'s CRITICAL entry before relying on this handoff.** Discovered during this handoff's own fresh-install verification (2026-08-06): `wappalyzer==2.0.1`'s pin does not actually guarantee reproducible package contents — PyPI is currently serving a different, incompatible build under that same version string than what this development machine has. Stage 5 (tech-stack detection) does not work on a genuinely fresh install as a result. This is not fixed yet — see the limitations doc for the two real fix options.

| Field | Value |
|---|---|
| Python version | 3.12 (developed and tested on 3.12.10) |
| Supported operating system(s) | Windows 11 (developed and tested). Not Windows-only by design, but not separately verified on Linux/macOS. |
| Dependency install command | `pip install -r requirements.txt` — see below for two required post-install steps |
| Post-install (required) | `pip uninstall -y pyarrow datasets` (Windows segfault avoidance) **and** `python scripts/patch_wappalyzer.py` (reproducible third-party patch — the one blocking acceptance condition, see `docs/KNOWN_LIMITATIONS.md`) |
| Primary run command | `python main.py --query "find 3 dental clinics in Islamabad with no online booking"` |
| Main entry point | `main.py` (CLI, full 8-stage pipeline). `api.py` is a secondary, **partial** entry point — Stages 1–2 only, see `docs/INTEGRATION_NOTES.md`. |
| Offline smoke-test command | `pytest -q tests/smoke` (14 tests) — no network, no API keys |
| Full offline+mocked test command | `pytest -q` (62 tests — smoke + unit + mocked; live-provider tests are excluded by default via `pytest.ini`) |
| Opt-in live provider check | `pytest -m live tests/integration` — costs real quota/credit (documented per-test in the file itself); not part of any default run |
| Required external services | Groq (LLM — every planning/reasoning stage depends on this; there is no non-LLM alternative implementation today), Serper.dev (discovery + Maps) |
| Recommended external services | ScrapingBee (`.env` var `zenrows`) — premium/JS-render fetch tier |
| Optional external services | Apify (Google review text + Reddit mentions), GitHub API, YouTube Data API, Cloudflare R2 (remote sync) |
| Expected sample output | `examples/sample_request.json` → `examples/sample_response.json` (real schema, matches `api.py`'s current `POST /pipeline/run` contract) |
| Storage/output locations | `storage/<domain>/` (per-business artifacts), `crawl_index.csv` (per-page index), `leads_clean.csv`/`leads_with_maps.csv` (per-business rollup), `accuracy_report.txt` (Stage 8 output), `rag/.chroma/` (evidence index) — full schema in `docs/DATA_CONTRACTS.md` |
| Working pipeline stages | 7 of 8 confirmed on this development machine's current environment (planning, discovery/scraping, routing, final reasoning, evidence retrieval, Maps enrichment, accuracy audit). Stage 5 (tech-stack detection) is confirmed **broken on a genuinely fresh install** as of 2026-08-06 — see the CRITICAL note above. See `docs/ARCHITECTURE.md` for exact per-stage contracts regardless. |
| Partial stages | `api.py` (Stages 1–2 only, not 3–8 — a real, documented gap, not a secret one) |
| Placeholder stages | `bi_providers.py`'s Apify BI provider (`APIFY_ENABLED=false` by default, registered but not implemented) |
| Disabled/future stages | None currently disabled by flag; LLM-based outreach/email-drafting is explicitly out of scope (no such capability exists in this codebase at all — confirmed, not merely turned off) |
| Known failing tests | On THIS development machine: none — all 62 offline/mocked tests pass, all 4 live-provider tests pass against real credentials. On a genuinely FRESH install (verified live 2026-08-06): `tests/unit/test_wappalyzer_patches.py`'s 3 tests correctly fail until the CRITICAL package-incompatibility issue above is resolved — this is the test suite doing its job, not a test bug. |
| Known limitations | See `docs/KNOWN_LIMITATIONS.md` in full — headline items: Yelp/Trustpilot bot-protection unresolved, Dockwa JS-rendered reviews unresolved, Roman Urdu language-detection gap, ScrapingBee quota exhaustion risk, no non-LLM query-planning alternative exists |
| Correction to an earlier handoff draft | An earlier internal handoff document ("Python_Code_Handoff_Guide_Current_Non_LLM_System.docx") incorrectly stated no LLM was integrated. That was factually wrong — verified live throughout this handoff work (real Groq API calls, real cost tracking). A later, corrected document ("Python_Code_Handoff_and_Execution_Guide.docx") properly documents LLM usage as a normal, required, versioned part of the system, and this handoff follows that corrected version. |
| Snapshot identity | Repository: `https://github.com/afraziaumer/AI-BDM`, branch `afrazia-updates`, commit `ee6868fba0eba325fce1a5569192d8bc843bccb8` |
| Archive (if a ZIP is needed instead of repo access) | `ai-bdm-python-non-llm-handoff-2026-08-06.zip`, built via `git archive` from the commit above — guarantees only git-tracked files are included, so no gitignored secret/generated file can leak in by construction. Independently re-verified: extracted to a clean location, fresh virtual environment, full dependency install (clean, no errors), `pytest -q tests/smoke` — 14/14 passing. Running the FULL suite (`pytest -q`) on that same fresh install is what surfaced the CRITICAL Wappalyzer finding above — the verification process itself is what caught this, not a hypothetical concern. |
| Primary developer contact | _fill in — name/email of whoever is answering questions about this handoff_ |
