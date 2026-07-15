from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List

from dotenv import load_dotenv
from groq import APIStatusError, Groq


def get_client() -> Groq:
    """Create Groq client; called only from CLI entrypoints (not on import)."""
    load_dotenv()

    api_key = os.getenv("groq_llm_apikey1") or os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing Groq API key. Put `groq_llm_apikey1=...` in .env (same folder as this script) "
            "or set `GROQ_API_KEY` env var."
        )

    return Groq(api_key=api_key)


_CALL_LLM_RETRIES = 2          # attempts per model (1 initial + 1 retry)
_CALL_LLM_BACKOFF_S = 1.5      # base backoff between retries of the SAME model
_CALL_LLM_MAX_TOKENS = 4000    # generous cap: the planner's verbose reasoning
                                # + expanded keyword lists can run long; a too-
                                # low default cuts the response off mid-JSON,
                                # which surfaces as a "json_validate_failed" /
                                # "max completion tokens reached" 400 — not a
                                # transient error, so retrying it is pointless.
# Status codes worth retrying the SAME model for: rate limits and server-side
# errors are transient. 4xx client errors (bad request, invalid JSON schema,
# auth, not-found) are deterministic — retrying them wastes time and just
# delays falling through to the fallback model.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    return True  # connection errors, timeouts, etc. — no status_code at all


def call_llm(
    client: Groq,
    messages: List[Dict[str, str]],
    response_format: Dict[str, str] | None = None,
) -> str:
    """Call LLM with a primary/fallback model. Returns raw assistant text.

    `response_format={"type": "json_object"}` forces strict JSON output (used
    by the planner). Temperature is pinned to 0 for deterministic plans.

    Each model gets up to `_CALL_LLM_RETRIES` attempts with a short backoff
    before falling through to the next model — but only for TRANSIENT
    failures (429/5xx/timeouts/connection errors); a deterministic 4xx (e.g.
    a schema-invalid or truncated JSON response) skips straight to the next
    model instead of retrying something that will fail identically every
    time. Callers whose failure mode is "silently treat as unavailable"
    (e.g. the lead relevance classifier) rely on this to make that failure
    mode rare, not routine.
    """
    primary_model = "openai/gpt-oss-20b"
    fallback_model = "openai/gpt-oss-120b"

    kwargs: Dict[str, Any] = {
        "temperature": 0,
        "max_completion_tokens": _CALL_LLM_MAX_TOKENS,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format

    last_exc: Exception | None = None
    for model in (primary_model, fallback_model):
        for attempt in range(_CALL_LLM_RETRIES):
            try:
                response = client.chat.completions.create(
                    model=model, messages=messages, **kwargs
                )
                return response.choices[0].message.content
            except Exception as e:  # noqa: BLE001
                last_exc = e
                print(f"Model {model} failed (attempt {attempt + 1}/"
                      f"{_CALL_LLM_RETRIES}): {e}")
                if not _is_retryable(e):
                    break  # deterministic failure — don't retry, try next model
                if attempt < _CALL_LLM_RETRIES - 1:
                    time.sleep(_CALL_LLM_BACKOFF_S * (attempt + 1))

    raise last_exc  # both models exhausted their retries


# System role: explains the WHOLE pipeline to the model so it knows exactly how
# each field is consumed downstream. This is what makes the plan executable.
PLANNER_SYSTEM_PROMPT = """\
You are the Query Deconstruction Engine for an automated B2B lead-generation \
pipeline. A salesperson types a messy natural-language request; you turn it into \
one precise JSON plan that downstream tools execute WITHOUT any further human help.

How your JSON is consumed downstream (this dictates how you must fill each field):
  1. DISCOVERY: `search_query` is sent VERBATIM to a Google web search (via Serper) \
to pull candidate business websites. Write it the way a human would type it into \
Google to find many such businesses: usually "<category> in <place>" \
(e.g. "marinas in Dubai"). Keep it broad enough to return lots of real \
businesses — do NOT bake the exclusion words into it (we filter those later), \
and do NOT add quotes or operators. If the city name could plausibly exist in \
more than one country/region (e.g. "Venice" is both Italy and Florida, \
"Cambridge" is both UK and Massachusetts, "Portland" is both Oregon and \
Maine), include the disambiguating region/country in `search_query` too \
(e.g. "salons in Venice, Italy", not just "salons in Venice") — do not rely \
on geo_location/country_code alone to carry that disambiguation.
  2. SCALE: `result_limit` tells the pipeline how many businesses to pull. Read it \
from the request ("give me 50 marinas..." -> 50). If the user gives no number, \
use 20.
  3. QUALIFICATION: after each candidate's website is scraped to plain text, \
`exclude_keywords` and `include_keywords` are matched case-insensitively as \
substrings against that text to DROP or KEEP the lead.

The single most important rule — KEYWORD EXPANSION:
A real website almost never uses the user's exact wording. If the user says \
"no smart monitoring tools", the site will instead say "IoT sensors", "remote \
telemetry", "real-time dashboard", "vessel tracking", etc. So you must EXPAND \
every constraint into the full set of realistic surface forms that would appear \
on such a website: synonyms, abbreviations, acronyms, product/tech names, and \
common phrasings. A single literal phrase is a FAILURE — aim for 6-15 varied, \
lowercase terms per concept. Do NOT include the generic industry word itself \
(e.g. don't put "marina") as a keyword.

PRECISION GUARD (equally important): every keyword is substring-matched, so it \
must be SPECIFIC enough not to match unrelated text. NEVER output a bare generic \
word like "app", "access", "entry", "gate", "system", "online", "digital", \
"smart" on its own — these cause false matches ("app" hits "happy"/"appetizer"). \
Always qualify them into a 2+ word phrase ("mobile app", "online booking", \
"smart gate access"). Prefer distinctive multi-word phrases over short fragments.

Decomposition steps you must perform internally before writing JSON:
  A. Identify the core business CATEGORY to search for (singular, generic).
  B. Identify and normalize the LOCATION (city + region/country if inferable).
  C. Extract the requested COUNT. Set `count_explicit` to true ONLY if the \
user's text itself states a number of businesses (e.g. "50 marinas", "find 10 \
salons", "give me twenty..."). If NO number appears anywhere in the request \
(e.g. "find salons in Spain", "marinas in Dubai with no CRM"), set \
`count_explicit` to false and `result_limit` to 20 — the caller ignores that \
default value whenever count_explicit is false, so its exact number doesn't \
matter, but it must still be present and > 0. When count_explicit is true, \
result_limit MUST be the exact number the user stated.
  D. Detect NEGATIVE constraints (no / without / lacking / excluding / not using) \
-> exclude_keywords. Expand each per the rule above. Note: business jargon and \
acronyms count — e.g. "no crm" must expand to "crm", "customer relationship \
management", "hubspot", "salesforce", "zoho crm", "pipedrive", etc.
  E. Detect POSITIVE constraints (must have / with / using / that offer) \
-> include_keywords. Expand each.
  F. Decide intent: "find" if there are no constraints, else "find_and_filter".
  G. Classify SEARCH TYPE into search_type:
     - "specific": the request targets ONE named business or a website/domain \
(e.g. "xyzmarina.com", "info on Blue Bay Marina", "is acme-marina.com using a \
CRM?"). When a domain/URL is present, put the bare domain (no scheme, no path, \
no www) in target_domain, e.g. "xyzmarina.com". If only a business name is given \
with no domain, set target_domain to "".
     - "general": a category + place that should return MANY businesses \
(e.g. "marinas in Dubai", "give me 50 pizza shops in NYC"). Set target_domain "".
  H. Determine country_code: ISO 3166-1 alpha-2 for the geo_location (e.g. "US", \
"AE", "GB", "AU", "SG"). Use "" when unknown or multiple countries.
  I. Generate phone_regex: a Python regex string (no flags, no re.compile wrapper) \
that matches the standard phone formats for that specific country. Rules:
     - Use look-around boundaries (?<!\\d) / (?!\\d) so zip codes or version \
numbers cannot be mistaken for phone numbers. A digit-only blob like "77586" \
or "1998-2026" must NOT match.
     - Handle the optional country prefix AND the local format (with/without the \
leading zero or area code in parentheses).
     - Cover mobile AND landline formats for that country.
     - US  : r"(?:(?:\\+1|1)[\\s.\\-]?)?(?<!\\d)(?:\\([2-9]\\d{2}\\)|[2-9]\\d{2})[\\s.\\-]?[2-9]\\d{2}[\\s.\\-]?\\d{4}(?!\\d)"
     - UAE : r"(?:\\+971|00971|0)[\\s.\\-]?(?:2|3|4|6|7|9|5[024568])[\\s.\\-]?\\d{3}[\\s.\\-]?\\d{4}(?!\\d)"
     - UK  : r"(?:\\+44|0)[\\s.\\-]?(?:7\\d{9}|[1-9]\\d{8,9})(?!\\d)"
     (The double-backslash is required because these are JSON string values.)
     The regex will be compiled with re.IGNORECASE | re.MULTILINE by the pipeline.
  J. Detect whether the request needs TECHNOLOGY STACK ANALYSIS \
-> needs_tech_stack (boolean). This is a SEPARATE, OPTIONAL pipeline stage that \
only runs after the normal lead collection above. Set it true ONLY when the \
request is actually asking about a website's technology, not just business \
qualities. Examples that must be true: "does this company use a CRM?", "what \
tech stack does this site use?", "is their website outdated?", "are they using \
WordPress?", "do they use React?", "what technologies power this website?", \
"do they have a customer portal?", "should we pitch a website redesign?", \
"what services could we offer based on their current tech?". Set it false for \
ordinary lead requests, even ones that happen to mention a technology as an \
EXCLUDE/INCLUDE keyword filter (e.g. "marinas with no CRM" is a keyword filter \
on exclude_keywords, NOT a tech-stack analysis request -> false). Default false \
whenever unsure.

Output rules: respond with ONLY a single valid JSON object. No markdown, no prose.
"""

# One fully worked example anchors the format and the expansion behaviour.
PLANNER_EXAMPLE = {
    "geo_location": "Miami, Florida, USA",
    "broad_industry": "marina",
    "search_query": "marinas in Miami",
    "result_limit": 20,
    "count_explicit": False,
    "search_type": "general",
    "target_domain": "",
    "intent": "find_and_filter",
    "country_code": "US",
    "phone_regex": r"(?:(?:\+1|1)[\s.\-]?)?(?<!\d)(?:\([2-9]\d{2}\)|[2-9]\d{2})[\s.\-]?[2-9]\d{2}[\s.\-]?\d{4}(?!\d)",
    "needs_tech_stack": False,
    "exclude_keywords": [
        "smart monitoring", "remote monitoring", "real-time monitoring",
        "iot", "internet of things", "sensors", "telemetry",
        "vessel tracking", "digital dashboard", "online dashboard",
        "automated alerts", "connected devices",
    ],
    "include_keywords": [],
    "reasoning": (
        "User wants marinas in Miami that do NOT use smart/IoT monitoring "
        "technology. No number of businesses was stated, so count_explicit is "
        "false and result_limit is a placeholder the caller will ignore in "
        "favor of its own small default. exclude_keywords cover the realistic "
        "surface forms such tech uses on marina websites so the Step-5 filter "
        "can detect them."
    ),
}


def plan_query(user_query: str) -> Dict[str, Any]:
    """Decompose a messy human query into a strict, executable JSON plan.

    Output schema:
      geo_location      - normalized place string
      broad_industry    - singular business category to search for
      search_query      - natural Google web-search query for Serper discovery
      result_limit      - how many businesses to pull; meaningful ONLY when
                          count_explicit is true (the caller substitutes its
                          own small default otherwise)
      count_explicit    - True only if the user's own text stated a number of
                          businesses; False means no count was given at all
      search_type       - "general" (category+place) or "specific" (one business)
      target_domain     - bare domain for a specific search, else ""
      intent            - "find" or "find_and_filter"
      country_code      - ISO 3166-1 alpha-2 for the location ("US", "AE", …)
      phone_regex       - Python regex matching that country's phone formats
      needs_tech_stack  - True only if the request asks about website technology
                          (CRM/CMS/framework/outdated-site/redesign questions);
                          gates the optional Tech Stack Detection stage, which
                          runs after normal lead collection, never instead of it
      exclude_keywords  - expanded lowercase phrases that DISqualify a lead
      include_keywords  - expanded lowercase phrases that a lead should mention
      reasoning         - short note on how the query was interpreted
    """
    example_json = json.dumps(PLANNER_EXAMPLE, ensure_ascii=False, indent=2)

    user_content = (
        "Deconstruct this lead-generation request into the JSON plan.\n\n"
        f"REQUEST: {user_query}\n\n"
        "Follow this exact shape (your values WILL differ; expand the keywords "
        "thoroughly for THIS request):\n"
        f"{example_json}"
    )

    client = get_client()
    text = call_llm(
        client,
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
    )

    # Be robust: if the model returns accidental text, attempt JSON extraction.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


VALID_CATEGORIES = (
    "match", "product_shop", "aggregator", "listicle", "unrelated", "wrong_location",
)


def classify_business(
    industry: str, geo: str, name: str, page_text: str
) -> Dict[str, str]:
    """Classify a scraped website by relevance to the lead request.

    Returns {"category", "reason"}. Only "match" is a usable lead; every other
    category is a reason to DROP the result:
      match         - a single real `industry` business in the right place
      product_shop  - an online store selling products, not a service business
      aggregator    - a directory / marketplace / booking platform (many listings)
      listicle      - a blog/article/ranking that lists many businesses
      unrelated     - not an `industry` business at all
      wrong_location- a real business but NOT in the requested location

    On malformed output (unparseable JSON, unexpected category string) this
    fails CLOSED to "unrelated" — a classifier hiccup must never silently
    admit a bad lead; it should simply mean one fewer qualified result.
    """
    snippet = (page_text or "")[:1800]  # cap tokens; the gist is near the top
    system = (
        "You are the Lead Relevance Classifier for a B2B lead-generation "
        f"pipeline. The salesperson wants INDIVIDUAL '{industry}' businesses "
        f"located in '{geo}' that they can contact and sell to. The single "
        "correct answer for a usable lead is a real business's OWN official "
        "website.\n\n"
        "Classify the website into EXACTLY one category:\n"
        f"  - \"match\": ONE real {industry} business, its OWN official site, "
        f"physically in/near '{geo}'. A usable lead.\n"
        "  - \"product_shop\": primarily an online store selling physical products "
        "(cart, checkout, shipping), not a local service business.\n"
        "  - \"aggregator\": a directory/marketplace/booking platform that lists or "
        "books MANY businesses (Yelp, TripAdvisor, Zomato, Justdial, OpenTable…).\n"
        "  - \"listicle\": a blog/news/article/ranking/'best of'/'guide' page that "
        "describes or lists multiple businesses (titles like 'Best cafes in X', "
        "'Top 10…', 'Exploring…', city blogs, WordPress/Medium posts).\n"
        f"  - \"wrong_location\": a real {industry} business but NOT in '{geo}' "
        "(e.g. a different city or country).\n"
        f"  - \"unrelated\": not a {industry} business at all (app store, social "
        "media, unrelated company, error page).\n\n"
        "Be STRICT: if the page lists many different businesses, it is an "
        "aggregator or listicle, never a match. When the location clearly differs "
        f"from '{geo}', return wrong_location.\n\n"
        "WATCH FOR NAME/PLACE COINCIDENCE: the word "
        f"'{industry}' appearing on the page is NOT enough by itself. It may "
        "appear only because it's part of a NEIGHBORHOOD, BUILDING, or AREA "
        f"NAME (e.g. a hotel, restaurant, or shop that happens to be located "
        f"in a place called 'Marina del Rey' will mention \"marina\" constantly "
        f"without itself being a marina). Judge by what the business's OWN "
        f"CORE OFFERING actually is — what it sells or does — not by incidental "
        f"word overlap with its address, neighborhood, or view. Likewise, a "
        f"business in a related or adjacent field (e.g. a boat rental/club, a "
        f"boat tour company, a marine-supply store) is NOT the same as a "
        f"'{industry}' business unless its own core service literally IS what a "
        f"'{industry}' provides — return \"unrelated\" for merely adjacent or "
        "themed businesses, even if boats/water/the industry word feature "
        "heavily in their marketing copy.\n"
        'Respond with ONLY JSON: {"category": "...", "reason": "<one short line>"}.'
    )
    user = f"BUSINESS NAME: {name}\n\nWEBSITE TEXT:\n{snippet}"

    client = get_client()
    text = call_llm(
        client,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1]) if start != -1 and end > start else {}

    category = str(data.get("category", "")).strip().lower()
    if category not in VALID_CATEGORIES:
        # A malformed/unexpected category string is a classifier hiccup, not
        # evidence the business matches — fail CLOSED (caller's _is_lead()
        # treats anything other than "match" as not-a-lead), same principle
        # as the model-unavailable path in phase1_pipeline.classify_relevance.
        category = "unrelated"
    return {"category": category, "reason": str(data.get("reason", "")) or "malformed_classifier_output"}


def generate_query_variations(
    user_query: str, industry: str, geo: str, exclude: List[str], max_variations: int = 5
) -> List[str]:
    """Generate additional Google search-query variations that preserve the
    SAME industry + location intent as the original request — used ONLY when
    the original query's discovery has been fully exhausted and more
    candidates are still needed to reach an explicitly-requested count.

    NEVER broadens the industry or the country/region: a variation may vary
    phrasing/synonyms, add search operators (e.g. "site:.es"), or narrow to
    specific well-known cities within the SAME requested location — never
    widen to a different industry or a different country.

    Returns up to `max_variations` new query strings not already in `exclude`
    (case-insensitive). Raises on failure (missing client, bad response) —
    the caller degrades to "no more variations" (treats discovery as
    genuinely exhausted) rather than ever broadening scope to compensate.
    """
    exclude_lower = {q.strip().lower() for q in exclude}
    system = (
        "You generate ADDITIONAL Google search queries for a B2B lead-generation "
        "crawler whose first search query didn't turn up enough real businesses. "
        f"The user wants '{industry}' businesses located in '{geo}' — every "
        "variation you produce MUST preserve exactly that industry and exactly "
        "that location/country. NEVER broaden the industry (e.g. don't widen "
        "'hair salon' to 'beauty business') and NEVER broaden the location (e.g. "
        "don't widen 'Spain' to 'Europe', don't switch to a different country). "
        f"You MAY narrow the location to specific well-known cities within "
        f"'{geo}', vary the industry phrasing with close synonyms, or add search "
        'operators (e.g. "site:.es").\n\n'
        "Respond with ONLY valid JSON of exactly this form: "
        '{"variations": ["query one", "query two", ...]}\n'
        f"Produce up to {max_variations} variations, each a short natural Google "
        "search query (no explanation, no surrounding quotes)."
    )
    user = (
        f"Original request: {user_query}\n"
        f"Already-tried search queries (do not repeat these): "
        f"{json.dumps(exclude, ensure_ascii=False)}"
    )

    client = get_client()
    text = call_llm(
        client,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1]) if start != -1 and end > start else {}

    raw_variations = data.get("variations")
    if not isinstance(raw_variations, list):
        return []
    variations: List[str] = []
    for v in raw_variations:
        if not isinstance(v, str):
            continue
        v = v.strip()
        if v and v.lower() not in exclude_lower:
            exclude_lower.add(v.lower())
            variations.append(v)
        if len(variations) >= max_variations:
            break
    return variations


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Planner: verify API + produce structured plans")
    parser.add_argument("--test-api", action="store_true", help="Run a minimal Groq API test")
    parser.add_argument(
        "--plan",
        type=str,
        default=None,
        help="Generate planning JSON for the given query string",
    )

    args = parser.parse_args()

    if not args.test_api and not args.plan:
        parser.print_help()
        raise SystemExit(2)

    client = get_client()

    if args.test_api:
        messages = [{"role": "user", "content": "Say 'API is working' in one line."}]
        out = call_llm(client, messages)
        print(out)
        return

    if args.plan is not None:
        blueprint = plan_query(args.plan)
        print(json.dumps(blueprint, ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    main()

