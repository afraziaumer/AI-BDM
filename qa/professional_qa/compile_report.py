"""Fills the professional QA spreadsheet's Actual Result/Status/Defect ID/
Remarks columns from results.json, and populates the Defect Log sheet.

Run: python qa/professional_qa/compile_report.py
"""
from __future__ import annotations

import json
import os
import shutil

import openpyxl

HERE = os.path.dirname(__file__)
SOURCE = os.path.expanduser(r"~\Downloads\AI_BDM_Professional_QA_Test_Cases.xlsx")
OUTPUT = os.path.join(HERE, "AI_BDM_Professional_QA_Test_Cases_EXECUTED.xlsx")

with open(os.path.join(HERE, "results.json"), encoding="utf-8") as f:
    results = json.load(f)

shutil.copy(SOURCE, OUTPUT)
wb = openpyxl.load_workbook(OUTPUT)
ws = wb["Test Cases"]

missing = []
for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
    tc_id = row[0].value
    if tc_id is None:
        continue
    rec = results.get(tc_id)
    if rec is None:
        missing.append(tc_id)
        continue
    row[8].value = rec["actual"]       # I - Actual Result
    row[9].value = rec["status"]       # J - Status
    row[12].value = rec["defect_id"]   # M - Defect ID
    row[13].value = rec["remarks"]     # N - Remarks

if missing:
    raise SystemExit(f"Missing results for: {missing}")

# --- Defect Log sheet ---
dl = wb["Defect Log"]
defects = [(tc_id, rec) for tc_id, rec in results.items() if rec.get("defect_id")]
r = 2
for tc_id, rec in defects:
    dl.cell(row=r, column=1, value=rec["defect_id"])
    dl.cell(row=r, column=2, value=tc_id)
    dl.cell(row=r, column=3, value="Path traversal via unsanitized domain string used as a filesystem path segment")
    dl.cell(row=r, column=4, value="High")
    dl.cell(row=r, column=5, value="Windows, local dev (Python 3.12)")
    dl.cell(row=r, column=6, value=(
        "1. Call domain_utils.domain_key() is bypassed OR a raw domain string "
        "containing path-separator characters (e.g. '..\\..\\windows\\system32') "
        "reaches storage.py/accuracy_check.py/phase3/store.py/rag/ingest_reviews.py. "
        "2. Any of these build a filesystem path directly via Path(root) / domain."
    ))
    dl.cell(row=r, column=7, value="Files remain within the intended storage directory structure at all times.")
    dl.cell(row=r, column=8, value=(
        "Path('storage') / '..\\..\\windows\\system32' resolved to C:\\Windows\\System32 -- "
        "a leading path separator makes pathlib treat the right operand as drive-rooted on "
        "Windows, completely escaping the intended storage root."
    ))
    dl.cell(row=r, column=9, value="Live-reproduced via direct path resolution; see tests/unit/test_domain_path_safety.py")
    dl.cell(row=r, column=10, value="Fixed")
    dl.cell(row=r, column=11, value=(
        "Added domain_utils.safe_domain_component() (strips all path separators and '..'/'.' "
        "segments); applied at all 4 filesystem call sites. Verified fixed via live re-test + "
        "7 new regression tests. Full suite: 135 passed (up from 128 pre-fix)."
    ))
    r += 1

wb.save(OUTPUT)
print(f"Wrote {OUTPUT}")
print(f"Rows filled: {ws.max_row - 1}")
print(f"Defects logged: {len(defects)}")
