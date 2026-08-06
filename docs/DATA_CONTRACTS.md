# Data Contracts, Caching, and Reliability

Every persisted artifact this pipeline writes, its schema, and its retry/idempotency behavior — centralized here rather than scattered across each stage's notes in `docs/ARCHITECTURE.md`, so this can be read on its own before designing a Laravel/PostgreSQL layer behind it.

## Per-page metadata index — `crawl_index.csv`

One row per **page** (not per business — a business with 5 crawled pages has 5 rows sharing the same `domain`/`website_url`). Written by `storage.py`'s `commit_domain()`, columns defined in `storage.INDEX_COLUMNS`:

| Column | Type | Notes |
|---|---|---|
| `company_name`, `domain`, `website_url`, `page_url` | string | `website_url` is the root site — groups all of one business's rows; `page_url` is this specific page |
| `page_title`, `meta_description` | string | as scraped |
| `email`, `phone_number`, `physical_address` | string | `"N/A"` when absent — never null/empty for a genuinely-checked-but-not-found field, so absence is distinguishable from "not attempted" |
| `txt_path` | string | local filesystem path to the cleaned page text. **OS-native separators** — backslash on Windows. A real bug (fixed) treated this as always forward-slash; any new code reading this must split on `[\\/]`, not `/` alone. |
| `crawl_status` | enum | `ok` \| `empty` \| `failed` |
| `http_status` | string | HTTP code, or the scrape-method fallback tag |
| `content_length`, `word_count` | int | of the cleaned text |
| `timestamp` | string | ISO-8601 UTC |
| `page_type` | enum | `home` \| `contact` \| `about` \| `services` \| `products` \| `blog` \| `legal` \| `other` |
| `relevance_category` | enum | `match` \| `unrelated` — deterministic scoring; the LLM safety-net call only fires for the ambiguous `possible_match` band (see `relevance_scoring.py`) |
| `relevance_score`, `relevance_verdict` | int, enum | 0–100 score; `very_relevant` \| `relevant` \| `possible_match` \| `reject` |
| `scoring_version` | string | the `relevance_scoring.SCORING_VERSION` active at commit time — a cached verdict is only trusted if this matches the CURRENT version; empty when scoring was disabled for that run |
| `validated_industry`, `validated_geo` | string | what this business was actually validated against (may differ from a later, different query's industry/geo) |
| `qualification_status` | enum | `qualified` \| `excluded_by_keyword` — a real, right-industry/location business that fails a keyword requirement (e.g. query said "no CRM", this one has one) is still committed and still counted as a genuine business, just tagged distinctly from a true qualified lead. Never silently dropped. |
| `excluded_keywords` | string | comma-joined matched exclude keywords, empty for `qualified` rows |

## Per-business rollup — `leads_clean.csv` / `leads_clean.json` / `leads_with_maps.csv`

One row per **business** (not per page), produced by `data_pipeline.to_business_level()` from `crawl_index.csv`. Columns: `company_name, domain, website_url, email, phone_number, physical_address, num_pages, date_added, page_title, description, pages_scraped, high_intent_pages, tech_stack, qualification_status, excluded_keywords, relevance_score, relevance_verdict` (`leads_clean.csv`), plus `maps_rating, maps_rating_count, maps_category, maps_matched` once Stage 7 (Google Maps) has run (`leads_with_maps.csv`).

`page_title`/`description` are selected from whichever committed page's own title best matches the business's `company_name` by word-overlap — **not** simply the shortest-URL page. This matters specifically for a business that's really a subsection of a larger shared site (e.g. one marina's page inside a whole city park authority's website) where the site's true root page was never crawled at all — a real bug, fixed this session (the "Pier 25 Marina" case). Preserve this behavior in any reimplementation; reverting to "shortest URL wins" silently breaks `page_title`/`description` for exactly that class of business.

`email`/`phone_number` are the union of every value found across ALL of that business's crawled pages (deduplicated), preferring the business's own registered domain or a known free-webmail provider over an off-domain address if both exist.

## Per-domain storage — `storage/<domain>/`

| Path | Contents |
|---|---|
| `storage/<domain>/*.txt` | cleaned page text, one file per crawled page |
| `storage/<domain>/*_context.json` | structured per-page context (used by Stage 3's search-first retrieval) |
| `storage/<domain>/reviews/<platform>.json` | one file per review platform checked — see schema below |
| `storage/<domain>/reviews/category.json` | `business_category.categorize_domain()`'s cached category (`{"category": "..."}`) — **not** a review-platform record; `accuracy_check.py`'s `_iter_review_records()` deliberately excludes it (and `google_maps.json`, below) by checking for the presence of both `"platform"` and `"reviews"` keys, which neither of these files has |
| `storage/<domain>/reviews/google_maps.json` | Stage 7's own rating/category cache, keyed by `"title"` not `"business_name"` — also excluded from `_iter_review_records()` for the same reason |
| `storage/<domain>/social_profiles.json`, `linkedin_candidates.json`, `decision_makers.json`, `organization.json`, `business_intelligence.json` | business-intelligence artifacts (`bi_providers.py`) |
| `storage/<domain>/tech_stack.json` | Stage 5's merged 4-engine detection result |
| `storage/<domain>/links.json`, `page_index.json`, `scraper_cache.json` | crawl-internal bookkeeping |
| `storage/<domain>/search_index/` | Stage 3's per-domain BM25 + embedding index (`bm25_corpus.json`, `embeddings.npy`, `index_meta.json`) |

### Review-platform record schema (`reviews/<platform>.json`)

```json
{
  "platform": "google_reviews",
  "business_name": "Example Dental Clinic",
  "matched": true,
  "listing_url": "https://www.google.com/maps?cid=...",
  "reviews": [
    {"text": "...", "rating": "5", "author": "...", "date": "2026-01-01T00:00:00Z"}
  ],
  "review_count_extracted": 2,
  "checked_at": "2026-08-05T00:00:00Z",
  "reason": "present only on a miss, e.g. 'no confident public listing found'"
}
```
Every genuine platform record always has both `"platform"` and `"reviews"` keys (even when `reviews` is empty on a miss) — this is the load-bearing distinction `accuracy_check.py` uses to skip the two non-platform files above.

## Domain-key vs. full-URL convention

Every lookup across `storage/`, `crawl_index.csv`, and the RAG evidence index is keyed by **bare registered domain** (`example-dental-clinic.com`), never a full URL (`https://example-dental-clinic.com/`). A real bug (fixed this session) had `main.py` passing full URLs into the RAG ingestion step, which silently matched nothing — "0 domains with review platforms" even when real data existed on disk. Any new integration code (e.g. a Laravel job payload) must normalize to bare domain before using it as a lookup key against this storage layer — use `domain_utils.domain_key()`, the single source of truth for this normalization, never a hand-rolled URL parse.

## Re-run / idempotency behavior

Re-running the same query does **not** re-scrape a business that's already committed under the current `scoring_version` for the same `validated_industry`/`validated_geo` — `phase1_pipeline._reuse_cached_relevance` reuses the cached verdict. Re-running with a **different** query against an already-committed domain still re-validates it against the new industry/geo (a business qualified for "marinas in Miami" is not blindly assumed qualified for "law firms in Chicago"). No artifact is ever versioned by re-run — a re-run overwrites the same file path for a given domain/platform; there is no "run history" retained per business today. A Laravel-facing job model that needs run history would need to snapshot these files at commit time, not assume the local store itself retains it.

## Cache-correctness scenarios (tested)

| Scenario | Required behavior | Verified by |
|---|---|---|
| Genuinely empty provider response (e.g. Serper Places returns zero results) | Cached as a real "no match" — safe to trust, saves a repeat lookup | `find_place()` returns `{"matched": false, "reason": "no Serper results"}` only when the request itself succeeded with an empty body |
| Request failure (timeout, DNS, non-200, rate limit) | **Never** cached as a no-match/no-mention — must be retried on the next run | `_serper_places()` returns `None` (not `[]`) on failure; `google_maps.enrich_domain()` and `review_harvester.enrich_platform()`'s reddit branch both check for this and skip `store.save()` when it's set. `tests/mocked/test_cache_correctness.py` exercises both paths directly. |
| Partial review batch (fewer reviews with actual text than the target) | Escalate the request (double the ask, up to a ceiling) rather than silently accepting a short result | `_fetch_apify_google_reviews()`'s escalation loop; stops early the moment the provider returns fewer raw items than requested (pool genuinely exhausted) or the ceiling is hit. `tests/mocked/test_review_escalation.py`. |
| Repeated domain failures (premium fetch tier) | Circuit breaker opens after 2 consecutive failures for that domain, stopping retry storms | Per-domain failure counter in `phase1_pipeline.py`'s `_tier2_premium_fetch()` |
| Windows stored paths | Backslash and forward-slash variants of a stored path resolve to the same file/lookup | `final_reasoning._txt_path_by_filename()` splits on `[\\/]`; `storage_sync.py` uses `Path.as_posix()` when building remote object keys (a distinct bug — R2/S3 keys must always use `/` regardless of host OS, confirmed live against a real bucket). `tests/unit/test_windows_paths.py`. |
