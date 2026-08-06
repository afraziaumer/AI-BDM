"""Wraps scripts/patch_wappalyzer.py's own verification as a real pytest
test, so "all four tech-detection engines load on a clean installation" is
part of the standard test run -- not just a standalone script someone has
to remember to run separately.

No network call -- this only inspects the already-installed wappalyzer
package's files.

Run: pytest -q tests/unit

Note: this test assumes scripts/patch_wappalyzer.py has already been run
once against the current environment (see README.md's "Known post-install
step"). If it hasn't, this test will fail with a clear VERIFY FAILED
message identifying exactly which fingerprint(s) are still broken --
that's the intended behavior, not a bug in the test.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def test_wappalyzer_package_is_patched_and_verifies_clean():
    import patch_wappalyzer

    root = patch_wappalyzer._wappalyzer_root()
    assert patch_wappalyzer.verify(root) is True


def test_all_technology_fingerprints_compile():
    """Direct compile-test of every regex in the installed fingerprint
    database (the same audit that originally found the Symfony bug) --
    duplicated here as a standalone assertion, independent of verify()'s
    print-based output, so a CI failure shows exactly which fingerprint(s)
    broke without needing to read stdout."""
    import json
    import re
    import patch_wappalyzer

    root = patch_wappalyzer._wappalyzer_root()
    with open(f"{root}/data/technologies.json", encoding="utf-8") as f:
        data = json.load(f)
    techs = data.get("technologies", data)

    fields = ["url", "html", "script", "scriptSrc", "text", "css", "robots",
              "headers", "meta", "cookies", "dom", "xhr"]

    def iter_patterns(info):
        for field in fields:
            val = info.get(field)
            if val is None:
                continue
            if isinstance(val, str):
                yield field, val
            elif isinstance(val, list):
                for v in val:
                    if isinstance(v, str):
                        yield field, v
            elif isinstance(val, dict):
                for k, v in val.items():
                    if isinstance(v, str):
                        yield f"{field}.{k}", v

    broken = []
    for name, info in techs.items():
        if not isinstance(info, dict):
            continue
        for field, pattern in iter_patterns(info):
            regex_part = pattern.split("\\;")[0]
            try:
                re.compile(regex_part, re.IGNORECASE)
            except re.error as exc:
                broken.append(f"{name} ({field}): {exc}")

    assert broken == [], f"{len(broken)} broken fingerprint regex(es): {broken}"


def test_patch_script_is_idempotent():
    """Running the patch twice in a row must not error or re-break
    anything -- the second run should report every fix as already applied."""
    import patch_wappalyzer

    root = patch_wappalyzer._wappalyzer_root()
    r1 = patch_wappalyzer.patch_beautifulsoup_parser(root)
    r2 = patch_wappalyzer.patch_beautifulsoup_parser(root)
    assert "SKIP" in r2, f"second run should skip an already-applied patch, got: {r2}"

    c1 = patch_wappalyzer.patch_config_encoding(root)
    c2 = patch_wappalyzer.patch_config_encoding(root)
    assert "SKIP" in c2, f"second run should skip an already-applied patch, got: {c2}"

    s1 = patch_wappalyzer.patch_symfony_regex(root)
    s2 = patch_wappalyzer.patch_symfony_regex(root)
    assert "SKIP" in s2, f"second run should skip an already-applied patch, got: {s2}"
