"""Generates qa/AI-BDM_Architecture_And_Concepts_Guide.pdf -- a complete,
plain-language A-Z architecture handover document: what the system does,
every concept (AI/LLM, scraping, NLP, embeddings, RAG, APIs, async jobs)
explained for a reader with no ML/scraping background, the full tech
stack, how each of the 8 pipeline stages is actually implemented, how
data is stored, and how it connects to the wider system (Laravel/Next.js).
Separate from the QA test plan and the shorter project walkthrough.
"""

from __future__ import annotations

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER
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
LIGHT_BLUE = colors.HexColor("#DCE6F1")
GREY = colors.HexColor("#595959")
CODE_BG = colors.HexColor("#F2F2F2")
GOOD = colors.HexColor("#E2EFDA")
WARN = colors.HexColor("#FFF2CC")

styles = getSampleStyleSheet()
S_TITLE = ParagraphStyle("Title3", parent=styles["Title"], textColor=NAVY, fontSize=27, fontName="Helvetica-Bold")
S_SUBTITLE = ParagraphStyle("Subtitle3", parent=styles["Normal"], textColor=GREY, fontSize=12.5, italic=True, spaceAfter=4)
S_H1 = ParagraphStyle("H1c", parent=styles["Heading1"], textColor=colors.white, fontSize=15,
                       fontName="Helvetica-Bold", leftIndent=8)
S_H2 = ParagraphStyle("H2c", parent=styles["Heading2"], textColor=NAVY, fontSize=13, spaceBefore=14, spaceAfter=6)
S_H3 = ParagraphStyle("H3c", parent=styles["Heading3"], textColor=STEEL, fontSize=11, spaceBefore=8, spaceAfter=4)
S_BODY = ParagraphStyle("Body3", parent=styles["Normal"], fontSize=10, leading=14.5, spaceAfter=7)
S_BODY_SMALL = ParagraphStyle("BodySmall", parent=S_BODY, fontSize=9, leading=12.5)
S_LIST = ParagraphStyle("List3", parent=S_BODY, spaceAfter=4)
S_CODE = ParagraphStyle("Code3", parent=styles["Normal"], fontName="Courier", fontSize=9, leading=12.5,
                         backColor=CODE_BG, borderPadding=(6, 8, 6, 8))
S_CAPTION = ParagraphStyle("Caption3", parent=styles["Normal"], fontSize=8.5, textColor=GREY, italic=True, spaceAfter=8)
S_TERM = ParagraphStyle("Term", parent=S_BODY, fontName="Helvetica-Bold", textColor=NAVY, fontSize=10.5, spaceAfter=2)
S_CELL = ParagraphStyle("Cell3", parent=styles["Normal"], fontSize=9, leading=12.5, alignment=TA_LEFT)
S_CELL_HDR = ParagraphStyle("CellHdr3", parent=S_CELL, fontName="Helvetica-Bold", textColor=colors.white)
S_DIAG_LABEL = ParagraphStyle("DiagLabel", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, alignment=TA_CENTER)

PAGE_SIZE = A4
MARGIN = 1.9 * cm
CONTENT_WIDTH = PAGE_SIZE[0] - 2 * MARGIN


def h1(text: str):
    tbl = Table([[Paragraph(text, S_H1)]], colWidths=[CONTENT_WIDTH])
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return tbl


def code(text: str):
    return Paragraph(text.replace("\n", "<br/>"), S_CODE)


def bullets(items, style=S_LIST):
    return ListFlowable([ListItem(Paragraph(i, style), leftIndent=14) for i in items], bulletType="bullet", start="•", leftIndent=14)


def term_block(term: str, plain: str, technical: str = ""):
    flow = [Paragraph(term, S_TERM), Paragraph(plain, S_BODY)]
    if technical:
        flow.append(Paragraph(f"<i>In this project:</i> {technical}", S_BODY_SMALL))
    return KeepTogether(flow + [Spacer(1, 0.15 * cm)])


def two_col_table(rows, col_widths=None, header=None):
    col_widths = col_widths or [CONTENT_WIDTH * 0.28, CONTENT_WIDTH * 0.72]
    data = []
    if header:
        data.append([Paragraph(h, S_CELL_HDR) for h in header])
    for row in rows:
        data.append([Paragraph(str(c), S_CELL) for c in row])
    tbl = Table(data, colWidths=col_widths)
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
    canvas.drawString(MARGIN, 1.1 * cm, "AI-BDM — Architecture & Concepts Guide")
    canvas.drawRightString(PAGE_SIZE[0] - MARGIN, 1.1 * cm, f"Page {doc.page}")
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Pipeline flow diagram (8 boxes, 2 rows of 4, connected by arrows)
# ---------------------------------------------------------------------------

def pipeline_diagram() -> Drawing:
    stages = [
        "1. Understand\nthe Request",
        "2. Search &\nRead Websites",
        "3. Pick the\nRight Pages",
        "4. Answer the\nSpecific Question",
        "5. Identify\nTechnology",
        "6. Build Search-\nable Memory",
        "7. Check\nGoogle Maps",
        "8. Final Quality\nCheck",
    ]
    width = CONTENT_WIDTH
    box_w, box_h = width / 4 - 0.4 * cm, 2.0 * cm
    gap_x, gap_y = 0.5 * cm, 1.4 * cm
    row_y = [3.6 * cm, 3.6 * cm + box_h + gap_y]
    d = Drawing(width, row_y[1] + box_h + 0.3 * cm)

    positions = []
    for row in range(2):
        for col in range(4):
            x = col * (box_w + gap_x)
            y = row_y[row]
            positions.append((x, y))

    # Arrows within each row (left to right) and one connector row1-end -> row2-start
    def arrow(x1, y1, x2, y2):
        d.add(Line(x1, y1, x2, y2, strokeColor=STEEL, strokeWidth=1.6))
        d.add(Line(x2, y2, x2 - 0.18 * cm, y2 + 0.12 * cm, strokeColor=STEEL, strokeWidth=1.6))
        d.add(Line(x2, y2, x2 - 0.18 * cm, y2 - 0.12 * cm, strokeColor=STEEL, strokeWidth=1.6))

    for row in range(2):
        for col in range(3):
            x1 = col * (box_w + gap_x) + box_w
            x2 = (col + 1) * (box_w + gap_x)
            y = row_y[row] + box_h / 2
            arrow(x1 + 0.05 * cm, y, x2 - 0.05 * cm, y)

    # Connector: end of row 1 (box 4, top row) down to start of row 2 (box 5)
    x_top_end = 3 * (box_w + gap_x) + box_w
    y_top = row_y[0] + box_h / 2
    x_bot_start = 0
    y_bot = row_y[1] + box_h / 2
    d.add(Line(x_top_end, y_top, x_top_end + 0.3 * cm, y_top, strokeColor=STEEL, strokeWidth=1.6))
    d.add(Line(x_top_end + 0.3 * cm, y_top, x_top_end + 0.3 * cm, y_bot, strokeColor=STEEL, strokeWidth=1.6))
    d.add(Line(x_top_end + 0.3 * cm, y_bot, x_bot_start - 0.05 * cm, y_bot, strokeColor=STEEL, strokeWidth=1.6))
    d.add(Line(x_bot_start - 0.05 * cm, y_bot, x_bot_start - 0.23 * cm, y_bot + 0.12 * cm, strokeColor=STEEL, strokeWidth=1.6))
    d.add(Line(x_bot_start - 0.05 * cm, y_bot, x_bot_start - 0.23 * cm, y_bot - 0.12 * cm, strokeColor=STEEL, strokeWidth=1.6))

    for (x, y), label in zip(positions, stages):
        d.add(Rect(x, y, box_w, box_h, fillColor=LIGHT_BLUE, strokeColor=NAVY, strokeWidth=1.1, rx=4, ry=4))
        lines = label.split("\n")
        n = len(lines)
        for i, line in enumerate(lines):
            ty = y + box_h / 2 + (n - 1 - 2 * i) * 0.22 * cm
            d.add(String(x + box_w / 2, ty, line, fontName="Helvetica-Bold", fontSize=7.6,
                          fillColor=NAVY, textAnchor="middle"))

    # "Start" and legend note below
    d.add(String(0, row_y[0] - 0.7 * cm,
                  "Steps 4, 5, 6, and 7 only run when the request actually needs them.",
                  fontName="Helvetica-Oblique", fontSize=8, fillColor=GREY))
    return d


def build_story() -> list:
    story = []

    # ---------------- Cover ----------------
    story.append(Paragraph("AI-BDM", S_TITLE))
    story.append(Paragraph("Architecture &amp; Concepts Guide — a complete, plain-language handover.", S_SUBTITLE))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width="100%", color=NAVY, thickness=1.4))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(
        "This document is written for someone taking ownership of, reviewing, or extending this project "
        "<b>without</b> a background in web scraping or machine learning. Every technical term is explained "
        "in plain English the first time it appears, before the abbreviation or code-level detail. By the "
        "end, you should understand not just what the system does, but why each piece exists and how it "
        "actually works under the hood.", S_BODY,
    ))
    story.append(Paragraph(
        "Two companion documents exist alongside this one: <b>AI-BDM_Project_Walkthrough.pdf</b> (a short, "
        "hands-on \"try it yourself\" guide) and <b>AI-BDM_QA_Test_Plan.xlsx</b> (142 detailed test cases). "
        "This document is the deepest of the three — read it when you need to actually understand the "
        "system, not just operate it.", S_BODY,
    ))
    story.append(PageBreak())

    # ---------------- Table of contents (manual, simple) ----------------
    story.append(h1("Contents"))
    story.append(Spacer(1, 0.4 * cm))
    toc_items = [
        "1. What This System Does",
        "2. Core Concepts, Explained in Plain English",
        "3. The Technology Stack",
        "4. The End-to-End Pipeline (All 8 Stages)",
        "5. How and Where Data Is Stored",
        "6. The Two Ways to Run This System",
        "7. How It Connects to the Wider Project",
        "8. What's Not Built Yet (Known Limitations)",
        "9. Glossary — Every Term, One Place",
    ]
    story.append(bullets(toc_items, style=S_BODY))
    story.append(PageBreak())

    # ============================================================
    # 1. What this system does
    # ============================================================
    story.append(h1("1. What This System Does"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "AI-BDM is a research automation system. You give it one plain-English sentence describing the "
        "kind of business you're looking for — for example, <i>\"find 3 dental clinics in Islamabad with "
        "no online booking\"</i> — and it does the research a human analyst would otherwise do by hand: "
        "search the web, visit real company websites, read them, cross-check the businesses are real and "
        "currently operating, look at customer reviews, and hand back a clean, evidence-backed list.", S_BODY,
    ))
    story.append(Paragraph(
        "\"BDM\" stands for Business Development / Business Data Management — this is built for B2B lead "
        "generation: finding real companies that match a specific description, the way a sales or "
        "marketing team would research prospects.", S_BODY,
    ))
    story.append(Paragraph("Why this is hard to do well (and why it's not \"just scraping\")", S_H2))
    story.append(bullets([
        "Understanding a loosely-worded request the way a person would, not just matching keywords.",
        "Telling a real, currently-operating business apart from a defunct one, a directory listing, or "
        "an unrelated company that happens to share a name.",
        "Reading a website and answering a specific yes/no question about it honestly — saying "
        "\"the evidence isn't there\" instead of guessing.",
        "Doing all of this at scale, reliably, without quietly caching a network hiccup as if it were a "
        "real \"this business doesn't exist\" answer.",
    ]))

    story.append(PageBreak())

    # ============================================================
    # 2. Core concepts
    # ============================================================
    story.append(h1("2. Core Concepts, Explained in Plain English"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "Read this section once, in order — every later section assumes you understand these terms.", S_BODY,
    ))

    story.append(term_block(
        "Artificial Intelligence / LLM (Large Language Model)",
        "A computer program trained on enormous amounts of text that can read a piece of text and "
        "produce a sensible response — summarize it, answer a question about it, extract structured "
        "information from it, or decide something about it. Think of it as a very well-read assistant "
        "that can follow written instructions, rather than a search engine that just matches keywords.",
        "This project uses Groq as its LLM provider (a company that runs open large language models very "
        "fast). Two model sizes are used: a smaller/cheaper one (<font face='Courier'>gpt-oss-20b</font>) "
        "for quick, simple decisions (like \"does this look like a dental clinic?\"), and a larger one "
        "(<font face='Courier'>gpt-oss-120b</font>) for anything requiring real reasoning (like planning "
        "the whole research request, or answering a specific question about a business). A third model "
        "(<font face='Courier'>qwen3.6-27b</font>) is kept as a backup that automatically takes over if "
        "the primary model fails or times out.",
    ))
    story.append(term_block(
        "NLP (Natural Language Processing)",
        "The general field of getting computers to understand and work with human language — reading "
        "text and doing something useful with it. An LLM is one (very powerful, modern) way of doing NLP; "
        "the term NLP is broader and also covers older, simpler techniques.",
        "Every step that reads your plain-English query, or reads a business's website text and reasons "
        "about it, is an NLP task, handled here by the LLM.",
    ))
    story.append(term_block(
        "Web Scraping",
        "Automatically visiting a webpage and reading its content, the same way a browser does when a "
        "person visits it — except done by code, at scale, and without a human clicking anything.",
        "Two tiers exist here. A cheap, fast \"native\" fetch works for most ordinary websites. Some "
        "sites actively try to block automated visitors (Cloudflare challenges, CAPTCHAs) — for those, a "
        "paid third-party service called ScrapingBee is used, which can render JavaScript and disguise "
        "the request as a real browser. If a site fails twice in a row, the system stops paying for the "
        "expensive option on that site and falls back to the cheap one for its remaining pages.",
    ))
    story.append(term_block(
        "Embeddings (\"meaning as numbers\")",
        "A way of converting a piece of text into a list of numbers (a \"vector\") that captures its "
        "<i>meaning</i>, not just its exact words. Two sentences that mean similar things end up with "
        "similar number-lists, even if they don't share a single word in common. This is what lets a "
        "computer search \"by meaning\" instead of by exact text match.",
        "A small, free, locally-run model called <font face='Courier'>all-MiniLM-L6-v2</font> turns every "
        "chunk of scraped website/review text into a 384-number vector. It runs on the same machine, no "
        "internet call needed, and is fast enough to embed thousands of text chunks in seconds.",
    ))
    story.append(term_block(
        "Vector Search / Vector Database",
        "Once text is turned into number-lists (embeddings), you need a way to quickly find \"which "
        "stored piece of text has a meaning closest to this new question?\" among potentially thousands "
        "of candidates. A vector database is a storage system built specifically for that kind of "
        "similarity search, much faster than comparing every single one by hand.",
        "This project uses Chroma, a lightweight, locally-run (no external server, no cost) vector "
        "database, stored in a folder called <font face='Courier'>rag/.chroma/</font>.",
    ))
    story.append(term_block(
        "BM25 (keyword search)",
        "A decades-old, non-AI search technique that ranks text by how well its exact words match a "
        "query — the same basic idea behind classic search engines before AI-based search existed. Not "
        "\"smart\" about meaning, but fast, reliable, and good at exact terms (like a specific product "
        "name).",
        "Used <i>alongside</i> embeddings (a \"hybrid\" search) — combining exact keyword matching with "
        "meaning-based matching catches more relevant results than either alone.",
    ))
    story.append(term_block(
        "RAG (Retrieval-Augmented Generation)",
        "A pattern for using an LLM more reliably: instead of asking the LLM to just answer from what it "
        "\"remembers\" (which risks it confidently making something up — called \"hallucinating\"), you "
        "first <i>retrieve</i> the actual, real, relevant text from your own data, and then ask the LLM "
        "to <i>generate</i> its answer using only that retrieved text as evidence. The LLM is instructed "
        "to say \"not enough evidence\" rather than guess when the retrieved text doesn't actually answer "
        "the question.",
        "Every specific-question answer this system gives (e.g. \"does this business have online "
        "booking?\") is grounded this way: the relevant scraped page text is retrieved first (via the "
        "hybrid BM25 + embeddings search above), and the LLM is only allowed to answer from that real "
        "text, always citing which page it came from.",
    ))
    story.append(term_block(
        "API (Application Programming Interface)",
        "A defined way for two separate computer programs to talk to each other over a network, using "
        "structured requests and responses instead of a human clicking a website. Think of it as a menu "
        "of specific things another program is allowed to ask this one to do.",
        "This project exposes a REST API (the most common API style, built on standard web requests) so "
        "other systems — like a Laravel backend — can trigger a research run and read results "
        "programmatically instead of running the command-line tool by hand.",
    ))
    story.append(term_block(
        "Synchronous vs. Asynchronous (\"async\"), and Background Jobs",
        "Synchronous means \"the caller waits until it's completely done\" — like standing at a counter "
        "until your order is ready. Asynchronous means \"the work starts, and the caller can check back "
        "later or get notified\" — like taking a buzzer at a restaurant and being told when your table's "
        "ready. A background job is work that runs this second way, without blocking anything else.",
        "A full research request can take several minutes. This project offers both styles: a simple "
        "\"wait for it\" endpoint for quick manual use, and a \"submit and poll for progress\" job system "
        "for real integrations where nothing should sit there frozen for minutes at a time.",
    ))
    story.append(term_block(
        "Idempotency (\"safe to repeat\")",
        "A fancy word for a simple, important idea: doing the same operation twice should have the exact "
        "same effect as doing it once — not create a duplicate. This matters because networks are "
        "unreliable; if a caller doesn't get a response and retries \"just in case,\" idempotency "
        "guarantees that retry doesn't accidentally run the (possibly expensive) operation a second time.",
        "A caller can attach a unique \"Idempotency-Key\" to a request; if the same key is sent again, "
        "the system returns the exact original result instead of doing the work over.",
    ))

    story.append(PageBreak())

    # ============================================================
    # 3. Tech stack
    # ============================================================
    story.append(h1("3. The Technology Stack"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph("Everything the system is built from, and in plain terms, why each piece is there.", S_BODY))

    stack_rows = [
        ("Language", "Python 3.12", "The programming language the whole system is written in — chosen for its strong ecosystem of both web-scraping and machine-learning libraries."),
        ("Web framework", "FastAPI + Uvicorn", "FastAPI defines the API's request/response shapes and validates them automatically; Uvicorn is the actual server process that keeps the API running and listening for requests."),
        ("Data validation", "Pydantic", "Defines the exact shape every request and response must have (which fields, what type, what's required) and automatically rejects anything that doesn't match — catches bad input before it reaches the real logic."),
        ("AI / language understanding", "Groq (hosted LLMs)", "Runs the large language models that understand requests, plan research, and answer specific questions from evidence. See the LLM concept above."),
        ("Web search", "Serper.dev", "A paid API that provides Google Search and Google Maps/Places results programmatically — the system's way of \"searching Google\" and \"looking up a business on Maps\" without a browser."),
        ("Scraping (standard)", "Python's requests library", "Fetches ordinary, non-protected web pages directly and cheaply."),
        ("Scraping (protected sites)", "ScrapingBee", "A paid service that renders JavaScript and disguises requests as a real browser, used only when the cheap method is blocked."),
        ("Review harvesting", "Apify (managed scraping actors)", "A platform that runs pre-built \"scraping robots\" for specific sites (Google Maps reviews, Reddit) that are otherwise very hard to scrape directly."),
        ("Technology fingerprinting", "Wappalyzer (a vendored, patched copy)", "Identifies what software (CMS, booking system, analytics, etc.) a business's website is built on, by recognizing patterns in its HTML/cookies/headers."),
        ("Meaning-based search", "sentence-transformers (all-MiniLM-L6-v2)", "The small local model that turns text into \"meaning numbers\" (embeddings) — see the Embeddings concept above."),
        ("Vector storage/search", "Chroma", "Stores those embeddings and finds the closest matches quickly — see the Vector Search concept above."),
        ("Keyword search", "rank-bm25", "The classic keyword-ranking algorithm used alongside embeddings for hybrid search."),
        ("Local data storage", "CSV files + a plain folder structure", "Per-business results and per-page crawl records are stored as CSV spreadsheets; each business's raw scraped text and reviews live in its own folder."),
        ("Optional remote storage", "Cloudflare R2 (via boto3)", "An optional, S3-compatible cloud storage service that can mirror the local per-business folders off-machine, for backup/durability."),
        ("Testing", "pytest", "The framework that runs this project's 115+ automated tests, checking real behavior without needing real API keys for most of them."),
        ("Packaging / environment", "pip + a virtual environment (venv)", "Standard Python tooling for installing exact, pinned dependency versions in an isolated environment, so the project behaves the same on any machine."),
        ("Containerization", "Docker", "Packages the whole service (code + Python + system dependencies) into a single, portable unit that runs identically on any machine with Docker installed — used for the production/integration deployment."),
    ]
    story.append(two_col_table(
        [(r[1], f"<b>{r[0]}</b><br/>{r[2]}") for r in stack_rows],
        col_widths=[CONTENT_WIDTH * 0.26, CONTENT_WIDTH * 0.74],
    ))

    story.append(PageBreak())

    # ============================================================
    # 4. The pipeline
    # ============================================================
    story.append(h1("4. The End-to-End Pipeline (All 8 Stages)"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "One research request flows through up to 8 stages, always in this order. Stages 4, 5, 6, and 7 "
        "only run when the request actually needs them — a plain \"find 3 dental clinics in Islamabad\" "
        "with no extra condition skips straight from Stage 2 to Stage 8.", S_BODY,
    ))
    story.append(Spacer(1, 0.3 * cm))
    story.append(pipeline_diagram())
    story.append(Spacer(1, 0.5 * cm))

    def stage_section(num, title, plain_explanation, how_it_works, tech_used, output):
        flow = [
            Paragraph(f"Stage {num} — {title}", S_H2),
            Paragraph(plain_explanation, S_BODY),
            Paragraph("<b>How it actually works:</b>", S_BODY_SMALL),
            bullets(how_it_works, style=S_BODY_SMALL),
            Paragraph(f"<b>Built with:</b> {tech_used}", S_BODY_SMALL),
            Paragraph(f"<b>What comes out:</b> {output}", S_BODY_SMALL),
            Spacer(1, 0.3 * cm),
        ]
        return flow

    story.extend(stage_section(
        1, "Understand the Request",
        "Turns your one plain-English sentence into a structured plan — the same way a human research "
        "assistant would first make sure they understood exactly what you're asking for before starting.",
        [
            "The LLM reads the sentence and extracts: what industry, what location, how many results "
            "you want, whether there's an extra condition to check (\"...with no online booking\"), and "
            "whether you named one specific company instead of asking for a general search.",
            "A safety check (\"moderation\") runs first, before anything else — if the request violates "
            "usage policy, the system stops immediately, before spending any money on search/scraping.",
            "If the request is too vague to act on confidently, the system stops and asks a clarifying "
            "question instead of guessing — up to 3 rounds before it proceeds with its best interpretation "
            "regardless.",
        ],
        "The Groq LLM (the larger model, since this needs real judgment, not just simple classification).",
        "A structured plan (industry, location, count, the extra condition if any) that every later stage reads.",
    ))
    story.extend(stage_section(
        2, "Search &amp; Read Real Websites",
        "The actual research: finding real candidate businesses and visiting their websites, the way a "
        "person would open browser tabs and read pages — except automated, and checking each one against "
        "the plan from Stage 1.",
        [
            "Searches the web (via Serper.dev, which wraps Google Search) for candidate businesses "
            "matching the industry and location.",
            "Visits each candidate's website and reads its content (web scraping — see the concept "
            "section). Cheap fetch first; the paid anti-bot-resistant option only if that's blocked.",
            "A fast relevance check filters out obvious non-matches (wrong industry, wrong location, "
            "clearly not a real business) before spending more effort on a candidate.",
            "If the request specified a target number of results (\"give me 20...\"), the system keeps "
            "searching — deeper search pages, related sites — until it genuinely finds that many real "
            "matches or exhausts reasonable options, rather than padding the count with weak matches.",
            "Also harvests customer reviews for each qualified business where available (Google reviews, "
            "Reddit mentions).",
        ],
        "Serper.dev (search), the scraping tools above, Apify (reviews).",
        "A committed record per real, qualifying business: its website text, contact details, and any reviews found.",
    ))
    story.extend(stage_section(
        3, "Pick the Right Pages to Focus On",
        "A business's website might have 20+ pages. Before trying to answer a specific question about "
        "it, the system figures out which few pages are actually likely to contain the answer, instead "
        "of re-reading everything every time.",
        [
            "Uses the hybrid keyword + meaning-based search (BM25 + embeddings) described in the concepts "
            "section to rank the business's own already-scraped pages by relevance to the question.",
            "Only falls back to asking the LLM to guess which page is relevant in the rare case the "
            "automatic search finds nothing useful — the common case needs no extra AI call at all.",
        ],
        "The hybrid search described above (BM25 + embeddings), with an LLM fallback.",
        "A short list of the most relevant pages for the next stage to actually read in depth.",
    ))
    story.extend(stage_section(
        4, "Answer the Specific Question",
        "Only runs when the request had an extra condition to check (like \"...with no online booking\"). "
        "Reads the relevant page text and gives a direct, honest answer — grounded in what was actually "
        "found, using the RAG pattern described in the concepts section.",
        [
            "The LLM is given the actual text from the pages Stage 3 picked, and asked the specific "
            "question, with strict instructions to answer only from that text.",
            "If the evidence genuinely isn't there, the answer says so explicitly (\"not enough "
            "evidence\") instead of guessing — this is the core anti-hallucination safeguard.",
            "Every answer cites exactly which page(s) it was grounded in.",
        ],
        "The Groq LLM (larger model), using the RAG pattern.",
        "A direct answer to the specific question, with a confidence level and the exact source pages cited.",
    ))
    story.extend(stage_section(
        5, "Identify the Website's Technology",
        "Only runs when the request cares about a business's technology (e.g. \"...using WordPress\"). "
        "Figures out what platform, booking system, or marketing tools a website actually runs on.",
        [
            "Examines the page's HTML, cookies, response headers, and DNS records for known technology "
            "\"fingerprints\" — recognizable patterns that reveal what software built the site.",
            "Four separate detection methods run independently and are merged, so one method failing "
            "doesn't blank out the whole result.",
        ],
        "Wappalyzer (a vendored, bug-patched copy — see Section 8 for why it's vendored, not a normal install).",
        "A profile of detected technologies (CMS, booking tools, analytics, etc.) for that business.",
    ))
    story.extend(stage_section(
        6, "Build a Searchable Memory of Everything Found",
        "Organizes everything read in Stage 2 (website text and reviews) into the meaning-based search "
        "index described in the concepts section, so Stage 3/4 can quickly find the right evidence "
        "instead of re-reading everything from scratch every time.",
        [
            "Breaks scraped text into small chunks, and turns each chunk into an embedding (a \"meaning "
            "number-list\") using the local embedding model.",
            "Stores those embeddings in the local vector database (Chroma), keeping website content and "
            "review content in two separate, independently-ranked groups, so one doesn't drown out the other.",
        ],
        "sentence-transformers (embedding model) + Chroma (vector database).",
        "A searchable local index of everything found for that business, ready for fast meaning-based lookup.",
    ))
    story.extend(stage_section(
        7, "Cross-Check Against Google Maps",
        "Confirms each business is real and currently operating by matching it against its actual Google "
        "Maps listing — catching cases where the website is stale, the business closed, or the match is "
        "simply wrong.",
        [
            "Looks the business up via Serper.dev's Google Places integration.",
            "Tries to confirm the match first by matching the physical address, then by phone number if "
            "the address alone isn't distinctive enough, and only falls back to matching by name alone "
            "when there's exactly one unambiguous candidate — each tier is recorded so you know how "
            "confident the match is.",
        ],
        "Serper.dev (Google Places/Maps).",
        "Confirmed rating, review count, and category from Google Maps, plus a confidence label for the match.",
    ))
    story.extend(stage_section(
        8, "Final Quality Check",
        "Before handing anything back, the system re-checks its own work — the same instinct a careful "
        "analyst has to double-check their findings before submitting them.",
        [
            "Checks that phone numbers, emails, addresses, and ratings are validly formatted.",
            "Checks that the business's name is consistent across every source (its own site, Google "
            "Maps, review platforms) — catching a case where the data actually belongs to a different, "
            "similarly-named business.",
            "Checks that harvested reviews genuinely reference this specific business, not something "
            "unrelated that happened to get picked up.",
            "Anything questionable is flagged in a report, never silently hidden or silently dropped.",
        ],
        "Rule-based checks — no AI involved in this stage, deliberately, since consistency-checking doesn't need judgment, just comparison.",
        "A quality report listing anything worth a second look, alongside the final results.",
    ))

    story.append(PageBreak())

    # ============================================================
    # 5. Data storage
    # ============================================================
    story.append(h1("5. How and Where Data Is Stored"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "No traditional database (like PostgreSQL or MySQL) is used inside this Python service itself — "
        "everything is stored as plain files, which keeps the system simple and makes every result "
        "directly inspectable without needing a database client.", S_BODY,
    ))
    story.append(two_col_table([
        ("<b>crawl_index.csv</b>", "One row per web page crawled — the most detailed record, spreadsheet-style."),
        ("<b>leads_clean.csv</b>", "One row per business (rolled up from all its pages) — the main results file."),
        ("<b>leads_with_maps.csv</b>", "The same rollup, with Google Maps confirmation data added."),
        ("<b>accuracy_report.txt</b>", "The Stage 8 quality-check output — anything flagged as questionable."),
        ("<b>storage/&lt;business-domain&gt;/</b>", "One folder per business, holding the actual scraped page text, harvested reviews, and technology profile — the raw evidence behind every result."),
        ("<b>rag/.chroma/</b>", "The local vector database from Stage 6 — can always be safely deleted and rebuilt from the storage/ folders."),
    ]))
    story.append(Paragraph(
        "Optionally, everything in storage/ can also be mirrored to Cloudflare R2 (cloud object storage) "
        "for an off-machine backup copy — this is disabled unless specifically configured.", S_BODY,
    ))

    story.append(PageBreak())

    # ============================================================
    # 6. Two ways to run it
    # ============================================================
    story.append(h1("6. The Two Ways to Run This System"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph("<b>1. Command line (for manual/direct use)</b>", S_H3))
    story.append(code('python main.py --query "find 3 dental clinics in Islamabad with no online booking"'))
    story.append(Paragraph(
        "Runs the full 8-stage pipeline directly in a terminal and writes results to the CSV files "
        "described above. This is the simplest way to actually see the system work.", S_BODY,
    ))
    story.append(Paragraph("<b>2. REST API (for another program to call it)</b>", S_H3))
    story.append(code("python -m uvicorn api:app --port 8000"))
    story.append(Paragraph(
        "Starts a web server exposing the same functionality over HTTP, so another system (like the "
        "Laravel backend) can trigger a research run and read results programmatically. Two styles exist "
        "within the API:", S_BODY,
    ))
    story.append(bullets([
        "<b>A simple, synchronous endpoint</b> (<font face='Courier'>POST /pipeline/run</font>) — the "
        "caller sends a request and waits (potentially several minutes) for the complete result. Simple, "
        "but ties up the caller the whole time.",
        "<b>An asynchronous job system</b> (<font face='Courier'>POST /api/v1/jobs</font> and related "
        "endpoints) — the caller submits a request, gets an ID back immediately, and can check progress "
        "or cancel it, without being frozen waiting. Built for real integrations where nothing should "
        "sit blocked for minutes. See the async/background job concept in Section 2.",
    ]))

    story.append(PageBreak())

    # ============================================================
    # 7. Wider system
    # ============================================================
    story.append(h1("7. How It Connects to the Wider Project"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "This Python service is one piece of a larger system with three separate parts, each with a "
        "clear, deliberately-enforced boundary:", S_BODY,
    ))
    story.append(two_col_table([
        ("<b>Next.js frontend</b>", "The actual website/app a user sees and clicks around in. It never talks to this Python service directly."),
        ("<b>Laravel backend</b>", "The system of record — handles user accounts, organizations, approvals, and is the <i>only</i> part allowed to send outbound email. It calls this Python service to run research, then imports the results into its own database."),
        ("<b>This Python service</b>", "Does the actual research work described in this document, and nothing else — it never sends email, never decides who's authorized to do what, and is never called directly by the frontend."),
    ]))
    story.append(Paragraph(
        "This separation is deliberate: research (which can be slow, occasionally wrong, and depends on "
        "unpredictable external websites) is kept isolated from anything that touches real user accounts "
        "or sends real outbound communication.", S_BODY,
    ))
    story.append(Paragraph(
        "All three parts are packaged with Docker (a way of bundling an application with everything it "
        "needs so it runs identically on any machine) so a developer can start the entire backend stack "
        "with one command, without installing PHP, Python, or a database directly on their own machine.", S_BODY,
    ))

    story.append(PageBreak())

    # ============================================================
    # 8. Known limitations
    # ============================================================
    story.append(h1("8. What's Not Built Yet (Known Limitations)"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "Stated honestly and specifically, rather than left for someone to discover the hard way:", S_BODY,
    ))
    story.append(bullets([
        "<b>No non-AI fallback exists.</b> Every request genuinely requires the LLM (Groq) to be "
        "reachable — there's no simpler rule-based backup path for understanding a request.",
        "<b>Some review platforms actively block automated access</b> (Yelp, Trustpilot) even with the "
        "paid anti-bot tool — a production fix would need each platform's own official paid API.",
        "<b>A vendored, hand-patched copy of Wappalyzer is used</b> instead of a normal install, because "
        "the publicly published version of that package was found to silently serve different, "
        "incompatible code over time under the same version number — a real reproducibility risk that's "
        "been permanently worked around by keeping a known-good copy directly in this project.",
        "<b>The website's robots.txt \"do not crawl\" rules are not currently enforced</b> — they're "
        "read only to find a site's page listing, not to respect crawling restrictions. A real, "
        "acknowledged gap, not a design choice.",
        "<b>No content is cryptographically verified</b> (no checksums/hashes) on any stored file yet.",
        "<b>The asynchronous job system keeps its state only in memory</b> — if the service restarts, "
        "any in-progress or recently-finished job information is lost (though anything already saved to "
        "the storage/ folders is safe).",
        "<b>\"Resuming\" a failed job restarts the search from the beginning</b>, not from the exact "
        "point it stopped — it just avoids redoing work for businesses already found and saved, rather "
        "than picking up mid-search.",
    ]))
    story.append(Paragraph(
        "The full, exhaustive list — including gaps specific to individual stages — lives in "
        "docs/KNOWN_LIMITATIONS.md and docs/backend-handoff/KNOWN_LIMITATIONS.md in the project repository.", S_CAPTION,
    ))

    story.append(PageBreak())

    # ============================================================
    # 9. Glossary
    # ============================================================
    story.append(h1("9. Glossary — Every Term, One Place"))
    story.append(Spacer(1, 0.4 * cm))
    glossary = [
        ("AI / LLM", "A large language model — a program that reads and generates text intelligently. See Section 2."),
        ("API", "A defined way for two programs to talk to each other over a network. See Section 2."),
        ("Async / Background job", "Work that runs without freezing the caller — submit now, check later. See Section 2."),
        ("BM25", "A classic, non-AI keyword search ranking algorithm. See Section 2."),
        ("Embedding", "Text converted into a list of numbers that captures its meaning. See Section 2."),
        ("Hallucination", "When an AI model confidently states something false or made-up. RAG (below) is the main defense against this."),
        ("Idempotency", "Doing an operation twice has the same effect as doing it once — \"safe to repeat.\" See Section 2."),
        ("NLP", "Natural Language Processing — the broad field of computers working with human language. See Section 2."),
        ("Pipeline", "The fixed sequence of stages a request goes through, described in Section 4."),
        ("RAG", "Retrieval-Augmented Generation — look up real facts first, then answer using only those facts. See Section 2."),
        ("REST API", "The most common style of web API, built on standard HTTP requests (GET, POST, etc.)."),
        ("Scraping", "Automatically reading a webpage's content, the way a browser would for a person. See Section 2."),
        ("Synchronous", "The caller waits until the work is completely finished before getting a response."),
        ("Vector database", "Storage built specifically for fast \"find the closest meaning\" search over embeddings. See Section 2."),
    ]
    story.append(two_col_table(glossary, col_widths=[CONTENT_WIDTH * 0.22, CONTENT_WIDTH * 0.78],
                                header=["Term", "Meaning"]))

    return story


def main() -> None:
    out_path = "qa/AI-BDM_Architecture_And_Concepts_Guide.pdf"
    doc = SimpleDocTemplate(
        out_path, pagesize=PAGE_SIZE,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
        title="AI-BDM Architecture & Concepts Guide", author="AI-BDM Python Team",
    )
    story = build_story()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
