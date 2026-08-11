"""Tests for storage._csv_safe() -- CSV/formula-injection guard.

Real vulnerability found while executing the proposed gap coverage
(GAP-SEC-001): a scraped business name/field is attacker-controlled (comes
from someone else's website) and flowed straight into crawl_index.csv via
plain csv.DictWriter with no escaping. A field starting with '=', '+', '-',
or '@' is executed as a formula by Excel/Sheets when the exported CSV is
opened -- CSV-level quoting does NOT prevent this, since the spreadsheet
app evaluates the cell's leading character regardless of the surrounding
CSV quotes. Fixed by prefixing a leading apostrophe (OWASP-standard
mitigation) via storage._csv_safe(), applied at both crawl_index.csv write
sites (_append_index, retire_domain).

Run: pytest -q tests/unit/test_csv_injection_safety.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import storage  # noqa: E402


def test_equals_sign_formula_is_neutralized():
    assert storage._csv_safe('=cmd|" /C calc"!A1').startswith("'=")


def test_plus_sign_formula_is_neutralized():
    assert storage._csv_safe("+1+1").startswith("'+")


def test_minus_sign_formula_is_neutralized():
    assert storage._csv_safe("-2+3").startswith("'-")


def test_at_sign_formula_is_neutralized():
    assert storage._csv_safe("@SUM(A1)").startswith("'@")


def test_leading_tab_is_neutralized():
    assert storage._csv_safe("\t=evil").startswith("'\t")


def test_normal_business_name_is_unaffected():
    assert storage._csv_safe("Normal Business Name") == "Normal Business Name"


def test_non_string_values_pass_through_unchanged():
    assert storage._csv_safe(42) == 42
    assert storage._csv_safe(None) is None
