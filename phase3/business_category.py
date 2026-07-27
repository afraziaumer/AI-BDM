"""Phase 3 — business category classification.

Sorts each already-committed business into one of a fixed set of ~24 broad
niche categories (Health & Medical, Automotive, Marinas & Boating, ...) so
the right review platforms can be picked per niche (e.g. Zomato/TripAdvisor
for Tourism & Travel, no point checking a marina review site for a dental
clinic).

Deliberately a keyword lookup, not an LLM call: the business's industry is
already known (`validated_industry`, extracted by the intent planner during
Phase 1) — classifying "dentist" -> "Health & Medical" needs no judgment
call an LLM would do better than a lookup table, and this way it's free,
instant, and deterministic. Falls back to matching the company name/page
title when validated_industry is missing or doesn't hit any keyword.

Run:
  python -m phase3.business_category --domain skydentalnyc.com --industry dentist
  python -m phase3.business_category --all
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from typing import Dict, List, Optional

from phase3 import store

PLATFORM = "category"
UNCATEGORIZED = "Uncategorized"

# Order matters only in that the FIRST category whose keywords match wins —
# keep more specific categories (Dental-ish terms under Health & Medical)
# ahead of anything that could plausibly overlap.
CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "Health & Medical": [
        "dentist", "dental", "clinic", "hospital", "medical", "doctor",
        "physician", "physiotherapy", "dermatology", "orthodontic",
        "healthcare", "diagnostic", "pharmacy", "surgeon", "surgery",
    ],
    "Beauty & Aesthetics": [
        "salon", "spa", "aesthetic", "cosmetic", "beauty", "skincare",
        "hair", "nail", "makeup", "waxing", "botox", "filler",
    ],
    "Automotive": [
        "car dealer", "car rental", "auto repair", "automotive", "garage",
        "mechanic", "tyre", "tire", "car wash", "dealership", "motors",
    ],
    "Marinas & Boating": [
        "marina", "yacht", "boat", "boating", "dock", "sailing", "vessel",
    ],
    "Tourism & Travel": [
        "travel agency", "tour operator", "tourism", "travel agent",
        "excursion", "sightseeing", "vacation package",
    ],
    "Hospitality & Hotels": [
        "hotel", "resort", "guesthouse", "motel", "inn", "hostel",
        "bed and breakfast",
    ],
    "Restaurants & Dining": [
        "restaurant", "dining", "eatery", "bistro", "diner", "buffet",
    ],
    "Cafes & Bakeries": [
        "cafe", "coffee shop", "bakery", "patisserie", "confectionery",
    ],
    "Fitness & Gyms": [
        "gym", "fitness", "yoga", "pilates", "crossfit", "personal trainer",
    ],
    "Education & Training": [
        "school", "academy", "tutoring", "coaching center", "training institute",
        "college", "university", "learning center",
    ],
    "Legal Services": [
        "law firm", "lawyer", "attorney", "legal services", "advocate",
    ],
    "Financial Services": [
        "accounting", "accountant", "insurance", "bank", "financial advisor",
        "tax services", "bookkeeping",
    ],
    "Real Estate": [
        "real estate", "realtor", "property management", "realty",
    ],
    "Construction & Contractors": [
        "construction", "contractor", "builder", "renovation", "roofing",
    ],
    "Home Services": [
        "plumbing", "plumber", "electrician", "hvac", "cleaning service",
        "pest control", "landscaping", "handyman",
    ],
    "Retail & Shopping": [
        "retail store", "boutique", "shop", "store", "mall",
    ],
    "IT & Software Services": [
        "software", "it services", "web development", "app development",
        "tech company", "saas",
    ],
    "Marketing & Advertising": [
        "marketing agency", "advertising", "digital marketing", "seo agency",
        "branding",
    ],
    "Manufacturing & Industrial": [
        "manufacturing", "factory", "industrial", "fabrication",
    ],
    "Logistics & Transportation": [
        "logistics", "shipping", "freight", "courier", "transportation",
    ],
    "Event Planning": [
        "event planning", "wedding planner", "event management",
    ],
    "Pet Services": [
        "veterinary", "vet clinic", "pet grooming", "pet boarding", "pet store",
    ],
    "Professional Services & Consulting": [
        "consulting", "consultancy", "hr services", "business advisory",
    ],
    "Entertainment & Recreation": [
        "cinema", "amusement park", "entertainment venue", "recreation center",
        "bowling", "arcade",
    ],
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def categorize(industry: str = "", company_name: str = "", page_title: str = "") -> str:
    """Return the best-matching category, or UNCATEGORIZED if nothing hits.

    Checks `industry` first (the most reliable, already-validated signal),
    then falls back to scanning company_name/page_title for the same
    keywords if industry alone didn't match anything.
    """
    haystacks = [_norm(industry), _norm(f"{company_name} {page_title}")]
    for haystack in haystacks:
        if not haystack:
            continue
        for category, keywords in CATEGORY_KEYWORDS.items():
            if any(kw in haystack for kw in keywords):
                return category
    return UNCATEGORIZED


def categorize_domain(domain: str, industry: str = "", company_name: str = "",
                       page_title: str = "") -> str:
    """Cache-first categorization for one domain — same pattern as
    google_maps.py's enrich_domain: check the cache first, only compute
    (here: a cheap lookup, not a network call) on a miss."""
    cached = store.load(domain, PLATFORM, max_age_days=None)  # category never goes stale
    if cached is not None:
        return cached["category"]
    category = categorize(industry, company_name, page_title)
    store.save(domain, PLATFORM, {"category": category})
    return category


def categorize_all(index_path: str = "crawl_index.csv") -> Dict[str, str]:
    """Categorize every distinct domain in the crawl index. Returns
    {domain: category}."""
    with open(index_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    seen: Dict[str, str] = {}
    for row in rows:
        domain = row.get("domain", "")
        if not domain or domain in seen:
            continue
        category = categorize_domain(
            domain,
            industry=row.get("validated_industry", ""),
            company_name=row.get("company_name", ""),
            page_title=row.get("page_title", ""),
        )
        seen[domain] = category
    return seen


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sort a business (or every committed business) into a niche category."
    )
    parser.add_argument("--domain", default=None, help="Single-business mode: the domain key.")
    parser.add_argument("--industry", default="", help="Single-business mode: validated_industry.")
    parser.add_argument("--company-name", default="", help="Single-business mode: company name.")
    parser.add_argument("--all", action="store_true",
                        help="Categorize every domain in crawl_index.csv.")
    parser.add_argument("--index", default="crawl_index.csv",
                        help="Path to crawl_index.csv (only used with --all).")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.all:
        results = categorize_all(args.index)
        by_category: Dict[str, List[str]] = {}
        for domain, category in results.items():
            by_category.setdefault(category, []).append(domain)
        for category, domains in sorted(by_category.items()):
            print(f"\n{category} ({len(domains)}):")
            for d in domains:
                print(f"  - {d}")
        return

    if args.domain:
        category = categorize_domain(args.domain, args.industry, args.company_name)
        print(f"{args.domain} -> {category}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
