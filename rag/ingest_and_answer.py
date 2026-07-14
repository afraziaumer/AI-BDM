"""Automatically answer the ORIGINAL scraping query for every business that was
just scraped for it — no need to type a question again.

Reads last_run.json (written by phase1_pipeline.py after every scrape run):
    {"query": "...", "domains": [...]}

and:
    1. Ingests ONLY each business's HIGH-INTENT pages — read from
       leads_clean.json's `high_intent_pages` field (Step 3's LLM route
       planner selection), NOT every page in storage/. A business with a
       40-page crawl contributes only its 2-6 high-intent pages to the RAG
       store, not all 40. Safe to re-run (Chroma upsert never duplicates).
       Domains with no high-intent pages (never committed, or Step 3 found
       nothing) are silently skipped — they have nothing to show for.
    2. Ranks ALL of those businesses' chunks together, in ONE combined list,
       against the SAME query and prints the TOP 10 overall — score + full
       text — directly to the terminal. No LLM call (pure retrieval only, for
       now) — see rag/top_matches.py. This is a single ranked list, not one
       section per business — a business with only 1-2 high-intent chunks can
       still outrank a business with 30+ chunks if its match is stronger.

This is the one command you need after a scrape — it never asks for input.

Run (from the project root, after `python main.py --query "..."`):
    python -m rag.ingest_and_answer
    python -m rag.ingest_and_answer --last-run last_run.json --k 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from .ingest_from_storage import DEFAULT_LEADS_JSON, iter_source_docs_from_high_intent
from .top_matches import top_matches

# Anchor concepts for gating whether to ALSO consult the scraper's
# already-extracted physical_address field (crawl_index.csv) as evidence. A
# real street address is strong, structured proof of a physical presence that
# no amount of page-TEXT word matching can replace -- an address string like
# "38 west 32 street Suite 1508" doesn't literally contain "office" or
# "physical" and isn't semantically close enough to fuzzy-match them either,
# so it's invisible to top_matches.py's chunk-based scoring entirely. This is
# a narrow, structural gate (deciding WHETHER a different, already-extracted
# data source is relevant) -- not a synonym list for query concepts.
_LOCATION_ANCHOR_WORDS = ("address", "location", "office", "premises", "headquarters")
_LOCATION_SIMILARITY_THRESHOLD = 0.60  # matches top_matches.MIN_FUZZY_SIMILARITY


def _is_location_related(word: str, embedder) -> bool:
    """Is `word` (the query's focus concept or its phrase partner) about a
    physical location/office/address?"""
    if not word:
        return False
    if word in _LOCATION_ANCHOR_WORDS:
        return True
    word_vec = embedder.embed_one(word)
    for anchor_vec in embedder.embed(list(_LOCATION_ANCHOR_WORDS)):
        sim = sum(a * b for a, b in zip(word_vec, anchor_vec))
        if sim >= _LOCATION_SIMILARITY_THRESHOLD:
            return True
    return False


def _load_last_run(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"No {path} found — run the scraper first "
            f'(e.g. python main.py --query "give me 5 cafes in new jersey with no app") '
            f"so there is a query and scraped businesses to answer against."
        )
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="Answer the last scrape's own query for every business it "
                    "actually scraped — no question re-entry needed."
    )
    parser.add_argument("--last-run", default="last_run.json",
                        help="Path to the last_run.json written by the scraper.")
    parser.add_argument("--leads-json", default=DEFAULT_LEADS_JSON,
                        help="Path to leads_clean.json (source of high_intent_pages).")
    parser.add_argument("--k", type=int, default=15,
                        help="How many top-matching chunks to show per business (default 15).")
    args = parser.parse_args()

    last_run = _load_last_run(args.last_run)
    query = (last_run.get("query") or "").strip()
    discovered_domains: List[str] = last_run.get("domains") or []
    if not query or not discovered_domains:
        raise SystemExit(f"{args.last_run} has no query/domains to work from.")

    from .pipeline import RagPipeline
    pipe = RagPipeline()

    # Ingest only domains with HIGH-INTENT pages (leads_clean.json) — not every
    # committed page. Domains with nothing high-intent (discovered-but-rejected,
    # or Step 3 found nothing) yield zero docs here and are naturally skipped.
    committed_domains: List[str] = []
    ingested_pages = 0
    for domain in discovered_domains:
        docs = list(iter_source_docs_from_high_intent(
            leads_json_path=args.leads_json, domain=domain
        ))
        if not docs:
            continue
        for doc in docs:
            pipe.ingest(doc)
        ingested_pages += len(docs)
        committed_domains.append(domain)

    print("\n" + "=" * 70)
    print("Query (reused from the scrape):", query)
    print(f"Domains discovered during scraping:      {len(discovered_domains)}")
    print(f"Domains with high-intent pages to embed: {len(committed_domains)}")
    print(f"High-intent pages ingested into RAG:     {ingested_pages}")
    print("=" * 70)

    if not committed_domains:
        print("\nNo committed businesses to answer for — nothing was actually "
              "scraped/stored for this query.")
        return

    # ONE combined ranking across all committed businesses together — not a
    # separate top-10 per business. Otherwise a business with 30+ chunks
    # (e.g. one the route planner selected many high-intent pages for) fills
    # its own section with 10 rows and visually looks like "everything came
    # from one site," while a business with just 1-2 chunks gets its own tiny
    # section that's easy to miss even if its match is the single best one.
    matches = top_matches(query, k=args.k, business=committed_domains)
    print(f"\nTop {len(matches)} chunk(s) overall, hybrid score "
          f"(semantic + keyword bonus)")
    print("=" * 70)
    for rank, m in enumerate(matches, 1):
        print(f"\n[{rank}] score={m['score']:.4f}  "
              f"(semantic={m['semantic_score']:.4f} + keyword={m['keyword_bonus']:.4f} "
              f"+ focus[{m.get('focus_word','')}]={m.get('focus_lift', 0.0):.4f})  "
              f"domain={m['domain']}  chunk={m['chunk_no']}")
        if m["matched_keywords"]:
            weights = m["matched_keyword_weights"]
            pretty = ", ".join(f"{w}({weights.get(w, 1.0):.2f})" for w in m["matched_keywords"])
            print("  matched keywords (word[→fuzzy match], effective weight):", pretty)
        print("URL:", m["url"])
        print("-" * 70)
        print(m["text"])
        print("-" * 70)

    # Per-business evidence summary. Scans EVERY chunk for each committed
    # business (not just the printed top-k above) -- a disclosure buried
    # outside the top-k chunks is still a real, useful answer to a BDM.
    # Distinguishes an explicit statement (present/absent) from silence,
    # since silence is not proof of absence.
    all_matches = top_matches(query, k=len(committed_domains) * 10_000,
                              business=committed_domains)
    focus_word = all_matches[0]["focus_word"] if all_matches else ""
    focus_partner = all_matches[0].get("focus_partner", "") if all_matches else ""
    evidence_by_domain: Dict[str, List[str]] = {}
    # Distinct PAGE URLs (not chunk count) where the phrase partner (e.g.
    # "office" in a "physical office" query) matches on its own, without
    # focus_word nearby -- keyed by domain then polarity. A business
    # consistently using the word the same way across several of its own
    # pages is a much stronger signal than one coincidental mention (which
    # could just be two unrelated things sharing a short chunk by chance —
    # see top_matches.py's phrase-proximity gating).
    partner_urls_by_domain: Dict[str, Dict[str, set]] = {}
    for m in all_matches:
        evidence_by_domain.setdefault(m["domain"], []).append(m["focus_evidence"])
        if m.get("partner_has_signal"):
            slot = partner_urls_by_domain.setdefault(m["domain"], {"confirmed_present": set(), "confirmed_absent": set()})
            slot.setdefault(m["partner_evidence"], set()).add(m["url"])

    PARTNER_CORROBORATION_MIN = 2  # distinct pages required to trust a standalone partner-word match

    # A real street address (already extracted by the scraper's contact
    # extraction into crawl_index.csv, independent of the RAG chunks
    # entirely) is strong, structured evidence of a physical presence -- but
    # only relevant to check when the query is actually asking about physical
    # location/office/address in the first place.
    address_domains: set = set()
    if focus_word and (focus_word in _LOCATION_ANCHOR_WORDS or focus_partner in _LOCATION_ANCHOR_WORDS):
        location_related = True
    elif focus_word:
        from .embedder import Embedder
        _embedder = Embedder()
        location_related = _is_location_related(focus_word, _embedder) or \
            _is_location_related(focus_partner, _embedder)
    else:
        location_related = False
    if location_related:
        import storage  # project-root module; resolves when run from C:\AI-BDM
        store = storage.get_store()
        for row in store.read_index():
            dom = (row.get("domain") or "").strip()
            addr = (row.get("physical_address") or "").strip()
            if dom in committed_domains and addr and addr != "N/A":
                address_domains.add(dom)

    if focus_word:
        print(f"\nPer-business evidence summary for focus concept: \"{focus_word}\"")
        print("=" * 70)
        for domain in committed_domains:
            evidences = evidence_by_domain.get(domain, [])
            has_present = "confirmed_present" in evidences
            has_absent = "confirmed_absent" in evidences
            via_partner = ""
            if not has_present and not has_absent and focus_partner:
                urls = partner_urls_by_domain.get(domain, {})
                present_pages = urls.get("confirmed_present", set())
                absent_pages = urls.get("confirmed_absent", set())
                if len(present_pages) >= PARTNER_CORROBORATION_MIN:
                    has_present = True
                    via_partner = f" (via \"{focus_partner}\" mentioned on {len(present_pages)} separate pages)"
                elif len(absent_pages) >= PARTNER_CORROBORATION_MIN:
                    has_absent = True
                    via_partner = f" (via \"{focus_partner}\" mentioned on {len(absent_pages)} separate pages)"
            via_address = ""
            if domain in address_domains:
                has_present = True
                via_address = " + a street address is on file for this business"
            if has_present and has_absent:
                verdict = (f"MIXED signals — found chunks both confirming and "
                          f"contradicting \"{focus_word}\"{via_partner}{via_address}")
            elif has_present:
                verdict = f"CONFIRMED PRESENT — explicitly mentions {focus_word}{via_partner}{via_address}"
            elif has_absent:
                verdict = f"CONFIRMED ABSENT — explicitly states no {focus_word} (or not yet available){via_partner}"
            else:
                verdict = (f"NO EVIDENCE FOUND — \"{focus_word}\" never mentioned; "
                          f"absence is unconfirmed, not proven")
            print(f"  {domain}: {verdict}")
        print("=" * 70)

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
