# Schemas — Deferred

`request.schema.json`, `progress.schema.json`, `result.schema.json`, and `error.schema.json` are not present in this folder.

They can't be written honestly yet — there is no request/progress/result/error contract implemented in code for them to describe (see `../JOB_EXECUTION.md` and `../KNOWN_LIMITATIONS.md`). Writing speculative JSON Schema files ahead of the actual implementation would just be a second place for the eventual real contract to drift out of sync with. This is a deliberate deferral, not a missing deliverable.
