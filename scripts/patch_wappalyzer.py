"""Reproducible patch script for three real bugs in the installed
``wappalyzer==2.0.1`` package. Run once after every fresh ``pip install``
(the bugs live inside the installed package's own files, not this
repository, so a reinstall silently reintroduces all three with no visible
error -- see docs/KNOWN_LIMITATIONS.md).

Idempotent: safe to run multiple times; each patch checks whether it's
already applied before touching anything.

Usage:
    python scripts/patch_wappalyzer.py
"""

from __future__ import annotations

import json
import re
import sys


def _wappalyzer_root() -> str:
    import wappalyzer
    import os
    return os.path.dirname(wappalyzer.__file__)


def patch_beautifulsoup_parser(root: str) -> str:
    """Wappalyzer.py hardcodes the lxml BeautifulSoup parser, which
    conflicts with this project's other native dependencies. Swap it for
    html.parser (stdlib, no conflict).

    CRITICAL, discovered during a real fresh-install verification of this
    handoff: PyPI's CURRENTLY-SERVED wheel for wappalyzer==2.0.1 is a
    different, incompatible build than the one this project was built
    against and patched -- same version string, no Wappalyzer.py at all,
    a completely different module layout (analyzers/, browser/, parsers/),
    and Wappalyzer.latest() doesn't exist on the new class (it now exposes
    analyze/analyze_many/close instead). tech_stack.py's legacy engine is
    CONFIRMED BROKEN against this new build on a genuinely fresh install --
    not a hypothetical, reproduced live. See docs/KNOWN_LIMITATIONS.md.
    This function degrades to a WARN (not a crash) when Wappalyzer.py
    simply doesn't exist, so a fresh install at least gets a clear signal
    instead of a traceback -- it does NOT fix the underlying incompatibility."""
    import os
    path = os.path.join(root, "Wappalyzer.py")
    if not os.path.exists(path):
        return (f"WARN: {path} does not exist -- this install's wappalyzer package "
                f"is a different, incompatible build (see docs/KNOWN_LIMITATIONS.md). "
                f"tech_stack.py's legacy engine will not work until this is resolved.")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    old = "BeautifulSoup(self.html, 'lxml')"
    new = "BeautifulSoup(self.html, 'html.parser')"
    if new in text:
        return f"SKIP (already patched): {path}"
    if old not in text:
        return f"WARN (expected pattern not found -- package version may have changed): {path}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new))
    return f"PATCHED: {path}"


def patch_config_encoding(root: str) -> str:
    """core/config.py opens technologies.json/categories.json/groups.json
    without encoding='utf-8'. On Windows this defaults to cp1252 and
    crashes on the files' real UTF-8 content."""
    import os
    path = os.path.join(root, "core", "config.py")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    replacements = [
        ("open(data_dir + '/technologies.json', 'r')",
         "open(data_dir + '/technologies.json', 'r', encoding='utf-8')"),
        ("open(data_dir + '/categories.json', 'r')",
         "open(data_dir + '/categories.json', 'r', encoding='utf-8')"),
        ("open(data_dir + '/groups.json', 'r')",
         "open(data_dir + '/groups.json', 'r', encoding='utf-8')"),
    ]
    already = sum(1 for _, new in replacements if new in text)
    if already == len(replacements):
        return f"SKIP (already patched): {path}"
    applied = 0
    for old, new in replacements:
        if old in text:
            text = text.replace(old, new)
            applied += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return f"PATCHED ({applied}/{len(replacements)} file reads): {path}"


def patch_symfony_regex(root: str) -> str:
    """The Symfony fingerprint's html regex uses JS's "[^]" ("match any
    character") idiom, which Python's re module doesn't support -- it
    mis-parses as an unbalanced-paren error, silently disabling Symfony
    detection (confirmed: the only genuinely broken regex among all 1270
    fingerprints, verified by compile-testing every one)."""
    import os
    path = os.path.join(root, "data", "technologies.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    techs = data.get("technologies", data)
    symfony = techs.get("Symfony")
    if symfony is None:
        return f"WARN (Symfony entry not found -- package version may have changed): {path}"
    old = symfony.get("html", "")
    if "[^]" not in old:
        return f"SKIP (already patched or pattern changed): {path}"
    new = old.replace("[^]+", r"[\s\S]+")
    symfony["html"] = new
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return f"PATCHED: {path}"


def verify(root: str) -> bool:
    """Compile-test every fingerprint regex in the database (the same audit
    used to find the Symfony bug) and confirm the config module imports
    cleanly with the corrected encoding. Returns True/False rather than
    exiting -- so this is safely callable from a test, not just the CLI
    (see main(), which is what actually exits non-zero on failure)."""
    import os
    path = os.path.join(root, "data", "technologies.json")
    with open(path, "r", encoding="utf-8") as f:
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
                    elif isinstance(v, list):
                        for vv in v:
                            if isinstance(vv, str):
                                yield f"{field}.{k}", vv

    broken = []
    for name, info in techs.items():
        if not isinstance(info, dict):
            continue
        for field, pattern in iter_patterns(info):
            regex_part = pattern.split("\\;")[0]
            try:
                re.compile(regex_part, re.IGNORECASE)
            except re.error as exc:
                broken.append((name, field, str(exc)))

    if broken:
        print(f"VERIFY FAILED: {len(broken)} broken regex(es) remain:")
        for name, field, err in broken:
            print(f"  {name} ({field}): {err}")
        return False
    print(f"VERIFY OK: all {len(techs)} technology fingerprints compile cleanly.")

    # Confirm the three engines that depend on the encoding fix actually load.
    import importlib
    import wappalyzer.core.config as config_module
    importlib.reload(config_module)
    print(f"VERIFY OK: wappalyzer.core.config loaded "
          f"({len(config_module.tech_db)} tech_db entries, "
          f"{len(config_module.cat_db)} categories, "
          f"{len(config_module.groups_db)} groups).")
    return True


def main() -> None:
    root = _wappalyzer_root()
    print(f"wappalyzer package root: {root}\n")
    print(patch_beautifulsoup_parser(root))
    print(patch_config_encoding(root))
    print(patch_symfony_regex(root))
    print()
    if not verify(root):
        sys.exit(1)


if __name__ == "__main__":
    main()
