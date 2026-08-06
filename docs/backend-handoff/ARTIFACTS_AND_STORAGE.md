# Artifacts and Storage

Every durable artifact this pipeline writes, where it lives, and its retry/idempotency behavior. This is the same content as `docs/DATA_CONTRACTS.md` (the canonical, more detailed version — read that one for full column-by-column schemas), reframed here to answer the specific questions the Laravel Backend Construction Handoff Guide's Section 4.2 ("Evidence processing") and Section 4.4 ("artifact manifest") ask about.

## What gets written, where

| Artifact | Location | Written by | Durable or regenerable? |
|---|---|---|---|
| Per-page crawl metadata | `crawl_index.csv` | `storage.py`'s `commit_domain()` | Durable — the authoritative per-page index |
| Per-business rollup | `leads_clean.csv` / `leads_clean.json` / `leads_with_maps.csv` | `data_pipeline.to_business_level()` (Stage 2 rollup), `phase3.google_maps.enrich()` (Stage 7 adds Maps columns) | Regenerable from `crawl_index.csv` — never hand-edited |
| Cleaned page text | `storage/<domain>/*.txt` | Stage 2 | Durable |
| Structured per-page context | `storage/<domain>/*_context.json` | Stage 2 (used by Stage 3's search-first retrieval) | Durable |
| Review-platform records | `storage/<domain>/reviews/<platform>.json` | Stage 2 (harvesting) | Durable |
| Google Maps match cache | `storage/<domain>/reviews/google_maps.json` | Stage 7 | Durable |
| Tech-stack profile | `storage/<domain>/tech_stack.json` | Stage 5 | Durable |
| Business-intelligence artifacts | `storage/<domain>/{social_profiles,linkedin_candidates,decision_makers,organization,business_intelligence}.json` | Stage 2's BI enrichment | Durable |
| Crawl-internal bookkeeping | `storage/<domain>/{links,page_index,scraper_cache}.json` | Stage 2 | Regenerable — internal only, not part of any external contract |
| Per-domain search index | `storage/<domain>/search_index/{bm25_corpus.json,embeddings.npy,index_meta.json}` | Stage 3 | Regenerable from already-crawled page text |
| Evidence vector store | `rag/.chroma/` | Stage 6 | Regenerable from already-crawled page/review text — delete and re-run to rebuild |
| Accuracy report | `accuracy_report.txt` | Stage 8 | Regenerable — overwritten each run, no history retained |
| Remote sync copy | Cloudflare R2 bucket, mirroring `storage/<domain>/` | `storage_sync.py`, optional | Durable (when enabled) — the only artifact with content hashing/change-detection today (an incremental per-domain manifest, not a cryptographic hash — see below) |

## Content hashing — a real, current gap

**No artifact in this pipeline is content-hashed (e.g. SHA-256) today.** The Laravel guide's Section 4.4 result contract wants each `artifact_manifest` entry to carry a `sha256`; this codebase doesn't compute one anywhere. R2 sync's "incremental skip" (`docs/RUNBOOK.md`) is based on file modification/size comparison via boto3, not a cryptographic digest. Flagging this explicitly rather than fabricating a hash field that doesn't exist — see `KNOWN_LIMITATIONS.md`.

## Freshness

`TECH_PROFILE_TTL_DAYS = 60` is the only explicit freshness/TTL constant in the codebase (`tech_stack.py`) — a stored tech profile older than 60 days is refreshed on the next query-time read. No other artifact type has a documented TTL; the general re-run behavior (below) is the closest equivalent for everything else.

## Idempotency / re-run behavior

Re-running the same query does **not** re-scrape a business already committed under the current `scoring_version` for the same `validated_industry`/`validated_geo` (`phase1_pipeline._reuse_cached_relevance`). Re-running with a **different** query against an already-committed domain still re-validates it against the new industry/geo. No artifact is versioned by re-run — a re-run overwrites the same file path for a given domain/platform; there is no "run history" retained locally today. A Laravel-facing job model that needs run history would need to snapshot these files at commit time.

**This is pipeline-level idempotency (don't redo expensive work for a business already known), not the job-request-level `Idempotency-Key` semantics** the Laravel guide's Section 5 describes for job *creation* — that concept doesn't exist here yet because there is no job-creation endpoint yet (see `KNOWN_LIMITATIONS.md`).

## Domain-key convention

Every lookup across `storage/`, `crawl_index.csv`, and the RAG evidence index is keyed by **bare registered domain** (`example-dental-clinic.com`), never a full URL. Use `domain_utils.domain_key()` — the single source of truth for this normalization — for any new integration code that needs to look up this storage layer.

## Artifact manifest — how this would map to the Laravel guide's contract

The guide's Section 4.4 wants an `artifact_manifest` array of `{type, uri, sha256}` per job result. Today's artifacts are local filesystem paths (or R2 object keys, when sync is enabled) — there is no `artifact://` URI resolver or content-addressed storage layer built yet. This mapping is part of the deferred 4.3–4.6 job-contract work; see `KNOWN_LIMITATIONS.md`.
