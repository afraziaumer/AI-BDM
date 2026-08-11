"""Generates qa/AI-BDM_Code_Flow_and_Module_Reference.pdf -- a code-level
walkthrough for an internship handover: the real function-call trace from
each entry point through the whole codebase (verified against the actual
source, not inferred), plus a complete, function-by-function reference in
plain language for every .py file in the project.
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
CARD_BG = colors.HexColor("#F7FAFC")
LIGHT_BLUE = colors.HexColor("#DCE6F1")

styles = getSampleStyleSheet()
S_TITLE = ParagraphStyle("T4", parent=styles["Title"], textColor=NAVY, fontSize=26, fontName="Helvetica-Bold")
S_SUBTITLE = ParagraphStyle("Sub4", parent=styles["Normal"], textColor=GREY, fontSize=12.5, italic=True, spaceAfter=4)
S_H1 = ParagraphStyle("H1_4", parent=styles["Heading1"], textColor=colors.white, fontSize=15, fontName="Helvetica-Bold", leftIndent=8)
S_H2 = ParagraphStyle("H2_4", parent=styles["Heading2"], textColor=NAVY, fontSize=12.5, spaceBefore=12, spaceAfter=5)
S_MODULE = ParagraphStyle("Module4", parent=styles["Heading3"], textColor=colors.white, fontSize=11,
                           fontName="Helvetica-Bold", backColor=STEEL, leftIndent=6, spaceBefore=0, spaceAfter=0,
                           borderPadding=(4, 4, 4, 4))
S_BODY = ParagraphStyle("Body4", parent=styles["Normal"], fontSize=9.6, leading=13.5, spaceAfter=6)
S_CAPTION = ParagraphStyle("Cap4", parent=styles["Normal"], fontSize=8.5, textColor=GREY, italic=True, spaceAfter=8)
S_CODE = ParagraphStyle("Code4", parent=styles["Normal"], fontName="Courier", fontSize=8.8, leading=12,
                         backColor=CODE_BG, borderPadding=(6, 8, 6, 8))
S_FUNC_NAME = ParagraphStyle("FuncName", parent=styles["Normal"], fontName="Courier-Bold", fontSize=9.3,
                              textColor=NAVY, spaceBefore=6, spaceAfter=2)
S_FUNC_FIELD = ParagraphStyle("FuncField", parent=styles["Normal"], fontSize=8.9, leading=12.5, leftIndent=8, spaceAfter=2)
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
    arrow = "" if depth == 0 else ">> "
    return Paragraph(f"{arrow}<font face='Courier' color='#1F4E78'><b>{call}</b></font> — {purpose}", style)


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(GREY)
    canvas.drawString(MARGIN, 1.0 * cm, "AI-BDM — Code Flow & Module Reference")
    canvas.drawRightString(PAGE_SIZE[0] - MARGIN, 1.0 * cm, f"Page {doc.page}")
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Per-function "card" and per-module block
# ---------------------------------------------------------------------------

def fcard(signature: str, needs: str, does: str, returns: str):
    """One function, explained in plain language: what goes in, what happens, what comes out."""
    flow = [
        Paragraph(signature, S_FUNC_NAME),
        Paragraph(f"<b>What it needs:</b> {needs}", S_FUNC_FIELD),
        Paragraph(f"<b>What it does:</b> {does}", S_FUNC_FIELD),
        Paragraph(f"<b>What it gives back:</b> {returns}", S_FUNC_FIELD),
    ]
    return KeepTogether(flow + [Spacer(1, 0.12 * cm)])


def module_block(filename: str, why_exists: str, funcs: list):
    flow = [
        Table([[Paragraph(filename, S_MODULE)]], colWidths=[CONTENT_WIDTH],
              style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), STEEL), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)])),
        Spacer(1, 0.15 * cm),
        Paragraph(why_exists, S_BODY),
    ]
    for f in funcs:
        flow.append(fcard(*f))
    flow.append(Spacer(1, 0.3 * cm))
    return flow


def build_story() -> list:
    story = []

    # ---------------- Cover ----------------
    story.append(Paragraph("AI-BDM", S_TITLE))
    story.append(Paragraph("Code Flow &amp; Module Reference — every file, every function, in plain language.", S_SUBTITLE))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width="100%", color=NAVY, thickness=1.4))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(
        "This is the code-level companion to <b>AI-BDM_Architecture_And_Concepts_Guide.pdf</b> (which "
        "explains <i>what</i> the system does and the ideas behind it, for a reader with no technical "
        "background at all). This document goes one level deeper: it explains <i>how it's actually "
        "built</i> — every file, and inside each file, every important function — in plain language, "
        "without assuming the reader already codes in Python.", S_BODY,
    ))
    story.append(Paragraph(
        "Every function description follows the same three-part pattern, like a recipe card:", S_BODY,
    ))
    story.append(bullets([
        "<b>What it needs</b> — the information this piece of code requires before it can start.",
        "<b>What it does</b> — the actual steps it takes, in plain English.",
        "<b>What it gives back</b> — the result it hands to whoever called it.",
    ]))
    story.append(Paragraph(
        "Read Section 1 first — the real story of what happens end to end. Section 3 is the detailed "
        "reference to come back to afterward, module by module, function by function.", S_BODY,
    ))
    story.append(PageBreak())

    story.append(h1("Contents"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(bullets([
        "1. The Complete Execution Trace — main.py, start to finish",
        "2. The Two Entry Points (CLI vs. API) and the Async Job Layer",
        "3. Module Reference — every file, every function, explained",
        "&nbsp;&nbsp;&nbsp;3.1 Core pipeline (Stages 1–3)",
        "&nbsp;&nbsp;&nbsp;3.2 Technology detection (Stage 5)",
        "&nbsp;&nbsp;&nbsp;3.3 Business intelligence / enrichment",
        "&nbsp;&nbsp;&nbsp;3.4 Reviews &amp; Maps (Stage 2 harvesting + Stage 7)",
        "&nbsp;&nbsp;&nbsp;3.5 Evidence index / RAG (Stage 6)",
        "&nbsp;&nbsp;&nbsp;3.6 Storage &amp; data rollup",
        "&nbsp;&nbsp;&nbsp;3.7 API &amp; async job layer",
        "&nbsp;&nbsp;&nbsp;3.8 Supporting / standalone tools",
        "4. Tests — how the codebase proves itself correct",
        "5. How to Summarize This in One Minute",
    ]))
    story.append(PageBreak())

    # ============================================================
    # Section 1: The trace (kept from before, lightly simplified)
    # ============================================================
    story.append(h1("1. The Complete Execution Trace"))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("This is exactly what happens, in order, when someone runs:", S_BODY))
    story.append(code('python main.py --query "find 3 dental clinics in Islamabad with no online booking"'))
    story.append(Paragraph(
        "Each line below is one function being called. The more a line is indented, the deeper it is — "
        "an indented line was called <i>by</i> the line directly above it, the way a phone call inside a "
        "phone call would be. This was traced directly from the real source code, not written from "
        "memory.", S_CAPTION,
    ))

    story.append(Paragraph("Entry point", S_H2))
    story.append(trace(0, "main.py : main()", "Reads the command you typed (--query, --concurrency, --no-rag, --no-maps) and hands off to run()."))
    story.append(trace(1, "main.py : run(...)", "The conductor of the whole process — every step below happens inside this one function, one after another."))

    story.append(Paragraph("Steps 1 &amp; 2 — Understand the request, then search and scrape", S_H2))
    story.append(trace(1, "main.py : _plan_interactively(...)", "Wraps Steps 1+2 so that if the AI needs to ask you a clarifying question, it can do that in the same terminal window before continuing."))
    story.append(trace(2, "phase1_pipeline.py : run_pipeline(...)", "The real engine for Steps 1 and 2 — everything below this line happens inside this one call."))
    story.append(trace(3, "phase1_pipeline.py : moderate_user_query(...)", "A safety check that runs FIRST, before spending any money — refuses to continue if the request breaks usage policy."))
    story.append(trace(3, "phase1_pipeline.py : deconstruct_intent(...)", "Figures out what you actually mean."))
    story.append(trace(4, "LLM_planner.py : plan_query(...)", "Sends your sentence to the AI (Groq) with detailed instructions, and gets back a structured plan: industry, location, how many results, whether there's an extra condition to check, and whether it needs to ask you something first."))
    story.append(trace(4, "model_router.py : select_model(...)", "Decides which specific AI model and settings this particular call should use."))
    story.append(trace(3, "phase1_pipeline.py : discover_targets(...) / discover_places(...)", "Finds candidate business web addresses using Google Search and Google Maps (both via Serper.dev)."))
    story.append(trace(3, "phase1_pipeline.py : process_single_lead(...)", "Runs once for every candidate business found. This is the single biggest, busiest function in the whole project — everything below happens inside it, once per business."))
    story.append(trace(4, "website_classifier.py : classify_homepage(...)", "A quick check: is this homepage worth fully reading, or does it look like a directory/junk page?"))
    story.append(trace(4, "route_filter.py : select_high_intent_routes(...)", "Out of every link on the site, picks just the handful worth actually reading (contact, about, services) instead of the whole site."))
    story.append(trace(4, "discovery_classifier.py : classify_search_result(...)", "Tells apart a real single business from a directory page that should be mined for MORE businesses."))
    story.append(trace(4, "relevance_scoring.py : score_relevance(...)", "A rule-based (no AI) check of how well this business actually matches what was asked for."))
    story.append(trace(4, "tech_stack.py : analyze_raw_html(...)", "Figures out what software/technology this website runs on, using the page already downloaded — done for every business regardless of whether the request asked about technology."))
    story.append(trace(4, "phase3/review_harvester.py : enrich_business(...)", "Looks for customer reviews on whichever platforms make sense for this kind of business."))
    story.append(trace(4, "bi_providers.py : get_manager().discover_social_profiles(...)", "Finds the business's official social media pages."))
    story.append(trace(4, "github_enrichment.py / youtube_enrichment.py", "Checks two platforms with genuine, rule-following official APIs."))
    story.append(trace(4, "bi_providers.py : get_manager().discover_decision_makers(...)", "Tries several ways, in order, to find named people at the business: its own contact page first, then structured data on the page, then LinkedIn (found via a normal Google search, never scraped directly), then a general search as a last resort."))
    story.append(trace(4, "organization.py : build_organization(...)", "Groups whoever was found into a guessed org chart (departments, counts)."))
    story.append(trace(4, "business_intelligence.py : build_business_intelligence_summary(...)", "Combines everything found above into one tidy summary for this business."))
    story.append(trace(4, "storage.py : commit_domain(...)", "Saves everything found for this business permanently — this is the actual \"write it to disk\" moment."))
    story.append(trace(4, "storage_sync.py : sync_folder(...)", "Optional — also copies that folder to cloud storage (Cloudflare R2), if that's turned on."))

    story.append(Paragraph("Step 3 — Pick the right pages to focus on", S_H2))
    story.append(trace(1, "route_planner.py : plan_routes(...)", "Runs once for every business that was discovered."))
    story.append(trace(2, "page_retrieval.py : hybrid_retrieve(...)", "Searches that one business's already-read pages for whichever ones are most relevant to the question — combining exact-word matching and meaning-based matching. This is the normal path and doesn't need to call the AI."))
    story.append(trace(2, "LLM_planner.py (fallback call)", "Only used in the rare case the search above finds nothing good enough."))

    story.append(Paragraph("Step 4 — Answer the specific question (only if the request asked one)", S_H2))
    story.append(trace(1, "final_reasoning.py : answer_query(...)", "Runs once per business, but only when the request had an extra condition to check."))
    story.append(trace(2, "(a Groq AI call, using only the real page text found above)", "Answers strictly from that real text, and says \"not enough evidence\" instead of guessing when the text doesn't actually answer the question."))

    story.append(Paragraph("Step 5 — Show the technology results (only if the request asked)", S_H2))
    story.append(trace(1, "tech_stack.py : get_stored_profile(...)", "Reads back the technology profile that was already worked out during Step 2 — nothing new is computed here unless something was somehow missed earlier."))

    story.append(Paragraph("Cleaning — build the one-row-per-business results file", S_H2))
    story.append(trace(1, "data_pipeline.py : run(...)", "Turns the detailed page-by-page records into one clean row per business."))
    story.append(trace(2, "data_pipeline.py : to_business_level(...)", "The actual rollup logic — for each business, picks the best title/description and combines contact details found across all its pages."))

    story.append(Paragraph("Step 6 — Build a searchable memory of everything found (skip with --no-rag)", S_H2))
    story.append(trace(1, "rag/ingest_and_answer.py : run(...)", ""))
    story.append(trace(2, "rag/chunker.py : chunk(...)", "Breaks the text into small, manageable pieces."))
    story.append(trace(2, "rag/embedder.py", "Turns each piece into a list of numbers representing its meaning (using a small, free, local AI model — no internet call)."))
    story.append(trace(2, "rag/store.py : get_store()", "Saves those number-lists into a local search database (Chroma)."))
    story.append(trace(2, "rag/retriever.py : retrieve(...)", "Searches that database for the pieces most relevant to this specific request."))

    story.append(Paragraph("Step 7 — Cross-check Google Maps (skip with --no-maps)", S_H2))
    story.append(trace(1, "phase3/google_maps.py : enrich(...)", ""))
    story.append(trace(2, "phase3/google_maps.py : find_place(...)", "Looks the business up on Google Maps and confirms the match — trying its address first, then its phone number, and only matching by name alone as a last resort."))

    story.append(Paragraph("Step 8 — Final quality check (always runs)", S_H2))
    story.append(trace(1, "accuracy_check.py : run(...)", "A read-only double-check over everything just saved — nothing is re-downloaded."))
    story.append(trace(2, "accuracy_check.py : check_field_validity(...) / check_identity_consistency(...) / check_review_relevance(...)", "The three specific checks — are the phone/email/rating formatted correctly, does the business's name match across every source, and do the reviews actually mention this business."))

    story.append(PageBreak())

    # ============================================================
    # Section 2: entry points
    # ============================================================
    story.append(h1("2. The Two Entry Points (CLI vs. API) and the Async Job Layer"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "Everything in Section 1 can be reached two different ways: typed directly into a terminal, or "
        "requested by another computer program over the network.", S_BODY,
    ))
    story.extend(module_block("main.py", "The command-line way of running everything — this is what a person types directly.", [
        ("run(query, concurrency, no_rag, no_maps)",
         "The plain-English request, how many pages to read at once, and two on/off switches for skipping Steps 6 and 7.",
         "Runs Steps 1 through 8 shown in Section 1, in order, printing progress to the screen as it goes.",
         "Nothing is returned directly — results are written to files (leads_clean.csv, leads_with_maps.csv, accuracy_report.txt)."),
        ("main()",
         "Whatever was typed after \"python main.py\" on the command line.",
         "Reads those typed options and calls run() with them.",
         "Nothing — this is just the doorway into run()."),
    ]))
    story.extend(module_block("api.py", "The network way of running things — lets another program (like the Laravel backend) trigger a research request instead of a person typing a command.", [
        ("run_pipeline_endpoint(req, response, idempotency_key)",
         "A request with the plain-English query and how many pages to read at once, sent over the network.",
         "Runs Steps 1+2 only (not the full 8 steps main.py runs) and waits until they're completely finished before replying. If the same request is sent twice with the same \"Idempotency-Key\" tag, the second call just repeats the first answer instead of doing the work again.",
         "The same kind of results summary main.py prints, but as structured data instead of screen text."),
        ("submit_job(req)",
         "The same kind of request, but this time the caller doesn't want to wait.",
         "Starts the work in the background immediately and hands back an ID for it right away.",
         "A small confirmation: \"job accepted, here's its ID, current status is queued.\""),
        ("job_status(job_id)",
         "The ID from submit_job().",
         "Looks up how that job is doing right now.",
         "Its current stage, how far along it is, and a timestamp proving it's still alive."),
        ("job_result(job_id, response)",
         "The ID from submit_job().",
         "Checks whether that job has actually finished yet.",
         "If it's still running: a polite \"come back later.\" If it's done: the full results."),
        ("cancel_job(job_id)",
         "The ID from submit_job().",
         "Asks the job to stop at the next safe opportunity, keeping whatever it already found.",
         "Whether the cancellation request was accepted (a job that already finished can't be cancelled, which isn't treated as an error)."),
        ("resume_job(job_id, req)",
         "The ID of a job that already failed or was cancelled, plus a brand-new ID for the retry.",
         "Starts a new job that searches for the exact same thing the original one was looking for. Businesses already found and saved by the original attempt are automatically skipped, so it isn't starting from zero.",
         "A new job ID, running in the background just like submit_job()."),
    ]))
    story.extend(module_block("job_runner.py", "The engine behind submit_job()/job_status()/etc. above — keeps track of every job's progress while it runs in the background.", [
        ("submit_job(req, resumed_from)",
         "A structured request (and, only when called by resume_job below, a note of which earlier job this is a redo of).",
         "Registers the job, then starts the actual pipeline work running in the background without making anyone wait for it.",
         "Nothing directly — the job now exists and can be checked on."),
        ("resume_job(original_job_id, new_job_id, new_correlation_id, tenant_ref)",
         "The ID of an old job to restart, plus a new ID for the fresh attempt.",
         "Copies the original request's search criteria onto a brand-new job and starts it.",
         "Nothing directly — same as submit_job(), the new job now exists."),
        ("get_status(job_id)",
         "A job ID.",
         "Looks up that job's current state.",
         "Its stage, progress, and last-seen-alive timestamp."),
        ("cancel_job(job_id)",
         "A job ID.",
         "Flags that job to stop as soon as it safely can.",
         "True if the cancellation was accepted, False if the job had already finished."),
        ("get_result(job_id)",
         "A job ID.",
         "Checks whether that job has reached a final state yet.",
         "Nothing if it's still running; the complete results once it's done."),
    ]))
    story.extend(module_block("job_translation.py", "A translator between two different ways of describing a search request.", [
        ("target_to_query(req)",
         "A structured request (separate fields for industry, location, company size, etc.).",
         "Converts those separate fields into the one plain-English sentence the rest of the system actually understands, since some of those fields (like company size) can't currently be honored and need to be flagged rather than silently ignored.",
         "The resulting sentence, plus a list of any warnings about parts of the request that couldn't be fully honored."),
    ]))
    story.extend(module_block("job_contracts.py", "Defines the exact, agreed-upon shape of every request/response the async job system uses, and sorts failures into understandable categories.", [
        ("classify_error(raw_message, correlation_id, exc)",
         "A description of something that went wrong, and a tracking ID.",
         "Looks at what kind of failure this actually was and sorts it into one of 8 standard categories (e.g. \"bad input,\" \"ran out of quota,\" \"the website blocked us\") instead of leaving it as an unstructured error message.",
         "A structured error record a calling program can act on automatically (retry, alert someone, give up), rather than just a string of text."),
    ]))

    story.append(PageBreak())

    # ============================================================
    # Section 3: Module reference (expanded, per-function)
    # ============================================================
    story.append(h1("3. Module Reference — Every File, Every Function"))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        "The rest of this document is a complete reference. Skip to whichever file you need — each one "
        "starts with why it exists, then a card for every important function in it.", S_BODY,
    ))

    # ---- 3.1 Core pipeline ----
    story.append(Paragraph("3.1 Core Pipeline (Stages 1–3)", S_H2))

    story.extend(module_block("phase1_pipeline.py", "The largest, most important file in the project. It's the real engine behind Steps 1 and 2 — everything about understanding a request, finding candidate businesses, reading their websites, and deciding which ones genuinely qualify happens here.", [
        ("run_pipeline(user_query, limit, concurrency, progress_cb, cancel_check)",
         "The plain-English request, and a few optional settings (how many results, how many pages to read at once, and optional \"check in\"/\"should I stop\" callbacks for the async job system).",
         "Runs the entire Step 1+2 process end to end: plans the request, searches for candidates, reads each candidate's website, and saves the ones that genuinely qualify.",
         "One big results summary — how many were found, how many qualified, and the details of each."),
        ("moderate_user_query(user_prompt)",
         "The plain-English request.",
         "Checks it against usage policy before anything else runs, so a problematic request never triggers any paid searches or scraping.",
         "Whether the request is safe to proceed with, and why not if it isn't."),
        ("deconstruct_intent(user_prompt)",
         "The plain-English request.",
         "Passes it to the AI planner (LLM_planner.py) and packages the structured plan that comes back.",
         "The structured plan — industry, location, how many results, etc."),
        ("discover_targets(session, query, limit, exclude_domains, start_page, per_page, max_pages, country_code, location)",
         "A search phrase and various paging/limit settings.",
         "Runs a Google web search (via Serper.dev) for candidate businesses, page by page.",
         "A list of candidate businesses found."),
        ("discover_places(session, query, limit, exclude_domains, country_code, location)",
         "A search phrase.",
         "Looks up real businesses directly through Google Maps (via Serper's Places data) — a faster, more reliable source of genuine businesses than a plain web search.",
         "A list of real candidate businesses with map data attached."),
        ("process_single_lead(session, semaphore, target, cache, exclude_keywords, include_keywords, industry, geo, country_code, phone_regex, user_query)",
         "One candidate business, plus the plan's criteria (industry, location, keywords to require or exclude).",
         "The single busiest function in the project — reads the business's website, decides whether it genuinely qualifies, harvests reviews and technology/social/decision-maker data, and saves everything if it qualifies. See Section 1 for the full breakdown of everything this calls.",
         "A record describing what happened for this one business (qualified, rejected, or failed, with details)."),
        ("crawl_site(session, root_url, root_html, root_method)",
         "A business's homepage address and its already-downloaded content.",
         "Reads through the site's pages one at a time, keeping only what's actually needed in memory (never saving the raw HTML to disk).",
         "The cleaned, readable text found across the site's pages."),
        ("extract_contacts(html, text, country_code, phone_regex, site_domain, verify_mx, email_min_score)",
         "One page's content.",
         "Pulls out every email address and phone number it can find on that page, and optionally double-checks that an email's domain can actually receive mail.",
         "The contact details found on that page."),
        ("classify_relevance(industry, geo, name, page_text, domain, address, schema_organization)",
         "A business's details and the industry/location being searched for.",
         "A rule-based (non-AI) check of how well this business matches — the main relevance gate that filters out obvious non-matches before spending more effort.",
         "A relevance verdict and score."),
        ("qualify_lead(matched_exclude, matched_include, include_keywords, has_text)",
         "Which required/excluded keywords were found across the business's pages.",
         "Decides the final qualification outcome — genuinely qualified, or excluded because of a keyword the request said to avoid.",
         "The qualification verdict."),
        ("resolve_effective_limit(plan, limit)",
         "The AI's planned result count, and an optional override.",
         "Works out the real target number of results to aim for, and whether the search should be a thorough multi-round effort or a single quick pass.",
         "The final target count and a yes/no for \"search aggressively.\""),
        ("load_cache() / load_last_run()",
         "Nothing (reads from the local saved files).",
         "Loads what's already been found and saved from previous runs, so already-known businesses aren't redone from scratch.",
         "The previously-saved data."),
        ("print_summary(summary)",
         "The results summary from run_pipeline().",
         "Formats it into a readable block of text.",
         "Nothing directly — prints straight to the screen."),
    ]))

    story.extend(module_block("LLM_planner.py", "Every single AI (LLM) call related to planning a request lives in this one file — nothing else in the project talks to the AI for planning purposes.", [
        ("plan_query(user_query)",
         "The plain-English request.",
         "Sends it to the AI with detailed, carefully-written instructions covering exactly what to figure out: industry, location, how many results, whether there's an extra condition, and whether a clarifying question is needed first.",
         "The structured plan as a dictionary of fields."),
        ("moderate_query(user_query)",
         "The plain-English request.",
         "Asks the AI to judge whether the request is safe/allowed, before anything else happens.",
         "Whether it's safe, and if not, why."),
        ("classify_business(industry, geo, name, page_text)",
         "A business's scraped page text and the industry/location being searched for.",
         "Asks the AI to judge whether this business genuinely matches — used as a careful double-check for borderline cases.",
         "A relevance classification."),
        ("generate_query_variations(user_query, industry, geo, exclude, max_variations)",
         "The original request and a list of search phrases already tried.",
         "Asks the AI for a handful of alternative ways to search for the exact same thing (never a different industry or location) — used only when the original search has run dry but more results are still needed.",
         "A list of new search phrases to try."),
        ("call_llm(client, messages, response_format)",
         "The prepared AI conversation and the expected reply format.",
         "The shared low-level function that actually sends any request to the AI provider (Groq), through the central model-routing rules.",
         "The AI's raw reply text."),
        ("get_client()",
         "Nothing (reads the API key from the environment).",
         "Creates the connection to the Groq AI service.",
         "A ready-to-use client object."),
    ]))

    story.extend(module_block("model_router.py", "The single place that decides which specific AI model gets used for which kind of task — nowhere else in the project is allowed to hardcode a model name.", [
        ("select_model(task)",
         "A label describing what kind of task this AI call is for (e.g. \"quick classification\" vs. \"deep reasoning\").",
         "Looks up the right model and settings for that specific kind of task — cheaper/faster models for simple decisions, a stronger model for genuine reasoning, with an automatic backup model if the first choice fails.",
         "Which model to use and its settings."),
        ("estimate_cost(model, prompt_tokens, completion_tokens)",
         "Which model was used and roughly how much text went in and came out.",
         "Works out a rough dollar cost for that one AI call, purely for logging/monitoring purposes.",
         "An estimated cost in dollars."),
    ]))

    story.extend(module_block("relevance_scoring.py", "A deterministic (non-AI, rule-based) way of scoring how well a business matches a request — used as the fast, cheap first-pass filter before anything more expensive happens.", [
        ("score_relevance(industry, geo, name, domain, sample_text, address, schema_organization, skip_safety_net)",
         "A business's details and the industry/location being searched for.",
         "Compares them using straightforward rules (does the text mention the right industry, does the address match the requested area) rather than asking the AI — much faster and free.",
         "A relevance score and verdict (a strong match, a possible match, or a rejection)."),
    ]))

    story.extend(module_block("discovery_classifier.py", "Sorts every search result into one of three buckets: a real single business, a directory/listing page worth mining for more businesses, or noise to ignore.", [
        ("classify_search_result(url, title)",
         "One search result's web address and title.",
         "Applies a set of rules to decide which of the three buckets it belongs in.",
         "The classification, with a confidence level."),
        ("extract_businesses_from_html(html, source_url, source_name, confidence, seen_hosts, needed)",
         "A directory-style page's content.",
         "Pulls out links that look like they point to individual real businesses, skipping ones already seen.",
         "A list of newly-found candidate businesses."),
        ("extract_businesses_from_source(session, source_url, source_name, confidence, seen_hosts, needed)",
         "A directory page's web address.",
         "Downloads that page and then runs extract_businesses_from_html() on it.",
         "The same list of newly-found candidates."),
        ("is_utility_link(host, path, anchor)",
         "One link found on a page.",
         "Checks whether it's just a sign-up/login/help/advertising link rather than a real business page.",
         "True or false."),
        ("domain_key(url_or_host) / compute_content_hash(html) / confidence_rank(confidence)",
         "A web address, a page's content, or a confidence label.",
         "Three small helper checks: normalizing a domain name, creating a fingerprint of a page's content (to detect duplicates), and turning a confidence label into a sortable number.",
         "The normalized domain, the fingerprint, or the sortable number, respectively."),
    ]))

    story.extend(module_block("website_classifier.py", "Decides, cheaply and quickly, whether a homepage is worth a full, expensive crawl at all.", [
        ("classify_homepage(domain, url, html, tech_raw, user_query)",
         "A homepage's already-downloaded content.",
         "Combines quick rule-based signals with, when needed, a fast AI check to decide: deep-crawl this site, treat it as a directory to mine, or skip it entirely.",
         "The decision, with the reasoning behind it."),
        ("mine_homepage_businesses(html, url, domain, classification, needed)",
         "A homepage already classified as directory-like.",
         "Extracts links to the real individual businesses listed on it.",
         "A list of newly-found candidate businesses."),
        ("extract_homepage_signals(html, url, domain, tech_raw) / rule_based_score(signals)",
         "A homepage's content.",
         "Pulls out cheap, obvious signals (like how many outbound links it has) and turns them into a \"how directory-like is this\" score, with no AI involved.",
         "The signals, and the score."),
        ("is_non_english(html_lang, text_blob)",
         "A page's declared language and its actual text.",
         "Makes a best-effort guess at whether the page is confidently NOT in English.",
         "True or false (defaults to \"assume English\" when unsure)."),
    ]))

    story.extend(module_block("route_filter.py", "Given a business's homepage, works out which of its own links are actually worth reading (contact, about, services) instead of the whole site.", [
        ("select_high_intent_routes(homepage_html, base_url, user_query, knowledge_gaps)",
         "A homepage's content and address.",
         "Runs the full pipeline of extracting links, filtering out junk, scoring what's left by how likely it is to contain useful information, and picking the best few.",
         "The chosen pages to actually read, each with a priority and a reason."),
        ("select_routes_for_site(website, user_query, knowledge_gaps)",
         "A business's web address.",
         "The same process as above, but starting from what's already been saved for that site rather than fresh HTML.",
         "The chosen pages."),
        ("extract_links(homepage_html) / normalize_urls(hrefs, base_url) / filter_junk(urls, base_url, config)",
         "A homepage's raw links.",
         "Three cleanup steps in sequence: pull out every link, turn them into full, consistent web addresses, then throw out ones that are obviously not useful content (like login pages or social share buttons).",
         "The cleaned, de-duplicated, junk-free list of links."),
        ("score_url(url) / rank_urls(urls) / score_candidate(link)",
         "A link (or list of links).",
         "Scores each one by how likely its web address is to lead to useful information (e.g. a URL containing \"contact\" or \"about\" scores higher), with no AI involved.",
         "A score, or the list sorted best-to-worst."),
        ("classify_link(link)",
         "One candidate link with its surrounding context.",
         "Decides how confident to be that this link is actually useful, based only on where it sits in the page's navigation.",
         "A confidence level and the reason for it."),
    ]))

    story.extend(module_block("route_planner.py", "Stage 3's actual entry point — for one already-crawled business, decides which of its saved pages are most relevant to answer a specific question.", [
        ("plan_routes(website, query)",
         "A business's web address and the question being asked.",
         "Combines a fast rule-based shortlist with the search-first retrieval in page_retrieval.py, only falling back to an AI call in the rare case nothing useful turns up automatically.",
         "The selected pages, with a confidence level for the choice."),
        ("load_page_metadata(domain) / build_link_graph(domain, pages)",
         "A business's domain.",
         "Reads back everything already saved about that business's pages and how they link to each other.",
         "That saved data, ready to be scored."),
        ("rule_based_prefilter(pages, graph) / heuristic_plan(candidates)",
         "A business's saved pages.",
         "Throws out obviously irrelevant pages and ranks what's left using simple rules — the deterministic fallback used when nothing fancier is needed.",
         "The pre-filtered/ranked shortlist."),
    ]))

    story.extend(module_block("page_retrieval.py", "The actual \"search this business's own pages for the right answer\" engine that Stage 3 relies on in the normal case — no AI call needed.", [
        ("hybrid_retrieve(domain, query, top_k)",
         "A business's domain and the question being asked.",
         "Searches that business's already-read pages using both exact keyword matching and meaning-based matching together, since combining the two finds more of what's actually relevant than either alone.",
         "The best-matching page excerpts, ranked."),
        ("build_index(domain)",
         "A business's domain.",
         "Makes sure that business's pages are ready to be searched quickly (builds the local search index if it doesn't already exist).",
         "Whether the index is ready."),
        ("rerank(hits, query, top_n)",
         "A first-pass list of matches.",
         "Re-orders them using a combined, weighted scoring rule — no AI involved, just consistent math.",
         "The final, re-ordered shortlist."),
    ]))

    story.extend(module_block("page_patterns.py &amp; page_intelligence.py", "Two small, no-AI helper files: one spots repeating link patterns on a homepage, the other builds a reusable, structured summary of any single page.", [
        ("page_patterns.py : extract_homepage_candidates(html, base_url, root_domain) / detect_patterns(candidates)",
         "A homepage's links.",
         "Groups links that share a common web-address pattern (e.g. every product page under /shop/...) so repeating structures are recognized.",
         "The grouped pattern summaries."),
        ("page_intelligence.py : build_page_context() / classify_page_type(...) / extract_structure(...) / extract_content(...)",
         "One page's cleaned text and headings.",
         "Builds a consistent, structured summary of that page — what type of page it is (contact/about/services/etc.), its headings, and short excerpts of its real content.",
         "That structured per-page summary, reused throughout the project."),
        ("page_intelligence.py : enrich_anchors(domain)",
         "A business's domain.",
         "Goes back and fills in link-text information for every page already saved for that business.",
         "Nothing directly — updates the saved data."),
    ]))

    story.extend(module_block("final_reasoning.py", "Stage 4 — the one place in the whole project that actually writes a synthesized answer to a specific question, always grounded in real evidence (the RAG pattern).", [
        ("answer_query(domain, query, top_n)",
         "A business's domain and the specific question being asked.",
         "Retrieves the most relevant already-read pages (via page_retrieval.py), then asks the AI to answer strictly from that real text, citing exactly which pages it used — and to say \"not enough evidence\" rather than guess when the text doesn't actually answer the question.",
         "The answer, its confidence level, and which pages it was based on."),
    ]))

    story.extend(module_block("domain_utils.py &amp; time_utils.py", "Two tiny, dependency-free helper files used everywhere in the project — kept deliberately small so importing them never accidentally drags in the heavy AI libraries.", [
        ("domain_utils.py : domain_key(url_or_host)",
         "Any web address or hostname.",
         "Strips it down to its clean, registered domain (e.g. turns \"help.example.com\" into \"example.com\").",
         "The clean domain name — the single, consistent way this project identifies a business."),
        ("domain_utils.py : strip_tracking(url)",
         "A web address.",
         "Removes tracking parameters (like utm_source, gclid) so the same real page isn't treated as different addresses.",
         "The cleaned web address."),
        ("time_utils.py : utc_now_iso(timespec)",
         "Nothing (reads the current time).",
         "Produces the current time in one single, consistent written format used everywhere a timestamp is saved.",
         "The current time as text, e.g. \"2026-08-06T12:00:00Z\"."),
    ]))

    story.extend(module_block("prompt_cache.py", "A small cache so the exact same question is never sent to the AI twice.", [
        ("get(task, model, messages) / set(task, model, messages, response)",
         "Which task/model/question was asked (get), or the answer to save (set).",
         "Checks whether this exact question has already been asked and answered before reusing it; otherwise saves a fresh answer for next time.",
         "The previously-saved answer, if one exists (get); nothing directly (set)."),
    ]))

    story.append(PageBreak())

    # ---- 3.2 Tech stack ----
    story.append(Paragraph("3.2 Technology Detection (Stage 5)", S_H2))
    story.extend(module_block("tech_stack.py", "Everything related to figuring out what software/technology a business's website runs on — CMS, booking tools, analytics, hosting, and more.", [
        ("analyze_raw_html(domain, html, headers, cookies)",
         "A business's homepage content, already downloaded — no extra fetch needed.",
         "Runs 4 separate detection methods (a legacy fingerprint engine, an extended engine covering cookies/page-structure/scripts, a domain-record lookup, and a robots.txt check) and merges everything they find into one profile.",
         "The combined raw technology profile for that business."),
        ("build_website_profile(domain, raw)",
         "The raw detection results from analyze_raw_html().",
         "Organizes the raw findings into readable categories and business-friendly groupings (e.g. \"booking system,\" \"analytics,\" \"CMS\").",
         "The full, organized technology profile."),
        ("get_stored_profile(domain, allow_fallback_scan)",
         "A business's domain.",
         "The normal way anything else in the project reads a technology profile — always checks what's already saved first, and only re-scans as a rare fallback.",
         "The saved technology profile."),
        ("build_normalized_tech_stack(raw) / build_categories_view(raw) / build_versions_view(raw)",
         "The raw detection results.",
         "Three different ways of re-organizing the same raw findings for different purposes (a curated summary, a by-category view, a by-version view).",
         "Each respective organized view."),
        ("generate_sales_signals(domain) / generate_sales_signals_from_profile(profile)",
         "A business's domain, or an already-built profile.",
         "Looks for patterns in the technology profile that suggest a sales opportunity (e.g. an outdated platform, no booking system).",
         "A list of sales signals with confidence levels."),
        ("has_crm(domain) / has_login_page(domain) / has_customer_portal(domain) / is_wordpress(domain) / is_shopify(domain) / is_outdated_stack(domain) / (and similar yes/no checks)",
         "A business's domain.",
         "Each is a simple, specific yes/no question answered by looking at the saved technology profile.",
         "True or false."),
    ]))

    story.append(PageBreak())

    # ---- 3.3 BI/enrichment ----
    story.append(Paragraph("3.3 Business Intelligence / Enrichment (part of Stage 2)", S_H2))
    story.extend(module_block("bi_providers.py", "The coordinator that ties every enrichment source below together behind one consistent interface, so the rest of the project doesn't need to know which specific source found what.", [
        ("get_manager()",
         "Nothing (reads which providers are enabled from configuration).",
         "Builds the manager object that knows how to call every configured enrichment source in the right order.",
         "The ready-to-use manager."),
        ("make_result(provider)",
         "Which provider produced a result.",
         "Wraps that provider's findings into the one standard response shape every enrichment source uses, so results from different sources can be handled identically.",
         "The standardized result."),
    ]))
    story.extend(module_block("social_discovery.py", "Finds a business's own official social media links, directly from its website — never guesses or searches externally for this part.", [
        ("extract_social_links(external_links)",
         "Every outbound link found while reading a business's website.",
         "Filters that list down to ones that are genuinely the business's own social media profiles.",
         "The confirmed social profile links, organized by platform."),
    ]))
    story.extend(module_block("linkedin_discovery.py", "Finds a business's LinkedIn company page and any named decision-makers on LinkedIn — always through a normal Google search (never by directly scraping LinkedIn, which its terms don't allow).", [
        ("discover_company_linkedin(session, company_name, domain, city, country, industry)",
         "A business's name, location, and industry.",
         "Tries several carefully-built Google search phrases in order until one confidently finds the business's real LinkedIn company page.",
         "The matched LinkedIn page, with a confidence score."),
        ("discover_decision_makers(session, company_name, domain, city, country, industry, max_people)",
         "The same business details.",
         "Searches for LinkedIn profile pages of people who plausibly work there, using targeted search phrases built around common executive job titles.",
         "A list of candidate people, each with a confidence score."),
        ("rank_linkedin_candidates()",
         "A list of possible LinkedIn matches.",
         "Scores each one for how likely it genuinely is the right page, so the best match can be picked with confidence.",
         "The candidates, ranked."),
        ("generate_google_xray_queries(...) / generate_decision_maker_xray_queries(...)",
         "A business's name/location/industry, or the specific executive titles being searched for.",
         "Builds the actual search phrases to try — this technique of searching a specific site through a normal search engine is called an \"X-Ray search.\"",
         "A list of search phrases to try in order."),
        ("is_cache_fresh(data) / save_linkedin_candidates(store, domain, company_page, profiles)",
         "Previously-saved LinkedIn data.",
         "Checks whether saved data is recent enough to trust, or saves freshly-found data for next time.",
         "True/false, or nothing (just saves)."),
    ]))
    story.extend(module_block("linkedin_enrichment.py &amp; linkedin_finder.py", "Turn a confirmed LinkedIn match into more detailed, structured company data.", [
        ("linkedin_enrichment.py : enrich_linkedin_company(match, website_domain, provider)",
         "An already-confirmed LinkedIn match.",
         "Builds a fuller structured record about that company from LinkedIn, without ever failing the whole process if this one step doesn't work.",
         "The enriched LinkedIn company record."),
        ("linkedin_finder.py : find_linkedin(session, name, website, geo)",
         "A business's name, website, and location.",
         "An alternative, simpler LinkedIn-matching entry point.",
         "The matched LinkedIn page and confidence."),
    ]))
    story.extend(module_block("github_enrichment.py &amp; youtube_enrichment.py", "The two enrichment sources that use genuine, fully rule-following official APIs (not search-based guessing).", [
        ("github_enrichment.py : enrich_github(session, github_url)",
         "A business's GitHub page address, if one was found.",
         "Fetches its public profile information directly from GitHub's own official API.",
         "The public GitHub profile data."),
        ("youtube_enrichment.py : enrich_youtube(session, youtube_url)",
         "A business's YouTube channel address, if one was found.",
         "Fetches its public channel information directly from YouTube's own official API.",
         "The public channel data (subscriber count, etc.)."),
    ]))
    story.extend(module_block("decision_maker_extractor.py, schema_org_extractor.py &amp; public_search_decision_makers.py", "Three different ways of finding named people at a business, tried in priority order (most reliable first).", [
        ("decision_maker_extractor.py : extract_decision_makers(page_url, text, company, external_links_on_page)",
         "An About/Team/Leadership page's text.",
         "Source #1 (highest priority): scans the business's own page for named people and their job titles.",
         "The people found, with their names, titles, and department guesses."),
        ("decision_maker_extractor.py : extract_contact_page_people(page_url, text, external_links_on_page)",
         "A Contact/Support/Locations page's text.",
         "A similar scan, specifically for contact-style pages.",
         "The people found there."),
        ("schema_org_extractor.py : extract_schema_org(html_or_soup, page_url)",
         "A page's raw content.",
         "Source #2: reads structured data many websites embed for search engines (called Schema.org/JSON-LD data), which sometimes directly lists staff.",
         "Any people found in that structured data."),
        ("public_search_decision_makers.py : discover_via_public_search(session, domain)",
         "A business's domain.",
         "Source #4, the last resort: tries general web searches scoped to that specific domain, looking for named people mentioned anywhere.",
         "Any people found this way."),
    ]))
    story.extend(module_block("organization.py &amp; buying_committee.py", "Take whichever people were found above and make sense of them as an organization.", [
        ("organization.py : build_organization(people)",
         "The list of people found by any of the sources above.",
         "Groups them into a guessed departmental structure.",
         "A breakdown by department, with counts."),
        ("buying_committee.py : infer_likely_ownership(person) / annotate_buying_committee(people)",
         "One person's details, or the whole list.",
         "Makes an educated guess at which business decisions that person is likely to have a say in, based on their title.",
         "The same person record, with a \"likely_owner_of\" field added."),
    ]))
    story.extend(module_block("business_intelligence.py", "Rolls every enrichment source above into one final, consolidated per-business summary.", [
        ("build_business_intelligence_summary(domain, social_profiles, linkedin_company, decision_makers, organization, github_profile, youtube_profile)",
         "The separate results from every enrichment source above, for one business.",
         "Combines them all into a single, tidy summary document for that business.",
         "The consolidated business-intelligence summary."),
    ]))
    story.extend(module_block("email_extractor.py", "Focused specifically on finding and validating email addresses from scraped pages.", [
        ("domain_has_mx(domain, timeout)",
         "A domain name found in an email address.",
         "Checks whether that domain is actually set up to receive email at all (a technical mail-server check), catching obviously fake or broken addresses.",
         "True, False, or \"couldn't determine\" if the check itself failed."),
    ]))

    story.append(PageBreak())

    # ---- 3.4 Reviews & Maps ----
    story.append(Paragraph("3.4 Reviews &amp; Maps (Stage 2 Harvesting + Stage 7)", S_H2))
    story.extend(module_block("phase3/review_harvester.py", "Finds and collects genuine customer reviews for a business, from whichever platforms actually make sense for its type of business.", [
        ("enrich_business(domain, name, industry, geo, page_title, address, phone)",
         "A business's details.",
         "Works out which review platforms are relevant for this kind of business (a dental clinic and a marina check completely different sites), then checks each one.",
         "Reviews found, organized by platform."),
        ("enrich_platform(session, domain, name, address, geo, platform, hosts, phone)",
         "One specific review platform to check.",
         "Searches for and confirms the business's listing on that one platform, then pulls whatever review text is genuinely, publicly available.",
         "That platform's result — matched or not, with any reviews found."),
        ("extract_reviews(html)",
         "A review page's content.",
         "Pulls out only review fields that are explicitly, publicly shown — never guesses or invents review content.",
         "The extracted reviews."),
        ("platforms_for_category(category)",
         "A business category (e.g. \"dental,\" \"marina\").",
         "Looks up which review platforms are actually relevant to check for that category.",
         "The list of relevant platforms."),
    ]))
    story.extend(module_block("phase3/business_category.py", "Sorts a business into a category so review_harvester.py above knows which platforms to check.", [
        ("categorize_domain(domain, industry, company_name, page_title)",
         "A business's details.",
         "The normal, cache-first way of getting a business's category — checks what's already saved before working it out fresh.",
         "The business's category."),
        ("categorize(industry, company_name, page_title)",
         "The same details.",
         "The actual rule-based matching logic.",
         "The best-matching category, or \"uncategorized\" if nothing fits."),
    ]))
    story.extend(module_block("phase3/google_maps.py", "Stage 7 — confirms each business against its real Google Maps listing.", [
        ("enrich(in_path, out_path, geo)",
         "The file of cleaned business results, and the general search location.",
         "Runs the Maps check for every business in that file.",
         "A new file with rating/review-count/match-confidence columns added."),
        ("find_place(session, name, address, geo, phone)",
         "One business's name, address, location, and phone number.",
         "Looks it up through Google Maps and tries to confirm the match — first by comparing the address, then the phone number, and only by name alone as a last resort when there's exactly one candidate.",
         "The matched listing (or no match), the confidence, and which method confirmed it."),
        ("enrich_domain(session, domain, name, address, geo, phone, max_age_days)",
         "A single already-saved business.",
         "The cache-first version of find_place() for one specific business — reuses a recent saved result instead of re-checking every time.",
         "The Maps match result for that business."),
    ]))
    story.extend(module_block("phase3/store.py", "Phase 3's own small, simple save/load system for review and Maps data — separate from the main storage.py.", [
        ("load(domain, platform, max_age_days) / save(domain, platform, record)",
         "A business's domain and which platform's data to load or save.",
         "Reads back a previously-saved record (only if it's recent enough), or writes a fresh one.",
         "The saved record, or nothing on save."),
    ]))

    story.append(PageBreak())

    # ---- 3.5 RAG ----
    story.append(Paragraph("3.5 Evidence Index / RAG (Stage 6)", S_H2))
    story.extend(module_block("rag/ingest_and_answer.py", "The main entry point for Stage 6 — what actually gets called by main.py.", [
        ("run(query, discovered_domains, leads_json_path, k)",
         "The request and which businesses were found.",
         "Coordinates the whole Stage 6 process: breaking text into pieces, turning them into meaning-vectors, saving them, and searching them for this specific request.",
         "A combined answer plus a per-business evidence summary, also printed to the screen."),
    ]))
    story.extend(module_block("rag/chunker.py, rag/embedder.py &amp; rag/store.py", "The three building blocks of the searchable evidence index, in order.", [
        ("rag/chunker.py : chunk(doc)",
         "One document's full text (a scraped page, or a review).",
         "Breaks it into smaller, manageable pieces small enough for the meaning-search model to handle well, keeping a bit of context (like the page's title) with each piece.",
         "A list of text chunks."),
        ("rag/embedder.py (embedding function)",
         "A text chunk.",
         "Runs it through the small, local, free AI model that turns text into a list of numbers representing its meaning.",
         "That list of numbers (the \"embedding\")."),
        ("rag/store.py : get_store()",
         "Nothing (reads which storage backend is configured).",
         "Connects to the local search database (Chroma) where all the embeddings are kept.",
         "A ready-to-use connection to that database."),
    ]))
    story.extend(module_block("rag/retriever.py", "The actual search step — the \"R\" in RAG.", [
        ("retrieve(question, store, embedder, business, k)",
         "A question, the search database, and which business to search within.",
         "Turns the question into its own meaning-vector, then finds the stored chunks whose meaning is closest to it.",
         "The most relevant chunks found, ready to be handed to Stage 4 for an actual answer."),
    ]))
    story.extend(module_block("rag/ingest_from_storage.py, rag/ingest_from_csv.py &amp; rag/ingest_reviews.py", "Three different starting points for feeding real data into the search index, depending on where the data is coming from.", [
        ("rag/ingest_from_storage.py : iter_source_docs_from_high_intent(...) / iter_source_docs_from_storage(...)",
         "Nothing extra — reads directly from the storage/ folders.",
         "Walks through everything already saved for a set of businesses and prepares it to be chunked and embedded.",
         "A stream of documents ready for rag/chunker.py."),
    ]))
    story.append(Paragraph(
        "A few other files in rag/ (generator.py, ask.py, eval.py, top_matches.py, match_query.py, "
        "llm_match_query.py) are standalone diagnostic/experimentation tools — useful for a developer "
        "manually testing the search index, but confirmed <b>not</b> called by the real pipeline "
        "(rag/ingest_and_answer.py above never imports them). Worth knowing they exist, but they're not "
        "part of what actually runs when a request comes in.", S_CAPTION,
    ))

    story.append(PageBreak())

    # ---- 3.6 Storage ----
    story.append(Paragraph("3.6 Storage &amp; Data Rollup", S_H2))
    story.extend(module_block("storage.py", "The main save/load system — everything about writing a business's results to disk and reading them back goes through here.", [
        ("get_store()",
         "Nothing.",
         "Gives back the one shared connection to the local storage system, used everywhere the project needs to save or read business data.",
         "That shared storage object, which itself provides commit_domain() (save everything found for one business) and various read functions."),
    ]))
    story.extend(module_block("storage_sync.py", "An optional add-on that copies everything storage.py saves locally out to cloud storage (Cloudflare R2) as well, for backup.", [
        ("get_provider()",
         "Nothing (reads whether remote sync is configured).",
         "Checks whether cloud sync is turned on and set up correctly.",
         "The ready-to-use cloud sync connection, or nothing if it's not configured — never an error either way."),
    ]))
    story.extend(module_block("data_pipeline.py", "Turns the detailed, page-by-page saved records into the clean, one-row-per-business results files people actually look at.", [
        ("run(path, dry_run, scope_all, routes, tech_stacks)",
         "Which saved records to process.",
         "Runs the full cleaning process end to end: load, clean, roll up to one row per business, check for problems, and write the final files.",
         "Nothing directly — writes leads_clean.csv, leads_clean.json, a quarantine file for anything dropped, and a data-quality report."),
        ("to_business_level(pages)",
         "The detailed, per-page records.",
         "Combines every page belonging to the same business into a single, best-effort business record — picking the best title, combining contact details found across pages.",
         "One record per business."),
        ("clean_rows(rows, expected_industry, expected_geo) / dedupe_pages(rows)",
         "The raw saved records.",
         "Normalizes messy fields and removes duplicate/noisy rows.",
         "The cleaned, de-duplicated rows, plus a separate list of what was rejected and why."),
        ("govern(businesses)",
         "The rolled-up business records.",
         "Checks each one against a set of validity rules.",
         "Which ones passed, and which failed with a reason."),
        ("write_clean_export(businesses, path) / write_clean_export_json(...) / write_quarantine(...) / write_report(...)",
         "The final processed data.",
         "Writes it out to the actual result files people open — a spreadsheet, a JSON version, a \"what got dropped and why\" file, and a readable summary report.",
         "Nothing directly — these are the files that end up on disk."),
    ]))
    story.extend(module_block("accuracy_check.py", "Stage 8 — the final, read-only quality check over everything the pipeline just produced.", [
        ("run(geo, path)",
         "Which results file to check.",
         "Runs all three checks below over every row and writes a combined report.",
         "Nothing directly — writes accuracy_report.txt."),
        ("check_field_validity(row)",
         "One business's row of results.",
         "Checks whether its phone number, email, address, and rating are validly formatted.",
         "A list of anything wrong."),
        ("check_identity_consistency(domain, company_name, page_title)",
         "A business's details from different sources.",
         "Checks whether its name is consistent across its own site, Google Maps, and review platforms — catching a case where the data actually belongs to a different, similarly-named business.",
         "Whether the identity is consistent, and a warning if not."),
        ("check_review_relevance(domain, company_name, geo)",
         "A business's harvested reviews.",
         "Checks whether reviews that came from a general search (like Reddit) actually mention this specific business, rather than something unrelated.",
         "A list of any reviews that look off-topic."),
    ]))

    story.append(PageBreak())

    # ---- 3.7 already covered in section 2 - note ----
    story.append(Paragraph("3.7 API &amp; Async Job Layer", S_H2))
    story.append(Paragraph(
        "Already covered in full, function by function, in Section 2 above (api.py, job_runner.py, "
        "job_translation.py, job_contracts.py) — not repeated here to avoid duplication.", S_BODY,
    ))

    # ---- 3.8 Supporting tools ----
    story.append(Paragraph("3.8 Supporting / Standalone Tools", S_H2))
    story.extend(module_block("scripts/patch_wappalyzer.py", "A one-time maintenance tool, not run during normal operation.", [
        ("main() / verify()",
         "Nothing (operates on the vendored technology-detection package).",
         "Used only when updating the technology-detection package to a newer version in the future, to re-apply the same fixes that were needed last time and confirm they worked.",
         "A pass/fail confirmation."),
    ]))
    story.append(Paragraph(
        "<b>async_scraper/</b> is a separate, self-contained scraping toolkit that lives in the same "
        "repository but is <b>not called by main.py's pipeline at all</b> — an independent utility, not "
        "part of the production call chain described in Section 1.", S_BODY,
    ))
    story.append(Paragraph(
        "<b>vendor/wappalyzer/</b> is a working copy of a third-party technology-detection package, "
        "committed directly into this project (rather than installed normally) because the publicly "
        "published version was found to silently change over time. See the Architecture guide, Section 8, "
        "for the full story.", S_BODY,
    ))

    story.append(PageBreak())

    # ============================================================
    # Section 4: Tests
    # ============================================================
    story.append(h1("4. Tests — How the Codebase Proves Itself Correct"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph("115+ automated tests, organized by how much of the real system each one touches:", S_BODY))
    story.append(bullets([
        "<b>tests/smoke/</b> — do the core pieces even load correctly, and does the final quality check work against sample data? No network, no API keys, runs in seconds.",
        "<b>tests/unit/</b> — pure logic checked in isolation (scoring rules, path handling) with no real files or network involved at all.",
        "<b>tests/mocked/</b> — real logic, but with the network/AI calls replaced by fakes, so the actual retry/caching/job behavior can be proven without needing real credentials or spending real money.",
        "<b>tests/contract/</b> — checks that example requests/results actually match the agreed-upon shapes exactly.",
        "<b>tests/integration/</b> — the only tests that touch real, paid services (Groq, Serper, ScrapingBee, Apify) — deliberately kept out of the everyday test run, only used when specifically asked for.",
    ]))
    story.append(Paragraph("Full detail — 142 hand-written test cases mapped to exactly what's already automated — is in AI-BDM_QA_Test_Plan.xlsx.", S_CAPTION))

    story.append(PageBreak())

    # ============================================================
    # Section 5
    # ============================================================
    story.append(h1("5. How to Summarize This in One Minute"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph("If your manager asks \"what did you actually build,\" here's the short version:", S_BODY))
    story.append(Paragraph(
        "\"I built the Python research engine for a B2B lead-generation system. It takes one plain-English "
        "request, uses an AI model to understand it, searches the web and reads real business websites, "
        "cross-checks each business against Google Maps and its own customer reviews, answers any specific "
        "question the request asked using real evidence instead of guessing, and runs its own automated "
        "quality check before handing back results. It's exposed both as a command-line tool and as a web "
        "API — including a version that runs in the background with progress tracking, cancellation, and "
        "the ability to resume — so a separate backend system can call it as part of a larger product. The "
        "whole thing has 115+ automated tests, is fully documented for handover, and runs in Docker "
        "alongside the rest of the system.\"", S_BODY,
    ))
    story.append(Paragraph(
        "If asked to go one level deeper on any single piece, Section 3 gives you the exact file and "
        "function to point to, and what it does in plain language.", S_CAPTION,
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
