"""HTML sufficiency check — decides whether Stage 1's HTML is good enough to
parse, or whether the page needs a real browser (Stage 2) to render its
JavaScript first.

This is the gate that keeps Playwright a FALLBACK, not the default: cheap,
synchronous, pure-function checks against text already extracted, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

from .config import ScraperConfig

# Presence of these strongly suggests a client-rendered shell whose real
# content hasn't materialized in the raw HTML at all.
_SPA_ROOT_MARKERS = (
    'id="root"', "id='root'", 'id="__next"', "id='__next'",
    'id="app"', "id='app'", "ng-version", "data-reactroot",
)
_WS_RE = re.compile(r"\s+")


@dataclass
class SufficiencyResult:
    sufficient: bool
    text: str
    title: str
    reason: str = ""  # populated only when insufficient — for logging


def check_sufficiency(html: str, config: ScraperConfig) -> SufficiencyResult:
    """Parse `html`, extract visible text, and decide if it's enough to
    proceed without a browser render. Never raises — a parse failure is
    itself grounds for "insufficient" (escalate and let the browser retry)."""
    if not html:
        return SufficiencyResult(False, "", "", reason="empty_response")

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # noqa: BLE001
        return SufficiencyResult(False, "", "", reason=f"parse_error:{exc}")

    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = _WS_RE.sub(" ", soup.get_text(" ")).strip()

    lowered = html.lower()
    if any(marker in lowered for marker in _SPA_ROOT_MARKERS) and len(text) < config.min_text_chars * 2:
        return SufficiencyResult(False, text, title, reason="spa_shell_marker")

    if len(text) < config.min_text_chars:
        return SufficiencyResult(False, text, title, reason="below_min_chars")

    # The ratio check is a REFINEMENT of "borderline thin," not an
    # independent trigger — real modern sites routinely carry heavy
    # CSS/JS/analytics bloat and can have a text:HTML ratio well under 2%
    # while still having plenty of genuine content (measured on a real
    # site during testing: 2,641 chars of real marina-homepage text in a
    # 154KB page = 1.7% ratio, comfortably legitimate). Only distrust the
    # ratio when text is ALSO still fairly thin in absolute terms — that
    # combination (a little text, buried in a lot of markup) is the actual
    # JS-shell signature; a lot of text in a lot of markup is just a
    # content-rich, script-heavy page.
    borderline_ceiling = config.min_text_chars * 4
    if len(text) < borderline_ceiling:
        ratio = len(text) / len(html) if html else 0.0
        if ratio < config.min_text_to_html_ratio:
            return SufficiencyResult(False, text, title, reason="low_text_ratio")

    return SufficiencyResult(True, text, title)
