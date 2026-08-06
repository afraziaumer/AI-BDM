# Job Execution — Deferred

This file is intentionally a stub, not an oversight.

The Laravel Backend Construction Handoff Guide's Section 4.3–4.6 asks for a versioned job-request/progress/result/error contract (`job_id`, `tenant_ref`, `correlation_id`, structured `target`, progress/heartbeat emission, cancellation, and an 8-class error taxonomy). **This work has been explicitly deferred by the Python team lead's decision** — it is real engineering (an async job wrapper around the existing synchronous pipeline), not a documentation gap, and building it was intentionally postponed to keep this handoff pass scoped to what already exists.

See `KNOWN_LIMITATIONS.md` in this folder for the full list of what's missing as a result, and `../INTEGRATION_NOTES.md` for how the *existing* partial API (`api.py`'s `POST /pipeline/run`) works today in its place.

When this work is picked up, this file should document: the job submission/polling/cancellation flow, how `target`/`limits`/`features` translate into the existing natural-language query the LLM planner expects (including which fields — `company_size`, most likely — don't map cleanly and need an explicit warning rather than a silent drop), and how progress events map onto the pipeline's actual stage boundaries.
