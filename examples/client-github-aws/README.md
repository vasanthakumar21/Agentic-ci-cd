# `client-github-aws` — GitHub Actions × AWS ECS sample

A self-contained sample client (R10.3): its own IR (`pipeline.yaml`) and its own
generated artifact. It is the `github` counterpart to `client-azdo-databricks`
and backs task **6.2** ("generate for a sample IR; validate the YAML; run in the
isolated account").

```
client-github-aws/
├── pipeline.yaml                      # the IR (ci_vendor: github × target_platform: aws-ecs)
├── .github/workflows/ci.yml           # the generated, committed, reviewable workflow artifact
├── Dockerfile                         # the generated, resilient build artifact (aws-ecs build verb, R7.1)
├── iac/aws-ecs/                       # the generated Terraform (ECR/ECS/IAM-OIDC) + its runbook (task 8.2)
│   ├── main.tf / variables.tf / outputs.tf / terraform.tfvars.example
│   └── README.md                      # terraform plan + verb-execution runbook (isolated account)
└── README.md                          # this file (incl. the isolated-account runbook)
```

> **Target-platform artifacts (task 8.2).** The `aws-ecs` plugin additionally emits
> the Terraform under [`iac/aws-ecs/`](iac/aws-ecs/README.md) and the resilient
> [`Dockerfile`](Dockerfile). Those are validated locally (determinism + structural
> HCL checks) by `tests/test_aws_ecs_terraform.py`; the live `terraform plan` +
> docker build/push + ECS deploy are documented manual steps for a throwaway account
> (terraform & docker are not installed here, and no isolated account is configured —
> R9). See the [iac/aws-ecs runbook](iac/aws-ecs/README.md).

## What this sample exercises (design §8a)

| IR concept                     | GitHub Actions mapping in `ci.yml`                                  |
| ------------------------------ | ------------------------------------------------------------------- |
| stage order                    | one `job` per enabled stage, chained by `needs`                     |
| security scans                 | a single `security` job with a step per requested scan              |
| `timeout_minutes`              | `timeout-minutes` on the job                                        |
| manual prod gate / approvals   | `environment: prod` (reviewers enforced at the GitHub env level)    |
| cloud OIDC (`auth: oidc`)      | `permissions: id-token: write` + `aws-actions/configure-aws-credentials` |
| secrets                        | `${{ secrets.NAME }}` references only — never literal values (R6)   |

## Regenerating the artifact

The committed `.github/workflows/ci.yml` is generated from `pipeline.yaml`. It is
deterministic (R5.3): regenerating produces a byte-identical file. The test
`tests/test_github_sample_generation.py` fails if the committed file drifts.

```bash
# from the repo root
python - <<'PY'
from pathlib import Path
import yaml
from cicdgen.ir import Pipeline
from cicdgen.plugins import StageGraph, load_ci_vendor_plugin

repo = Path(".").resolve()
d = repo / "examples" / "client-github-aws"
pipe = Pipeline.model_validate(yaml.safe_load((d / "pipeline.yaml").read_text()))
plugin = load_ci_vendor_plugin("github", plugins_root=repo / "plugins")
content = plugin.emit_pipeline_files(StageGraph.from_pipeline(pipe))[0].content
(d / ".github" / "workflows" / "ci.yml").write_text(content, encoding="utf-8", newline="")
PY
```

## YAML validation (local, no account)

The workflow is validated two ways by `tests/test_github_sample_generation.py`:

1. **Parse** — PyYAML must load it (and the top-level `on:` trigger must survive
   as the string `"on"`, not the YAML‑1.1 boolean `true`).
2. **Structure** — `cicdgen.validate.validate_workflow_structure` asserts the
   GitHub Actions invariants: `on`/`jobs` present, every job has `runs-on` +
   non-empty `steps`, all `needs` references resolve, environment keys are valid,
   and `permissions.id-token: write` is granted because OIDC login steps exist.
3. **Schema (when online)** — the workflow is validated against the published
   [SchemaStore GitHub Actions schema](https://json.schemastore.org/github-workflow.json)
   with `jsonschema`. Offline (e.g. in an isolated environment) this step is
   skipped and the structural check stands in.

---

## ⚠️ Runbook — running it in a throwaway (isolated) GitHub account

> **Not executed by this repo.** Actually *running* the workflow needs a real
> GitHub account/org, configured environment reviewers, and real cloud
> credentials. Per R9 (isolation) this MUST be done in a **throwaway** account
> that touches no production or existing TekCor4 resources. The steps below are a
> manual checklist — nothing here runs automatically, and no real network/account
> operations are performed by the generator or the tests.

**Prerequisites**
- A throwaway GitHub org/account dedicated to this test (nothing shared with prod).
- A throwaway AWS account with an OIDC identity provider for GitHub Actions
  (`token.actions.githubusercontent.com`) and a deploy role scoped to that account only.

**Steps**
1. **Create the throwaway repo** in the isolated org, e.g. `isolated-demos/orders-api`.
2. **Copy the generated workflow** into the repo at `.github/workflows/ci.yml`
   (the exact file committed here). Commit it on a branch, open a PR — do **not**
   push to any existing repo or the TEK‑969 branch.
3. **Create the GitHub Environments** named in the IR:
   - `dev` — no required reviewers (auto gate).
   - `prod` — add **required reviewers** (≥ 2, matching `approvals.required: 2`
     and the `platform-approvers` group). This is what enforces the manual prod
     gate; the workflow only *references* `environment: prod`.
4. **Configure the OIDC trust + secret** (by reference only — R6):
   - In AWS, create/confirm the GitHub OIDC provider and a deploy role trusting
     `repo:isolated-demos/orders-api:*` (tighten to specific environments if you prefer).
   - In the GitHub repo (or environment) secrets, set `AWS_DEPLOY_ROLE_ARN` to that
     role's ARN. The workflow consumes it as `${{ secrets.AWS_DEPLOY_ROLE_ARN }}` —
     the value never appears in any committed file.
5. **Trigger the workflow**:
   - Push / open the PR to `main` (triggers `push` + `pull_request`), or use the
     **Run workflow** button (`workflow_dispatch`).
   - `test` → `build` → `deploy-dev` run automatically; `deploy-prod` pauses for a
     required reviewer to approve (the manual gate).
6. **Verify isolation (R9.1/R13.2)**: confirm the run touched only the throwaway
   repo/account — no existing TekCor4 repo and not the TEK‑969 branch.
7. **Tear down** the throwaway repo, role, and secrets when finished.

> The scan steps in the `security` job and the `make …` commands are placeholders
> the target-platform plugin / client gap-fills; swap in the real scanners and
> build/deploy verbs for a full end-to-end run.
