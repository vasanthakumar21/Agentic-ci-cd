# `client-azdo-databricks` — Azure DevOps × Databricks sample

A self-contained sample client (R10.3): its own IR (`pipeline.yaml`) and its own
generated artifact. It is the `azure-devops` counterpart to `client-github-aws`
and backs task **7.2** ("generate + validate against the CI mapping table —
design §8a").

```
client-azdo-databricks/
├── pipeline.yaml          # the IR (ci_vendor: azure-devops × target_platform: databricks)
├── azure-pipelines.yml    # the generated, committed, reviewable pipeline artifact
└── README.md              # this file (incl. the isolated-account runbook)
```

## What this sample exercises (design §8a — Azure DevOps column)

| IR concept                     | Azure Pipelines mapping in `azure-pipelines.yml`                      |
| ------------------------------ | --------------------------------------------------------------------- |
| stage order                    | one `stage` per enabled stage, chained by `dependsOn`                 |
| security scans                 | a scan-only stage is folded into a single `security` stage (one step per scan) |
| `timeout_minutes`              | `timeoutInMinutes` on the job                                         |
| manual prod gate / approvals   | a `deployment` job bound to `environment: prod` with a `runOnce` strategy (approvals/checks enforced on the environment) |
| cloud OIDC (`auth: oidc`)      | workload identity federation via a service connection (`AzureCLI@2` + `azureSubscription: azure-wif`) |
| secrets                        | a linked variable group (`variables: - group: cicd-secrets`); members referenced as `$(NAME)` — never literal values (R6) |

> The IR stage `id: security` requests scans but has no `runs` command, so it is
> *folded into* the single synthetic `security` stage rather than emitted twice
> (which would duplicate the stage name and run an empty command).

## Regenerating the artifact

The committed `azure-pipelines.yml` is generated from `pipeline.yaml`. It is
deterministic (R5.3): regenerating produces a byte-identical file. The test
`tests/test_azdo_sample_generation.py` fails if the committed file drifts.

```bash
# from the repo root (PYTHONPATH=generator)
python - <<'PY'
from pathlib import Path
import yaml
from cicdgen.ir import Pipeline
from cicdgen.plugins import StageGraph, load_ci_vendor_plugin

repo = Path(".").resolve()
d = repo / "examples" / "client-azdo-databricks"
pipe = Pipeline.model_validate(yaml.safe_load((d / "pipeline.yaml").read_text()))
plugin = load_ci_vendor_plugin("azure-devops", plugins_root=repo / "plugins")
content = plugin.emit_pipeline_files(StageGraph.from_pipeline(pipe))[0].content
(d / "azure-pipelines.yml").write_text(content, encoding="utf-8", newline="")
PY
```

## YAML validation (local, no account)

`tests/test_azdo_sample_generation.py` validates the pipeline two ways:

1. **Parse** — PyYAML must load it into a mapping with `trigger`, a non-empty
   `stages` list, and a `variables` list linking the variable group.
2. **Structure (design §8a)** — `cicdgen.validate.validate_azure_pipeline_structure`
   asserts the Azure DevOps invariants: `trigger` present; `stages` a non-empty
   list with unique names; every stage except the head has a `dependsOn` that
   resolves to a real *earlier* stage; env stages are `deployment` jobs bound to
   an `environment` with a `runOnce` strategy; `timeoutInMinutes` is positive
   where present; a variable group is linked; and no literal secrets appear
   (only `$(NAME)` references). A companion
   `assert_mapping_table_rows_represented` confirms each §8a row is present.

---

## ⚠️ Runbook — running it in a throwaway (isolated) Azure DevOps project

> **Not executed by this repo.** Actually *running* the pipeline needs a real
> Azure DevOps organization/project, a service connection with workload identity
> federation, configured environment approvals, and a real Databricks workspace.
> Per R9 (isolation) this MUST be done in a **throwaway** project/subscription
> that touches no production or existing TekCor4 resources. The steps below are a
> manual checklist — nothing here runs automatically.

**Prerequisites**
- A throwaway Azure DevOps org/project dedicated to this test.
- A throwaway Azure subscription with an app registration/service connection set
  up for **workload identity federation** (no client secrets), named `azure-wif`
  to match the generated pipeline.
- A throwaway Databricks workspace (the `target_platform`), reachable by its URL/
  token referenced from the variable group (by reference only — R6).

**Steps**
1. **Create the throwaway project** in the isolated org and import this repo (or
   just the generated `azure-pipelines.yml`). Do **not** push to any existing
   repo or the TEK‑969 branch.
2. **Create the service connection** `azure-wif` using workload identity
   federation. The pipeline references it by name only; no secret value is stored
   in any committed file.
3. **Create the variable group** `cicd-secrets` (Pipelines → Library) and add the
   Databricks workspace URL/token and any other secret values there. The pipeline
   links it (`variables: - group: cicd-secrets`) and references members as
   `$(NAME)` — the values never appear in a committed file.
4. **Create the Environments** named in the IR:
   - `dev` — no approvals (auto gate).
   - `prod` — add an **Approvals** check (≥ 2 reviewers, matching
     `approvals.required: 2` / the `platform-approvers` group). This enforces the
     manual prod gate; the pipeline only *references* `environment: prod` on its
     deployment job.
5. **Run the pipeline**: push to `main` (the configured `trigger`). `test` →
   `build` → `deploy-dev` run automatically; `deploy-prod` pauses for the
   environment approval (the manual gate).
6. **Verify isolation (R9.1/R13.2)**: confirm the run touched only the throwaway
   project/subscription/workspace — no existing TekCor4 resource and not the
   TEK‑969 branch.
7. **Tear down** the throwaway project, service connection, variable group, and
   workspace artifacts when finished.

> The scan steps in the `security` stage and the `make …` commands are
> placeholders the target-platform (Databricks) plugin / client gap-fills; swap
> in the real scanners and build/deploy verbs for a full end-to-end run.
