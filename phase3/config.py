"""Phase 3 configuration — constants shared across every review-platform module.

Reuses the SAME Serper key from .env that phase1_pipeline also reads (no new
secret to configure) — but loads it independently rather than importing
phase1_pipeline, since phase1_pipeline itself imports phase3.google_maps to
run enrichment after a commit. Importing phase1_pipeline here would create a
circular import (phase1_pipeline -> phase3 -> phase1_pipeline). Phase 3 stays
fully decoupled from Phase 1's internals, the same way rag/ is.

Storage lives under the existing per-domain `storage/<domain>/` folder Phase 1
already commits to, in a `reviews/` subfolder, so a business's review data
sits next to everything else already gathered about it — but this module
never touches storage.py's PageStore interface directly; it only reads
already-committed data and writes its own plain JSON files.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

SERPER_API_KEY = os.getenv("serper") or os.getenv("SERPER_API_KEY")
SERPER_SEARCH_URL = "https://google.serper.dev/search"
SERPER_PLACES_URL = "https://google.serper.dev/places"
SERPER_TIMEOUT_S = 15

# Uses the same premium-scraper credential as Phase 1 (currently ScrapingBee,
# see phase1_pipeline.PremiumScraper — that class, not this URL/timeout, is
# what review_harvester.py's premium-tier fetch actually calls through).
ZENROWS_API_KEY = os.getenv("zenrows") or os.getenv("ZENROWS_API_KEY")

# Enabled by default after a business is committed. Set false to skip the
# category-specific review harvest on a particular run.
REVIEW_ENRICHMENT_ENABLED = os.getenv(
    "REVIEW_ENRICHMENT_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
# Review listings and extracted public reviews are immutable cache artifacts
# for this workflow. ``None`` tells Phase 3's store never to expire them;
# remove a platform JSON file manually when a deliberate refresh is wanted.
REVIEW_CACHE_DAYS = None
MAX_REVIEWS_PER_PLATFORM = 20
# Hard cap on how many review-listing pages get fetched per platform (page 1
# plus up to 2 more via a "?page=N"/"&page=N" guess). Pagination stops early,
# before this cap, the moment a page contributes zero NEW reviews — this is a
# ceiling on spend, not a target every platform is expected to reach.
REVIEW_MAX_PAGES = 3

STORAGE_ROOT = "storage"          # same root LocalPageStore uses by default
REVIEWS_SUBDIR = "reviews"        # storage/<domain>/reviews/<platform>.json

DEFAULT_CONCURRENCY = 4            # matches linkedin_finder.py's CONCURRENCY

# How much an address needs to overlap before we trust a Places match belongs
# to the business we searched for (see google_maps._looks_like_match).
ADDRESS_MATCH_MIN_TOKENS = 1
