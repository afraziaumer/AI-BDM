"""LinkedIn/social discovery via Google X-Ray search (Serper) — NEVER a
LinkedIn scraper, never browser automation against linkedin.com. Everything
here reads what Google's own crawler already indexed about a public
linkedin.com page (company pages AND /in/ personal profiles); it does not
fetch linkedin.com itself.

Two discovery jobs live here, sharing the same query-generation/scoring
machinery:
  1. Company page discovery (`discover_company_linkedin`) — when
     social_discovery.py found no LinkedIn link on the business's own site.
  2. Decision-maker discovery (`discover_decision_makers`) — X-Ray searches
     for linkedin.com/in/ profiles associated with this company + common
     executive titles. This is a SEPARATE, lower-priority source from
     decision_maker_extractor.py's website-based extraction and
     public_search_decision_makers.py's site:domain.com fallback — see
     phase1_pipeline.py's orchestration for the priority order (website
     first, LinkedIn X-Ray last).

Also the shared engine behind the SAME company-page fallback for Facebook/
Instagram/X (see `discover_profile_via_search`, used directly by
phase1_pipeline.py's orchestration for those three) — one confidence-
scoring/search implementation, not four+ near-identical copies of it.

Named interface points (Step 11 of the spec this module implements) — swap
ANY of these for a licensed provider (Apollo/Proxycurl/People Data
Labs/ZoomInfo/Clearbit) later without touching callers:
    discover_company_linkedin()      — company page discovery
    generate_google_xray_queries()   — query generation (company page)
    search_google_xray()             — one raw search-provider call
    rank_linkedin_candidates()       — confidence scoring
    discover_decision_makers()       — person discovery
    save_linkedin_candidates()       — persistence (see storage.py)
Today every one of these is Serper-backed; a future provider swap means
reimplementing these six functions against a new backend, not restructuring
the rest of AI-BDM.

IMPORTANT — this module NEVER fetches linkedin.com (or facebook.com/
instagram.com/x.com). It only:
  1. Generates candidate URL slugs / query variations from the company
     name/domain/city/country/industry (cheap, local, no network) — these
     are CANDIDATES to check, never accepted on their own.
  2. Verifies candidates against Serper's search index (the same provider
     and constants already used for business discovery elsewhere in this
     project — see phase1_pipeline.SERPER_API_KEY/SERPER_SEARCH_URL) —
     i.e. what Google's own crawler has already indexed, not a live scrape.

A candidate is only accepted if Serper's index actually surfaces a result
whose title/snippet clears a confidence floor. No match above that floor ->
the result is left null/excluded. "Never guess" is enforced structurally:
nothing this module returns is based on a guessed slug/query alone, only on
independently-indexed confirmation of it.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import aiohttp

from decision_maker_extractor import _looks_like_person_name

logger = logging.getLogger("ai_bdm.linkedin_discovery")

# Same "retry transient, don't retry deterministic" policy already
# established for LLM/scrape calls elsewhere in this project (see
# LLM_planner.call_llm, phase1_pipeline.PremiumScraper.fetch).
_MAX_ATTEMPTS = 2
_BACKOFF_BASE_S = 1.5

MIN_CONFIDENCE = 0.72  # below this, the caller stores null — never a low-confidence guess.
MIN_PERSON_CONFIDENCE = 0.55  # decision-maker X-Ray floor — see
                              # rank_linkedin_candidates' docstring for why
                              # this is lower than company-page confidence.

_WORD_RE = re.compile(r"[a-z0-9]+")
_GENERIC_SUFFIXES = ("inc", "llc", "ltd", "co", "corp", "company", "group", "the")

# Step 5 — negative filtering: appended (as Google exclusion terms) to every
# X-Ray query, and separately checked against result text as a confidence
# penalty (a search engine's `-word` exclusion is a hint, not a guarantee —
# see rank_linkedin_candidates).
_NEGATIVE_FILTER_TERMS = (
    "former", "previous", "past", "retired", "alumni", "intern", "student",
    "volunteer",
)
NEGATIVE_XRAY_SUFFIX = " ".join(f"-{t}" for t in _NEGATIVE_FILTER_TERMS)

# Step 3/4 — executive title groups. Each inner tuple becomes one OR-ed
# boolean group in a single query (Step 4's advanced operator usage) rather
# than one query per title — fewer Serper calls for the same coverage.
TITLE_GROUPS: Tuple[Tuple[str, ...], ...] = (
    ("CEO", "Chief Executive Officer"),
    ("Founder", "Co-Founder", "Owner"),
    ("Managing Director", "General Manager"),
    ("COO", "Chief Operating Officer"),
    ("CFO", "Chief Financial Officer"),
    ("CTO", "Chief Technology Officer"),
    ("CMO", "Chief Marketing Officer"),
    ("Director",),
    ("Operations Manager",),
    ("Business Development", "Business Development Manager"),
    ("Sales Director",),
    ("Marketing Director",),
)

LINKEDIN_CANDIDATES_TTL_DAYS = 60  # Step 10 — same staleness-gate pattern as
                                   # tech_stack.TECH_PROFILE_TTL_DAYS: a
                                   # cached discovery is reused as-is until
                                   # it's this old, never re-searched sooner.


def _slugify(text: str) -> str:
    words = _WORD_RE.findall(text.lower())
    words = [w for w in words if w not in _GENERIC_SUFFIXES] or words
    return "-".join(words)


def generate_candidate_slugs(company_name: str, domain: str) -> List[str]:
    """Local, no-network candidate slugs — see module docstring: these are
    checked against Serper's index, never trusted on their own."""
    bare_domain = domain.split(".")[0]
    name_slug = _slugify(company_name) if company_name else ""
    candidates = []
    for s in (
        bare_domain,
        name_slug,
        f"{name_slug}-group" if name_slug else "",
        f"{bare_domain}-group",
        f"the-{name_slug}" if name_slug else "",
    ):
        if s and s not in candidates:
            candidates.append(s)
    return candidates


def city_country_from_geo(geo: str) -> Tuple[str, str]:
    """('Los Angeles', 'USA') from "Los Angeles, California, USA"; ('Dubai',
    '') from "Dubai"; ('', '') from "". Best-effort split of the planner's
    free-text geo_location string (see phase1_pipeline.deconstruct_intent) —
    first comma-separated segment as city, last as country, matching the
    "City, Region, Country" / "City, Country" / "Country" shapes that
    string already comes in. Not a geocoder: good enough to append to an
    X-Ray query (Step 6), not meant for anything more precise."""
    parts = [p.strip() for p in (geo or "").split(",") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def generate_google_xray_queries(
    company_name: str, domain: str, city: str = "", country: str = "",
    industry: str = "",
) -> List[str]:
    """Step 2/4/6 — multiple Google X-Ray query variations for finding this
    business's LinkedIn COMPANY page. Local, no-network (see module
    docstring) — every query here is a candidate to be VERIFIED against
    Serper's index (discover_company_linkedin), never accepted on its own.

    Deliberately includes both narrow (name + city/country/industry) and
    broad (bare slug) variations — discover_profile_via_search tries them
    in order and stops at the first high-confidence hit, so listing more
    variations only costs extra Serper calls when the earlier ones come up
    empty, never accuracy.
    """
    queries: List[str] = []
    if company_name:
        queries.append(f'site:linkedin.com/company "{company_name}"')
        if city:
            queries.append(f'site:linkedin.com/company "{company_name}" {city}')
        if country:
            queries.append(f'site:linkedin.com/company "{company_name}" {country}')
        if industry:
            queries.append(f'site:linkedin.com/company "{company_name}" {industry}')
    if domain:
        queries.append(f'site:linkedin.com/company "{domain}"')
    queries += [
        f"site:linkedin.com/company {slug}"
        for slug in generate_candidate_slugs(company_name, domain)
    ]
    if domain:
        queries.append(f"site:linkedin.com {domain}")

    seen: set = set()
    deduped: List[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped


def _normalize_for_match(text: str) -> str:
    words = _WORD_RE.findall(text.lower())
    return " ".join(w for w in words if w not in _GENERIC_SUFFIXES)


def _title_confidence(company_name: str, result_title: str) -> float:
    """0..1 fuzzy match between the business name and a search result's
    title — the ONLY signal that turns a guessed slug (or a plain search)
    into an accepted match. Generic legal suffixes (Inc/LLC/Group...)
    stripped from both sides so they don't inflate or deflate the score.

    Deliberately the MINIMUM of two different signals, not either alone —
    verified empirically to matter: "Redondo Beach Marina" vs. "Redondo
    Beach Sportfishing" (a real, different business at the same location)
    scores 0.74 on character-level SequenceMatcher alone, purely from
    sharing the "Redondo Beach " prefix — above a naive 0.72 floor despite
    being the wrong business. Token-set overlap catches this: {redondo,
    beach, marina} vs {redondo, beach, sportfishing} = 0.5 Jaccard, since it
    penalizes the ONE WORD THAT ACTUALLY DISTINGUISHES the two businesses
    disagreeing, where character-diffing barely notices. Requiring both
    measures to agree is the same fix in spirit as the LinkedIn/Marina-del-
    Rey place-name-conflation prompt fix elsewhere in this project: a shared
    LOCATION or generic prefix must never be enough on its own."""
    a, b = _normalize_for_match(company_name), _normalize_for_match(result_title)
    if not a or not b:
        return 0.0
    char_ratio = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = set(a.split()), set(b.split())
    token_jaccard = (
        len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if (tokens_a | tokens_b) else 0.0
    )
    return min(char_ratio, token_jaccard)


@dataclass
class ProfileMatch:
    url: str
    title: str
    snippet: str
    confidence: float


LinkedInMatch = ProfileMatch  # backward-compat name — linkedin_enrichment.py
                              # and others already import this exact name.


async def search_google_xray(session: aiohttp.ClientSession, query: str) -> List[Dict[str, Any]]:
    """One raw Google X-Ray query (via Serper) -> its 'organic' results. The
    ONE named swap point (Step 11) for replacing the search-based discovery
    backend entirely — reuses this project's existing Serper constants/
    session convention (see phase1_pipeline.discover_targets) but is
    deliberately a plain, focused helper — LinkedIn/social discovery doesn't
    need discover_targets' business-discovery-specific pagination/dedup/
    directory-mining logic. Shared by public_search_decision_makers.py too,
    rather than that module keeping its own copy of the same raw call."""
    # Lazy import: phase1_pipeline imports THIS module at load time (to wire
    # it into the crawl pipeline), so a top-level `from phase1_pipeline
    # import ...` here would be a circular import that fails before either
    # module finishes loading. By the time this function actually RUNS,
    # phase1_pipeline is fully initialized — same lazy-import pattern
    # route_planner.py already uses for phase1_pipeline._domain_key.
    from phase1_pipeline import SERPER_API_KEY, SERPER_SEARCH_URL, SERPER_TIMEOUT_S

    if not SERPER_API_KEY:
        logger.error("Serper key missing — cannot verify LinkedIn candidates.")
        return []
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with session.post(
                SERPER_SEARCH_URL, json={"q": query, "num": 10}, headers=headers,
                timeout=aiohttp.ClientTimeout(total=SERPER_TIMEOUT_S),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("organic", []) or []
                if resp.status in (429, 500, 502, 503, 504) and attempt < _MAX_ATTEMPTS - 1:
                    import asyncio
                    await asyncio.sleep(_BACKOFF_BASE_S * (attempt + 1))
                    continue
                logger.warning("Serper search failed (status=%s) for %r", resp.status, query)
                return []
        except Exception as exc:  # noqa: BLE001 - network layer, degrade quietly
            logger.warning("Serper search error for %r: %s", query, exc)
            return []
    return []


def _best_match_in_results(
    company_name: str, results: List[Dict[str, Any]], path_ok,
) -> Optional[ProfileMatch]:
    best: Optional[ProfileMatch] = None
    for item in results:
        link = item.get("link", "")
        if not path_ok(link):
            continue
        title = item.get("title", "") or ""
        confidence = _title_confidence(company_name, title)
        if best is None or confidence > best.confidence:
            best = ProfileMatch(
                url=link, title=title, snippet=item.get("snippet", "") or "",
                confidence=confidence,
            )
    return best


async def discover_profile_via_search(
    session: aiohttp.ClientSession, queries: List[str], company_name: str,
    path_ok, min_confidence: float = MIN_CONFIDENCE,
) -> Optional[ProfileMatch]:
    """Generic engine: try `queries` in order against Serper, keep the
    highest-confidence result whose URL passes `path_ok` (a platform-specific
    "is this actually a profile page, not a login/help/permalink" check —
    see social_discovery.py's `_is_noise_url` for the same idea applied to
    links already found on a site, vs. here applied to search results).
    Stops early once a near-certain match is found. None if nothing clears
    `min_confidence` — never the best-of-a-bad-lot guess.

    Shared by discover_linkedin_company below and, directly, by
    phase1_pipeline.py's Facebook/Instagram/X fallback discovery — one
    implementation of "search, score, verify," not one per platform.
    """
    best: Optional[ProfileMatch] = None
    for query in queries:
        results = await search_google_xray(session, query)
        match = _best_match_in_results(company_name, results, path_ok)
        if match and (best is None or match.confidence > best.confidence):
            best = match
        if best and best.confidence >= 0.95:
            break  # already about as confident as this method gets

    if best is None or best.confidence < min_confidence:
        if best is not None:
            logger.info(
                "Profile candidate below confidence floor (%.2f < %.2f): %s",
                best.confidence, min_confidence, best.url,
            )
        return None
    return best


def _linkedin_company_path_ok(link: str) -> bool:
    return "linkedin.com" in link and urlparse(link).path.lower().startswith("/company/")


async def discover_company_linkedin(
    session: aiohttp.ClientSession, company_name: str, domain: str,
    city: str = "", country: str = "", industry: str = "",
) -> Optional[ProfileMatch]:
    """Step 2/6 — try each generate_google_xray_queries() variation in order
    (name+city/country/industry first, broad slug/domain searches last).
    See discover_profile_via_search for the shared scoring/verification
    engine. `city`/`country` typically come from city_country_from_geo()."""
    queries = generate_google_xray_queries(company_name, domain, city, country, industry)
    return await discover_profile_via_search(
        session, queries, company_name, _linkedin_company_path_ok,
    )


# ===========================================================================
# Decision-maker discovery (Step 3/4/5/6/7) — linkedin.com/in/ X-Ray search
# ===========================================================================
# LinkedIn profile page titles Google indexes typically look like
# "John Smith - Chief Executive Officer - ABC Marina | LinkedIn" or
# "John Smith - CEO at ABC Marina | LinkedIn" — name first, then role,
# separated by a dash/pipe, with "LinkedIn" itself as a trailing segment.
_LINKEDIN_IN_TITLE_RE = re.compile(
    r"^([A-Z][a-zA-Z'’.\-]+(?:\s+[A-Z][a-zA-Z'’.\-]+){1,3})\s*[-|–]\s*(.+)$"
)
_TRAILING_LINKEDIN_RE = re.compile(r"\s*[-|–]\s*LinkedIn\s*$", re.IGNORECASE)


def _parse_linkedin_in_title(title: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(name, role, company_segment) parsed from a linkedin.com/in/ search
    result's title, or (None, None, None) if the shape doesn't match or the
    "name" half doesn't actually look like a person's name (reuses
    decision_maker_extractor.py's guard — the same job-posting-title-
    mistaken-for-a-name failure mode applies here as it did for
    public_search_decision_makers.py).

    `company_segment` is the structured "Company" half of "Role at Company"
    / "Role - Company" — a much higher-precision company signal than
    searching the whole free-text snippet (see rank_linkedin_candidates'
    docstring for why: verified via real testing that a common word/
    technology-name company like "Stripe" produces false positives when
    matched against a whole snippet's free text, since "Stripe" the payment
    processor gets name-dropped in countless unrelated bios)."""
    cleaned = _TRAILING_LINKEDIN_RE.sub("", title or "").strip()
    m = _LINKEDIN_IN_TITLE_RE.match(cleaned)
    if not m:
        return None, None, None
    name, rest = m.group(1).strip(), m.group(2).strip()
    if not _looks_like_person_name(name):
        return None, None, None
    # "CEO at ABC Marina" -> role="CEO", company_segment="ABC Marina".
    # "Chief Executive Officer - ABC Marina" -> split on the FIRST
    # remaining dash/pipe instead: role=left half, company_segment=right.
    at_split = re.split(r"\bat\b", rest, maxsplit=1, flags=re.IGNORECASE)
    if len(at_split) == 2:
        role, company_segment = at_split[0].strip(), at_split[1].strip()
    else:
        dash_split = re.split(r"\s+[-|–]\s+", rest, maxsplit=1)
        role = dash_split[0].strip()
        company_segment = dash_split[1].strip() if len(dash_split) == 2 else None
    return name, (role or None), (company_segment or None)


def _company_mention_score(
    company_name: str, domain: str, company_segment: Optional[str], text_lower: str,
) -> float:
    """0..1 company-match confidence — PREFERS the structured
    `company_segment` (the "Company" half of a parsed "Role at Company"
    title) when available, since that's a precise, single-purpose field;
    only falls back to a whole-text (title+snippet) mention check when a
    title doesn't parse into that shape at all.

    The `company_segment` path uses `_title_confidence`'s symmetric
    similarity (appropriate here — company_name and company_segment are
    both short, comparable-length phrases, the same shape
    _title_confidence was designed for). The whole-text fallback path is a
    weaker token-subset containment check, capped lower, precisely because
    it can't distinguish "this IS the employer" from "this word appears
    somewhere in unrelated prose" (the Stripe false-positive).

    The domain check requires the segment's normalized text to EXACTLY
    equal the domain token, not merely CONTAIN it as a substring — verified
    via real testing that a naive substring check (`"stripe" in
    "stripeshow"`) wrongly gave a maximum-confidence match between the real
    "Stripe" and an unrelated small business called "Stripe Show" that just
    happens to share that word. Same bug class as this session's
    "cto"-inside-"director" fix: a short token accidentally matching inside
    a longer, semantically DIFFERENT string. "Stripe Show" has a real extra
    word beyond "Stripe" — that's a different, more specific company, not
    the same one, and gets no special treatment beyond ordinary similarity
    scoring below."""
    domain_token = domain.split(".")[0].lower() if domain else ""
    if company_segment:
        segment_normalized = _normalize_for_match(company_segment).replace(" ", "")
        if domain_token and domain_token == segment_normalized:
            return 1.0
        return _title_confidence(company_name, company_segment)

    if domain_token and re.search(rf"\b{re.escape(domain_token)}\b", text_lower):
        return 0.6  # bare domain mention with no structured company field —
                    # plausible, but capped well below a confirmed match
    company_tokens = set(_normalize_for_match(company_name).split()) if company_name else set()
    if not company_tokens:
        return 0.0
    text_tokens = set(_WORD_RE.findall(text_lower))
    return 0.5 * (len(company_tokens & text_tokens) / len(company_tokens))


def rank_linkedin_candidates(
    *, result_title: str = "", snippet: str = "", company_name: str = "",
    domain: str = "", city: str = "", country: str = "", industry: str = "",
    company_segment: Optional[str] = None,
) -> float:
    """Step 7 — confidence score (0..1) for ONE discovered LinkedIn
    candidate (company page OR person profile — both pass their
    result_title/snippet through here). Combines:
      - company-name/domain mention strength in the result text
      - location mention (city/country) — Step 6's precision boost, made
        measurable
      - industry keyword mention
      - a penalty if a negative-filter term (Step 5) slipped through
        anyway — a `-word` Google exclusion is a hint, not a guarantee, so
        a candidate whose text still shows a negative-filter term (Step 5)
        is REJECTED outright (score 0.0), not merely discounted — Step 5's
        actual intent is "prioritize current employees," i.e. exclude a
        former/retired/alumni hit, not just rank it lower. A soft penalty
        was tried first and verified (via real testing) to be the wrong
        call: a 0.3-point discount landed a "former CEO" match EXACTLY at
        the confidence floor by coincidence, letting it slip through — the
        same class of near-miss this project treats as a bug elsewhere
        (see decision_maker_extractor.py's negation handling).

    Never a guessed floor: every contributing signal must be textually
    present to count — there is no baseline/default confidence.
    """
    text = f"{result_title} {snippet}".lower()
    if any(re.search(rf"\b{t}\b", text) for t in _NEGATIVE_FILTER_TERMS):
        return 0.0
    company_score = _company_mention_score(company_name, domain, company_segment, text)
    location_bonus = 0.1 if (
        (city and re.search(rf"\b{re.escape(city.lower())}\b", text))
        or (country and re.search(rf"\b{re.escape(country.lower())}\b", text))
    ) else 0.0
    industry_bonus = 0.05 if industry and industry.lower() in text else 0.0
    score = min(1.0, company_score * 0.85 + location_bonus + industry_bonus)
    return max(0.0, round(score, 3))


def generate_decision_maker_xray_queries(
    company_name: str, domain: str, city: str = "", country: str = "",
) -> List[str]:
    """Step 3/4/5/6 — one X-Ray query per executive title GROUP (boolean OR
    within a group — Step 4's advanced-operator usage — rather than one
    query per individual title, so full title coverage costs one Serper
    call per group, not one per title)."""
    company_fragment = f'"{company_name}"' if company_name else (f'"{domain}"' if domain else "")
    if not company_fragment:
        return []
    location_suffix = f" {city}" if city else (f" {country}" if country else "")
    queries = []
    for group in TITLE_GROUPS:
        if len(group) == 1:
            title_fragment = group[0]
        else:
            title_fragment = "(" + " OR ".join(
                f'"{t}"' if " " in t else t for t in group
            ) + ")"
        queries.append(
            f"site:linkedin.com/in/ {company_fragment} {title_fragment}"
            f"{location_suffix} {NEGATIVE_XRAY_SUFFIX}"
        )
    return queries


async def discover_decision_makers(
    session: aiohttp.ClientSession, company_name: str, domain: str,
    city: str = "", country: str = "", industry: str = "",
    max_people: int = 10,
) -> List[Dict[str, Any]]:
    """Step 3/4/5/6/7 — X-Ray search for linkedin.com/in/ profiles
    associated with this company across common executive titles. NEVER
    fetches linkedin.com — only reads Serper's already-indexed titles/
    snippets for /in/ pages (see module docstring).

    This is a SEPARATE, lower-priority source from decision_maker_
    extractor.py's website-based extraction — see phase1_pipeline.py's
    orchestration for why website sources are always tried first (Step 8).

    Returns a list of {name, role, department, email, phone, linkedin,
    photo, location, source, source_url, confidence, query_used,
    timestamp} dicts — the same unified per-person schema decision_maker_
    extractor.py/schema_org_extractor.py/public_search_decision_makers.py
    all produce (plus X-Ray-specific query_used/timestamp for Step 9's
    audit trail), so phase1_pipeline.py's merge across sources never has to
    special-case this one's shape. Deduped by profile URL (keeping the
    highest-confidence sighting), sorted highest-confidence first, capped
    at `max_people`.
    """
    # Local import: decision_maker_extractor already imports FROM this
    # module indirectly via public_search_decision_makers's chain, so this
    # stays a function-local import to avoid any load-order surprise.
    from decision_maker_extractor import infer_department

    queries = generate_decision_maker_xray_queries(company_name, domain, city, country)
    candidates: Dict[str, Dict[str, Any]] = {}
    now = time.time()

    for query in queries:
        results = await search_google_xray(session, query)
        for item in results:
            link = item.get("link", "")
            if "linkedin.com/in/" not in link.lower():
                continue
            name, role, company_segment = _parse_linkedin_in_title(item.get("title", ""))
            if not name:
                continue
            confidence = rank_linkedin_candidates(
                result_title=item.get("title", ""), snippet=item.get("snippet", "") or "",
                company_name=company_name, domain=domain, city=city,
                country=country, industry=industry, company_segment=company_segment,
            )
            if confidence < MIN_PERSON_CONFIDENCE:
                continue
            existing = candidates.get(link)
            if existing is None or confidence > existing["confidence"]:
                candidates[link] = {
                    "name": name, "role": role, "department": infer_department(role or ""),
                    "email": None, "phone": None, "linkedin": link, "photo": None,
                    "location": None, "source": "linkedin_xray", "source_url": link,
                    "confidence": confidence, "query_used": query, "timestamp": now,
                }
        if len(candidates) >= max_people:
            break

    ranked = sorted(candidates.values(), key=lambda c: c["confidence"], reverse=True)
    return ranked[:max_people]


# ===========================================================================
# Storage / caching (Step 9/10)
# ===========================================================================
def _candidates_age_days(data: Dict[str, Any]) -> Optional[float]:
    ts = data.get("timestamp")
    if not isinstance(ts, (int, float)):
        return None
    return (time.time() - ts) / 86400


def is_cache_fresh(data: Optional[Dict[str, Any]]) -> bool:
    """Step 10 — True if a stored linkedin_candidates.json is still within
    LINKEDIN_CANDIDATES_TTL_DAYS. None/missing timestamp counts as stale
    (forces a refresh) rather than trusting an undated record forever."""
    if not data:
        return False
    age = _candidates_age_days(data)
    return age is not None and age <= LINKEDIN_CANDIDATES_TTL_DAYS


def save_linkedin_candidates(
    store: Any, domain: str, company_page: Optional[str],
    profiles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Step 9/11 — assemble + persist linkedin_candidates.json via the
    storage layer (see storage.write_linkedin_candidates_now). The ONE
    named interface point for how discovery results get saved — a future
    provider swap only needs to keep producing `profiles` in this shape,
    not know anything about the storage layer itself.

    Never loses discovery metadata (Step 9): every profile already carries
    its own source/confidence/query_used/timestamp (see discover_decision_
    makers) — this function only adds the top-level timestamp and
    company_page reference.
    """
    data = {
        "company_page": company_page,
        "profiles": profiles,
        "timestamp": time.time(),
    }
    store.write_linkedin_candidates_now(domain, data)
    return data
