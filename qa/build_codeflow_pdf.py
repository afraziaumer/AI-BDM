"""Generates qa/AI-BDM_Code_Flow_and_Module_Reference.pdf -- a code-level
walkthrough for an internship handover: the real function-call trace from
each entry point through the whole codebase (verified against the actual
source, not inferred), plus a complete module-by-module reference (every
.py file: purpose, key functions, who calls it / what it calls).
"""

from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

NAVY = colors.HexColor("#1F4E78")
STEEL = colors.HexColor("#2E75B6")
GREY = colors.HexColor("#595959")
CODE_BG = colors.HexColor("#F2F2F2")
LIGHT_BLUE = colors.HexColor("#DCE6F1")

styles = getSampleStyleSheet()
S_TITLE = ParagraphStyle("T4", parent=styles["Title"], textColor=NAVY, fontSize=26, fontName="Helvetica-Bold")
S_SUBTITLE = ParagraphStyle("Sub4", parent=styles["Normal"], textColor=GREY, fontSize=12.5, italic=True, spaceAfter=4)
S_H1 = ParagraphStyle("H1_4", parent=styles["Heading1"], textColor=colors.white, fontSize=15, fontName="Helvetica-Bold", leftIndent=8)
S_H2 = ParagraphStyle("H2_4", parent=styles["Heading2"], textColor=NAVY, fontSize=12.5, spaceBefore=12, spaceAfter=5)
S_H3 = ParagraphStyle("H3_4", parent=styles["Heading3"], textColor=STEEL, fontSize=10.5, spaceBefore=8, spaceAfter=3)
S_BODY = ParagraphStyle("Body4", parent=styles["Normal"], fontSize=9.6, leading=13.5, spaceAfter=6)
S_CAPTION = ParagraphStyle("Cap4", parent=styles["Normal"], fontSize=8.5, textColor=GREY, italic=True, spaceAfter=8)
S_CODE = ParagraphStyle("Code4", parent=styles["Normal"], fontName="Courier", fontSize=8.8, leading=12,
                         backColor=CODE_BG, borderPadding=(6, 8, 6, 8))
S_CELL = ParagraphStyle("Cell4", parent=styles["Normal"], fontSize=8.6, leading=12, alignment=TA_LEFT)
S_CELL_HDR = ParagraphStyle("CellHdr4", parent=S_CELL, fontName="Helvetica-Bold", textColor=colors.white)
S_TRACE_STEP = ParagraphStyle("TraceStep", parent=styles["Normal"], fontSize=10.5, fontName="Helvetica-Bold",
                               textColor=NAVY, spaceBefore=10, spaceAfter=3)

PAGE_SIZE = A4
MARGIN = 1.7 * cm
CONTENT_WIDTH = PAGE_SIZE[0] - 2 * MARGIN


def h1(text: str):
    tbl = Table([[Paragraph(text, S_H1)]], colWidths=[CONTENT_WIDTH])
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return tbl


def code(text: str):
    return Paragraph(text.replace("\n", "<br/>"), S_CODE)


def bullets(items, style=S_BODY):
    return ListFlowable([ListItem(Paragraph(i, style), leftIndent=14) for i in items], bulletType="bullet", start="•", leftIndent=14)


def trace(depth: int, call: str, purpose: str):
    indent = 0.5 * cm * depth
    style = ParagraphStyle(f"trace{depth}", parent=S_BODY, leftIndent=indent, spaceAfter=3, fontSize=9.3, leading=12.5)
    arrow = "" if depth == 0 else "&gt;&gt; "
    return Paragraph(f"{arrow}<font face='Courier' color='#1F4E78'><b>{call}</b></font> — {purpose}", style)


def module_table(rows, col_widths=None, header=None):
    col_widths = col_widths or [CONTENT_WIDTH * 0.22, CONTENT_WIDTH * 0.30, CONTENT_WIDTH * 0.48]
    data = []
    if header:
        data.append([Paragraph(h, S_CELL_HDR) for h in header])
    for row in rows:
        data.append([Paragraph(str(c), S_CELL) for c in row])
    tbl = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BFBFBF")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, colors.HexColor("#F2F6FA")]),
    ]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), NAVY))
    tbl.setStyle(TableStyle(style))
    return tbl


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(GREY)
    canvas.drawString(MARGIN, 1.0 * cm, "AI-BDM — Code Flow & Module Reference")
    canvas.drawRightString(PAGE_SIZE[0] - MARGIN, 1.0 * cm, f"Page {doc.page}")
    canvas.restoreState()


def build_story() -> list:
    story = []

    # ---------------- Cover ----------------
    story.append(Paragraph("AI-BDM", S_TITLE))
    story.append(Paragraph("Code Flow &amp; Module Reference — for explaining the implementation, file by file, function by function.", S_SUBTITLE))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width="100%", color=NAVY, thickness=1.4))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(
        "This is the code-level companion to <b>AI-BDM_Architecture_And_Concepts_Guide.pdf</b> (which "
        "explains <i>what</i> the system does and the concepts behind it, for a non-technical reader). "
        "This document explains <i>how it's actually built</i> — which file calls which function, in what "
        "order, and why each module exists. Every call chain below was verified directly against the "
        "source code, not reconstructed from memory or documentation.", S_BODY,
    ))
    story.append(Paragraph(
        "Read Section 1 first for the real, end-to-end story of what happens when the pipeline runs. "
        "Section 3 is the reference to come back to afterward, module by module.", S_BODY,
    ))
    story.append(PageBreak())

    story.append(h1("Contents"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(bullets([
        "1. The Complete Execution Trace — main.py, start to finish",
        "2. The Two Entry Points (CLI vs. API) and the Async Job Layer",
        "3. Module Reference — every file, grouped by responsibility",
        "4. Tests — how the codebase proves itself correct",
        "5. How to Summarize This in One Minute",
    ]))
    story.append(PageBreak())

    # ============================================================
    # Section 1: The trace
    # ============================================================
    story.append(h1("1. The Complete Execution Trace"))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        "This is exactly what happens, in order, when someone runs:", S_BODY,
    ))
    story.append(code('python main.py --query "find 3 dental clinics in Islamabad with no online booking"'))
    story.append(Paragraph(
        "Notation: each line is one function call. Indentation shows nesting — an indented line is called "
        "<i>by</i> the line above it. This was traced directly from <font face='Courier'>main.py</font>'s "
        "<font face='Courier'>run()</font> function and the functions it calls into.", S_CAPTION,
    ))

    story.append(Paragraph("Entry point", S_H2))
    story.append(trace(0, "main.py : main()", "Parses the --query/--concurrency/--no-rag/--no-maps command-line arguments, then hands off to run()."))
    story.append(trace(1, "main.py : run(query, concurrency, no_rag, no_maps)", "The actual orchestrator — everything below happens inside this one async function, in this order."))

    story.append(Paragraph("Steps 1 &amp; 2 — Understand the request, then search and scrape", S_H2))
    story.append(trace(1, "main.py : _plan_interactively(query, concurrency)", "Wraps Step 1+2 so a clarification question can be answered in the same terminal session before continuing."))
    story.append(trace(2, "phase1_pipeline.py : run_pipeline(query, ...)", "The real Stage 1+2 engine — everything from here down is inside this one call."))
    story.append(trace(3, "phase1_pipeline.py : moderate_user_query(query)", "Safety check FIRST, before any paid API is touched. Blocks and returns immediately if the query violates usage policy."))
    story.append(trace(3, "phase1_pipeline.py : deconstruct_intent(query)", "Understands the request."))
    story.append(trace(4, "LLM_planner.py : plan_query(query)", "The actual LLM call (Groq) that turns the sentence into a structured plan: industry, location, count, intent, clarification decision, etc."))
    story.append(trace(4, "model_router.py : select_model(TaskType.INTENT_PLANNING)", "Decides which Groq model/settings this specific call should use (see Section 3)."))
    story.append(trace(3, "phase1_pipeline.py : discover_targets(...) / discover_places(...)", "Finds candidate business URLs via Serper.dev (web search + Google Places)."))
    story.append(trace(3, "phase1_pipeline.py : process_single_lead(...)", "Runs once per candidate business — the biggest single function in the codebase. Expanded fully below."))
    story.append(trace(4, "website_classifier.py : classify_homepage(...)", "Fast rule + LLM-assisted check: is this actually worth a deep crawl, or reject it now?"))
    story.append(trace(4, "route_filter.py : select_high_intent_routes(...)", "Picks which of a site's own links are worth crawling (contact/about/services pages), not every page on the site."))
    story.append(trace(4, "discovery_classifier.py : classify_search_result(...)", "Distinguishes a real single business from a directory/listing page that should be mined for MORE businesses instead."))
    story.append(trace(4, "relevance_scoring.py : score_relevance(...)", "Deterministic (non-LLM) scoring against the plan's industry/location/keywords — the main relevance gate."))
    story.append(trace(4, "tech_stack.py : analyze_raw_html(domain, html, headers, cookies)", "Runs unconditionally on the homepage HTML already in memory (no extra fetch) — Stage 5's actual detection work, done here regardless of whether the query asked for it; only later SURFACING is conditional."))
    story.append(trace(4, "phase3/review_harvester.py : enrich_business(...)", "Harvests customer reviews from whichever platforms make sense for this business's category (Google Reviews, Reddit, industry-specific sites)."))
    story.append(trace(4, "bi_providers.py : get_manager().discover_social_profiles(...)", "Finds the business's official social media profiles."))
    story.append(trace(4, "github_enrichment.py : enrich_github(...) / youtube_enrichment.py : enrich_youtube(...)", "Two platforms with genuine, ToS-compliant official APIs — checked directly."))
    story.append(trace(4, "bi_providers.py : get_manager().discover_decision_makers(...)", "Tries, in priority order: the site's own contact page, Schema.org structured data, LinkedIn (via Google search, never scraped directly), then general public search."))
    story.append(trace(4, "organization.py : build_organization(people)", "Infers the business's likely org structure from whoever was found."))
    story.append(trace(4, "business_intelligence.py : build_business_intelligence_summary(...)", "Rolls all of the above into one consolidated per-business summary."))
    story.append(trace(4, "storage.py : commit_domain(domain, ...)", "Writes everything found for this business to storage/&lt;domain&gt;/ and adds its row(s) to crawl_index.csv — the actual save-to-disk step."))
    story.append(trace(4, "storage_sync.py : sync_folder(domain, ...)", "Optional — mirrors that folder to Cloudflare R2 if remote sync is configured."))

    story.append(Paragraph("Step 3 — Pick the right pages to focus on", S_H2))
    story.append(trace(1, "route_planner.py : plan_routes(domain, query)", "Runs once per discovered business."))
    story.append(trace(2, "page_retrieval.py : hybrid_retrieve(...)", "BM25 (keyword) + embedding (meaning) search over that business's own already-crawled pages — the common, no-LLM path."))
    story.append(trace(2, "LLM_planner.py : (route-planning fallback call)", "Only runs if hybrid_retrieve finds nothing usable — rare in practice."))

    story.append(Paragraph("Step 4 — Answer the specific question (only if the query asked one)", S_H2))
    story.append(trace(1, "final_reasoning.py : answer_query(domain, query)", "Runs once per business, only when the plan's intent is find_and_filter."))
    story.append(trace(2, "(Groq LLM call, RAG pattern)", "Reads the real page text Step 3 selected and answers strictly from it, citing sources, or says the evidence isn't there."))

    story.append(Paragraph("Step 5 — Surface tech-stack results (only if the query asked)", S_H2))
    story.append(trace(1, "tech_stack.py : get_stored_profile(domain)", "Reads back the profile that was ALREADY computed unconditionally during Step 2 (see analyze_raw_html above) — no new scan happens here unless a domain was somehow never profiled."))

    story.append(Paragraph("Cleaning — build the per-business rollup file", S_H2))
    story.append(trace(1, "data_pipeline.py : run(routes, tech_stacks)", "Reads crawl_index.csv (one row per page) and rolls it up into leads_clean.csv (one row per business)."))
    story.append(trace(2, "data_pipeline.py : to_business_level(...)", "The actual per-page-to-per-business rollup logic — picks the best title/description, unions contact info across pages."))

    story.append(Paragraph("Step 6 — Build the searchable evidence index (skippable with --no-rag)", S_H2))
    story.append(trace(1, "rag/ingest_and_answer.py : run(query, domains)", ""))
    story.append(trace(2, "rag/chunker.py : chunk(...)", "Breaks page/review text into small pieces."))
    story.append(trace(2, "rag/embedder.py", "Turns each chunk into a 384-number \"meaning vector\" (sentence-transformers, local, no API call)."))
    story.append(trace(2, "rag/store.py : get_store()", "Saves the vectors into the local Chroma vector database (rag/.chroma/)."))
    story.append(trace(2, "rag/retriever.py : retrieve(...)", "Hybrid keyword + meaning search over the newly-built index, for this specific query."))

    story.append(Paragraph("Step 7 — Cross-check Google Maps (skippable with --no-maps)", S_H2))
    story.append(trace(1, "phase3/google_maps.py : enrich(\"leads_clean.csv\", \"leads_with_maps.csv\", geo)", ""))
    story.append(trace(2, "phase3/google_maps.py : find_place(...)", "Looks the business up via Serper's Google Places integration and matches it — address first, then phone, then name-only as a last resort — against the address already scraped from the business's own site."))

    story.append(Paragraph("Step 8 — Final quality check (always runs)", S_H2))
    story.append(trace(1, "accuracy_check.py : run(...)", "Read-only pass over everything just committed — no re-scraping."))
    story.append(trace(2, "accuracy_check.py : check_field_validity(...) / check_identity_consistency(...) / check_review_relevance(...)", "The three specific checks, written to accuracy_report.txt."))

    story.append(PageBreak())

    # ============================================================
    # Section 2: entry points
    # ============================================================
    story.append(h1("2. The Two Entry Points (CLI vs. API) and the Async Job Layer"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "Everything in Section 1 is reachable two ways: directly from a terminal, or over HTTP from "
        "another program.", S_BODY,
    ))
    story.append(module_table([
        ("main.py", "CLI entry point", "Runs the full 8-step trace above. This is the only path that reaches Steps 3–8 — the API (below) currently only reaches Steps 1–2."),
        ("api.py : run_pipeline_endpoint()", "POST /api/v1/pipeline/run", "Calls phase1_pipeline.run_pipeline() directly (Steps 1–2 only) and blocks until it's done. Supports an optional Idempotency-Key header so a retried call replays the original result instead of running twice."),
        ("api.py : submit_job() / job_status() / job_result() / cancel_job() / resume_job()", "POST/GET /api/v1/jobs/*", "The async alternative — submit returns immediately with a job_id; the caller polls for progress/status instead of blocking."),
        ("job_runner.py : submit_job() / _execute()", "Called by api.py's submit_job()", "Runs phase1_pipeline.run_pipeline() as a background task, feeding it progress-callback and cancel-check hooks, and turns the result into the versioned JobResult contract (job_contracts.py)."),
        ("job_translation.py : target_to_query()", "Called by job_runner._execute()", "Converts a structured request (industry/location/company size) into the one natural-language sentence Stage 1's LLM_planner actually understands."),
    ], header=["Function", "Reached via", "What it does"], col_widths=[CONTENT_WIDTH * 0.30, CONTENT_WIDTH * 0.20, CONTENT_WIDTH * 0.50]))

    story.append(PageBreak())

    # ============================================================
    # Section 3: Module reference
    # ============================================================
    story.append(h1("3. Module Reference"))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("Every source file in the project, grouped by what it's responsible for.", S_BODY))

    def group(title, rows):
        flow = [Paragraph(title, S_H3), module_table(rows, header=["File", "Purpose", "Key functions"],
                                                        col_widths=[CONTENT_WIDTH * 0.24, CONTENT_WIDTH * 0.46, CONTENT_WIDTH * 0.30])]
        return flow

    story.extend(group("Core pipeline (Stages 1–3)", [
        ("main.py", "CLI entry point; owns the full Step 1–8 orchestration shown in Section 1.", "run(), main()"),
        ("phase1_pipeline.py", "The largest module — Stage 1+2's real engine: discovery, scraping, per-candidate processing, storage commit.", "run_pipeline(), process_single_lead(), deconstruct_intent()"),
        ("LLM_planner.py", "Every LLM prompt/call for planning: the main plan, clarification questions, query-variation generation.", "plan_query(), generate_query_variations()"),
        ("model_router.py", "The one place that decides which Groq model + settings a given task uses — nothing else hardcodes a model name.", "select_model(), estimate_cost()"),
        ("relevance_scoring.py", "Deterministic (non-LLM) scoring of how well a candidate matches the plan's industry/location/keywords.", "score_relevance()"),
        ("discovery_classifier.py", "Decides whether a search result is a real single business, a directory to mine, or noise to skip.", "classify_search_result(), extract_businesses_from_html()"),
        ("website_classifier.py", "Decides whether a homepage is worth a full crawl at all, before spending more effort on it.", "classify_homepage(), mine_homepage_businesses()"),
        ("route_filter.py", "Picks which links on a site are worth crawling (contact/about/services), filtering out junk.", "select_high_intent_routes(), rank_urls()"),
        ("route_planner.py", "Stage 3 — picks the most relevant already-crawled pages for a specific query.", "plan_routes()"),
        ("page_retrieval.py", "The hybrid keyword + meaning-based search route_planner uses in the common case.", "hybrid_retrieve(), build_index()"),
        ("page_patterns.py", "Non-LLM homepage link pattern detection (e.g. finding a site's own nav structure).", "extract_homepage_candidates(), detect_patterns()"),
        ("page_intelligence.py", "Generic, reusable per-page summary (page type, structure, content).", "classify_page_type(), build_page_context()"),
        ("final_reasoning.py", "Stage 4 — answers the query's specific extra condition, grounded in real page text (RAG).", "answer_query()"),
        ("domain_utils.py", "Tiny, dependency-light URL/domain helpers shared everywhere (kept separate to avoid pulling in heavy ML imports where not needed).", "domain_key(), strip_tracking()"),
        ("time_utils.py", "One helper: consistent, Z-suffixed ISO-8601 timestamps everywhere a timestamp is generated.", "utc_now_iso()"),
        ("prompt_cache.py", "Generic cache so an identical LLM prompt is never recomputed twice.", "get(), set()"),
    ]))

    story.extend(group("Technology detection (Stage 5)", [
        ("tech_stack.py", "Merges 4 independent detection engines (legacy Wappalyzer, extended cookie/DOM/JS, DNS, robots.txt) into one technology profile per business.", "analyze_raw_html(), get_stored_profile(), build_website_profile()"),
        ("vendor/wappalyzer/", "A working, hand-patched copy of the Wappalyzer package, committed directly into the repo (not pip-installed) — see Section 8 of the Architecture guide for why.", "(third-party, vendored)"),
    ]))

    story.extend(group("Business intelligence / enrichment (part of Stage 2)", [
        ("bi_providers.py", "The pluggable manager that coordinates every enrichment source below into one consistent interface.", "get_manager()"),
        ("social_discovery.py", "Finds a business's official social media links directly from its own site.", "extract_social_links()"),
        ("linkedin_discovery.py", "Finds a company's LinkedIn page via Google search (never scrapes LinkedIn directly).", "discover_company_linkedin(), discover_decision_makers()"),
        ("linkedin_enrichment.py / linkedin_finder.py", "Turns a confirmed LinkedIn match into structured company data.", "enrich_linkedin_company(), find_linkedin()"),
        ("github_enrichment.py / youtube_enrichment.py", "Official-API-based enrichment for the two platforms with genuine, ToS-compliant APIs.", "enrich_github(), enrich_youtube()"),
        ("decision_maker_extractor.py", "Decision-maker source #1: the business's own contact page.", "extract_decision_makers(), extract_contact_page_people()"),
        ("schema_org_extractor.py", "Decision-maker source #2: structured data (Schema.org/JSON-LD) already embedded in the page.", "extract_schema_org()"),
        ("public_search_decision_makers.py", "Decision-maker source #4 (last resort): general public search.", "discover_via_public_search()"),
        ("organization.py", "Infers a plausible org structure from whichever decision-makers were actually found.", "build_organization()"),
        ("buying_committee.py", "For each decision-maker found, infers who's actually likely to own a buying decision.", "infer_likely_ownership(), annotate_buying_committee()"),
        ("business_intelligence.py", "Rolls every enrichment source above into one consolidated per-business summary.", "build_business_intelligence_summary()"),
        ("email_extractor.py", "Production-grade email extraction/validation from scraped HTML.", "domain_has_mx()"),
    ]))

    story.extend(group("Reviews &amp; Maps (Stage 2 harvesting + Stage 7)", [
        ("phase3/review_harvester.py", "Category-aware review discovery across whichever platforms make sense for a business's industry.", "enrich_business(), platforms_for_category()"),
        ("phase3/business_category.py", "Classifies a business into a review-platform category (dental, marina, salon, etc.).", "categorize_domain()"),
        ("phase3/google_maps.py", "Stage 7 — Google Places lookup and address/phone/name matching.", "find_place(), enrich()"),
        ("phase3/store.py", "Phase 3's own tiny JSON cache, separate from the main storage layer.", "load(), save()"),
        ("phase3/config.py", "Shared constants for the phase3 package.", "(constants only)"),
    ]))

    story.extend(group("Evidence index / RAG (Stage 6)", [
        ("rag/chunker.py", "Breaks page/review text into small, embeddable chunks.", "chunk()"),
        ("rag/embedder.py", "Turns text chunks into meaning-vectors using the local sentence-transformers model.", "(embedding function)"),
        ("rag/store.py", "Wraps the local Chroma vector database.", "get_store()"),
        ("rag/retriever.py", "Hybrid keyword + meaning search over the vector store — the actual \"R\" in RAG.", "retrieve()"),
        ("rag/ingest_and_answer.py", "Orchestrates chunk → embed → store → retrieve for a given query and set of businesses; what Step 6 actually calls.", "run()"),
        ("rag/ingest_from_storage.py / rag/ingest_from_csv.py / rag/ingest_reviews.py", "Different entry points for feeding real scraped data into the index (from the storage/ folders, from a CSV, or reviews specifically).", "iter_source_docs_from_storage(), etc."),
        ("rag/pipeline.py / rag/contract.py", "Wires the RAG stages together and defines the data shapes flowing between them.", "(orchestration / type definitions)"),
        ("rag/top_matches.py / rag/ask.py / rag/eval.py / rag/generator.py / rag/llm_match_query.py", "Standalone diagnostic/experimentation scripts — confirmed not imported by the real pipeline (rag/ingest_and_answer.py). Useful for manual debugging, not part of the production call chain.", "(dev tools, not wired into main.py)"),
    ]))

    story.extend(group("Storage &amp; data rollup", [
        ("storage.py", "The main storage layer — writes/reads everything under storage/&lt;domain&gt;/ and crawl_index.csv.", "get_store(), commit_domain()"),
        ("storage_sync.py", "Optional remote mirror of storage/ to Cloudflare R2.", "get_provider(), sync_folder()"),
        ("data_pipeline.py", "Rolls crawl_index.csv (per-page) into leads_clean.csv (per-business); also the data-quality report tooling.", "to_business_level(), run(), clean_rows()"),
        ("accuracy_check.py", "Stage 8 — the final read-only quality pass.", "run(), check_field_validity(), check_identity_consistency(), check_review_relevance()"),
    ]))

    story.extend(group("API &amp; async job layer", [
        ("api.py", "FastAPI app — both the synchronous endpoint and the async job endpoints described in Section 2.", "run_pipeline_endpoint(), submit_job(), job_status(), etc."),
        ("job_contracts.py", "The versioned Pydantic request/progress/result/error data shapes for the async job system, plus the 8-class error taxonomy.", "classify_error()"),
        ("job_runner.py", "The in-process job store and background execution engine for the async job system.", "submit_job(), resume_job(), get_status(), cancel_job()"),
        ("job_translation.py", "Converts a structured job request into the natural-language query Stage 1 expects.", "target_to_query()"),
    ]))

    story.extend(group("Supporting / standalone tools", [
        ("scripts/patch_wappalyzer.py", "One-time tool for re-vendoring a newer Wappalyzer release in the future — not run during normal operation.", "main(), verify()"),
        ("async_scraper/", "A separate, standalone scraping toolkit not called by main.py's pipeline at all — an independent utility living in the same repo.", "(standalone package)"),
    ]))

    story.append(PageBreak())

    # ============================================================
    # Section 4: Tests
    # ============================================================
    story.append(h1("4. Tests — How the Codebase Proves Itself Correct"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "115+ automated tests, organized by how much of the real system each one touches:", S_BODY,
    ))
    story.append(module_table([
        ("tests/smoke/", "Do the core modules even import cleanly, and does the Stage 8 accuracy check work end-to-end against saved fixture data? No network, no API keys."),
        ("tests/unit/", "Pure logic in isolation — request validation, scoring math, path handling — no I/O at all."),
        ("tests/mocked/", "Real logic, but with the network/provider calls replaced by fakes — proves the actual retry/caching/job/API behavior without needing real credentials or spending real money."),
        ("tests/contract/", "Validates that example request/progress/result/error payloads match the versioned schemas exactly, and that the schema files haven't drifted from the code."),
        ("tests/integration/", "The only tests that touch real, paid provider APIs (Groq, Serper, ScrapingBee, Apify) — deliberately excluded from the default test run, run only on request."),
    ], header=["Folder", "What it actually proves"], col_widths=[CONTENT_WIDTH * 0.22, CONTENT_WIDTH * 0.78]))
    story.append(Paragraph("Full detail (142 hand-written test cases, mapped to exactly what's automated already) is in AI-BDM_QA_Test_Plan.xlsx.", S_CAPTION))

    story.append(PageBreak())

    # ============================================================
    # Section 5: One-minute summary
    # ============================================================
    story.append(h1("5. How to Summarize This in One Minute"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "If your manager asks \"what did you actually build,\" here's the short version:", S_BODY,
    ))
    story.append(Paragraph(
        "\"I built the Python research engine for a B2B lead-generation system. It takes one plain-English "
        "request, uses an LLM to understand it, searches the web and scrapes real business websites, "
        "cross-checks each business against Google Maps and its own customer reviews, answers any specific "
        "question the request asked using a retrieval-augmented-generation pattern grounded in the real "
        "scraped text, and runs its own automated quality check before handing back results. It's exposed "
        "both as a command-line tool and as a REST API — including an async job system with progress "
        "tracking, cancellation, and resumability — so a separate Laravel backend can call it as part of a "
        "larger product. The whole thing has 115+ automated tests, is fully documented for handover, and "
        "runs in Docker alongside the rest of the system.\"", S_BODY,
    ))
    story.append(Paragraph(
        "If asked to go one level deeper on any single piece, Section 3's module table gives you the exact "
        "file and function names to point to.", S_CAPTION,
    ))

    return story


def main() -> None:
    out_path = "qa/AI-BDM_Code_Flow_and_Module_Reference.pdf"
    doc = SimpleDocTemplate(
        out_path, pagesize=PAGE_SIZE,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
        title="AI-BDM Code Flow & Module Reference", author="AI-BDM Python Team",
    )
    story = build_story()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
