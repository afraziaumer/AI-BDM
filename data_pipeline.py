"""
AI BDM Platform - Data Quality Pipeline
=======================================

Standalone data-governance layer over the scraped lead store
(`scavenger_leads_cache.csv`). It does NOT scrape anything — it reads what the
pipeline saved and turns it into a clean, well-structured dataset.

Stages (run in order by `run()`):
  1. PROFILING      - measure the raw data: row counts, fill rates, duplicates.
  2. CLEANING       - normalize whitespace/encoding, validate emails/phones,
                      strip tracking params, repair mojibake.
  3. DEDUP / NOISE  - drop aggregator/utility/empty rows; de-duplicate pages.
  4. TRANSFORMATION - canonical per-page schema + a per-business rollup.
  5. EXPLORATION    - summary stats (contact coverage, top domains, methods).
  6. GOVERNANCE     - validate each business against rules; quarantine failures.

Non-destructive: it never rewrites or backs up the raw store — it only READS
`scavenger_leads_cache.csv` and DERIVES the clean outputs from it.

Outputs:
  - leads_clean.csv                  readable per-business dataset (no raw HTML)
  - leads_quarantine.csv             rows rejected by governance + the reason
  - data_quality_report.txt          human-readable, wrapped profiling/report

Reuses helpers from phase1_pipeline so cleaning rules stay consistent with the
scraper (domain keys, tracking-param stripping, contact validation, mojibake).

Run:
  ./env/bin/python data_pipeline.py                # clean the default store
  ./env/bin/python data_pipeline.py --dry-run      # report only, write nothing
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import textwrap
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import phase1_pipeline as pp
import email_extractor as ee

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

RAW_STORE = pp.OUTPUT_CSV_FILE                 # scavenger_leads_cache.csv
CLEAN_EXPORT = "leads_clean.csv"
QUARANTINE = "leads_quarantine.csv"
REPORT = "data_quality_report.txt"

# Per-business readable export columns (raw HTML deliberately excluded).
CLEAN_COLUMNS = [
    "company_name", "domain", "website_url", "email", "phone_number",
    "physical_address", "num_pages", "date_added", "page_title", "description",
    "pages_scraped",
    "high_intent_pages",   # Step 3 LLM-selected routes (JSON list of dicts)
]
DESCRIPTION_WIDTH = 280   # chars kept for the readable one-line description
MAX_PHONES_PER_BUSINESS = 8   # cap the phone list (a bigger list = directory page)


def _email_is_own(email: str, biz_domain: str) -> bool:
    """True if an email is on the business's own registered domain or is a
    free-webmail address (both are legitimate business contacts)."""
    dom = email.split("@")[-1].lower().removeprefix("www.")
    return pp._domain_key(dom) == biz_domain or dom in ee._FREE_PROVIDERS


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_raw(path: str) -> List[Dict[str, str]]:
    """Robustly read the store. Tolerates the old/corrupted header (extra
    trailing commas) by reading via DictReader and keeping only known columns."""
    if not os.path.exists(path):
        print(f"[load] No store at {path}")
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    cleaned: List[Dict[str, str]] = []
    for row in rows:
        # Drop the junk None/'' keys produced by trailing-comma headers.
        cleaned.append({k: (v or "") for k, v in row.items()
                        if k in pp.CSV_HEADERS})
    print(f"[load] {len(cleaned)} rows from {path}")
    return cleaned


# --------------------------------------------------------------------------- #
# 1. Profiling
# --------------------------------------------------------------------------- #
def profile(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    """Measure the raw data so we know what we're cleaning."""
    n = len(rows)
    fill = {c: 0 for c in pp.CSV_HEADERS}
    for r in rows:
        for c in pp.CSV_HEADERS:
            v = (r.get(c) or "").strip()
            if v and v != "N/A":
                fill[c] += 1

    sites = [pp._strip_tracking(r.get("website_url", "") or "") for r in rows]
    pages = [pp._strip_tracking(r.get("page_url", "") or "") for r in rows]
    domains = [pp._domain_key(s) for s in sites if s]

    report = {
        "rows": n,
        "distinct_businesses": len(set(d for d in domains if d)),
        "distinct_pages": len(set(p for p in pages if p)),
        "duplicate_page_rows": len(pages) - len(set(pages)) if pages else 0,
        "fill_rate": {c: (fill[c], round(100 * fill[c] / n, 1) if n else 0.0)
                      for c in pp.CSV_HEADERS},
    }
    return report


# --------------------------------------------------------------------------- #
# 2. Cleaning  +  3. Dedup / noise removal
# --------------------------------------------------------------------------- #
def _clean_cell(value: str) -> str:
    """Collapse whitespace and repair double-encoded (mojibake) text."""
    return pp._fix_mojibake(" ".join((value or "").split()))


def _clean_multi(raw: str, is_valid, lower: bool = False) -> str:
    """Clean a possibly-multi-valued cell (values joined by CONTACT_SEP): split,
    validate each value, dedupe, and rejoin. Returns 'N/A' if none survive."""
    out: List[str] = []
    for v in (raw or "").split(pp.CONTACT_SEP):
        v = v.strip()
        if not v or v == "N/A":
            continue
        if lower:
            v = v.lower()
        if is_valid(v) and v not in out:
            out.append(v)
    return pp.CONTACT_SEP.join(out) if out else "N/A"


def clean_rows(
    rows: List[Dict[str, str]]
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Normalize fields and drop noisy rows. Returns (clean_pages, rejected)."""
    clean: List[Dict[str, str]] = []
    rejected: List[Dict[str, str]] = []

    for r in rows:
        site = pp._strip_tracking(r.get("website_url", "") or "")
        host = pp.urlparse(site).netloc.lower().removeprefix("www.")
        domain = pp._domain_key(site)

        # --- noise filters --------------------------------------------------
        if not site or not domain:
            rejected.append({**r, "_reason": "no_website"})
            continue
        if pp._is_aggregator(host):
            rejected.append({**r, "_reason": "aggregator_domain"})
            continue
        if host and pp._is_utility_link(host, pp.urlparse(site).path, ""):
            rejected.append({**r, "_reason": "utility_domain"})
            continue

        page_text = _clean_cell(r.get("page_text", ""))
        # email / phone cells may hold several values joined by CONTACT_SEP.
        email = _clean_multi(r.get("email", ""), pp._valid_email, lower=True)
        # A phone is kept only if it validates or carries an international "+",
        # so stray digit runs (e.g. "212206612192") are dropped here too.
        phone = _clean_multi(
            r.get("phone_number", ""), lambda p: bool(pp._format_phone(p))
        )

        # A page with no text AND no contact carries no value -> drop.
        if not page_text and email == "N/A" and phone == "N/A":
            rejected.append({**r, "_reason": "empty_no_contact"})
            continue

        clean.append({
            "company_name": _clean_cell(r.get("company_name", "")) or domain,
            "website_url": site,
            "page_url": pp._strip_tracking(r.get("page_url", "") or site),
            "page_title": _clean_cell(r.get("page_title", "")),
            "meta_description": _clean_cell(r.get("meta_description", "")),
            "email": email,
            "phone_number": phone,
            "physical_address": _clean_cell(r.get("physical_address", "")) or "N/A",
            "scrape_source_method": (r.get("scrape_source_method", "") or "").strip() or "N/A",
            "date_added": (r.get("date_added", "") or "").strip(),
            "page_text": page_text,
            "raw_html": r.get("raw_html", "") or "",
            "_domain": domain,
        })

    return clean, rejected


def dedupe_pages(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Collapse duplicate (business, page) rows, keeping the richest copy
    (longest page_text / best contact)."""
    best: Dict[Tuple[str, str], Dict[str, str]] = {}
    for r in rows:
        key = (r["_domain"], r["page_url"])
        cur = best.get(key)
        if cur is None or _richness(r) > _richness(cur):
            best[key] = r
    return list(best.values())


def _richness(r: Dict[str, str]) -> int:
    """Heuristic completeness score used to pick the best of duplicate rows."""
    score = len(r.get("page_text", ""))
    if r.get("email", "N/A") != "N/A":
        score += 5000
    if r.get("phone_number", "N/A") != "N/A":
        score += 2000
    return score


# --------------------------------------------------------------------------- #
# 4. Transformation — per-business rollup
# --------------------------------------------------------------------------- #
def to_business_level(pages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Aggregate page rows into one record per business (registered domain)."""
    groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in pages:
        groups[r["_domain"]].append(r)

    businesses: List[Dict[str, Any]] = []
    for domain, rows in groups.items():
        rows_sorted = sorted(rows, key=lambda r: len(pp.urlparse(r["page_url"]).path.rstrip("/")))
        home = rows_sorted[0]  # shortest path == homepage

        names = Counter(r["company_name"] for r in rows if r["company_name"])
        # Union every unique email / phone found across all of the site's pages.
        emails = pp._union_contacts(rows, "email")
        phones = pp._union_contacts(rows, "phone_number")
        # Prefer the business's own-domain / free-webmail addresses: if any
        # exist, drop off-domain leaks (e.g. "health@home.ms" mis-parsed from a
        # "health at home" phrase). Only keep off-domain when there's nothing else.
        preferred = [e for e in emails if _email_is_own(e, domain)]
        emails = preferred if preferred else emails
        # A single business rarely has 20 numbers — a huge list means a
        # directory/locations page. Cap it (homepage/main number stays first).
        phones = phones[:MAX_PHONES_PER_BUSINESS]
        email = pp.CONTACT_SEP.join(emails) if emails else "N/A"
        phone = pp.CONTACT_SEP.join(phones) if phones else "N/A"
        addr = next((r["physical_address"] for r in rows if r["physical_address"] != "N/A"), "N/A")
        dates = sorted(d for d in (r.get("date_added", "") for r in rows) if d)

        desc_source = home.get("meta_description") or home.get("page_text", "")
        description = _clean_cell(desc_source)[:DESCRIPTION_WIDTH]
        # All pages we actually scraped for this business, so coverage is visible.
        page_urls = list(dict.fromkeys(
            r.get("page_url", "") for r in rows_sorted if r.get("page_url")
        ))

        businesses.append({
            "company_name": names.most_common(1)[0][0] if names else domain,
            "domain": domain,
            "website_url": home["website_url"],
            "email": email,
            "phone_number": phone,
            "physical_address": addr,
            "num_pages": len(rows),
            "date_added": dates[0] if dates else "",
            "page_title": home.get("page_title", ""),
            "description": description,
            "pages_scraped": pp.CONTACT_SEP.join(page_urls),
        })

    businesses.sort(key=lambda b: b["company_name"].lower())
    return businesses


# --------------------------------------------------------------------------- #
# 5. Exploration
# --------------------------------------------------------------------------- #
def explore(businesses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summary statistics over the cleaned business-level dataset."""
    n = len(businesses) or 1
    with_email = sum(1 for b in businesses if b["email"] != "N/A")
    with_phone = sum(1 for b in businesses if b["phone_number"] != "N/A")
    tlds = Counter(b["domain"].rsplit(".", 1)[-1] for b in businesses if "." in b["domain"])
    dates = sorted(b["date_added"] for b in businesses if b["date_added"])
    return {
        "businesses": len(businesses),
        "email_coverage": (with_email, round(100 * with_email / n, 1)),
        "phone_coverage": (with_phone, round(100 * with_phone / n, 1)),
        "top_tlds": tlds.most_common(5),
        "date_range": (dates[0], dates[-1]) if dates else ("-", "-"),
    }


# --------------------------------------------------------------------------- #
# 6. Governance — validate + quarantine
# --------------------------------------------------------------------------- #
def govern(
    businesses: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Apply validation rules. Returns (valid, invalid_with_reason)."""
    valid, invalid = [], []
    for b in businesses:
        problems = []
        if not b["domain"]:
            problems.append("missing_domain")
        # email may hold several values joined by CONTACT_SEP; valid if any is.
        if b["email"] != "N/A":
            addrs = [e.strip() for e in b["email"].split(pp.CONTACT_SEP) if e.strip()]
            if not any(pp._valid_email(e) for e in addrs):
                problems.append("bad_email")
        if b["num_pages"] < 1:
            problems.append("no_pages")
        if b["email"] == "N/A" and b["phone_number"] == "N/A":
            problems.append("no_contact")  # warn-level; still kept below

        # Hard-fail only on structural problems; "no_contact" is a soft warning.
        hard = [p for p in problems if p != "no_contact"]
        if hard:
            invalid.append({**b, "_reason": ";".join(hard)})
        else:
            b["_warnings"] = ";".join(problems)
            valid.append(b)
    return valid, invalid


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def write_clean_export(businesses: List[Dict[str, Any]], path: str) -> None:
    """Write the readable per-business dataset (no raw HTML)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CLEAN_COLUMNS)
        writer.writeheader()
        for b in businesses:
            writer.writerow({c: b.get(c, "") for c in CLEAN_COLUMNS})


def write_quarantine(rejected_pages: List[Dict[str, str]],
                     invalid_biz: List[Dict[str, Any]], path: str) -> None:
    """Write everything that was dropped/failed, with a reason column."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["level", "reason", "company_name", "website_url", "detail"])
        for r in rejected_pages:
            writer.writerow(["page", r.get("_reason", ""), r.get("company_name", ""),
                             r.get("website_url", ""), r.get("page_url", "")])
        for b in invalid_biz:
            writer.writerow(["business", b.get("_reason", ""), b.get("company_name", ""),
                             b.get("website_url", ""), b.get("domain", "")])


def write_report(prof: Dict[str, Any], stats: Dict[str, Any],
                 valid: List[Dict[str, Any]], rejected_n: int,
                 invalid_n: int, path: str,
                 last_run: Optional[Dict[str, Any]] = None) -> None:
    """Write a human-readable, wrapped data-quality report."""
    last_run = last_run or {}
    lines: List[str] = []
    lines.append("=" * 70)
    lines.append("AI BDM — DATA QUALITY REPORT")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    if last_run.get("query"):
        lines.append(f"Query    : {last_run['query']}")
        lines.append(f"Scope    : latest query only "
                     f"({len(last_run.get('domains') or [])} businesses)")
    else:
        lines.append("Scope    : entire store (all queries)")
    lines.append("=" * 70)

    lines.append("\n[1] PROFILING (raw store)")
    lines.append(f"  rows                : {prof['rows']}")
    lines.append(f"  distinct businesses : {prof['distinct_businesses']}")
    lines.append(f"  distinct pages      : {prof['distinct_pages']}")
    lines.append(f"  duplicate page rows : {prof['duplicate_page_rows']}")
    lines.append("  column fill rates:")
    for col, (cnt, pct) in prof["fill_rate"].items():
        lines.append(f"    - {col:<22} {cnt:>5}  ({pct}%)")

    lines.append("\n[2-3] CLEANING / NOISE REMOVAL")
    lines.append(f"  rows quarantined (noise/empty/aggregator): {rejected_n}")

    lines.append("\n[4-5] EXPLORATION (clean business-level dataset)")
    lines.append(f"  businesses          : {stats['businesses']}")
    lines.append(f"  email coverage      : {stats['email_coverage'][0]} ({stats['email_coverage'][1]}%)")
    lines.append(f"  phone coverage      : {stats['phone_coverage'][0]} ({stats['phone_coverage'][1]}%)")
    lines.append(f"  top TLDs            : {stats['top_tlds']}")
    lines.append(f"  date range          : {stats['date_range'][0]} .. {stats['date_range'][1]}")

    lines.append("\n[6] GOVERNANCE")
    lines.append(f"  valid businesses    : {len(valid)}")
    lines.append(f"  invalid (quarantined): {invalid_n}")

    lines.append("\n" + "-" * 70)
    lines.append("SAMPLE CLEAN LEADS (first 10):")
    lines.append("-" * 70)
    for b in valid[:10]:
        lines.append(f"\n• {b['company_name']}  [{b['domain']}]")
        lines.append(f"    site : {b['website_url']}")
        lines.append(f"    email: {b['email']}   phone: {b['phone_number']}")
        lines.append(f"    pages: {b['num_pages']}   added: {b['date_added'] or '-'}")
        if b.get("description"):
            wrapped = textwrap.fill(b["description"], width=66,
                                    initial_indent="    ", subsequent_indent="    ")
            lines.append(wrapped)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def scope_to_last_run(
    rows: List[Dict[str, str]]
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """Filter raw rows to just the businesses touched by the most recent query.

    Reads `last_run.json` (written by phase1_pipeline). If it's missing or names
    no domains, returns the rows unchanged so the pipeline still works on a raw
    store produced before this feature existed.
    """
    last = pp.load_last_run()
    domains = set(last.get("domains") or [])
    if not domains:
        return rows, last
    scoped = [
        r for r in rows
        if pp._domain_key(pp._strip_tracking(r.get("website_url", "") or "")) in domains
    ]
    return scoped, last


def run(path: str = RAW_STORE, dry_run: bool = False,
        scope_all: bool = False,
        routes: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Run the full data-quality pipeline and write the outputs.

    By default the report covers only the latest query (per `last_run.json`).
    Pass `scope_all=True` to process the entire cumulative store instead.

    `routes` optionally maps a business's registered domain -> a JSON string of
    its Step 3 high-intent pages; when given, that value is written into the
    `high_intent_pages` column.
    """
    raw = load_raw(path)
    if not raw:
        return {}

    last_run: Dict[str, Any] = {}
    if not scope_all:
        scoped, last_run = scope_to_last_run(raw)
        if last_run.get("domains"):
            print(f"[scope]   latest query: {last_run.get('query', '?')!r} "
                  f"-> {len(scoped)} of {len(raw)} rows "
                  f"({len(last_run['domains'])} businesses)")
            raw = scoped
        else:
            print("[scope]   no last_run.json found — processing entire store.")

    prof = profile(raw)
    clean, rejected = clean_rows(raw)
    deduped = dedupe_pages(clean)
    businesses = to_business_level(deduped)
    # Attach Step 3 high-intent pages (JSON list of routes) per business by domain.
    routes = routes or {}
    for b in businesses:
        b["high_intent_pages"] = routes.get(b["domain"], "")
    stats = explore(businesses)
    valid, invalid = govern(businesses)

    print(f"[profile] {prof['rows']} rows | {prof['distinct_businesses']} businesses "
          f"| {prof['duplicate_page_rows']} dup pages")
    print(f"[clean]   kept {len(deduped)} pages | quarantined {len(rejected)} noisy rows")
    print(f"[rollup]  {len(businesses)} businesses | "
          f"email {stats['email_coverage'][1]}% phone {stats['phone_coverage'][1]}%")
    print(f"[govern]  {len(valid)} valid | {len(invalid)} invalid")

    if dry_run:
        print("[dry-run] no files written.")
        write_report(prof, stats, valid, len(rejected), len(invalid), REPORT, last_run)
        print(f"[report]  {REPORT}")
        return {"valid": len(valid), "invalid": len(invalid)}

    # Derive (read-only on the raw store): clean export, quarantine, report.
    write_clean_export(valid, CLEAN_EXPORT)
    write_quarantine(rejected, invalid, QUARANTINE)
    write_report(prof, stats, valid, len(rejected), len(invalid), REPORT, last_run)

    print(f"[write]   clean export -> {CLEAN_EXPORT}  ({len(valid)} businesses)")
    print(f"[write]   quarantine   -> {QUARANTINE}  ({len(rejected)} rows)")
    print(f"[write]   report       -> {REPORT}")
    return {"valid": len(valid), "invalid": len(invalid),
            "quarantined": len(rejected)}


def main() -> None:
    parser = argparse.ArgumentParser(description="AI BDM data-quality pipeline")
    parser.add_argument("--store", default=RAW_STORE, help="CSV store to clean.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Profile + report only; write no output files.")
    parser.add_argument("--all", action="store_true",
                        help="Process the entire store instead of just the "
                             "latest query (default: latest query only).")
    args = parser.parse_args()
    run(args.store, dry_run=args.dry_run, scope_all=args.all)


if __name__ == "__main__":
    main()
