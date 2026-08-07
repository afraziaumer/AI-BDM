"""Generates qa/AI-BDM_Project_Walkthrough.pdf -- a plain-language
explanation of what AI-BDM does and a complete, copy-pasteable set of
steps to run it and see it work, for someone (e.g. a QA tester) who
needs to understand the project before testing it. Separate from
qa/AI-BDM_QA_Test_Plan.xlsx, which is the detailed test-case catalogue.
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
LIGHT_BLUE = colors.HexColor("#DCE6F1")
GREY = colors.HexColor("#595959")
CODE_BG = colors.HexColor("#F2F2F2")

styles = getSampleStyleSheet()
S_TITLE = ParagraphStyle("Title2", parent=styles["Title"], textColor=NAVY, fontSize=28, fontName="Helvetica-Bold")
S_SUBTITLE = ParagraphStyle("Subtitle2", parent=styles["Normal"], textColor=GREY, fontSize=13, italic=True, spaceAfter=4)
S_H1 = ParagraphStyle("H1", parent=styles["Heading1"], textColor=colors.white, fontSize=16,
                       fontName="Helvetica-Bold", backColor=NAVY, leftIndent=8, spaceBefore=0,
                       spaceAfter=0, borderPadding=(8, 8, 8, 8))
S_H2 = ParagraphStyle("H2", parent=styles["Heading2"], textColor=NAVY, fontSize=13, spaceBefore=14, spaceAfter=6)
S_BODY = ParagraphStyle("Body2", parent=styles["Normal"], fontSize=10.5, leading=15, spaceAfter=8)
S_BODY_BOLD = ParagraphStyle("BodyBold", parent=S_BODY, fontName="Helvetica-Bold")
S_LIST = ParagraphStyle("List2", parent=S_BODY, spaceAfter=4)
S_CODE = ParagraphStyle("Code", parent=styles["Normal"], fontName="Courier", fontSize=9.5, leading=13,
                         backColor=CODE_BG, borderPadding=(6, 8, 6, 8), leftIndent=4)
S_CAPTION = ParagraphStyle("Caption", parent=styles["Normal"], fontSize=9, textColor=GREY, italic=True, spaceAfter=10)
S_STAGE_NUM = ParagraphStyle("StageNum", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=20,
                              textColor=colors.white, alignment=1)
S_STAGE_TITLE = ParagraphStyle("StageTitle", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=11.5, textColor=NAVY)
S_STAGE_BODY = ParagraphStyle("StageBody", parent=S_BODY, fontSize=10, spaceAfter=2)

PAGE_SIZE = A4
MARGIN = 2.0 * cm
CONTENT_WIDTH = PAGE_SIZE[0] - 2 * MARGIN


def h1(text: str):
    tbl = Table([[Paragraph(text, S_H1)]], colWidths=[CONTENT_WIDTH])
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return tbl


def code(text: str):
    return Paragraph(text.replace("\n", "<br/>"), S_CODE)


def bullets(items: list[str]):
    return ListFlowable(
        [ListItem(Paragraph(i, S_LIST), leftIndent=14) for i in items],
        bulletType="bullet", start="•", leftIndent=14,
    )


def numbered(items: list[str]):
    return ListFlowable(
        [ListItem(Paragraph(i, S_LIST)) for i in items],
        bulletType="1", leftIndent=18,
    )


def stage_row(num: str, plain_title: str, technical_name: str, explanation: str):
    left = Table([[Paragraph(num, S_STAGE_NUM)]], colWidths=[1.3 * cm], rowHeights=[1.3 * cm])
    left.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    right = [
        Paragraph(f"{plain_title} <font color='#808080' size='9'>({technical_name})</font>", S_STAGE_TITLE),
        Paragraph(explanation, S_STAGE_BODY),
    ]
    tbl = Table([[left, right]], colWidths=[1.6 * cm, CONTENT_WIDTH - 1.6 * cm])
    tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    return tbl


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(GREY)
    canvas.drawString(MARGIN, 1.2 * cm, "AI-BDM — Project Walkthrough")
    canvas.drawRightString(PAGE_SIZE[0] - MARGIN, 1.2 * cm, f"Page {doc.page}")
    canvas.restoreState()


def build_story() -> list:
    story = []

    # --- Cover / intro ---
    story.append(Paragraph("AI-BDM", S_TITLE))
    story.append(Paragraph("What it does, how it works, and how to try it yourself — in plain language.", S_SUBTITLE))
    story.append(Spacer(1, 0.4 * cm))
    story.append(HRFlowable(width="100%", color=NAVY, thickness=1.4))
    story.append(Spacer(1, 0.5 * cm))

    story.append(Paragraph(
        "This document explains the AI-BDM project without assuming you already know how it "
        "works — what it does, why each part exists, and exact copy-and-paste steps to run it "
        "and see real results. If you're testing this project, read this first; the detailed "
        "test-case spreadsheet (AI-BDM_QA_Test_Plan.xlsx) is the companion document for the "
        "actual testing work.", S_BODY,
    ))

    story.append(PageBreak())

    # --- Section: What is it ---
    story.append(h1("1. What Is AI-BDM, In One Paragraph?"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "Imagine you needed to find <b>3 dental clinics in Islamabad that don't currently let "
        "patients book appointments online</b>. Doing that by hand means: searching Google, "
        "opening a dozen websites, reading each one to check if they have online booking, "
        "checking if they're even a real, currently-operating business, and writing it all "
        "down. AI-BDM does all of that automatically. You type one plain-English sentence, "
        "and it hands you back a list of real businesses that actually match — with the "
        "evidence for every claim, not just a guess.", S_BODY,
    ))
    story.append(Paragraph(
        "It's built for B2B lead generation — finding real companies that match a very "
        "specific description, the kind of research a sales or marketing team would otherwise "
        "do by hand.", S_BODY,
    ))

    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("What you put in, and what you get back", S_H2))
    tbl = Table(
        [
            [Paragraph("<b>You type:</b>", S_BODY), Paragraph('"find 3 dental clinics in Islamabad with no online booking"', S_CODE)],
            [Paragraph("<b>You get back:</b>", S_BODY), Paragraph(
                "A list of real, currently-operating dental clinics in Islamabad, each with "
                "its name, website, contact details, a confirmed match against Google Maps, "
                "and a plain answer to the actual question asked (\"does this business offer "
                "online booking? here's why we think so\") — grounded in the real text found "
                "on their website, never a guess.", S_BODY)],
        ],
        colWidths=[3.2 * cm, CONTENT_WIDTH - 3.2 * cm],
    )
    tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    story.append(tbl)

    story.append(PageBreak())

    # --- Section: How it works ---
    story.append(h1("2. How It Works, Step By Step"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "Behind the scenes, one request goes through 8 steps, in order. Each step has a "
        "plain-English name here and its real technical name in the code, in parentheses, so "
        "you can connect the two when reading the deeper documentation later.", S_BODY,
    ))
    story.append(Spacer(1, 0.3 * cm))

    stages = [
        ("1", "Understand the request", "Query Planning",
         "Reads your one sentence and figures out the real, structured meaning behind it: "
         "what industry, what location, how many results you want, and whether there's a "
         "specific extra question to answer (like \"...with no online booking\"). If your "
         "request is too vague to act on, it stops here and asks you a clarifying question "
         "instead of guessing."),
        ("2", "Search and read real websites", "Discovery & Scraping",
         "Searches the web for real candidate businesses, then actually visits each one's "
         "website and reads the page content — the same way a person would open a browser tab "
         "and read the page. Businesses that clearly aren't a real match are filtered out here."),
        ("3", "Pick the right pages to focus on", "Route/Page Selection",
         "A business's website might have 20 pages. This step figures out which few pages "
         "actually matter for answering your question, instead of re-reading everything every "
         "time."),
        ("4", "Answer your specific question", "Final Reasoning",
         "Only runs when your request had an extra condition (like \"...with no online "
         "booking\"). Reads the relevant page text and gives a direct answer, always backed by "
         "the actual text it read — if the evidence isn't there, it says so honestly instead "
         "of making something up."),
        ("5", "Identify their website's technology", "Tech-Stack Detection",
         "Figures out what software a business's website runs on — what platform it's built "
         "with, what booking/CRM/marketing tools it uses. Useful when the request is about "
         "technology (\"...using WordPress\")."),
        ("6", "Build a searchable memory of everything found", "Evidence Index",
         "Organizes everything read in step 2 (website text and customer reviews) into a "
         "searchable index, so later steps can quickly find the exact right piece of "
         "evidence instead of re-reading everything from scratch."),
        ("7", "Double-check against Google Maps", "Maps Enrichment",
         "Cross-checks each business against its real Google Maps listing — confirming its "
         "address, phone number, and rating actually match, so you're not looking at a wrong "
         "or outdated business."),
        ("8", "Final quality check", "Accuracy Audit",
         "Before handing anything back, it re-checks its own work: are phone numbers/emails "
         "formatted correctly, does the business name actually match across every source, do "
         "the reviews it found genuinely mention this business? Anything questionable gets "
         "flagged, not hidden."),
    ]
    for num, plain, tech, expl in stages:
        story.append(stage_row(num, plain, tech, expl))

    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(
        "Steps 4, 5, 6, and 7 only run when your request actually needs them — a simple "
        "\"find 3 dental clinics in Islamabad\" with no extra condition skips straight from "
        "step 2 to step 8.", S_CAPTION,
    ))

    story.append(PageBreak())

    # --- Section: Try it yourself ---
    story.append(h1("3. Try It Yourself — Complete Steps"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "Everything below is copy-and-paste ready. Run these from a terminal inside the "
        "project folder.", S_BODY,
    ))

    story.append(Paragraph("Step 1 — Install Python 3.12", S_H2))
    story.append(Paragraph(
        "If you don't already have it, download and install Python 3.12 from python.org. "
        "This project is built and tested specifically on 3.12.", S_BODY,
    ))

    story.append(Paragraph("Step 2 — Create an isolated environment for this project", S_H2))
    story.append(Paragraph(
        "This keeps AI-BDM's dependencies separate from anything else on your machine. "
        "Windows PowerShell:", S_BODY,
    ))
    story.append(code("py -3.12 -m venv .venv\n.\\.venv\\Scripts\\Activate.ps1\npython -m pip install --upgrade pip"))
    story.append(Paragraph("Mac/Linux:", S_BODY))
    story.append(code("python3.12 -m venv .venv\nsource .venv/bin/activate\npython -m pip install --upgrade pip"))

    story.append(Paragraph("Step 3 — Install the project's dependencies", S_H2))
    story.append(code("pip install -r requirements.txt"))
    story.append(Paragraph(
        "This installs every package the project needs. It can take a few minutes the first "
        "time. Afterward, run this one cleanup command (safe, explained in README.md):", S_BODY,
    ))
    story.append(code("pip uninstall -y pyarrow datasets"))

    story.append(Paragraph("Step 4 — Get two free API keys", S_H2))
    story.append(Paragraph(
        "AI-BDM needs two outside services to actually work: one that understands language "
        "(Groq) and one that searches the web (Serper.dev). Both have a free tier, enough to "
        "try this out.", S_BODY,
    ))
    story.append(bullets([
        "Groq — sign up at console.groq.com and create an API key. This is what lets the "
        "program understand your sentence and reason about what it reads.",
        "Serper.dev — sign up at serper.dev and create an API key. This is what lets the "
        "program search Google and look things up on Google Maps.",
    ]))

    story.append(Paragraph("Step 5 — Add your keys to the project", S_H2))
    story.append(numbered([
        "In the project folder, find the file named <font face='Courier'>.env.example</font>.",
        "Make a copy of it named exactly <font face='Courier'>.env</font> (no extension after "
        "that).",
        "Open .env in a text editor and paste your Groq key after "
        "<font face='Courier'>groq_llm_apikey1=</font> and your Serper key after "
        "<font face='Courier'>serper=</font>.",
        "Save the file. Never share this file or commit it anywhere — it holds your real keys.",
    ]))

    story.append(Paragraph("Step 6 — Run your first real query", S_H2))
    story.append(Paragraph("From the project folder, with your environment activated:", S_BODY))
    story.append(code('python main.py --query "find 3 dental clinics in Islamabad with no online booking"'))

    story.append(Paragraph("What you'll see happen", S_H2))
    story.append(numbered([
        "The program prints what it understood your request to mean (its \"plan\").",
        "It starts searching and shows each candidate business it finds and checks.",
        "For each real match, it shows what it scraped, whether reviews were found, and (if "
        "relevant) the direct answer to your extra question.",
        "It cross-checks against Google Maps.",
        "It runs its own final quality check.",
        "It prints a summary: how many businesses it found, how many genuinely qualified, "
        "and where the full results were saved.",
    ]))
    story.append(Paragraph(
        "A simple 3-business query typically finishes in a few minutes — most of that time is "
        "the program actually waiting on real websites and APIs to respond, the same as if a "
        "person were doing this by hand, just faster and more thorough.", S_CAPTION,
    ))

    story.append(Paragraph("Step 7 — Where to find the results afterward", S_H2))
    tbl2 = Table(
        [
            [Paragraph("<b>leads_clean.csv</b>", S_BODY), Paragraph("One row per business found — the main results file. Open it in Excel.", S_BODY)],
            [Paragraph("<b>leads_with_maps.csv</b>", S_BODY), Paragraph("The same list, with Google Maps details added (rating, review count, confirmed match).", S_BODY)],
            [Paragraph("<b>accuracy_report.txt</b>", S_BODY), Paragraph("The program's own self-check — anything it flagged as questionable.", S_BODY)],
            [Paragraph("<b>storage/</b> folder", S_BODY), Paragraph("One folder per business, with the actual page text and reviews it read — the raw evidence behind every result.", S_BODY)],
        ],
        colWidths=[4.2 * cm, CONTENT_WIDTH - 4.2 * cm],
    )
    tbl2.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story.append(tbl2)

    story.append(PageBreak())

    # --- Section: Optional API demo ---
    story.append(h1("4. Optional: Trying the Web API Version"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "Everything above runs directly from a terminal. There's also a web version (an API) "
        "that other programs can call over the network — useful for understanding how a real "
        "backend system would talk to AI-BDM. This part covers only the two most basic "
        "requests; the QA Test Plan spreadsheet has the complete set.", S_BODY,
    ))

    story.append(Paragraph("Step 1 — Start the server", S_H2))
    story.append(code("python -m uvicorn api:app --reload --port 8000"))

    story.append(Paragraph("Step 2 — Open the interactive docs in your browser", S_H2))
    story.append(Paragraph(
        "Go to <font face='Courier'>http://127.0.0.1:8000/docs</font> in any browser. This "
        "page lets you try every request by clicking buttons, no coding needed — a good way "
        "to explore without memorizing commands.", S_BODY,
    ))

    story.append(Paragraph("Step 3 — A simple check that it's alive", S_H2))
    story.append(code("curl http://127.0.0.1:8000/api/v1/health"))
    story.append(Paragraph(
        "Should return a short message confirming the service is running and which of your "
        "API keys it detected.", S_BODY,
    ))

    story.append(Spacer(1, 0.5 * cm))
    story.append(h1("5. Where To Go Next"))
    story.append(Spacer(1, 0.4 * cm))
    story.append(bullets([
        "<b>AI-BDM_QA_Test_Plan.xlsx</b> — the full, detailed list of 142 test cases covering "
        "every part of the project. Use this once you're comfortable with what the project "
        "does and want to start structured testing.",
        "<b>README.md</b> (in the project folder) — the technical setup reference, if "
        "anything above doesn't match what you see.",
        "<b>docs/</b> folder — deeper technical documentation for every part of the system, "
        "for when a test case raises a question this document doesn't answer.",
    ]))

    return story


def main() -> None:
    out_path = "qa/AI-BDM_Project_Walkthrough.pdf"
    doc = SimpleDocTemplate(
        out_path, pagesize=PAGE_SIZE,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
        title="AI-BDM Project Walkthrough", author="AI-BDM Python Team",
    )
    story = build_story()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
