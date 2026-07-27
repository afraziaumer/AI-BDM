# Phase 3 — Review Discovery & Problem-Signal Extraction

Decoupled the same way `rag/` is decoupled from the scraper: it only reads
data Phase 1 already committed (company name, domain, address) and never
touches `storage.py`'s staging/commit lifecycle directly — it just writes its
own small JSON files under `storage/<domain>/reviews/`.

```text
already-committed business -> find review platform listing -> save signal
```

Built to work standalone even though Phase 2 (owner/contact enrichment)
doesn't exist yet — nothing here depends on it.

## The pipeline (so far)

| Step | File | What it does |
|------|------|--------------|
| 0 | `config.py` | Shared constants — reuses phase1_pipeline's Serper key, no new secret |
| 1 | `store.py` | Cache-first JSON read/write, `storage/<domain>/reviews/<platform>.json` |
| 2 | `google_maps.py` | First platform: Serper Places search + address-match validation + rating/review-count |

Every lookup follows the same rule as the rest of the project: cache first,
external call only on a miss, and any failure (no results, no address match)
returns `matched: False` rather than raising or guessing — the field is just
left blank downstream.

## Quick start

Single-business test:

```bash
python -m phase3.google_maps --domain skydentalnyc.com --name "Sky Dental" \
    --address "123 Main St, New York" --geo "New York"
```

Batch mode (same shape as `linkedin_finder.py`):

```bash
python -m phase3.google_maps --in leads_clean.csv --out leads_with_maps.csv --geo "Islamabad"
```

## Category-specific review harvesting

After Phase 1 commits a business, Phase 3 categorizes it and checks the five
review platforms configured for that category. It discovers a public listing
with Serper, retrieves the listing through ZenRows, and saves up to 20 public
reviews (text, rating, author, and date when exposed) in
`storage/<domain>/reviews/<platform>.json`. Results are cached permanently;
delete a platform JSON file only when you explicitly want to refresh it.

Set `REVIEW_ENRICHMENT_ENABLED=false` to disable this automatic step. It uses
the existing Serper key plus `ZENROWS_API_KEY` (or `zenrows`). Platforms that
require sign-in or do not expose public review text are recorded with a reason;
the pipeline does not invent or bypass protected reviews.

## Remaining work

- Full review TEXT (this first pass only pulls rating + review count — the
  "cheap tier" from the project plan)
- RAG scoring of review text against the original query (`rag/top_matches.py`
  reuse) — next step once review text itself is being pulled
- TripAdvisor, Zomato, BBB, JustDial — same pattern as `google_maps.py`, not
  yet added
- The final merge + LLM synthesis step — depends on Phase 2's business/owner
  record existing (or a stub for it), see the project plan doc
