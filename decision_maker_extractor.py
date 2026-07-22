"""Step 4 of social/LinkedIn intelligence — extract publicly-listed
leadership from the BUSINESS'S OWN crawled pages (About/Team/Leadership),
not from LinkedIn.

Same data shape the user asked for (full name, position, LinkedIn URL,
photo, location, company, headline) from a completely different, fully
legitimate source: a business that publishes a "Team"/"Leadership" page is
publishing exactly this information for the public to find. If that page
happens to link to a person's LinkedIn profile, the LINK is recorded (never
the profile itself scraped) — see social_discovery.py's docstring for the
same distinction applied to company-level social links.

Runs DURING the crawl (phase1_pipeline.crawl_site's per-page loop), on
pages page_intelligence.py already classified as "team" or "about" — no
separate re-crawl or re-classification. Heuristic, not NLP-grade parsing
(matches this project's existing style — see rag/top_matches.py's negation
detection, page_intelligence.py's nav-line filtering — a proximity/pattern
heuristic that catches the common real-world pattern cheaply, documented as
such, not claimed to be exhaustive).

Fields this CANNOT honestly fill from a text-only pass — profile photo (an
<img> src, not text) and stated location — are left null rather than
guessed; see module-level NOTE below.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

# Exact titles from the spec, plus generic "Head of X" / "X Manager"
# patterns so novel department names still match without hardcoding every
# possible one.
_TITLE_TERMS = (
    "chief executive officer", "ceo", "founder", "co-founder", "cofounder",
    "owner", "managing director", "director", "president", "vice president",
    "vp", "chief technology officer", "cto", "chief information officer",
    "cio", "chief operating officer", "coo", "chief marketing officer", "cmo",
    "general manager", "marina manager", "it manager",
    "digital transformation manager", "technology manager",
    "innovation manager", "customer experience manager",
    "business development manager", "operations manager",
)
_GENERIC_TITLE_RE = re.compile(
    r"\b(head of [a-z ]{2,30}|[a-z ]{2,25} manager)\b", re.IGNORECASE
)
_TITLE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _TITLE_TERMS) + r")\b", re.IGNORECASE
)

# "John Smith" / "Mary-Jane O'Watson" — 2-4 capitalized words, common
# name punctuation allowed. Deliberately conservative: false negatives
# (missing an unusually-formatted name) are safer than false positives here.
_NAME_RE = re.compile(
    r"^[A-Z][a-zA-Z'’\-]+(?:\s+[A-Z][a-zA-Z'’\-]+){1,3}$"
)
# "Name, Title" / "Name — Title" / "Name | Title" on ONE line.
_INLINE_PAIR_RE = re.compile(
    r"^([A-Z][a-zA-Z'’\-]+(?:\s+[A-Z][a-zA-Z'’\-]+){1,3})\s*[,|—–-]\s*(.+)$"
)

MAX_PEOPLE_PER_PAGE = 15  # sanity cap — a page matching more than this is
                          # almost certainly matching on marketing copy, not
                          # real bios; better to cap than flood the record.


def _clean_line(line: str) -> str:
    return line.lstrip("#-• ").strip()


def _find_linkedin_url(name: str, external_links_on_page: Sequence[Dict[str, str]]) -> Optional[str]:
    """A person's own name as the ANCHOR TEXT of a linkedin.com/in/ link on
    the SAME page is a strong, specific signal — not a guess, a citation of
    what the page itself already asserts."""
    name_lower = name.lower()
    for link in external_links_on_page:
        url = link.get("url", "")
        if "linkedin.com/in/" not in url.lower():
            continue
        anchor = (link.get("anchor") or "").strip().lower()
        if anchor and (anchor in name_lower or name_lower in anchor):
            return url
    return None


def extract_decision_makers(
    page_url: str, text: str, company: str,
    external_links_on_page: Optional[Sequence[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Scan one About/Team/Leadership page's cleaned text for name+title
    pairs. Never raises — a parse miss just means fewer/no people found.

    NOTE on intentionally-null fields: `profile_image_url` and `location`
    are not populated by this pass — both require the page's raw HTML
    (an <img> tag, a structured address block) which is no longer available
    once cleaned text is what's stored (see phase1_pipeline.clean_page_for_llm).
    Left null rather than guessed, per this feature's own "never guess" rule.
    """
    external_links_on_page = external_links_on_page or []
    lines = [_clean_line(ln) for ln in text.split("\n") if ln.strip()]
    people: List[Dict[str, Any]] = []
    seen_names: set = set()

    for i, line in enumerate(lines):
        name: Optional[str] = None
        title: Optional[str] = None

        inline = _INLINE_PAIR_RE.match(line)
        if inline and (_TITLE_RE.search(inline.group(2)) or _GENERIC_TITLE_RE.search(inline.group(2))):
            name, title = inline.group(1).strip(), inline.group(2).strip()
        elif _TITLE_RE.search(line) or _GENERIC_TITLE_RE.search(line):
            # Title on its own line — look at the line immediately before
            # for a bare name (the common "Name\nTitle" team-card layout).
            if i > 0 and _NAME_RE.match(lines[i - 1]):
                name, title = lines[i - 1], line

        if not name or not title:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)

        linkedin_url = _find_linkedin_url(name, external_links_on_page)
        people.append({
            "full_name": name,
            "current_position": title,
            "linkedin_url": linkedin_url,
            "profile_image_url": None,   # see module NOTE — not derivable from text
            "location": None,            # see module NOTE — not derivable from text
            "company": company,
            "headline": f"{title} at {company}" if company else title,
            "source_page": page_url,
        })
        if len(people) >= MAX_PEOPLE_PER_PAGE:
            break

    return people
