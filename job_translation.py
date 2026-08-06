"""Translates a structured JobRequest (Section 4.3's target/limits/features
shape) into the single natural-language query string
`LLM_planner.plan_query()` actually understands.

There is no structured-field request path anywhere else in this codebase
today -- every existing entry point (`main.py --query "..."`, `api.py`'s
`POST /api/v1/pipeline/run`) takes one sentence and lets the LLM decompose
it into `geo_location`/`broad_industry`/etc. itself. This module is the
one place that bridges the two, and it does so honestly: fields that
don't map onto anything this pipeline actually does are surfaced as
explicit warnings, never silently dropped or faked as working.
"""

from __future__ import annotations

from typing import List, Tuple

from job_contracts import JobRequest, Location


def _format_location(loc: Location) -> str:
    parts = [p for p in (loc.city, loc.region, loc.country) if p]
    return ", ".join(parts)


def target_to_query(req: JobRequest) -> Tuple[str, List[str]]:
    """Returns (query_string, warnings). `query_string` is what gets passed
    to `phase1_pipeline.run_pipeline()` as `user_query`; `warnings` lists
    every JobRequest field that could NOT be honestly represented in that
    one sentence, for the caller to fold into the eventual JobResult's
    top-level `warnings` list rather than pretend the request was fully
    satisfied.
    """
    warnings: List[str] = []

    industry = req.target.industry.strip()
    count = req.limits.max_prospects

    location_phrase = ""
    if req.target.locations:
        formatted = [_format_location(loc) for loc in req.target.locations if _format_location(loc)]
        if formatted:
            # The planner (LLM_planner.plan_query) reads ONE geo_location
            # phrase out of the sentence -- multiple locations collapse
            # into an "or"-joined phrase it may or may not fully resolve
            # (this is an LLM interpretation, not a guaranteed multi-region
            # search across N distinct areas). Flag that honestly rather
            # than claim N independent regional searches will run.
            location_phrase = f" in {' or '.join(formatted)}"
            if len(formatted) > 1:
                warnings.append(
                    f"target.locations had {len(formatted)} entries; the underlying "
                    "planner reads one natural-language geo phrase from the query, "
                    "not N independent regional searches. Joined as an 'or' phrase -- "
                    "verify plan.geo_location in the job's progress/result reflects "
                    "what you actually wanted before trusting multi-region coverage."
                )

    tech_stack_hint = ""
    if req.features.tech_stack:
        # needs_tech_stack is an LLM-determined field on the plan (set when
        # the query text itself implies a CRM/CMS/tooling question) -- it
        # is NOT a directly settable flag anywhere in this pipeline. Best
        # effort: phrase the query so the planner is likely to set it, but
        # this is a nudge, not a guarantee, and is flagged as such.
        tech_stack_hint = " including their website technology stack (CRM/CMS/hosting)"
        warnings.append(
            "features.tech_stack=true was requested, but needs_tech_stack is an "
            "LLM-inferred field on the plan, not a directly settable parameter -- "
            "the query was phrased to make this likely, not guaranteed. Check "
            "plan.needs_tech_stack in the job's progress/result to confirm."
        )

    if req.target.company_size is not None:
        warnings.append(
            "target.company_size was provided but is not usable: no employee-count "
            "signal exists anywhere in this pipeline's scrape/enrichment/scoring "
            "path. Silently ignored rather than silently pretended to filter on it."
        )

    if not req.features.maps:
        # features.maps=False is at least representable (Stage 7 can be
        # skipped) -- but features.maps=True (the default) can NOT
        # currently be honored by job_runner.py's wrapping of
        # run_pipeline(), because Google Maps enrichment (Stage 7) is a
        # SEPARATE step in main.py's orchestration, operating on
        # leads_clean.csv after run_pipeline() already returned -- it is
        # not part of run_pipeline() at all. See job_runner.py and
        # docs/backend-handoff/JOB_EXECUTION.md for the honest scope note.
        pass
    else:
        warnings.append(
            "features.maps=true was requested, but this job runner currently wraps "
            "only Stages 1-2 (phase1_pipeline.run_pipeline()) -- Stage 7 (Google Maps "
            "enrichment) is a separate step in main.py's orchestration that this job "
            "runner does not yet invoke. maps_rating/maps_matched fields will not be "
            "populated on any returned prospect. See docs/backend-handoff/JOB_EXECUTION.md."
        )

    if not req.features.reviews:
        warnings.append(
            "features.reviews=false was requested, but review/Reddit harvesting inside "
            "Stage 2 is currently controlled by the server-side REVIEW_ENRICHMENT_ENABLED "
            "environment variable, not a per-job parameter -- this request-level toggle "
            "is not yet honored per-job."
        )

    query = f"find {count} {industry} businesses{location_phrase}{tech_stack_hint}".strip()
    return query, warnings
