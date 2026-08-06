# Vendored `wappalyzer` (pre-patched)

**Why this exists**: confirmed live on 2026-08-06 that PyPI serves different, incompatible code under the exact same `wappalyzer==2.0.1` version string over time — the build this project was originally developed against has a top-level `Wappalyzer.py` with a `Wappalyzer.latest()` classmethod; the build PyPI serves today does not (different module layout entirely, a `Wappalyzer` class with `analyze`/`analyze_many`/`close` instead). Pinning the version number does not guarantee reproducible code for this package. `tech_stack.py` therefore imports this vendored copy (via a `sys.path` shim at the top of that file) instead of relying on `pip install wappalyzer`.

**What's included** — only the files this project's actual usage needs (confirmed by grepping every `wappalyzer` import in `tech_stack.py` and following the legacy engine's own internal imports), not the full ~35MB upstream package:
- `Wappalyzer.py`, `__init__.py` — the legacy detection engine
- `core/`, `analyzers/`, `parsers/` — its dependencies
- `data/technologies.json`, `categories.json`, `groups.json` — the fingerprint database

**Deliberately excluded**: `data/wappalyzer-extension.zip` (34MB, a browser-extension bundle — only referenced as an unused path string in `core/config.py`, never opened by anything this project calls), `browser/` (Playwright-based engine, not used), `scanner.py` (the new engine's entry point, not used).

**Already patched** for the three real bugs documented in `docs/KNOWN_LIMITATIONS.md` (BeautifulSoup parser, UTF-8 file encoding, a broken Symfony fingerprint regex) — `scripts/patch_wappalyzer.py` applied these before this copy was vendored. Because this directory is committed to the repository, every future install gets this exact, already-working code — there is no post-install patch step anymore.

## Updating this vendored copy

Only do this deliberately, and re-verify `tests/unit/test_wappalyzer_patches.py` afterward:

1. `pip install wappalyzer==<version> --target /tmp/wappalyzer_check --no-deps` into a scratch location (never directly into this repo).
2. Confirm it actually has `Wappalyzer.py` with a `.latest()` classmethod before trusting it — this is exactly the check that failed silently before this vendoring fix existed.
3. Run `python scripts/patch_wappalyzer.py` against that scratch install to apply the three known patches.
4. Copy only the files listed above into this directory, replacing the old ones.
5. Run `pytest -q tests/unit/test_wappalyzer_patches.py` and `pytest -q tests/smoke` to confirm nothing broke.
