"""Post-hoc accuracy/QA pass over the last committed pipeline run.

Reads leads_with_maps.csv (or leads_clean.csv, whichever exists) plus every
storage/<domain>/reviews/*.json review record already committed, and flags
likely mistakes across three independent checks:

  1. Field validity        - does this data look real or malformed?
  2. Identity consistency  - is the same business being described everywhere
                              (website title, Google Maps listing, each
                              review platform), or did something latch onto
                              the wrong page?
  3. Review relevance      - does each individual review/mention actually
                              reference this business, or is it unrelated
                              chatter that happened to live on a matched page?

This is read-only over what's already committed -- it never re-scrapes
anything, so it's cheap to re-run after every query.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from phase1_pipeline import CONTACT_SEP, _valid_email
from data_pipeline import _email_is_own
from domain_utils import safe_domain_component
from phase3 import store
from phase3.config import REVIEWS_SUBDIR, STORAGE_ROOT
from phase3.google_maps import PLATFORM as MAPS_PLATFORM
from phase3.google_maps import _looks_like_match as _maps_address_overlaps
from phase3.review_harvester import _tokens

LEADS_WITH_MAPS = "leads_with_maps.csv"
LEADS_CLEAN = "leads_clean.csv"
REPORT_PATH = "accuracy_report.txt"

# Fraction of a business's own name-tokens that must reappear elsewhere
# before we trust it's actually still talking about the same business.
NAME_OVERLAP_MIN = 0.3


def _split_multi(raw: str) -> List[str]:
    return [v.strip() for v in (raw or "").split(CONTACT_SEP) if v.strip() and v.strip() != "N/A"]


# --------------------------------------------------------------------------- #
# 1. Field validity
# --------------------------------------------------------------------------- #
def check_field_validity(row: Dict[str, str]) -> List[str]:
    flags: List[str] = []
    domain = row.get("domain", "")

    for email in _split_multi(row.get("email", "")):
        if not _valid_email(email):
            flags.append(f"email doesn't look like a real address: {email}")
        elif not _email_is_own(email, domain):
            flags.append(f"email isn't on the business's own domain or a known webmail provider: {email}")

    for phone in _split_multi(row.get("phone_number", "")):
        digits = re.sub(r"\D", "", phone)
        if not (7 <= len(digits) <= 15):
            flags.append(f"phone doesn't look like a real number: {phone}")

    # A loose sanity check, not the strict extraction pattern _ADDRESS_RE
    # uses elsewhere: that regex expects a descriptive street name between
    # the house number and a suffix word (e.g. "12 Main Street"), which
    # rejects real, already-clean short-form addresses like "8 Markaz,
    # Islamabad 44000" -- a false positive, not a real data problem. This
    # only catches addresses with no digit at all or implausibly short ones.
    address = (row.get("physical_address") or "").strip()
    if address and address != "N/A":
        if not re.search(r"\d", address):
            flags.append(f"address has no house/street number at all: {address}")
        elif len(address) < 8:
            flags.append(f"address looks too short to be a real address: {address}")

    rating = (row.get("maps_rating") or "").strip()
    if rating:
        try:
            if not (0 <= float(rating) <= 5):
                flags.append(f"maps_rating out of the 0-5 range: {rating}")
        except ValueError:
            flags.append(f"maps_rating isn't a number: {rating}")

    count = (row.get("maps_rating_count") or "").strip()
    if count:
        try:
            if int(float(count)) < 0:
                flags.append(f"maps_rating_count is negative: {count}")
        except ValueError:
            flags.append(f"maps_rating_count isn't a number: {count}")

    return flags


# --------------------------------------------------------------------------- #
# 2. Cross-source identity consistency
# --------------------------------------------------------------------------- #
def _name_overlap(reference: str, candidate: str) -> float:
    """Fraction of `reference`'s tokens that also appear in `candidate`.

    1.0 (never flagged) when `reference` has no usable tokens to compare --
    an empty/generic name isn't evidence of a wrong match, just missing data.
    """
    ref_tokens = _tokens(reference)
    if not ref_tokens:
        return 1.0
    return len(ref_tokens & _tokens(candidate)) / len(ref_tokens)


def _iter_review_records(domain: str):
    """Yield only genuine per-platform review_harvester records.

    storage/<domain>/reviews/ also holds category.json (business_category's
    own cache) and google_maps.json (google_maps.py's Step-7 rating/category
    cache, keyed by "title" not "business_name") -- neither is a platform
    review record, so both would produce bogus "listing name doesn't match"
    flags if treated as one. Every genuine review_harvester record always
    has both "platform" and "reviews" keys (even when empty), which those
    two files never do -- that's what distinguishes them here.
    """
    reviews_dir = Path(STORAGE_ROOT) / safe_domain_component(domain) / REVIEWS_SUBDIR
    if not reviews_dir.is_dir():
        return
    for platform_file in sorted(reviews_dir.glob("*.json")):
        try:
            record = json.loads(platform_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if "platform" in record and "reviews" in record:
            yield record


def check_identity_consistency(
    domain: str, company_name: str, page_title: str, physical_address: str = "",
) -> List[str]:
    flags: List[str] = []

    if page_title and page_title != "N/A":
        overlap = _name_overlap(company_name, page_title)
        if overlap < NAME_OVERLAP_MIN:
            flags.append(
                f"the website's own page title barely matches the company name "
                f"({overlap:.0%} word overlap) -- title was {page_title!r}"
            )

    # google_maps.json is deliberately excluded from _iter_review_records
    # (it's keyed "title", not "business_name" -- see that function's
    # docstring), which meant a Maps listing matched via address or phone
    # (not name) never got its NAME cross-checked against company_name at
    # all here -- a business matched by address/phone can legitimately have
    # a materially different name (rebrand, chain listing, wrong nearby
    # business at a shared address) and nothing flagged it. Checked
    # separately here instead of folding into _iter_review_records, since
    # it isn't a platform review record and needs its own field names.
    maps_record = store.load(domain, MAPS_PLATFORM)
    if maps_record and maps_record.get("matched"):
        maps_title = maps_record.get("title") or ""
        if maps_title:
            overlap = _name_overlap(company_name, maps_title)
            if overlap < NAME_OVERLAP_MIN:
                flags.append(
                    f"google_maps listing name barely matches the company name "
                    f"({overlap:.0%} word overlap) -- listing was {maps_title!r}, "
                    f"possibly a different business (matched via "
                    f"{maps_record.get('match_basis', '?')})"
                )
        maps_address = maps_record.get("address") or ""
        if (
            maps_address and physical_address and physical_address != "N/A"
            and not _maps_address_overlaps(maps_address, physical_address)
        ):
            flags.append(
                f"google_maps listing address shares no distinctive word with "
                f"the website's own address -- website: {physical_address!r}, "
                f"maps: {maps_address!r} (matched via "
                f"{maps_record.get('match_basis', '?')})"
            )

    for record in _iter_review_records(domain):
        if not record.get("matched"):
            continue  # nothing to cross-check -- no listing was found for this platform
        listed_name = record.get("business_name") or ""
        overlap = _name_overlap(company_name, listed_name)
        if overlap < NAME_OVERLAP_MIN:
            flags.append(
                f"{record.get('platform', '?')} listing name barely matches "
                f"({overlap:.0%} word overlap) -- listing was {listed_name!r}, "
                f"possibly the wrong business page"
            )

    return flags


# --------------------------------------------------------------------------- #
# 3. Review-relevance grounding
# --------------------------------------------------------------------------- #
# A review posted directly on a business's own confirmed listing page
# (google_reviews, healthgrades, yelp, ...) has no reason to restate the
# business's name or city in its text -- nobody writes "I love <business
# name> in <city>" reviewing a page that's already unambiguously that
# business. Testing this live against a real business flagged 16/25 real,
# on-topic Google reviews as "unrelated" for exactly that reason -- pure
# noise. The check is only meaningful for a source found by an open keyword
# SEARCH rather than a business-confirmed listing page, where an individual
# result genuinely can be about something else (reddit, currently the only
# such platform) -- so it's scoped to those.
_SEARCH_BASED_PLATFORMS = {"reddit"}

from langdetect import DetectorFactory, LangDetectException, detect

DetectorFactory.seed = 0  # langdetect is otherwise non-deterministic run to run


def _is_english(text: str) -> bool:
    """Best-effort: True for English OR when detection itself can't tell
    (very short text, or a genuine detector failure) -- word-overlap can't
    meaningfully judge relevance in a language it doesn't understand, so
    "unknown" must not be flagged as "irrelevant" either.

    Known gap: this only catches non-Latin-script languages (Arabic,
    Chinese, Cyrillic, Devanagari, ...) reliably. Code-switched Roman
    Urdu/English -- real English words in Latin script -- reads as English
    to a statistical detector (confirmed live: "Sindh Balochistan ka idk k
    Kya criteria hota..." detects as "en"), so it still hits the
    word-overlap check below and can still be flagged. Solving that would
    need a different approach entirely (e.g. a curated Roman-Urdu function-
    word list), not language detection.
    """
    try:
        return detect(text) == "en"
    except LangDetectException:
        return True


def check_review_relevance(domain: str, company_name: str, geo: str) -> List[str]:
    flags: List[str] = []
    name_tokens = _tokens(company_name)
    geo_tokens = _tokens(geo)

    for record in _iter_review_records(domain):
        platform = record.get("platform") or "?"
        if platform not in _SEARCH_BASED_PLATFORMS:
            continue
        for i, review in enumerate(record.get("reviews") or [], start=1):
            text = review.get("text", "")
            text_tokens = _tokens(text)
            if not text_tokens:
                continue
            if name_tokens & text_tokens:
                continue  # mentions the business by name -- clearly relevant
            if geo_tokens & text_tokens:
                continue  # mentions the city/geo -- weak but acceptable signal
            if not _is_english(text):
                continue  # word-overlap can't judge relevance in a language it can't read
            snippet = text[:80].replace("\n", " ")
            flags.append(
                f'{platform} review #{i} never mentions the business name or geo -- '
                f'possibly unrelated chatter: "{snippet}..."'
            )

    return flags


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def load_rows(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(geo: str = "", path: str = "") -> None:
    """`path` forces which CSV to audit (e.g. main.py passes leads_clean.csv
    explicitly when --no-maps skipped writing leads_with_maps.csv this run --
    without that, this would silently audit a STALE leads_with_maps.csv left
    over from an earlier run instead of what was actually just committed).
    Left empty, it auto-detects: leads_with_maps.csv if present, else
    leads_clean.csv -- the standalone-CLI behavior.
    """
    if not path:
        path = LEADS_WITH_MAPS if Path(LEADS_WITH_MAPS).exists() else LEADS_CLEAN
    if not Path(path).exists():
        print(f"[accuracy_check] neither {LEADS_WITH_MAPS} nor {LEADS_CLEAN} exists -- "
              f"run the pipeline first.")
        return

    rows = load_rows(path)
    print(f"[accuracy_check] auditing {len(rows)} business(es) from {path}")

    report_sections: List[str] = []
    total_flags = 0
    businesses_with_flags = 0

    for row in rows:
        domain = row.get("domain", "")
        company_name = row.get("company_name", "")
        page_title = row.get("page_title", "")

        flags: List[str] = []
        flags += [f"[field]    {f}" for f in check_field_validity(row)]
        flags += [f"[identity] {f}" for f in check_identity_consistency(
            domain, company_name, page_title, row.get("physical_address", ""),
        )]
        flags += [f"[review]   {f}" for f in check_review_relevance(domain, company_name, geo)]

        if flags:
            businesses_with_flags += 1
            total_flags += len(flags)
            section = [f"\n=== {company_name or domain} ({domain}) -- {len(flags)} flag(s) ==="]
            section += [f"  - {f}" for f in flags]
            report_sections.append("\n".join(section))

    header = (
        f"Accuracy check report\n"
        f"Source file: {path}\n"
        f"Businesses audited: {len(rows)}\n"
        f"Businesses with at least one flag: {businesses_with_flags}\n"
        f"Total flags: {total_flags}\n"
        f"{'=' * 70}\n"
    )
    Path(REPORT_PATH).write_text(header + "\n".join(report_sections), encoding="utf-8")
    print(f"[accuracy_check] {businesses_with_flags}/{len(rows)} business(es) flagged, "
          f"{total_flags} total flag(s) -> {REPORT_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post-hoc accuracy/QA pass over the last committed pipeline run.")
    parser.add_argument("--geo", default="",
                         help="Geo/city term to accept as a relevance signal in review text "
                              "(optional -- e.g. 'Islamabad').")
    args = parser.parse_args()
    run(geo=args.geo)


if __name__ == "__main__":
    main()
