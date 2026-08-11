"""Adds a 'Missing Coverage (Proposed)' sheet to a copy of the professional
QA workbook, listing test cases identified as NOT covered by the original
164-case suite. These are proposed only -- Actual Result/Status are left as
Not Run, nothing here has been executed.

Run: python qa/professional_qa/add_gap_sheet.py
"""
from __future__ import annotations

import os

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER = [
    "TC ID", "Stage", "Module", "Test Scenario", "Preconditions",
    "Test Data / Input", "Test Steps", "Expected Result", "Actual Result",
    "Status", "Priority", "Test Type", "Defect ID", "Remarks",
]

ROWS = [
    # --- Security ---
    dict(id="GAP-SEC-001", stage="Security", module="CSV/formula injection",
         scenario="Malicious field value scraped from a business website",
         precond="Run storage export",
         data="A scraped business name/field begins with '=', '+', '-', or '@' (e.g. '=cmd|...')",
         steps="Discover/qualify a business whose scraped name or field starts with a formula-trigger character; export crawl_index.csv; open in Excel/Sheets.",
         expected="Exported CSV never lets a field value execute as a formula when opened in a spreadsheet application (leading formula-trigger characters are neutralized/escaped).",
         priority="P1", ttype="Security"),
    dict(id="GAP-SEC-002", stage="Security", module="SSRF via discovered/scraped URLs",
         scenario="Search result or on-page link points at an internal/private address",
         precond="Run discovery/scraping",
         data="A malicious page links to http://169.254.169.254/ (cloud metadata) or a localhost/private-range address",
         steps="Have discovery or the internal-link crawler encounter such a URL and attempt to fetch it.",
         expected="The fetch layer refuses to request private/internal/link-local IP ranges; the business is skipped or the offending URL is dropped, not fetched.",
         priority="P0", ttype="Security"),
    dict(id="GAP-SEC-003", stage="Security", module="ReDoS via LLM-generated phone_regex",
         scenario="Planner-generated phone_regex is adversarially pathological",
         precond="Run query planning + contact extraction",
         data="A phone_regex value with catastrophic-backtracking potential despite being short (e.g. \"(a+)+$\")",
         steps="Force or simulate the planner returning such a regex, then run contact extraction against a long block of scraped text.",
         expected="Regex execution completes in bounded time (or the compiled regex is rejected/timed out) -- extraction never hangs the pipeline. The existing MAX_PHONE_REGEX_LEN length cap alone is not sufficient evidence; this must be probed directly.",
         priority="P1", ttype="Security"),
    dict(id="GAP-SEC-004", stage="Security", module="API key auth enforcement",
         scenario="Request made to a protected endpoint",
         precond="API running",
         data="POST /api/v1/pipeline/run with (a) no X-API-Key header, (b) an invalid key",
         steps="Send both requests and inspect the response.",
         expected="Both requests are rejected with 401/403 before any pipeline work starts; no research is executed and no leads are consumed.",
         priority="P0", ttype="Security"),
    dict(id="GAP-SEC-005", stage="Security", module="Denial-of-wallet / run cost ceiling",
         scenario="Adversarial or runaway query",
         precond="Submit a query designed to maximize LLM/API calls (e.g. deliberately ambiguous, triggering many query-variation rounds)",
         data="A single query or a burst of concurrent job submissions",
         steps="Observe total LLM/API spend for one run and for a burst of submissions.",
         expected="A per-run or per-tenant cost/call ceiling exists and is enforced; a single adversarial query cannot cause unbounded spend.",
         priority="P2", ttype="Security"),
    dict(id="GAP-SEC-006", stage="Security", module="Tenant isolation",
         scenario="Two distinct tenant_ref values",
         precond="Submit jobs as two different tenants",
         data="tenant_ref='tenant-a' and tenant_ref='tenant-b' with overlapping queries",
         steps="Submit jobs for both tenants and inspect job listing/results/storage access.",
         expected="Tenant A can never list, read, or cancel Tenant B's jobs or results -- tenant_ref is enforced as an isolation boundary, not just a label.",
         priority="P1", ttype="Security"),

    # --- Stage 1 ---
    dict(id="GAP-FUNC-001", stage="Stage 1", module="Query Planning",
         scenario="Non-English query text", precond="Application running",
         data="\"Islamabad mein 3 dental clinics dhoondo\" (Roman Urdu) or a fully non-Latin-script query",
         steps="Submit query and inspect generated plan.",
         expected="Plan correctly extracts industry/location/count, or needs_clarification is set honestly if the model cannot parse it -- never a silently wrong/empty plan.",
         priority="P2", ttype="Functional"),
    dict(id="GAP-FUNC-002", stage="Stage 1", module="Query Planning",
         scenario="Extremely long query", precond="Application running",
         data="A query several thousand characters long (e.g. pasted document text)",
         steps="Submit query and inspect generated plan / any truncation behavior.",
         expected="Query is handled without crashing or truncating in a way that silently drops the actual request; a graceful length-limit response is acceptable.",
         priority="P2", ttype="Functional"),
    dict(id="GAP-FUNC-003", stage="Stage 1", module="Query Planning",
         scenario="Total planner failure", precond="Both primary and fallback planning models fail",
         data="Simulate/force both LLM tiers to error or exhaust retries",
         steps="Submit a normal query while both models are unavailable.",
         expected="A clear, honest error/needs_clarification response is returned -- never a fabricated plan or a crash.",
         priority="P1", ttype="Functional"),
    dict(id="GAP-FUNC-004", stage="Stage 1", module="Query Planning",
         scenario="Conflicting multi-location query", precond="Application running",
         data="\"find 3 dentists in Islamabad or Lahore\"",
         steps="Submit query and inspect generated plan.",
         expected="Plan either clarifies the ambiguity or picks a defensible interpretation (e.g. treats as two valid locations) -- never silently drops one location without any signal.",
         priority="P2", ttype="Functional"),

    # --- Stage 2 ---
    dict(id="GAP-FUNC-005", stage="Stage 2", module="Discovery & Scraping",
         scenario="Redirect loop", precond="A discovered site redirects to itself or in a cycle",
         data="A target URL that HTTP-redirects in a loop",
         steps="Attempt to scrape the site.",
         expected="The fetch is bounded by a redirect-count limit and fails gracefully -- no hang.",
         priority="P2", ttype="Functional"),
    dict(id="GAP-FUNC-006", stage="Stage 2", module="Discovery & Scraping",
         scenario="Non-HTML content at a page URL", precond="A discovered link resolves to a PDF/image, not HTML",
         data="A business page link that serves a PDF or image content-type",
         steps="Attempt to scrape/extract text from it.",
         expected="The page is skipped or handled as empty content, never crashes text extraction.",
         priority="P2", ttype="Functional"),
    dict(id="GAP-FUNC-007", stage="Stage 2", module="Discovery & Scraping",
         scenario="Premium scraper (ScrapingBee) quota/rate-limit failure", precond="ScrapingBee account exhausted/rate-limited",
         data="A site that requires premium scraping (native fetch blocked)",
         steps="Attempt scraping when ScrapingBee itself errors.",
         expected="Pipeline degrades gracefully (skips or native-fetch-falls-back) without crashing -- same class of guarantee already proven for the Serper-quota case.",
         priority="P2", ttype="Functional"),
    dict(id="GAP-FUNC-008", stage="Stage 2", module="Discovery & Scraping",
         scenario="Business with no website, Maps-only", precond="A real business exists only as a Maps/directory listing",
         data="A query targeting a category with some website-less businesses",
         steps="Run discovery + Maps enrichment.",
         expected="The business is still captured via the Maps stage and not silently dropped just because Stage 2 found no website to scrape.",
         priority="P2", ttype="Functional"),

    # --- Stage 3 ---
    dict(id="GAP-FUNC-009", stage="Stage 3", module="Route Planner",
         scenario="Single-page site", precond="A business site has only one page (the homepage)",
         data="A minimal one-page site",
         steps="Run route selection.",
         expected="Route planner selects the single available page without error; no off-by-one/empty-list failure.",
         priority="P2", ttype="Functional"),
    dict(id="GAP-FUNC-010", stage="Stage 3", module="Route Planner",
         scenario="Zero-useful-page site", precond="Every crawled page is filtered as noise",
         data="A site whose only pages are /cart, /wishlist, /privacy etc.",
         steps="Run route selection.",
         expected="Route planner returns an empty/near-empty selection and the pipeline reports this honestly downstream rather than erroring.",
         priority="P2", ttype="Functional"),
    dict(id="GAP-FUNC-011", stage="Stage 3", module="Route Planner",
         scenario="Tied retrieval scores", precond="Two or more pages score identically",
         data="Pages with equal BM25/embedding scores",
         steps="Run route selection twice on the same input.",
         expected="Selection is deterministic across repeated runs (stable tie-breaking), not order-dependent randomness.",
         priority="P3", ttype="Functional"),

    # --- Stage 4 ---
    dict(id="GAP-FUNC-012", stage="Stage 4", module="Final Reasoning",
         scenario="Contradictory evidence across pages", precond="Two pages of the same business disagree",
         data="e.g. one page says 'no online booking', another page's footer widget says 'book now'",
         steps="Run reasoning over both pages as evidence.",
         expected="The answer either reflects the more authoritative/recent signal or flags the contradiction -- never silently picks one at random without justification in the reasoning trace.",
         priority="P2", ttype="Functional"),
    dict(id="GAP-FUNC-013", stage="Stage 4", module="Final Reasoning",
         scenario="Evidence exceeding context window", precond="Route planner selects many/large pages",
         data="8 large selected pages whose combined text is very large",
         steps="Run reasoning with maximal evidence volume.",
         expected="Evidence is truncated/summarized safely without an API error or a context-length crash.",
         priority="P2", ttype="Functional"),

    # --- Stage 5 ---
    dict(id="GAP-FUNC-014", stage="Stage 5", module="Technology Detection",
         scenario="Site behind bot-challenge (WAF/Cloudflare)", precond="Detection fetch is blocked by a challenge page",
         data="A site returning a Cloudflare/WAF challenge page instead of real content",
         steps="Run detection against it.",
         expected="Detection reports low-confidence/unknown rather than misreporting the challenge page's own tech signature as the business's tech stack.",
         priority="P2", ttype="Functional"),

    # --- Stage 6 ---
    dict(id="GAP-FUNC-015", stage="Stage 6", module="RAG",
         scenario="Chroma collection reuse across process restarts", precond="Same domain queried in two separate process runs",
         data="Same business queried twice in separate Python process invocations",
         steps="Compare ingestion/query behavior across the restart.",
         expected="Behavior is documented and consistent -- either the collection is correctly reused (no duplicate re-ingestion) or correctly rebuilt; not an undefined/inconsistent state.",
         priority="P2", ttype="Functional"),
    dict(id="GAP-FUNC-016", stage="Stage 6", module="RAG",
         scenario="Query against zero ingested content", precond="A business with no successfully ingested pages",
         data="A business whose scrape entirely failed",
         steps="Run a RAG query against it.",
         expected="Returns an empty/honest no-evidence result, not a crash or a hallucinated answer.",
         priority="P1", ttype="Functional"),

    # --- Stage 7 ---
    dict(id="GAP-FUNC-017", stage="Stage 7", module="Google Maps",
         scenario="Maps API quota/error handling", precond="Google Maps API key exhausted or erroring",
         data="A run where the Maps API returns errors for every call",
         steps="Run the pipeline through the Maps stage under this condition.",
         expected="Pipeline degrades gracefully (Maps-derived fields left empty/unknown) rather than crashing or fabricating Maps data -- same class of guarantee already proven for Serper.",
         priority="P1", ttype="Functional"),
    dict(id="GAP-FUNC-018", stage="Stage 7", module="Google Maps",
         scenario="Duplicate/multiple Maps listings", precond="A business has multiple branch listings",
         data="A chain with several Maps entries under similar names",
         steps="Run Maps matching against the target business.",
         expected="The correct listing is matched (or ambiguity is flagged) rather than an arbitrary/wrong branch being silently attached.",
         priority="P2", ttype="Functional"),

    # --- Stage 8 ---
    dict(id="GAP-FUNC-019", stage="Stage 8", module="Accuracy Check",
         scenario="Fully empty business record", precond="A business record with no populated fields at all",
         data="A record where every extractable field failed",
         steps="Run the accuracy/DataState scoring over it.",
         expected="Resolves to the correct lowest DataState value and a clear low/zero score -- no crash, no exception.",
         priority="P1", ttype="Functional"),

    # --- Storage ---
    dict(id="GAP-FUNC-020", stage="Storage", module="Concurrent writes",
         scenario="Two pipeline runs write the same domain concurrently", precond="Run storage layer",
         data="Two parallel runs both discover the same business domain",
         steps="Trigger concurrent writes to the same domain's storage folder.",
         expected="No corrupted/partially-written files; writes are safely serialized or isolated (staging/commit pattern holds under concurrency).",
         priority="P1", ttype="Functional"),
    dict(id="GAP-FUNC-021", stage="Storage", module="Corrupted index recovery",
         scenario="crawl_index.csv is malformed on load", precond="Run storage layer",
         data="A hand-corrupted/truncated crawl_index.csv",
         steps="Start the pipeline with this file present.",
         expected="Pipeline either recovers gracefully (skips bad rows) or fails with a clear error -- never a silent, partial, or crashing load.",
         priority="P2", ttype="Functional"),

    # --- REST/API ---
    dict(id="GAP-FUNC-022", stage="REST/API", module="Malformed request body",
         scenario="Genuinely broken JSON, not a business-validation error", precond="POST to /api/v1/pipeline/run",
         data="A request body that is not valid JSON at all",
         steps="Send the malformed request.",
         expected="Returns 422 with a clear parse error, never a 500 or an unhandled exception.",
         priority="P1", ttype="Functional"),
    dict(id="GAP-FUNC-023", stage="REST/API", module="Job cancellation promptness",
         scenario="Cancel a job mid-flight during expensive work (e.g. mid-crawl)",
         precond="Submit a job, then cancel while it's actively scraping",
         data="A long-running job",
         steps="Cancel and measure how quickly in-flight work actually stops (not just the status flag flipping).",
         expected="In-flight work (HTTP fetches, LLM calls) is actually aborted promptly, not left running to completion in the background after cancellation is acknowledged.",
         priority="P1", ttype="Functional"),
    dict(id="GAP-FUNC-024", stage="REST/API", module="Idempotency record lifecycle",
         scenario="Idempotency-Key store growth over time", precond="Submit many distinct Idempotency-Keys over time",
         data="Repeated distinct requests",
         steps="Inspect whether idempotency records are ever expired/evicted.",
         expected="Records either expire after a reasonable window or the growth is a documented, bounded, accepted tradeoff -- not unbounded memory growth.",
         priority="P3", ttype="Functional"),
    dict(id="GAP-FUNC-025", stage="REST/API", module="Cursor pagination edge cases",
         scenario="Invalid or past-the-end cursor value", precond="Call a paginated listing endpoint",
         data="cursor=<garbage> and cursor=<value past the last page>",
         steps="Call the endpoint with both.",
         expected="Returns a clear 400/empty-page response, never a crash or an out-of-range exception.",
         priority="P2", ttype="Functional"),

    # --- Performance ---
    dict(id="GAP-PERF-005", stage="Performance", module="Memory over a large run",
         scenario="Many businesses processed in one job", precond="Run a job targeting a large result_limit",
         data="A job processing dozens of businesses in one run",
         steps="Monitor process memory across the full run.",
         expected="Memory stays bounded/does not grow unboundedly (no leak) across a long run.",
         priority="P2", ttype="Performance"),
    dict(id="GAP-PERF-006", stage="Performance", module="Sustained high job-submission rate",
         scenario="20-50 jobs submitted in quick succession",
         precond="Submit far more concurrent jobs than the PERF-001 baseline (5)",
         data="A burst of 20-50 simultaneous job submissions",
         steps="Submit the burst and observe where responsiveness/isolation first degrades.",
         expected="The service degrades predictably (queues, or a documented concurrency ceiling) rather than becoming unresponsive or corrupting job state.",
         priority="P2", ttype="Performance"),
]


def build(path_in: str, path_out: str, sheet_name: str = "Missing Coverage (Proposed)") -> None:
    wb = openpyxl.load_workbook(path_in)
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)

    bold = Font(bold=True, color="FFFFFF")
    fill = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")

    ws.append(HEADER)
    for c in ws[1]:
        c.font = bold
        c.fill = fill

    for r in ROWS:
        ws.append([
            r["id"], r["stage"], r["module"], r["scenario"], r["precond"],
            r["data"], r["steps"], r["expected"], "", "Not Run",
            r["priority"], r["ttype"], "", "",
        ])

    widths = [14, 12, 26, 34, 30, 40, 40, 46, 20, 12, 10, 12, 12, 24]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    wb.save(path_out)
    print(f"Wrote {path_out} -- {len(ROWS)} proposed cases in sheet '{sheet_name}'")


if __name__ == "__main__":
    src = os.path.expanduser(r"~\Downloads\AI_BDM_Professional_QA_Test_Cases.xlsx")
    build(src, src)  # in place, same file/path
    executed = os.path.join(os.path.dirname(__file__), "AI_BDM_Professional_QA_Test_Cases_EXECUTED.xlsx")
    if os.path.exists(executed):
        build(executed, executed)
