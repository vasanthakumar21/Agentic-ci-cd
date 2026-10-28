# `bootstrap/` — one-time vendor/cloud wiring

Scripted, repeatable bootstrap of the non-generatable vendor setup (R7), driven by
`.cicd/*.local.yaml` — not a UI click-through.

Properties:

- **Dry-run by default** — prints the exact planned actions (push N secrets, create
  OIDC trust, register runners, set branch protection, optionally create the repo)
  without executing. Live/destructive actions are gated behind `--apply` + explicit
  confirmation (R9.2).
- **Idempotent** — re-running detects existing resources and never duplicates (R7.3).
- **Honest reporting** — clearly separates "automated X" from "manual Y (see
  runbook)" (R7.4).
- **Fail-fast on missing keys** — a missing required local key aborts naming the key;
  it never silently proceeds (R6.5). Secret values are never logged or echoed (R9.3).
