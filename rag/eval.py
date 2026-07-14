"""Tiny regression eval for retrieval accuracy.

Ingests a small, FIXED set of synthetic businesses (hand-written, never real
scraped data) into an isolated Chroma store (rag/.chroma_eval — never the
production rag/.chroma), runs a handful of known queries against it, and
checks that specific expected outcomes hold.

Why this exists: every previous scoring change in this project (hybrid
retrieval, IDF weighting, diminishing-returns keyword bonus, chunk-size
reduction, boilerplate stripping) was checked by hand — running a live query
against whatever happened to be in the store and eyeballing the printed
ranking. That caught real bugs, but it also produced one confident-but-wrong
claim (that a diluted chunk would rank higher after a chunking fix — it
actually ranked lower). A fixed benchmark with known right answers catches
that kind of regression immediately and automatically instead of relying on
manual inspection each time.

The eval corpus is intentionally synthetic (not derived from storage/ or
leads_clean.json) so it stays stable no matter what gets scraped for real,
and never leaves this directory in a modified/inconsistent state.

Run (from the project root):
    python -m rag.eval
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import config
from .contract import SourceDoc

EVAL_CHROMA_DIR = str(Path(config.PACKAGE_DIR) / ".chroma_eval")
EVAL_COLLECTION = "eval_chunks"

# Each business below is synthetic and hand-written to probe ONE specific
# ranking behavior that was fixed (or should stay fixed) in this project.
EVAL_DOCS: List[SourceDoc] = [
    # Probe: a short, maximally-rare, distinctive fact should be found and
    # ranked highly even though every OTHER business shares the location terms
    # in the query.
    SourceDoc(
        url="https://rareword-marina.example/",
        title="Rareword Marina",
        content="Rareword Marina\n\nPO Box 1, Anytown, NY\nrareword-marina.example\nit has no mobile app",
        domain="rareword-marina.example",
    ),
    # Probes: three businesses that ONLY match the common/shared query terms
    # ("new", "york") and have no other distinguishing content. None of these
    # should outrank the rare "app" match above.
    SourceDoc(
        url="https://commonword-marina.example/",
        title="Commonword Marina",
        content="Commonword Marina is a marina in new york. Commonword Marina serves boaters across new york.",
        domain="commonword-marina.example",
    ),
    SourceDoc(
        url="https://otherbiz-one.example/",
        title="Other Business One",
        content="Other Business One is a marina in new york, serving the new york area.",
        domain="otherbiz-one.example",
    ),
    SourceDoc(
        url="https://otherbiz-two.example/",
        title="Other Business Two",
        content="Other Business Two is a marina in new york with full-service docks.",
        domain="otherbiz-two.example",
    ),
    # Probe: cross-page boilerplate stripping. Both of this business's pages
    # share an identical nav line ("Home - About - Contact - Navtest Marina"),
    # which should be stripped before chunking so it can never surface in a
    # stored chunk's text.
    SourceDoc(
        url="https://navtest-marina.example/",
        title="Navtest Marina",
        content="Home - About - Contact - Navtest Marina\nWelcome to Navtest Marina, a marina in new york.",
        domain="navtest-marina.example",
    ),
    SourceDoc(
        url="https://navtest-marina.example/about",
        title="Navtest Marina",
        content="Home - About - Contact - Navtest Marina\nNavtest Marina has been serving boaters since 1990.",
        domain="navtest-marina.example",
    ),
    # Probes: embedding-based fuzzy word matching. This business's text says
    # "application", never the literal word "app" — it should still be
    # recognized as matching an "app" query. Compared only against a business
    # with neither word, so the test isolates the fuzzy-match mechanism
    # instead of competing against other docs that happen to contain "app"
    # literally (see the "business" filter on its eval case below).
    SourceDoc(
        url="https://fuzzyapp-marina.example/",
        title="Fuzzyapp Marina",
        content="Fuzzyapp Marina\n\nThis marina does not have an online booking application yet.",
        domain="fuzzyapp-marina.example",
    ),
    SourceDoc(
        url="https://plaindocks-marina.example/",
        title="Plaindocks Marina",
        content="Plaindocks Marina is a full-service marina with fuel docks and boat repair.",
        domain="plaindocks-marina.example",
    ),
    # Probe: negation awareness. This business's text literally contains "app"
    # -- but AFFIRMED, not negated ("just launched ... app"), the opposite of
    # what a "no app" query wants. It must NOT outrank a business that
    # actually confirms it has no app, even though both literally match "app".
    SourceDoc(
        url="https://hasapp-marina.example/",
        title="Hasapp Marina",
        content="Hasapp Marina just launched an amazing new mobile app for boat owners.",
        domain="hasapp-marina.example",
    ),
    # Paired with hasapp-marina above, written at the SAME sentence fluency
    # level (one well-formed sentence, no address/boilerplate padding) so the
    # comparison isolates JUST the negation mechanism -- not a side effect of
    # one chunk being more diluted/fragmented than the other.
    SourceDoc(
        url="https://trulynoapp-marina.example/",
        title="Trulynoapp Marina",
        content="Trulynoapp Marina is a full-service marina for boaters, but it does not have a mobile app at this time.",
        domain="trulynoapp-marina.example",
    ),
    # Probe: SOFT negation ("in progress", not an explicit no/not) must still
    # be recognized as absence, not treated as an affirmed match. This is a
    # real case found on real scraped data during this project ("application
    # develpoment in progress" on a bonniecastlemarina.com page) that was
    # initially mis-flagged as an affirmed "has an app" match before this fix.
    SourceDoc(
        url="https://softneg-marina.example/",
        title="Softneg Marina",
        content="Softneg Marina for boaters. Mobile app development in progress.",
        domain="softneg-marina.example",
    ),
    # Probe: soft negation with a TIME REFERENCE other than the literal
    # "coming soon" ("coming next season") must also be recognized as absence,
    # not an affirmed match. Real case found on real scraped data during this
    # project (boathousemarinellc.com) that was initially mis-flagged before
    # this fix widened _SOFT_NEGATION_RE beyond the exact phrase "coming soon".
    SourceDoc(
        url="https://comingsoon-marina.example/",
        title="Comingsoon Marina",
        # One tightly fluent sentence, same structure/length as hasapp-marina's
        # content -- isolates the negation comparison from semantic-score
        # dilution differences (a two-clause version of this doc previously
        # lost on raw semantic score alone despite correct negation handling).
        content="Comingsoon Marina is a full-service marina for boaters, and our new booking app is coming next season.",
        domain="comingsoon-marina.example",
    ),
    # Probe: CONTEXT understanding beyond word matching. This business says
    # "platform", never "app"/"application" -- and "platform" is BELOW the
    # fuzzy word-match threshold for "app", so the keyword mechanism alone
    # cannot connect them. Only focus-semantic (comparing the chunk's MEANING
    # to the "app" concept) can recognize this as answering a "no app" query.
    SourceDoc(
        url="https://platform-marina.example/",
        title="Platform Marina",
        content="Platform Marina is a full-service marina for boaters; it has no online booking platform.",
        domain="platform-marina.example",
    ),
]

EVAL_CASES: List[Dict[str, Any]] = [
    {
        "query": "give me marinas in new york with no app",
        "description": 'A rare, distinctive keyword match ("app") must outrank '
                       'several businesses that only match common/shared words '
                       '("new"/"york") — the diminishing-returns keyword bonus '
                       "is what this checks. Restricted to just these four "
                       "businesses so the comparison stays stable regardless of "
                       "what else gets added to the corpus elsewhere for other "
                       "test cases.",
        "expect_top_domain": "rareword-marina.example",
        "business": ["rareword-marina.example", "commonword-marina.example",
                    "otherbiz-one.example", "otherbiz-two.example"],
    },
    {
        "query": "home about contact",
        "description": 'Cross-page nav-menu boilerplate ("Home - About - Contact") '
                       "must have been stripped before chunking, so it cannot be "
                       "present verbatim in any stored chunk's text.",
        "expect_boilerplate_absent": "Home - About - Contact",
    },
    {
        "query": "marina with no app",
        "description": 'A chunk that only ever says "application" (never the literal '
                       'word "app") must still be recognized as answering an "app" '
                       "query via embedding-based fuzzy word matching, and must "
                       "outrank a business with no app/application mention at all. "
                       "Restricted to just these two businesses so the test isolates "
                       "the fuzzy-match mechanism from unrelated docs elsewhere in "
                       "the corpus that happen to contain \"app\" literally.",
        "expect_top_domain": "fuzzyapp-marina.example",
        "business": ["fuzzyapp-marina.example", "plaindocks-marina.example"],
    },
    {
        "query": "marina with no app",
        "description": 'A chunk that literally contains "app" but AFFIRMED '
                       '("just launched our app") must NOT outrank a business that '
                       "actually confirms it has no app — negation awareness must "
                       "catch that the two mean opposite things despite sharing the "
                       "same word. A third business with NEITHER word is included "
                       "in the filter (not just the two being compared) so \"app\"'s "
                       "IDF weight stays meaningful instead of collapsing to ~0 "
                       "(which would happen if every filtered business shared it).",
        "expect_top_domain": "trulynoapp-marina.example",
        "business": ["trulynoapp-marina.example", "hasapp-marina.example",
                    "plaindocks-marina.example"],
    },
    {
        "query": "marina with no app",
        "description": 'A chunk saying "app development in PROGRESS" (soft negation -- '
                       "no explicit no/not, but implies the app doesn't exist yet) must "
                       "be treated as agreeing with a \"no app\" query, NOT penalized as "
                       'a mismatch the way "just launched our app" (a genuinely affirmed '
                       "match) correctly is. Real case found on real scraped data during "
                       "this project.",
        "expect_top_domain": "softneg-marina.example",
        "business": ["softneg-marina.example", "hasapp-marina.example",
                    "plaindocks-marina.example"],
    },
    {
        "query": "marina with no app",
        "description": 'A chunk saying "app is coming NEXT SEASON" (soft negation with a '
                       'time reference other than the literal "coming soon") must be '
                       'classified as NOT mismatched (agreeing with "no app"), while '
                       '"just launched our app" must be classified as mismatched -- '
                       "checked directly via the negation flag itself, not via overall "
                       "ranking (which also depends on each sentence's raw semantic "
                       "score, unrelated to whether negation was classified correctly). "
                       "Real case found on real scraped data during this project "
                       "(boathousemarinellc.com).",
        "expect_negation_correct": [
            ("comingsoon-marina.example", False),  # soft-negated -- must NOT be flagged
            ("hasapp-marina.example", True),        # affirmed -- must BE flagged
        ],
        "business": ["comingsoon-marina.example", "hasapp-marina.example",
                    "plaindocks-marina.example"],
    },
    {
        "query": "marina with no app",
        "description": 'A chunk saying "no online booking PLATFORM" (never app/'
                       'application, and "platform" is below the fuzzy word-match '
                       "threshold for \"app\") must still outrank a business with no "
                       "app-related content at all -- proving the system understands "
                       "the concept by CONTEXT/meaning (focus-semantic), not just "
                       "literal or fuzzy word overlap.",
        "expect_top_domain": "platform-marina.example",
        "business": ["platform-marina.example", "plaindocks-marina.example"],
    },
]


def _isolated_ingest() -> None:
    """Chunk + embed EVAL_DOCS into the isolated eval store only — never the
    production rag/.chroma. Mirrors the REAL production path
    (iter_source_docs_from_high_intent -> RagPipeline.ingest), including its
    cross-page boilerplate stripping — not just chunker.chunk() directly —
    so this eval actually exercises the same steps production ingestion does.
    """
    from collections import defaultdict

    from .chunker import chunk as chunk_fn
    from .contract import SourceDoc as _SourceDoc
    from .embedder import Embedder
    from .ingest_from_storage import _strip_repeated_boilerplate

    # Group by domain and strip cross-page boilerplate exactly like
    # iter_source_docs_from_high_intent does, before any chunking happens.
    by_domain: Dict[str, List[_SourceDoc]] = defaultdict(list)
    for doc in EVAL_DOCS:
        by_domain[doc.domain].append(doc)

    cleaned_docs: List[_SourceDoc] = []
    for domain, docs in by_domain.items():
        cleaned_contents = _strip_repeated_boilerplate([d.content for d in docs])
        for doc, cleaned in zip(docs, cleaned_contents):
            if not cleaned.strip():
                continue
            cleaned_docs.append(_SourceDoc(
                url=doc.url, title=doc.title, content=cleaned, domain=doc.domain,
            ))

    embedder = Embedder()
    embedder.warmup()  # torch before chromadb — see top_matches.py's note

    import chromadb
    client = chromadb.PersistentClient(path=EVAL_CHROMA_DIR)
    col = client.get_or_create_collection(
        name=EVAL_COLLECTION, metadata={"hnsw:space": "cosine"}
    )

    for doc in cleaned_docs:
        chunks = chunk_fn(doc)
        if not chunks:
            continue
        vectors = embedder.embed([c.text for c in chunks])
        col.upsert(
            ids=[c.chunk_id or f"{c.url}#{c.chunk_no}" for c in chunks],
            embeddings=vectors,
            documents=[c.text for c in chunks],
            metadatas=[c.as_metadata() for c in chunks],
        )


def run_eval() -> bool:
    """Ingest the fixed eval corpus into an isolated store and check every
    case. Prints a pass/fail report. Returns True iff everything passed."""
    shutil.rmtree(EVAL_CHROMA_DIR, ignore_errors=True)  # always start clean
    _isolated_ingest()

    from .top_matches import top_matches
    all_passed = True

    print("\n" + "=" * 70)
    print("RAG retrieval eval (fixed synthetic corpus, isolated store)")
    print("=" * 70)

    for case in EVAL_CASES:
        # k=50 comfortably covers this whole tiny synthetic corpus, so every
        # case's target domain(s) show up regardless of rank position.
        matches = top_matches(
            case["query"], k=50, business=case.get("business"),
            chroma_dir=EVAL_CHROMA_DIR, collection_name=EVAL_COLLECTION,
        )
        print(f"\nQuery: {case['query']}")
        print(f"  {case['description']}")

        passed = True
        if "expect_top_domain" in case:
            top_domain = matches[0]["domain"] if matches else None
            ok = top_domain == case["expect_top_domain"]
            passed = passed and ok
            print(f"  expected top domain: {case['expect_top_domain']}  "
                  f"got: {top_domain}  -> {'PASS' if ok else 'FAIL'}")
        if "expect_boilerplate_absent" in case:
            leaked = [m for m in matches if case["expect_boilerplate_absent"] in m["text"]]
            ok = not leaked
            passed = passed and ok
            print(f"  boilerplate text must be absent from every stored chunk  "
                  f"-> {'PASS' if ok else f'FAIL ({len(leaked)} chunk(s) leaked it)'}")
        if "expect_negation_correct" in case:
            # Checks the negation classification DIRECTLY (via the
            # "[opposite meaning?]" flag on each domain's own match) rather
            # than via overall ranking -- ranking also depends on each
            # sentence's raw semantic score, which has nothing to do with
            # whether negation was classified correctly and made two
            # otherwise-solid test cases flaky for the wrong reason.
            by_domain = {m["domain"]: m for m in matches}
            for domain, should_be_flagged in case["expect_negation_correct"]:
                m = by_domain.get(domain)
                flagged = bool(m and any("[opposite meaning?]" in k for k in m["matched_keywords"]))
                ok = flagged == should_be_flagged
                passed = passed and ok
                print(f"  {domain}: expected flagged={should_be_flagged}  "
                      f"got={flagged}  -> {'PASS' if ok else 'FAIL'}")

        all_passed = all_passed and passed

    print("\n" + "=" * 70)
    print("RESULT:", "ALL PASSED" if all_passed else "SOME FAILED")
    print("=" * 70)
    return all_passed


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ok = run_eval()
    shutil.rmtree(EVAL_CHROMA_DIR, ignore_errors=True)  # never leave test data behind
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
