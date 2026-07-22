"""Decision-maker source #1 (highest priority): the business's own crawled
pages — About/Team/Leadership pages (name+title heuristic) and Contact/
Support pages (role-tagged email/phone). See schema_org_extractor.py for
source #2 (structured data) and public_search_decision_makers.py for the
last-resort fallback (#4) — this module owns only the two on-site, text-
based sources.

Same fully-legitimate-source principle as social_discovery.py: a business
that publishes a "Team"/"Leadership"/"Contact" page is publishing exactly
this information for the public to find. Nothing here scrapes a third
party; a discovered LinkedIn link is recorded as a citation, never
followed/scraped (see _find_linkedin_url).

Heuristic, not NLP-grade parsing — matches this project's established style
(rag/top_matches.py's negation detection, page_intelligence.py's nav-line
filtering): a proximity/pattern heuristic that catches the common real-world
layout cheaply, documented as such, with confidence scores reflecting how
much to trust each pattern rather than treating every hit as certain.

Output schema, one dict per person (see phase1_pipeline.py's
business_intelligence assembly for how these merge across sources):
    {name, role, department, email, phone, linkedin, photo, location,
     source, source_url, confidence}
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

# Exact titles from the spec, plus generic "Head of X" / "X Manager" /
# "X Director" patterns so novel department names still match without
# hardcoding every possible one.
_TITLE_TERMS = (
    "chief executive officer", "ceo", "founder", "co-founder", "cofounder",
    "owner", "managing director", "director", "president", "vice president",
    "vp", "chief technology officer", "cto", "chief information officer",
    "cio", "chief operating officer", "coo", "chief financial officer", "cfo",
    "chief marketing officer", "cmo", "general manager", "marina manager",
    "it manager", "digital transformation manager", "technology manager",
    "innovation manager", "customer experience manager",
    "customer success manager", "business development manager",
    "operations manager", "support manager", "hr manager",
    "human resources manager", "finance manager", "sales director",
    "marketing director",
)
_GENERIC_TITLE_RE = re.compile(
    r"\b(head of [a-z ]{2,30}|[a-z ]{2,25} (?:manager|director))\b", re.IGNORECASE
)
_TITLE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _TITLE_TERMS) + r")\b", re.IGNORECASE
)

# Title/department keyword -> department bucket (see organization.py, which
# consumes this same mapping to group people into org-structure sections).
# Checked in order; first match wins, so more specific terms come first.
DEPARTMENT_KEYWORDS: List[tuple] = [
    ("Executive", ("ceo", "chief executive", "founder", "owner", "president",
                    "managing director", "general manager")),
    ("Technology", ("cto", "chief technology", "cio", "chief information",
                     "it manager", "technology manager", "digital transformation",
                     "innovation manager")),
    ("Operations", ("coo", "chief operating", "operations manager",
                     "general manager", "marina manager")),
    ("Sales", ("sales director", "sales manager", "business development")),
    ("Marketing", ("cmo", "chief marketing", "marketing director", "marketing manager")),
    ("Finance", ("cfo", "chief financial", "finance manager")),
    ("Customer Success", ("customer success", "customer experience")),
    ("Support", ("support manager", "support")),
    ("Human Resources", ("hr manager", "human resources")),
]


def _contains_keyword(text: str, keyword: str) -> bool:
    """Word-boundary match, not a bare substring check — short acronym
    keywords ("cto", "coo", "vp"...) are otherwise dangerously prone to
    accidental hits inside ordinary words. Verified empirically: a naive
    `"cto" in "sales director".lower()` is True ("dire-CTO-r"), which
    misclassified "Sales Director" as the Technology department — the same
    class of bug as this session's "case-studies" tokenizer fix and the
    LinkedIn place-name-conflation fix (a short/generic token matching where
    it structurally shouldn't)."""
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def infer_department(role: str) -> Optional[str]:
    low = (role or "").lower()
    for department, keywords in DEPARTMENT_KEYWORDS:
        if any(_contains_keyword(low, k.strip()) for k in keywords):
            return department
    if _contains_keyword(low, "director") or _contains_keyword(low, "vp") or "vice president" in low:
        return "Executive"
    if _contains_keyword(low, "manager"):
        return "Operations"
    return None


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
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\+?\(?\d[\d\-.\s()]{7,}\d")

# Job-posting/corporate-jargon words that satisfy _NAME_RE's shape (title-
# case, 2-4 words) but are never actually part of a person's name — found
# via real testing: a Serper snippet for a job listing titled "Administrative
# Business Partner, Office of the CEO" matched the inline Name-Title pattern
# and got stored as a PERSON named "Administrative Business Partner". Same
# root cause as the decision to require token-level agreement in
# linkedin_discovery's title-confidence check: a shape-only pattern match
# isn't enough, the content must also be plausible for what it claims to be.
_NON_NAME_WORDS = frozenset({
    "administrative", "business", "partner", "officer", "department",
    "associate", "specialist", "coordinator", "representative", "assistant",
    "team", "staff", "office", "division", "unit", "group", "committee",
    "board", "council", "services", "solutions", "support", "customer",
    "client", "product", "engineering", "human", "resources", "talent",
})


def _looks_like_person_name(candidate: str) -> bool:
    words = candidate.split()
    return not any(w.lower() in _NON_NAME_WORDS for w in words)

# Confidence per extraction PATTERN — reflects how much a human should trust
# it, not a single flat "found something" score. "Name, Title" on one line
# is close to unambiguous; "title on its own line with a name-shaped line
# right before it" is a common layout but has more false-positive surface
# (e.g. a customer testimonial's byline).
_CONF_INLINE_PAIR = 0.9
_CONF_STACKED_LINES = 0.78
_CONF_ROLE_EMAIL = 0.7

MAX_PEOPLE_PER_PAGE = 15  # sanity cap — more than this on one page is
                          # almost certainly matching marketing copy, not
                          # real bios; better to cap than flood the record.

# Role-indicating local-parts for contact-page email scanning (Step 4.3).
# "info"/"contact"/"hello"/"admin" deliberately excluded — too generic to
# imply a specific department.
_ROLE_EMAIL_PREFIXES: Dict[str, str] = {
    "sales": "Sales", "marketing": "Marketing", "support": "Support",
    "help": "Support", "hr": "Human Resources", "careers": "Human Resources",
    "jobs": "Human Resources", "finance": "Finance", "billing": "Finance",
    "accounts": "Finance", "press": "Marketing", "media": "Marketing",
    "partnerships": "Sales", "business": "Sales",
}


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


def _build_person(
    name: str, role: str, source_page: str, confidence: float,
    external_links_on_page: Sequence[Dict[str, str]],
    email: Optional[str] = None, phone: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "name": name,
        "role": role,
        "department": infer_department(role),
        "email": email,
        "phone": phone,
        "linkedin": _find_linkedin_url(name, external_links_on_page),
        "photo": None,       # requires raw HTML at extraction time — see
                              # module docstring; not derivable from cleaned text
        "location": None,    # same limitation as photo — left null, not guessed
        "source": "website",
        "source_url": source_page,
        "confidence": confidence,
    }


def extract_decision_makers(
    page_url: str, text: str, company: str,
    external_links_on_page: Optional[Sequence[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Scan one About/Team/Leadership page's cleaned text for name+title
    pairs. Never raises — a parse miss just means fewer/no people found."""
    external_links_on_page = external_links_on_page or []
    lines = [_clean_line(ln) for ln in text.split("\n") if ln.strip()]
    people: List[Dict[str, Any]] = []
    seen_names: set = set()

    for i, line in enumerate(lines):
        name: Optional[str] = None
        title: Optional[str] = None
        confidence = 0.0

        inline = _INLINE_PAIR_RE.match(line)
        if inline and (_TITLE_RE.search(inline.group(2)) or _GENERIC_TITLE_RE.search(inline.group(2))):
            name, title, confidence = inline.group(1).strip(), inline.group(2).strip(), _CONF_INLINE_PAIR
        elif _TITLE_RE.search(line) or _GENERIC_TITLE_RE.search(line):
            # Title on its own line — look at the line immediately before
            # for a bare name (the common "Name\nTitle" team-card layout).
            if i > 0 and _NAME_RE.match(lines[i - 1]):
                name, title, confidence = lines[i - 1], line, _CONF_STACKED_LINES

        if not name or not title or not _looks_like_person_name(name):
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)

        people.append(_build_person(
            name, title, page_url, confidence, external_links_on_page,
        ))
        if len(people) >= MAX_PEOPLE_PER_PAGE:
            break

    return people


def extract_contact_page_people(
    page_url: str, text: str,
    external_links_on_page: Optional[Sequence[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Step 4.3 — Contact/Support/Locations pages: named contacts (same
    name+title heuristic as extract_decision_makers) PLUS role-tagged
    department emails (e.g. "sales@company.com") that aren't tied to a
    named person but still identify who owns an inbound channel.

    Returns a MIXED list: some entries have a `name`, some are department-
    level (`name: None`, `department` set, `source: "contact_page"`) — the
    caller's merge step (business_intelligence assembly) keeps both kinds,
    since "who owns sales inquiries" is useful even with no named person.
    """
    external_links_on_page = external_links_on_page or []
    people = extract_decision_makers(page_url, text, "", external_links_on_page)
    for p in people:
        p["source"] = "contact_page"

    seen_emails = {p["email"] for p in people if p.get("email")}
    for match in _EMAIL_RE.finditer(text):
        email = match.group(0).lower()
        if email in seen_emails:
            continue
        local_part = email.split("@")[0]
        department = _ROLE_EMAIL_PREFIXES.get(local_part)
        if not department:
            continue
        seen_emails.add(email)
        # A nearby phone number, if this email's line also has one (common
        # "Sales: sales@x.com / +1 555 1234" contact-block layout).
        nearby_phone = None
        for line in text.split("\n"):
            if email in line.lower():
                phone_match = _PHONE_RE.search(line)
                if phone_match:
                    nearby_phone = phone_match.group(0).strip()
                break
        people.append({
            "name": None, "role": None, "department": department,
            "email": email, "phone": nearby_phone, "linkedin": None,
            "photo": None, "location": None, "source": "contact_page",
            "source_url": page_url, "confidence": _CONF_ROLE_EMAIL,
        })
    return people
