from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from groq import Groq


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


def call_llm(
    client: Groq,
    messages: List[Dict[str, str]],
    response_format: Dict[str, str] | None = None,
) -> str:
    """Call LLM with a primary/fallback model. Returns raw assistant text.

    `response_format={"type": "json_object"}` forces strict JSON output (used
    by the planner). Temperature is pinned to 0 for deterministic plans.
    """
    primary_model = "openai/gpt-oss-20b"
    fallback_model = "openai/gpt-oss-120b"

    kwargs: Dict[str, Any] = {"temperature": 0}
    if response_format is not None:
        kwargs["response_format"] = response_format

    try:
        response = client.chat.completions.create(
            model=primary_model, messages=messages, **kwargs
        )
    except Exception as e:
        print(f"Primary model failed ({primary_model}): {e}")
        response = client.chat.completions.create(
            model=fallback_model, messages=messages, **kwargs
        )

    return response.choices[0].message.content


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
and do NOT add quotes or operators.
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
  C. Extract the requested COUNT into result_limit (default 20 if none given).
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

Output rules: respond with ONLY a single valid JSON object. No markdown, no prose.
"""

# One fully worked example anchors the format and the expansion behaviour.
PLANNER_EXAMPLE = {
    "geo_location": "Miami, Florida, USA",
    "broad_industry": "marina",
    "search_query": "marinas in Miami",
    "result_limit": 20,
    "search_type": "general",
    "target_domain": "",
    "intent": "find_and_filter",
    "country_code": "US",
    "phone_regex": r"(?:(?:\+1|1)[\s.\-]?)?(?<!\d)(?:\([2-9]\d{2}\)|[2-9]\d{2})[\s.\-]?[2-9]\d{2}[\s.\-]?\d{4}(?!\d)",
    "exclude_keywords": [
        "smart monitoring", "remote monitoring", "real-time monitoring",
        "iot", "internet of things", "sensors", "telemetry",
        "vessel tracking", "digital dashboard", "online dashboard",
        "automated alerts", "connected devices",
    ],
    "include_keywords": [],
    "reasoning": (
        "User wants marinas in Miami that do NOT use smart/IoT monitoring "
        "technology. exclude_keywords cover the realistic surface forms such "
        "tech uses on marina websites so the Step-5 filter can detect them."
    ),
}


def plan_query(user_query: str) -> Dict[str, Any]:
    """Decompose a messy human query into a strict, executable JSON plan.

    Output schema:
      geo_location      - normalized place string
      broad_industry    - singular business category to search for
      search_query      - natural Google web-search query for Serper discovery
      result_limit      - how many businesses to pull (from the query; default 20)
      search_type       - "general" (category+place) or "specific" (one business)
      target_domain     - bare domain for a specific search, else ""
      intent            - "find" or "find_and_filter"
      country_code      - ISO 3166-1 alpha-2 for the location ("US", "AE", …)
      phone_regex       - Python regex matching that country's phone formats
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


VALID_CATEGORIES = ("match", "product_shop", "aggregator", "unrelated")


def classify_business(
    industry: str, geo: str, name: str, page_text: str
) -> Dict[str, str]:
    """Classify a scraped website by relevance to the lead request.

    Returns {"category", "reason"} where category is one of:
      match        - a single real `industry` business (a usable lead)
      product_shop - primarily an online store selling products, not a service
      aggregator   - a directory / marketplace / booking platform listing many
      unrelated    - not an `industry` business at all

    On any malformed/uncertain output we default to "match" so a classifier
    hiccup never silently discards a real lead.
    """
    snippet = (page_text or "")[:1500]  # cap tokens; the gist is near the top
    system = (
        "You are the Lead Relevance Classifier for a B2B lead-generation "
        f"pipeline. The salesperson wants individual '{industry}' businesses in "
        f"'{geo}' that they can contact and sell to.\n\n"
        "Classify the website into EXACTLY one category:\n"
        f"  - \"match\": a single real {industry} business that provides its own "
        "services at its own location(s) — a usable lead.\n"
        "  - \"product_shop\": primarily an online store selling physical products "
        "(cart, checkout, 'add to cart', shipping), not a service business.\n"
        "  - \"aggregator\": a directory, marketplace, or booking platform that "
        "lists or books MANY different businesses (e.g. 'find and book near you').\n"
        f"  - \"unrelated\": not a {industry} business at all.\n\n"
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
        category = "match"
    return {"category": category, "reason": str(data.get("reason", ""))}


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

