# `iac/databricks` — generated Terraform for the Databricks target (task 9.2)

Generated, committed, reviewable backing infrastructure for project
`minimal-svc` (target_platform: `databricks`). Produced by the `databricks`
plugin's `supply_iac(pipeline)` and byte-for-byte deterministic (R5.3).

```
iac/databricks/
├── main.tf                     # provider (workspace by ref) + databricks_job + optional UC schema
├── variables.tf                # every workspace/client input (no literals — R6)
├── outputs.tf                  # job id / job name / unity catalog schema (null when UC disabled)
├── terraform.tfvars.example    # documented placeholder values (copy to terraform.tfvars)
└── README.md                   # this file
```

## What it provisions (design §8b — workspace, Unity Catalog, job)

| Area          | What                                                                          |
| ------------- | ----------------------------------------------------------------------------- |
| Workspace     | `provider "databricks"` wired to an **existing** workspace by `var.databricks_host` / `var.databricks_token` (token sensitive) |
| Job           | `databricks_job.app` — runs the uploaded wheel (`var.artifact_path`) on an ephemeral job cluster. Optional `schedule` + `run_as`. NO container image. |
| Unity Catalog | `databricks_schema.app` — **optional**, gated by `var.enable_unity_catalog` (default `true`). Omitted entirely when `false`. |

There is deliberately NO ECR, NO ECS, NO container image anywhere — the deployable
is a Databricks *job* (the shape most unlike aws-ecs, design §8b).

## Terraform vs CLI/API — who does what

This target is "Terraform/API": the two cooperate, and share the SAME conventions
(job name `minimal-svc-job`, the artifact path variable) so they stay consistent.

| Concern                         | Terraform (this module) | CLI/API verbs (the plugin)          |
| ------------------------------- | ----------------------- | ----------------------------------- |
| Create / update the job         | ✅ `databricks_job`     | also `deploy` (`databricks jobs`)*  |
| Optional Unity Catalog schema   | ✅ `databricks_schema`  | —                                   |
| Upload the artifact (wheel)     | — (references the path) | ✅ `package` (`databricks fs cp`)   |
| Run the job + check run state   | —                       | ✅ `verify` (`run-now` / `get-run`) |

\* The module and the `deploy` verb both converge the SAME job (same name +
artifact-path convention). Terraform is the declarative source of truth for the
job + UC wiring; the CLI verbs upload the artifact and run/verify the job in CI.
Pick one owner for the job definition per client (Terraform for drift-controlled
infra, or the `deploy` verb for a lighter CLI-only flow) — both are provided so a
client can choose.

## ⚠️ Honest workspace boundary (R7.1)

**This module does not create the Databricks workspace or the Unity Catalog
metastore/catalog.** Those are account-console / cloud-tenancy operations
(`databricks_mws_*`, metastore assignment) that need account-level credentials and
a configured cloud account — things a per-client CI module cannot portably own, and
which would fail without credentials this module never sees. They are a **manual /
account-level prerequisite**:

1. Create (or reuse) a Databricks workspace in the account console / via the
   account-level Terraform provider (out of scope here).
2. (If using Unity Catalog) create the metastore + catalog and assign it to the
   workspace; set `var.unity_catalog` to that catalog. Otherwise set
   `enable_unity_catalog = false`.
3. Provide `databricks_host` + `databricks_token` for the existing workspace (by
   reference — from your secret store, never committed; R6).

This module then wires the **job + optional schema** into that existing workspace.

## Local validation (no workspace, no terraform binary)

A real `terraform plan` needs the terraform binary **and** a throwaway workspace
(kept out of this environment by R9). The repo validates the HCL structurally via
`cicdgen.validate.validate_hcl_structure` and the determinism / contract tests in
`tests/test_plugin_databricks.py` and `tests/test_databricks_terraform.py`.
