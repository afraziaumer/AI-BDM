"""Deterministic business-relevance scoring engine — replaces the LLM
relevance classifier (LLM_planner.classify_business) as the PRIMARY gate for
"is this business actually the right industry, in the right place?".

By the time a candidate reaches this stage the pipeline already has real,
concrete signals about it (industry/location keywords already found in its
own text, its domain name, any schema.org structured data, its physical
address) — asking an LLM to re-derive a verdict from those same signals for
EVERY single scraped business was the single biggest Groq token sink in the
pipeline (see the token-usage refactor this module is part of). This engine
scores those signals directly, for free, in-process, no network call.

Two exceptions where an LLM is still involved, deliberately narrow:
  - `possible_match` (score 40-64): genuinely ambiguous cases fall back to
    LLM_planner.classify_business() as a safety net. This band is exactly
    where a real bug lived earlier in this project's life: a hotel and a
    boat club were both wrongly counted as "marinas" because they sit in/near
    a place called "Marina del Rey" (a NAME, not an industry) — a pure
    keyword engine cannot tell "we ARE a marina" apart from "we are located
    near a place called Marina" the way an LLM's semantic reading can. The
    place-name heuristic below (_strip_place_name_mentions) targets that
    exact failure directly, but doesn't eliminate the risk entirely, so the
    ambiguous band still gets a real semantic check rather than a coin flip.
  - Everything scoring clearly high (>=65) or clearly low (<40) never touches
    an LLM at all — this is the vast majority of businesses.

Fail-closed on internal errors (same principle used throughout this
pipeline): any exception while scoring returns score=0 / category="unrelated"
— a scoring bug must only ever mean one fewer qualified lead, never a wrongly
admitted one.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("ai_bdm.relevance_scoring")

# Bumped whenever WEIGHTS/THRESHOLDS/the matching logic changes, so a cached
# verdict computed under an older version is never silently reused (see
# phase1_pipeline.py's cache-hit relevance check, which compares this against
# the version stored on the cached row).
SCORING_VERSION = 1

# Configurable weights (sum to 100) and thresholds — see module docstring for
# why each category is weighted the way it is. Tune here, not inline.
WEIGHTS = {
    "industry": 40,
    "location": 30,
    "schema": 10,
    "description": 10,
    "metadata": 10,
}
THRESHOLDS = {
    "very_relevant": 80,
    "relevant": 65,
    "possible_match": 40,
    # anything below possible_match's floor is "reject"
}

# schema.org @types that indicate the page describes an actual organization/
# business rather than pure editorial content (Article/BlogPosting/WebPage
# etc., which schema_org_extractor.py never returns as `organization` anyway,
# but a defensive allowlist keeps this signal meaningful if that changes).
_BUSINESS_SCHEMA_TYPES = frozenset({
    "organization", "localbusiness", "corporation", "store", "restaurant",
    "hotel", "lodgingbusiness", "professionalservice", "homeandconstructionbusiness",
    "medicalbusiness", "autodealer", "realestateagent", "travelagency",
    "financialservice", "legalservice", "sportsactivitylocation",
})

# The known failure pattern this engine must not reintroduce: an industry
# word that's actually part of a PLACE name ("Marina del Rey", "Marina Bay",
# "the Marina District") rather than a description of the business itself.
_PLACE_NAME_SUFFIX_RE = re.compile(
    r"\b(del|bay|district|point|cove|harbor|harbour|beach|village|shores?)\b\s+"
    r"[A-Z][a-z]+",
)


def _word_forms(term: str) -> List[str]:
    """Simple, safe pluralization for word-boundary matching — bare `\\bXs?\\b`
    is not enough because "marina" wouldn't match inside "marinas" without
    it (both characters either side of the boundary are word characters, so
    \\b never fires there). No full stemmer, just the common real-world cases
    this pipeline's industry words actually take."""
    term = term.strip().lower()
    forms = {term}
    if term.endswith("y") and len(term) > 1 and term[-2] not in "aeiou":
        forms.add(term[:-1] + "ies")
    else:
        forms.add(term + "s")
    return sorted(forms)


def _compile_term_pattern(term: str) -> Optional[re.Pattern]:
    if not term or not term.strip():
        return None
    forms = [re.escape(f) for f in _word_forms(term)]
    return re.compile(r"\b(?:" + "|".join(forms) + r")\b", re.IGNORECASE)


def _strip_place_name_mentions(text: str, pattern: re.Pattern) -> str:
    """Remove occurrences of the industry term that are immediately followed
    by a place-name pattern ("Marina del Rey", "Marina Bay ...") so they
    don't count as evidence the business itself IS that industry — the
    Marina-del-Rey mitigation described in the module docstring."""
    def _replace(m: re.Match) -> str:
        tail = text[m.end():m.end() + 40]
        if _PLACE_NAME_SUFFIX_RE.match(tail.lstrip()):
            return " " * (m.end() - m.start())  # blank out, preserve offsets
        return m.group(0)
    return pattern.sub(_replace, text)


def _industry_evidence(
    industry: str, name: str, domain: str, sample_text: str,
    schema_organization: Optional[Dict[str, Any]],
) -> tuple[int, List[str]]:
    """Tiered industry-match score (0/25/40) across independent sources —
    matching in title/name/domain/schema is stronger, more trustworthy
    evidence than a bare hit somewhere in the body text, so 2+ independent
    sources agreeing is required for full credit."""
    pattern = _compile_term_pattern(industry)
    if pattern is None:
        return WEIGHTS["industry"], ["no_industry_specified"]  # nothing to check

    # Place-name mitigation applies to EVERY text source, not just the body
    # — "Marina del Rey" shows up just as easily in a schema.org description
    # or an og:title as it does in body copy (this was caught by the
    # jamaicabayinn.com regression test: the mention only lived in
    # schema_organization.description, and the bug leaked straight through
    # unfiltered until this was fixed).
    evidence: List[str] = []
    clean_text = _strip_place_name_mentions(sample_text or "", pattern)
    if pattern.search(clean_text):
        evidence.append("industry_in_page_text")
    if pattern.search(_strip_place_name_mentions(name or "", pattern)):
        evidence.append("industry_in_business_name")
    if pattern.search(_strip_place_name_mentions(domain or "", pattern)):
        evidence.append("industry_in_domain")
    schema_org = schema_organization or {}
    schema_blob = " ".join(str(schema_org.get(k) or "") for k in ("type", "description"))
    if pattern.search(_strip_place_name_mentions(schema_blob, pattern)):
        evidence.append("industry_in_schema_org")

    strong_sources = sum(1 for e in evidence if e != "industry_in_page_text")
    if strong_sources >= 1 and evidence:
        return WEIGHTS["industry"], evidence          # a strong source + any evidence
    if len(evidence) >= 2:
        return WEIGHTS["industry"], evidence           # 2+ weak sources agree
    if len(evidence) == 1:
        # A single body-text-only mention, with no title/name/domain/schema
        # corroboration, is deliberately capped LOW — verified via a real
        # regression: a California state boating-regulation site
        # (dbw.parks.ca.gov) scored exactly 65 ("match") on a "marina" query
        # from nothing but one incidental body-text mention + the automatic
        # location(30)+metadata(10) credit every page with an address gets.
        # A regulatory/informational page ABOUT an industry reads almost
        # identically to a real business IN it under bare keyword matching —
        # this weak tier must stay low enough that location+metadata alone
        # can NEVER push it past the possible_match ceiling into an
        # auto-match; it must go through the safety net instead.
        return round(WEIGHTS["industry"] * 0.375), evidence   # 15 of 40 — single weak hit
    return 0, evidence


def _location_evidence(
    geo: str, address: str, sample_text: str,
    schema_organization: Optional[Dict[str, Any]],
) -> tuple[int, List[str]]:
    """Full geo-phrase match beats a partial (city-only or region-only)
    match — a business merely mentioning the country isn't necessarily
    physically local, but a city+region match together is strong evidence."""
    geo = (geo or "").strip()
    if not geo:
        return WEIGHTS["location"], ["no_location_requested"]

    tokens = [t.strip(",.") for t in geo.split() if len(t.strip(",.")) > 2]
    if not tokens:
        return WEIGHTS["location"], ["no_location_requested"]

    schema_org = schema_organization or {}
    schema_addr = schema_org.get("address")
    haystacks = " ".join([
        address or "", sample_text or "",
        schema_addr if isinstance(schema_addr, str) else str(schema_addr or ""),
    ]).lower()

    full_phrase = geo.lower()
    if full_phrase in haystacks:
        return WEIGHTS["location"], ["full_geo_phrase_match"]

    hits = [t for t in tokens if t.lower() in haystacks]
    if len(hits) >= 2:
        return WEIGHTS["location"], [f"tokens_matched:{','.join(hits)}"]
    if len(hits) == 1:
        return round(WEIGHTS["location"] * 0.6), [f"tokens_matched:{hits[0]}"]
    return 0, []


def _schema_evidence(schema_organization: Optional[Dict[str, Any]]) -> tuple[int, List[str]]:
    if not schema_organization:
        return 0, []
    evidence = ["schema_org_present"]
    score = round(WEIGHTS["schema"] * 0.5)
    schema_type = str(schema_organization.get("type") or "").strip().lower()
    if schema_type in _BUSINESS_SCHEMA_TYPES:
        score = WEIGHTS["schema"]
        evidence.append(f"schema_type:{schema_type}")
    return score, evidence


def _description_evidence(
    industry: str, sample_text: str, industry_score: int,
) -> tuple[int, List[str]]:
    """Gated on industry_score already being at FULL credit (a corroborated,
    strong-source match) — otherwise this double-counts the exact same
    incidental body-text mention the industry bucket already (deliberately)
    scores low. Verified via a real regression: without this gate, a
    California boating-regulation site (dbw.parks.ca.gov) got 15 (weak
    industry hit) + 10 (the SAME hit, counted again here) + 30 (location) +
    10 (metadata) = 65 — an auto-match from one incidental mention, counted
    twice under two different category names."""
    if industry_score < WEIGHTS["industry"]:
        return 0, []
    pattern = _compile_term_pattern(industry)
    if pattern is None:
        return WEIGHTS["description"], []
    intro = _strip_place_name_mentions((sample_text or "")[:300], pattern)
    if pattern.search(intro):
        return WEIGHTS["description"], ["industry_in_description"]
    return 0, []


def _metadata_evidence(address: str, sample_text: str) -> tuple[int, List[str]]:
    if (address or "").strip() or len(sample_text or "") > 200:
        return WEIGHTS["metadata"], ["has_contact_or_content_signal"]
    return 0, []


def _verdict_for_score(score: int) -> tuple[str, str]:
    if score >= THRESHOLDS["very_relevant"]:
        return "very_relevant", "match"
    if score >= THRESHOLDS["relevant"]:
        return "relevant", "match"
    if score >= THRESHOLDS["possible_match"]:
        return "possible_match", "match"  # overwritten by the safety net
    return "reject", "unrelated"


def score_relevance(
    industry: str, geo: str, name: str, domain: str,
    sample_text: str, address: str = "",
    schema_organization: Optional[Dict[str, Any]] = None,
    skip_safety_net: bool = False,
) -> Dict[str, Any]:
    """Score how well a scraped business matches the requested industry+geo.

    Returns {"score", "verdict", "category", "reason",
             "matched_industry_evidence", "matched_location_evidence",
             "version"}. `category` mirrors the old LLM classifier's shape
    ("match"/"unrelated") so _commit_status/_is_lead/print_summary keep
    working unmodified. For the "possible_match" band (40-64), this function
    calls LLM_planner.classify_business as a safety net and returns ITS
    verdict instead — see module docstring for why.

    `skip_safety_net=True` (used by phase1_pipeline's Hybrid Relevance Gate
    for its cheap homepage-only pre-check, before the internal-page crawl
    has run at all) skips that LLM call even in the possible_match band and
    returns the plain deterministic result immediately — there's no reason
    to spend an LLM call resolving ambiguity on PARTIAL data when a limited
    crawl is about to gather more anyway. In this mode `category` is left as
    whatever `_verdict_for_score` defaults it to ("match", unresolved) —
    callers passing `skip_safety_net=True` must key off `verdict`, not
    `category`, for anything landing in the possible_match band. Every
    existing caller keeps the default `False` and is unaffected.
    """
    try:
        industry_score, industry_ev = _industry_evidence(
            industry, name, domain, sample_text, schema_organization,
        )
        location_score, location_ev = _location_evidence(
            geo, address, sample_text, schema_organization,
        )
        schema_score, schema_ev = _schema_evidence(schema_organization)
        desc_score, desc_ev = _description_evidence(industry, sample_text, industry_score)
        meta_score, meta_ev = _metadata_evidence(address, sample_text)

        score = industry_score + location_score + schema_score + desc_score + meta_score
        verdict, category = _verdict_for_score(score)
        reason = (
            f"industry={industry_score}/{WEIGHTS['industry']} "
            f"location={location_score}/{WEIGHTS['location']} "
            f"schema={schema_score}/{WEIGHTS['schema']} "
            f"description={desc_score}/{WEIGHTS['description']} "
            f"metadata={meta_score}/{WEIGHTS['metadata']} -> {score}/100 ({verdict})"
        )

        if verdict == "possible_match" and not skip_safety_net:
            return _safety_net_classify(
                industry, geo, name, sample_text, score, industry_ev, location_ev, reason,
            )

        return {
            "score": score, "verdict": verdict, "category": category, "reason": reason,
            "matched_industry_evidence": industry_ev,
            "matched_location_evidence": location_ev,
            "version": SCORING_VERSION,
        }
    except Exception as exc:  # noqa: BLE001 - fail CLOSED, see module docstring
        logger.warning("Relevance scoring failed for %s (%s): treating as "
                        "not-a-lead, not as a match.", name, exc)
        return {
            "score": 0, "verdict": "reject", "category": "unrelated",
            "reason": f"scoring_error:{exc}",
            "matched_industry_evidence": [], "matched_location_evidence": [],
            "version": SCORING_VERSION,
        }


def _safety_net_classify(
    industry: str, geo: str, name: str, sample_text: str, score: int,
    industry_ev: Sequence[str], location_ev: Sequence[str], deterministic_reason: str,
) -> Dict[str, Any]:
    """The one narrow LLM exception: score 40-64 (possible_match) is exactly
    the band most prone to the place-name/adjacent-business confusion a pure
    keyword engine can't resolve — see module docstring. Reuses the existing
    classify_business prompt unchanged; on ANY failure (LLM down, both
    models exhausted) this fails CLOSED to "unrelated", same as the
    deterministic engine's own error path — an outage must never silently
    promote an ambiguous case to "match"."""
    from LLM_planner import classify_business  # local: avoid import cost/cycles
    # for the vast majority of calls that never reach this branch.
    try:
        llm_verdict = classify_business(industry, geo, name, sample_text)
        category = llm_verdict.get("category", "unrelated")
        return {
            "score": score, "verdict": "possible_match", "category": category,
            "reason": f"{deterministic_reason}; safety_net={llm_verdict.get('reason', '')}",
            "matched_industry_evidence": list(industry_ev),
            "matched_location_evidence": list(location_ev),
            "version": SCORING_VERSION,
        }
    except Exception as exc:  # noqa: BLE001 - fail CLOSED
        logger.warning(
            "Relevance safety-net LLM call failed for %s (%s) — treating "
            "ambiguous match as not-a-lead, not as a match.", name, exc,
        )
        return {
            "score": score, "verdict": "possible_match", "category": "unrelated",
            "reason": f"{deterministic_reason}; safety_net_unavailable:{exc}",
            "matched_industry_evidence": list(industry_ev),
            "matched_location_evidence": list(location_ev),
            "version": SCORING_VERSION,
        }
