"""
AI-BDM pipeline entry point.

One natural-language query runs the whole pipeline:

    Step 1  Intent planning        (LLM_planner via phase1_pipeline)
    Step 2  Discovery + scraping   (phase1_pipeline.run_pipeline -> stores raw HTML)
    Step 3  High-intent routing    (route_planner, per committed business)
    Step 4  Tech Stack Detection   (tech_stack, ONLY if the query's intent needs it)

Usage:
    ./env/bin/python main.py --query "give me 10 marinas in dubai with no mobile apps"
    ./env/bin/python main.py --query "does acme-marina.com use a CRM?"

Step 3 reads each business's committed cleaned .txt previews from the storage
layer (written by the streaming crawler) and picks the highest-intent pages —
see route_planner.py. Raw HTML is never stored.

Note: this used to call the older route_filter.select_routes_for_site(), a
second, separate LLM call doing the same job as route_planner.py's Step 5
(phase1_pipeline.py's own main() already uses route_planner). That duplicate
LLM layer has been removed — both entry points now go through the same
route_planner.py. route_filter.py itself is still used (its normalize_urls
helper is a dependency of route_planner.py), just not its LLM call anymore.

Step 4 is optional and gated entirely by the planner's `needs_tech_stack` flag
(set only for tech/CRM/CMS/redesign-style questions — see LLM_planner.py). It
always runs AFTER Phase 1 has already discovered and crawled the businesses; it
never triggers a crawl itself and never bypasses Phase 1's normal workflow.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Dict

import phase1_pipeline as p1
import route_planner as rp

logger = logging.getLogger("ai_bdm.main")


async def run(query: str, concurrency: int = 5) -> None:
    # ---- Step 1 (plan) + Step 2 (discover + scrape + store homepage HTML) ----
    summary = await p1.run_pipeline(query, concurrency=concurrency)
    p1.print_summary(summary)

    plan = summary.get("plan") or {}

    # Every distinct business discovered in Step 2 (homepage HTML is in the store).
    businesses: Dict[str, str] = {}
    for r in summary.get("results", []):
        website = r.get("website_url")
        if website and website not in businesses:
            businesses[website] = r.get("company_name") or ""

    print("\n" + "#" * 68)
    print(f"STEP 3 — LLM ROUTE PLANNER  ({len(businesses)} business(es))")
    print("#" * 68)

    if not businesses:
        print("No businesses discovered — nothing to route.")
        return

    routed = 0
    # domain -> JSON list of that business's high-intent routes (for leads_clean.csv).
    routes: Dict[str, str] = {}
    entries = list(businesses.items())  # [(website, name), ...], stable order
    domains = [p1._domain_key(website) for website, _ in entries]
    plans = await asyncio.gather(
        *[asyncio.to_thread(rp.plan_routes, d) for d in domains]
    )
    for (website, name), domain, plan_result in zip(entries, domains, plans):
        pages = plan_result.get("selected_pages", [])
        if not pages:
            logger.info("No route plan for %s — skipping.", domain)
            continue
        routed += 1
        routes[domain] = json.dumps(pages, ensure_ascii=False, default=str)
        print(f"\n■ {name or website}  (confidence: {plan_result.get('confidence', '?')})")
        for s in pages:
            print(f"    [{s['priority']}] {s['filename']} — {s.get('reason', '')}")

    print(f"\nStep 3 complete: routed {routed}/{len(businesses)} business(es).")

    # ---- Step 4: Tech Stack Detection — ONLY if this query's intent needs it.
    # Runs after Phase 1 has already discovered/crawled every business above;
    # it never triggers a crawl itself, it only consumes Phase 1's output.
    tech_stacks: Dict[str, str] = {}
    if plan.get("needs_tech_stack") and businesses:
        import tech_stack as ts
        print("\n" + "#" * 68)
        print(f"STEP 4 — TECH STACK DETECTION  ({len(businesses)} business(es))")
        print("#" * 68)
        # get_stored_profile() ALWAYS checks storage first (populated during
        # Phase 1's crawl above) — it only re-scans if a domain was somehow
        # never profiled (e.g. crawled before this feature existed).
        profiles = await asyncio.gather(*[
            asyncio.to_thread(ts.get_stored_profile, p1._domain_key(website))
            for website in businesses
        ])
        for website, profile in zip(businesses, profiles):
            domain = p1._domain_key(website)
            tech_stacks[domain] = json.dumps(profile, ensure_ascii=False, default=str)
            print(f"\n■ {businesses[website] or website}")
            if profile.get("error"):
                print(f"    error: {profile['error']}")
                continue
            normalized = profile.get("normalized_tech_stack", {})
            for bucket in ts.NORMALIZED_BUCKETS:
                if normalized.get(bucket):
                    names = ", ".join(t["name"] for t in normalized[bucket])
                    print(f"    {bucket:<14}: {names}")
            for s in profile.get("sales_signals", []):
                print(f"    [{s['confidence']:.2f}] {s['signal']} "
                      f"-> {', '.join(s['recommended_services'])}")

    # ---- Cleaning: build leads_clean.csv (with high_intent_pages / tech_stack) ----
    import data_pipeline
    print("\n[clean] Building leads_clean.csv...")
    data_pipeline.run(routes=routes, tech_stacks=tech_stacks)


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
