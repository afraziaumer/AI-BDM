"""
AI-BDM pipeline entry point.

One natural-language query runs the whole pipeline:

    Step 1  Intent planning        (LLM_planner via phase1_pipeline)
    Step 2  Discovery + scraping   (phase1_pipeline.run_pipeline -> stores raw HTML)
    Step 3  High-intent routing    (route_planner: search-first retrieval over page
                                    context, per committed business — an LLM call
                                    only when retrieval finds nothing usable)
    Step 4  Final reasoning        (final_reasoning.answer_query, ONLY when the
                                    query has a specific ask beyond plain discovery
                                    — reads the top 3-5 retrieved pages' actual text
                                    and answers the question, citing sources)
    Step 5  Tech Stack Detection   (tech_stack, ONLY if the query's intent needs it)
    Step 6  RAG chunk retrieval    (rag.ingest_and_answer.run: embeds each business's
                                    high-intent pages into the chunk store and prints
                                    one combined ranked-chunk answer + evidence summary)
    Step 7  Google Maps enrichment (phase3.google_maps.enrich: rating/review-count/
                                    category, address-matched against the business's
                                    own scraped address; writes leads_with_maps.csv)

Skip Step 6/7 with --no-rag / --no-maps.

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
import sys
from typing import Dict

# Windows' default console encoding is cp1252, not UTF-8 (confirmed live:
# sys.stdout.encoding == "cp1252" here) -- every OTHER printed string in
# this codebase is a fixed English literal we wrote ourselves, so this never
# came up, but clarification_questions (LLM_planner.py's step K) is raw
# LLM-generated free text, which routinely includes "smart" punctuation
# (non-breaking hyphens, curly quotes, em dashes) that cp1252 can't encode --
# confirmed live: printing a real model response crashed with
# UnicodeEncodeError on U+2011. Reconfiguring stdout/stderr to UTF-8 here,
# once, protects every print() in the whole run, not just this one feature.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import final_reasoning as fr
import phase1_pipeline as p1
import route_planner as rp

logger = logging.getLogger("ai_bdm.main")

# How many rounds of clarifying questions to accept in one run before just
# proceeding with the planner's best guess. Bounds how long a terminal
# session can go back-and-forth if the model keeps finding more to ask.
MAX_CLARIFICATION_ROUNDS = 3


def _is_zero_signal_plan(plan: Dict) -> bool:
    """True when the planner found literally no usable lead-gen signal at
    all -- industry, location, AND search query all blank -- as opposed to
    a genuine but under-specified request (e.g. "find some dental clinics"
    has an industry, just no location). Both cases set needs_clarification,
    but they aren't the same situation: one just needs a detail filled in,
    the other isn't a business search at all (e.g. "basit goes to work").
    Distinguishing them lets the clarification message be honest instead
    of implying every input was "almost there."""
    return not (
        (plan.get("broad_industry") or "").strip()
        or (plan.get("geo_location") or "").strip()
        or (plan.get("search_query") or "").strip()
    )


async def _plan_interactively(query: str, concurrency: int) -> Dict:
    """Run Step 1+2 (p1.run_pipeline), resolving clarification questions in
    THIS terminal session before any real discovery/scraping happens --
    print the question(s), read the answer with input(), append it to the
    query in plain text, and re-plan. plan_query() already parses arbitrary
    natural language, so no separate answer-parsing logic is needed here;
    each round is just a richer version of the same query string.

    Capped at MAX_CLARIFICATION_ROUNDS so a model that keeps finding more to
    ask can't loop forever -- after the cap (or if the user just presses
    enter with no answer), re-runs run_pipeline() ONE more time with
    bypass_clarification=True so discovery genuinely executes against the
    plan's best-guess fields (see LLM_planner.py's step K -- every field is
    always filled in even when needs_clarification is true). Real bug fixed
    here: an earlier version just flipped needs_clarification to False on
    the SAME summary object run_pipeline() had already returned early with
    (before any discovery ever ran) -- "proceeding with my best guess"
    silently did nothing, returning zero results while claiming success.
    """
    current_query = query
    for round_num in range(1, MAX_CLARIFICATION_ROUNDS + 1):
        summary = await p1.run_pipeline(current_query, concurrency=concurrency)
        if not summary.get("needs_clarification"):
            return summary

        questions = summary.get("clarification_questions") or []
        plan = summary.get("plan") or {}
        print("\n" + "=" * 64)
        if _is_zero_signal_plan(plan):
            # Real gap found live: "basit goes to work" (zero industry,
            # location, or search-query signal at all) got the exact same
            # "a bit more detail would help" framing as a genuine but
            # under-specified request like "find some dental clinics"
            # (which DOES have an industry, just no location) -- misleading
            # the user into thinking their input was on the right track
            # when the planner found literally nothing usable. Distinguish
            # the two cases honestly instead of treating them identically.
            print("I couldn't find a business/lead-generation request in that.")
            print('This tool searches for businesses by industry and location --')
            print('for example: "find 10 dental clinics in Austin, Texas".')
        else:
            print("A bit more detail would help before I search:")
            for i, q in enumerate(questions, start=1):
                print(f"  {i}. {q}")
        print("=" * 64)

        if round_num == MAX_CLARIFICATION_ROUNDS:
            print("(That's enough back-and-forth -- proceeding with my best guess.)")
            return await p1.run_pipeline(
                current_query, concurrency=concurrency, bypass_clarification=True,
            )

        answer = input("Your answer: ").strip()
        if not answer:
            print("(No answer given -- proceeding with my best guess.)")
            return await p1.run_pipeline(
                current_query, concurrency=concurrency, bypass_clarification=True,
            )
        # Ties the answer back to the exact question(s) it's answering,
        # instead of just tacking it onto the end of the query as a bare
        # sentence. Confirmed live this matters: a query like "3 salons in
        # lahore. both, make sure they have no crm" left it unclear whether
        # "both" meant chains+independents or something else entirely to a
        # FRESH LLM call with no memory of what was actually asked -- it
        # correctly picked up "no crm" but re-asked the exact same
        # chain-scope question right back, ignoring the answer already
        # given. Restating the question next to the answer removes that
        # ambiguity.
        questions_text = " / ".join(questions)
        current_query = (
            f"{current_query}\n\n"
            f"(Previously asked: \"{questions_text}\" -- answer: \"{answer}\")"
        )

    # Unreachable: the loop's final iteration (round_num == MAX_CLARIFICATION_ROUNDS)
    # always returns explicitly above. Kept only so the function has an
    # unconditional return for type-checking purposes.
    raise AssertionError("unreachable: every loop path returns explicitly")


async def run(query: str, concurrency: int = 10,
              no_rag: bool = False, no_maps: bool = False) -> None:
    # ---- Step 1 (plan, interactively resolving any clarification questions
    # in this terminal session) + Step 2 (discover + scrape + store homepage
    # HTML) ----
    summary = await _plan_interactively(query, concurrency)
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
        *[asyncio.to_thread(rp.plan_routes, d, query) for d in domains]
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

    # ---- Step 4: Final Reasoning — ONLY when the query has a specific ask
    # beyond plain discovery (intent == "find_and_filter" means the user's
    # own text stated a constraint/question, e.g. "...with no CRM" — a bare
    # "find 20 marinas in Miami" is intent == "find", nothing to answer).
    # Reads the top 3-5 already-retrieved pages' actual text (never the
    # whole site) and synthesizes one grounded, source-cited answer per
    # business — the "LLM Reasoning -> Final Result" stage of the
    # search-first architecture. Best-effort per business: one failure
    # never blocks the rest.
    if plan.get("intent") == "find_and_filter" and routes:
        print("\n" + "#" * 68)
        print(f"STEP 4 — FINAL REASONING  ({len(routes)} business(es) with a route plan)")
        print("#" * 68)
        answered_domains = list(routes.keys())
        answers = await asyncio.gather(*[
            asyncio.to_thread(fr.answer_query, d, query) for d in answered_domains
        ])
        name_by_domain = {p1._domain_key(w): n for w, n in businesses.items()}
        for domain, answer in zip(answered_domains, answers):
            label = name_by_domain.get(domain, domain)
            print(f"\n■ {label}  (confidence: {answer.get('confidence', '?')})")
            if answer.get("answer"):
                print(f"    {answer['answer']}")
                print(f"    sources: {', '.join(answer.get('source_pages', []))}")
            else:
                print(f"    No answer found ({answer.get('reason', 'unknown')}).")

    # ---- Step 5: Tech Stack Detection — ONLY if this query's intent needs it.
    # Runs after Phase 1 has already discovered/crawled every business above;
    # it never triggers a crawl itself, it only consumes Phase 1's output.
    tech_stacks: Dict[str, str] = {}
    if plan.get("needs_tech_stack") and businesses:
        import tech_stack as ts
        print("\n" + "#" * 68)
        print(f"STEP 5 — TECH STACK DETECTION  ({len(businesses)} business(es))")
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

    # ---- Step 6: RAG chunk ingestion + retrieval — embed each business's
    # high-intent pages (just written to leads_clean.json above) into the
    # chunk store and print one combined ranked-chunk answer + per-business
    # evidence summary for this query. Same logic as
    # `python -m rag.ingest_and_answer`, called in-process since the
    # query/domains are already in hand here (no last_run.json round-trip).
    if not no_rag and businesses:
        print("\n" + "#" * 68)
        print(f"STEP 6 — RAG CHUNK INGESTION + RETRIEVAL")
        print("#" * 68)
        import rag.ingest_and_answer as raa
        await asyncio.to_thread(raa.run, query, domains)

    # ---- Step 7: Google Maps enrichment — rating/review-count/category via
    # Serper Places, address-matched against each business's own scraped
    # address (never guessed). Batch mode over the leads_clean.csv just
    # written -> leads_with_maps.csv. Runs after Step 6 so both read the
    # exact same committed leads_clean.csv.
    if not no_maps and businesses:
        print("\n" + "#" * 68)
        print(f"STEP 7 — GOOGLE MAPS ENRICHMENT")
        print("#" * 68)
        from phase3 import google_maps as gm
        await gm.enrich("leads_clean.csv", "leads_with_maps.csv", plan.get("geo_location", "") or "")

    # ---- Step 8: Accuracy check — read-only QA pass over everything just
    # committed (field validity, cross-source identity consistency, review-
    # relevance grounding). Always runs automatically so there's no separate
    # `python accuracy_check.py` step to remember. Points at leads_clean.csv
    # instead of leads_with_maps.csv when --no-maps skipped Step 7 this run —
    # otherwise it would silently audit a stale leads_with_maps.csv left over
    # from an earlier run instead of what this run actually committed.
    if businesses:
        print("\n" + "#" * 68)
        print(f"STEP 8 — ACCURACY CHECK")
        print("#" * 68)
        import accuracy_check
        accuracy_check.run(
            geo=plan.get("geo_location", "") or "",
            path="leads_clean.csv" if no_maps else "leads_with_maps.csv",
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI-BDM: run a query end-to-end, Step 1 through Step 7.")
    parser.add_argument("--query", required=True, help="Natural-language lead query.")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="Concurrent scrape workers for Step 2.")
    parser.add_argument("--no-rag", action="store_true",
                        help="Skip Step 6 (RAG chunk ingestion + retrieval).")
    parser.add_argument("--no-maps", action="store_true",
                        help="Skip Step 7 (Google Maps rating/address enrichment).")
    args = parser.parse_args()
    asyncio.run(run(args.query, args.concurrency, args.no_rag, args.no_maps))


if __name__ == "__main__":
    main()
