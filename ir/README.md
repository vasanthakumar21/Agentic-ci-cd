# `ir/` — Intermediate Representation

The single vendor-neutral source of truth (R1). Holds:

- `pipeline.schema.json` — the published JSON Schema for `pipeline.yaml` (task 2.1).
- Example / reference IR shapes documenting the two independent axes
  (`ci_vendor` × `target_platform`), environments, stages, approvals, timeouts,
  retries, gates, security scans, thresholds, and notifications.

The IR is the **only** file a developer edits to change pipeline behaviour; no
vendor YAML is hand-edited. Invalid IR fails fast with a precise, field-level error
(mirroring the tekflow-ingest Pydantic pattern).
