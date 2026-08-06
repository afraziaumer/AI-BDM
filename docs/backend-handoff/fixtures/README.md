# Fixtures

The actual fixture files live at [`../../../tests/fixtures/`](../../../tests/fixtures/) (repo-root `tests/fixtures/`), not duplicated here — pytest reads directly from that location, and copying them here would risk the two copies silently drifting apart the first time one gets updated and the other doesn't.

## What's there today

- `tests/fixtures/leads_with_maps_sample.csv` — a sanitized per-business rollup row, used by `tests/smoke/test_offline_smoke.py`'s accuracy-check tests.
- `tests/fixtures/fixture_storage/example-dental-clinic.com/reviews/{google_reviews,reddit}.json` — sanitized review-platform records (one with a real match, exercising the "success with data" shape).

## What's honestly missing

The Laravel guide's Section 4.1 wants fixtures for successful, empty, partial, **retryable-failure**, and **terminal-failure** cases specifically. Today, the empty/retryable-failure/terminal-failure scenarios are exercised in test code via fake objects (`_FakeSession`, `_FakeResponse` in `tests/mocked/test_cache_correctness.py` and `tests/mocked/test_review_escalation.py`), not saved as standalone fixture files on disk. The behavior itself is real and tested — see those files directly for the exact scenarios covered — but there's no `tests/fixtures/*_transient_failure.json`-style file a reviewer could open without reading test code. Flagged here rather than silently left out; not blocked by the deferred job-contract work, just not yet done.
