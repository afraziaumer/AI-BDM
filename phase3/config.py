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

# Google Maps' review panel is virtualized (never in any static or JS-rendered
# page source) and full browsing requires a signed-in Google session, which
# this project will not automate — confirmed live, no fetch tier (native,
# premium plain, premium JS-render) can ever get review TEXT from it. Apify's
# compass/Google-Maps-Reviews-Scraper actor is the one thing that does: it
# runs the actual scraping on Apify's own infrastructure as a paid (here,
# free-tier, ~$0.0006/review) service — this project never touches Google's
# page directly for review text, only Apify's own API, given the SAME
# address/phone/name-verified listing URL google_reviews discovery already
# produces. See phase3/review_harvester.py's _fetch_apify_google_reviews.
APIFY_API_TOKEN = os.getenv("apify") or os.getenv("APIFY_API_TOKEN")
APIFY_GOOGLE_REVIEWS_ACTOR = "compass~Google-Maps-Reviews-Scraper"
APIFY_RUN_SYNC_URL = (
    f"https://api.apify.com/v2/acts/{APIFY_GOOGLE_REVIEWS_ACTOR}/run-sync-get-dataset-items"
)
APIFY_TIMEOUT_S = 180
# _fetch_apify_google_reviews retries with a bigger maxReviews when the first
# run comes up short of MAX_REVIEWS_PER_PLATFORM reviews WITH TEXT (star-only
# ratings get filtered out, so raw count != texted count). This bounds how
# far it escalates (doubling each retry) so a business with a huge review
# pool but a low text ratio can't blow past a sane per-business Apify spend.
APIFY_MAX_REVIEWS_ATTEMPT = 100

# Reddit blocks every direct fetch tier this project has (native 403,
# api.reddit.com/old.reddit.com 403, ScrapingBee plain = bot-verification
# wall, ScrapingBee JS-render = timeout) -- confirmed live. Apify's
# trudax/reddit-scraper-lite actor searches Reddit itself (posts + comments,
# by keyword) on Apify's own infrastructure instead, the same escape hatch
# APIFY_GOOGLE_REVIEWS_ACTOR is for Google Maps. See
# phase3/review_harvester.py's _fetch_apify_reddit_mentions.
APIFY_REDDIT_ACTOR = "trudax~reddit-scraper-lite"
APIFY_REDDIT_RUN_SYNC_URL = (
    f"https://api.apify.com/v2/acts/{APIFY_REDDIT_ACTOR}/run-sync-get-dataset-items"
)
# Observed live: a 15-item search run took ~2-4 minutes (slower than the
# Google Reviews actor) -- given a generous margin over APIFY_TIMEOUT_S.
APIFY_REDDIT_TIMEOUT_S = 280

# Enabled by default after a business is committed. Set false to skip the
# category-specific review harvest on a particular run.
REVIEW_ENRICHMENT_ENABLED = os.getenv(
    "REVIEW_ENRICHMENT_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
# Review listings and extracted public reviews are immutable cache artifacts
# for this workflow. ``None`` tells Phase 3's store never to expire them;
# remove a platform JSON file manually when a deliberate refresh is wanted.
REVIEW_CACHE_DAYS = None
MAX_REVIEWS_PER_PLATFORM = 25
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
