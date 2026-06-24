## LLM_planner.py
- [x] Move Groq calls under CLI entrypoints (get_client) to prevent import-time execution
- [x] Add CLI flags: --test-api and --plan "<query>"
- [x] Implement plan_query() to output structured JSON including negative constraints (e.g., "no iot devices")
- [x] Verify: run python LLM_planner.py --test-api
- [x] Verify: run python LLM_planner.py --plan "Find me marinas in dubai with no iot devices."

## Phase 1 pipeline (phase1_pipeline.py)
- [x] Step 1: intent deconstruction (reuses LLM_planner.plan_query, primary/fallback models)
- [x] Step 2: Serper Places discovery (async aiohttp)
- [x] Step 3: two-tier scrape — native aiohttp first, ZenRows (js_render + premium_proxy) failover
- [x] Step 4: thread-safe CSV persistence with HTML sanitization
- [x] Cache check: skip URLs already analyzed (CSV dev stand-in for MongoDB Atlas)
- [x] Website resolution: Serper web-search fallback when Maps omits `website`
- [x] Discovery retry (Serper Places sometimes returns empty on cold call)
- [x] Raise CSV field-size limit (raw HTML > 128 KB would crash the cache reader)
- [x] Structured output return + print_summary
- [x] Verify: full live end-to-end run (NATIVE + ZENROWS + cache all exercised)

## REST API (api.py)
- [x] FastAPI layer over the pipeline
- [x] GET /health, POST /pipeline/run, GET /leads, GET /leads/count
- [x] Pydantic request validation (422 on bad input)
- [x] Verify: all endpoints exercised live (run: uvicorn api:app --port 8000)

## Storage quality
- [x] Store clean visible text (BeautifulSoup) instead of raw HTML; column renamed page_text
- [x] Migrated existing CSV in place (1.19 MB -> 24 KB, no re-scrape)
- [ ] Consider: treat empty page_text (JS-only/gov shells) as low-value, flag or re-scrape

## Planner upgrade (LLM_planner.py)
- [x] Master-prompt rewrite: system role explains full pipeline + how each field is consumed
- [x] New fields: search_query (Maps-ready), include_keywords, reasoning; normalized geo_location
- [x] Keyword EXPANSION rule (6-15 surface forms per concept) for real Step-5 matching
- [x] PRECISION GUARD: no bare generic words (avoids "app" matching "happy")
- [x] JSON mode + temperature 0 for deterministic plans
- [x] Pipeline uses plan.search_query for discovery
- [x] Verified on marina / dental / hotel queries

## Discovery + extraction overhaul
- [x] Switch discovery from Serper Places (caps at 10, no websites) to paginated web search
- [x] Planner extracts result_limit from the query (default 20); removed --limit flag & API limit field
- [x] Filter aggregators + blog/listicle/directory pages out of discovery
- [x] Extract email (mailto + regex, junk-filtered) and phone (tel + regex, date-guarded)
- [x] Added email column; migrated existing CSV
- [x] Verified live on Dubai marina queries (real contacts extracted)
- [ ] Discovery precision still imperfect (some directories slip); consider hybrid Maps+web
- [x] Directory harvesting (lightweight): follow listicle outbound links to businesses
      when direct results are short; native-only, skips WAF-protected directories
- [ ] (Deferred) Deep directory crawler via ZenRows + LLM relevance scoring of results
- [x] Harvest noise fix: dedup by registered domain (collapse subdomains), drop
      utility/CTA/footer links (sign-up, help, login, satellite) + ad domains (reklam5)
- [x] Cleaned existing CSV of junk rows (predictwind subdomains, ads, aggregators)

## Step 5 — Lead qualification (DONE)
- [x] qualify_lead(): exclude/include substring matching against page_text
- [x] Qualifies both freshly-scraped and cache-hit leads (cache upgraded to full rows)
- [x] Empty page_text -> 'no_content' (not silently qualified)
- [x] Summary returns qualified[] + qualified_count; print_summary shows verdicts + final list
- [x] API /pipeline/run returns the qualified list
- [x] Verified live (Dubai marinas all qualified; salesforce/crm text -> excluded)

## Next (Phase 2 / production)
- [ ] Production model swap via env flag: primary gpt-oss-120b, failover qwen3.6-27b
- [ ] Swap CSV -> MongoDB Atlas for the cache/store layer
- [ ] Step 5: downstream analysis/filtering of stored HTML against exclude_keywords
