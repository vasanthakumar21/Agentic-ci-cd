# Agentic, Platform-Agnostic CI/CD Generator

A **pipeline-as-data compiler**. A single declarative config (the Intermediate
Representation, or **IR**) names a **CI vendor** and a **target platform**
*independently*; an agent plus deterministic templates compile it into concrete
pipeline files, platform-specific deploy steps, and backing IaC.

Switching either axis = change the IR + regenerate.

> **Isolation (R9.1):** This is a greenfield product developed in its own isolated
> repo. It must **not** touch the existing TekCor4 repos or the TEK-969 DTUK work.

## Two independent axes

| Axis              | Meaning                        | Examples (pluggable)                                      |
|-------------------|--------------------------------|-----------------------------------------------------------|
| `ci_vendor`       | *where* the pipeline runs      | GitHub Actions, GitLab CI, Azure DevOps, Bitbucket, Jenkins |
| `target_platform` | *what* gets built and deployed | AWS ECS, Databricks, Azure Functions, Kubernetes, dbt       |

Any valid combination is generatable (e.g. `azure-devops × databricks`,
`github × aws-ecs`). Adding a new CI vendor or target platform is adding a
**provider plugin**, not rewriting the core.

## Folder layout (design §2)

```
agentic-cicd-generator/
├── ir/              # IR JSON Schema + example pipeline.yaml contracts
├── plugins/
│   ├── ci/          # CI-vendor plugins (github, azure-devops, …)
│   └── target/      # target-platform plugins (aws-ecs, databricks, …)
├── templates/       # deterministic per-vendor / per-platform render templates
├── generator/       # generator core (Python): validate → resolve → render → write
│   └── cicdgen/     # importable package
├── bootstrap/       # one-time, idempotent, dry-run-default vendor wiring
├── docs/            # IR reference, local-config reference, plugin-authoring guide
├── examples/        # per-client, self-contained IRs (R10.3 — no cross-contamination)
└── tests/           # unit + property tests for the generator core
```

Local, per-client config lives in `.cicd/*.local.yaml` (git-ignored). Committed
`*.example` files document every key. Generated files reference secrets **by key**,
never by value (R6).

## Developer setup

```bash
# Requires Python >= 3.11. Using uv (recommended) or pip.
uv sync                      # or: pip install -e ".[dev]"
pytest                       # run the test suite
ruff check . && mypy .       # lint + type-check
```

## Documentation

- `docs/sharing-and-onboarding.md` — first-time developer guide: set up, generate,
  and deploy a service (start here).
- `docs/auth-modes.md` — OIDC (keyless) vs static-access-key authentication.
- `docs/ir-reference.md` — every field of `pipeline.yaml`.
- `docs/plugin-authoring-guide.md` — add a new CI vendor or target platform.
- `docs/local-config-reference.md` — the git-ignored `.cicd/*.local.yaml` files.
- `examples/client-sales-pipeline/` — a worked multi-service example
  (Lambda + Step Functions) with `generate.py` and `deploy.py`.

## Status

Implemented and test-covered: the IR schema + validator, the two plugin families
(5 CI vendors, ~20 target platforms across AWS/Azure/GCP/Kubernetes), the
generator core (validate → dispatch → render → write + manifest), the bootstrapper,
and the agent driver. Run `pytest` for the full suite.
