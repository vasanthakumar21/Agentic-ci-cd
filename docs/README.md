# `docs/` — Agentic, Vendor-Agnostic CI/CD Generator

This directory is the product documentation for the CI/CD generator: a
*pipeline-as-data compiler* with two independent plugin axes — `ci_vendor`
(where the pipeline runs) × `target_platform` (what it builds and deploys). Any
valid combination of the two is generatable, because neither axis knows about
the other.

Start with the architecture overview, then dive into the reference you need.

## Index

| Document | What it covers |
|----------|----------------|
| [ARCHITECTURE_AND_STATUS.md](ARCHITECTURE_AND_STATUS.md) | The end-to-end explanation: what the product is, the full data flow, where the code lives, the design principles and the requirements they map to, current status (task-by-task), the real test count, and how to run it. **Read this first.** |
| [ir-reference.md](ir-reference.md) | The IR (`pipeline.yaml`) reference — every field on both axes, enums, required-vs-optional, and documented defaults. Derived from `ir/pipeline.schema.json` + `generator/cicdgen/ir/models.py`. |
| [local-config-reference.md](local-config-reference.md) | The `.cicd/*.local.yaml` reference (`bootstrap.local.yaml` + `secrets.local.yaml`) — every key, value-free. Emphasises secrets-by-reference (R6) and never-commit-values (R6.6/R9.3). |
| [bootstrap-guide.md](bootstrap-guide.md) | How bootstrap works — dry-run by default, `--apply` + confirm, idempotency, the `Executor` seam, the automated-vs-manual report, and the irreducible manual steps. |
| [plugin-authoring-guide.md](plugin-authoring-guide.md) | How to add a CI-vendor or target-platform plugin **without touching the core** (R3.4): the directory + `get_plugin()` convention, the plugin contracts, the data types, the §8a/§8b mapping tables, and the determinism + secrets rules. |
| [migration-runbook-template.md](migration-runbook-template.md) | The template/explanation of the `MIGRATION_RUNBOOK.md` emitted on an axis swap — what changed, required `.local` keys for the new axes, irreducible manual steps, and the ordered checklist. |

## The one-sentence model

> A developer declares intent in a single vendor-neutral `pipeline.yaml` (the IR);
> the generator validates it and dispatches to two named plugins — one CI vendor,
> one target platform — to compile a complete, committed, reviewable CI/CD setup,
> with every secret referenced by name and nothing hand-authored.
