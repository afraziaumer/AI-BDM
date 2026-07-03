"""
AI-BDM pipeline entry point.

One natural-language query runs the whole pipeline through Step 3:

    Step 1  Intent planning        (LLM_planner via phase1_pipeline)
    Step 2  Discovery + scraping   (phase1_pipeline.run_pipeline -> stores raw HTML)
    Step 3  High-intent routing    (route_filter, per scraped business)

Usage:
    ./env/bin/python main.py --query "give me 10 marinas in dubai with no mobile apps"

Step 3 reads each business's homepage HTML from the raw store
(scavenger_leads_cache.csv), plus the user's query and the knowledge gaps the
planner derived (e.g. "no mobile apps" -> mobile-app signals to look for).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Dict

import phase1_pipeline as p1
import route_filter as rf

logger = logging.getLogger("ai_bdm.main")


async def run(query: str, concurrency: int = 5) -> None:
    # ---- Step 1 (plan) + Step 2 (discover + scrape + store homepage HTML) ----
    summary = await p1.run_pipeline(query, concurrency=concurrency)
    p1.print_summary(summary)

    plan = summary.get("plan") or {}
    # Knowledge gaps for Step 3 = what the planner says to look for on each site.
    knowledge_gaps = plan.get("exclude_keywords") or []

    # Every distinct business discovered in Step 2 (homepage HTML is in the store).
    businesses: Dict[str, str] = {}
    for r in summary.get("results", []):
        website = r.get("website_url")
        if website and website not in businesses:
            businesses[website] = r.get("company_name") or ""

    print("\n" + "#" * 68)
    print(f"STEP 3 — HIGH-INTENT ROUTE FILTERING  ({len(businesses)} business(es))")
    print(f"knowledge gaps: {', '.join(knowledge_gaps) or '(none)'}")
    print("#" * 68)

    if not businesses:
        print("No businesses discovered — nothing to route.")
        return

    routed = 0
    # domain -> JSON list of that business's high-intent routes (for leads_clean.csv).
    routes: Dict[str, str] = {}
    for website, name in businesses.items():
        loaded = rf.load_homepage_from_store(website)
        if not loaded:
            logger.info("No homepage HTML stored for %s — skipping Step 3.", website)
            continue
        base_url, html = loaded
        result = rf.select_high_intent_routes(html, base_url, query, knowledge_gaps)
        routed += 1
        routes[p1._domain_key(base_url)] = json.dumps(result.selected, ensure_ascii=False)
        print(f"\n■ {name or base_url}")
        print(f"    candidates: {len(result.candidate_links)}  |  method: {result.selection_method}")
        for s in result.selected:
            print(f"    [{s['priority']}] ({s.get('confidence', '?')}) {s['url']}")
            print(f"         reason: {s.get('reason', '')}")

    print(f"\nStep 3 complete: routed {routed}/{len(businesses)} business(es).")

    # ---- Cleaning: build leads_clean.csv (with high_intent_pages) from raw store ----
    import data_pipeline
    print("\n[clean] Building leads_clean.csv...")
    data_pipeline.run(p1.OUTPUT_CSV_FILE, routes=routes)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI-BDM: run a query through Step 1 -> Step 2 -> Step 3.")
    parser.add_argument("--query", required=True, help="Natural-language lead query.")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="Concurrent scrape workers for Step 2.")
    args = parser.parse_args()
    asyncio.run(run(args.query, args.concurrency))


if __name__ == "__main__":
    main()
