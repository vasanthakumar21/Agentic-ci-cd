"""Terraform (HCL) the databricks plugin supplies as its backing IaC.

 — the databricks ``IaC`` cell is **workspace, Unity Catalog, job
(Terraform/API)**. This is the shape MOST unlike aws-ecs: there is NO ECR, NO ECS,
NO container image anywhere. Instead the infrastructure is a Databricks *job* that
runs the uploaded artifact (a wheel on a cluster, or a notebook task), optionally a
Unity Catalog ``catalog``/``schema`` the job writes into, and the workspace host +
token referenced as *variables* (never literals, R6).

This module renders that infrastructure as valid HCL using the official
``databricks/databricks`` Terraform provider, parameterised by the IR and by
Terraform *variables* — never by a literal workspace URL, token, or account id.

What the module provisions
-------------------------------------------------------------------------------
* **workspace wiring** — the ``databricks`` provider is configured from
 ``var.databricks_host`` / ``var.databricks_token`` (host + token *by reference*,
 the token ``sensitive``). This wires the module INTO an existing workspace. See
 the honest-boundary note below: *provisioning the workspace itself* is an
 account-level step (needs account creds / cloud tenancy), deliberately out of
 scope here, while the job + Unity Catalog
 wiring into that workspace is exactly what this module does.
* **databricks_job** — the job the ``deploy`` verb creates/updates and the
 ``verify`` verb runs. It references the uploaded wheel/notebook by a variable
 path (on DBFS / a Unity Catalog Volume), so the committed HCL never hard-codes a
 workspace-specific artifact location. Named from the IR project; an optional
 ``schedule`` (cron) and ``run_as`` are variables the client fills — left unset by
 default so no surprise scheduling is imposed.
* **databricks_schema** (Unity Catalog) — OPTIONAL, gated by
 ``var.enable_unity_catalog`` (default ``true``) via ``count``. Clients without
 Unity Catalog set the flag ``false`` and the schema (and its output) vanish —
 they are not forced into UC. When enabled, the schema is created in an existing
 catalog (``var.unity_catalog``) and names where job output lands.

The honest workspace boundary
-------------------------------------
"Workspace" here means *wiring resources into an existing Databricks workspace*,
not creating the workspace account resource. Creating a workspace is an
account-console / cloud-tenancy operation (``databricks_mws_*`` needs account-level
credentials and a configured cloud account) that a per-client CI module cannot
portably own. We therefore configure the provider against an existing workspace
(host + token by reference) and document workspace *creation* as a manual /
account-level prerequisite in the generated README — rather than emitting
account-level resources that would fail without credentials this module never sees.

Why variables, not literals
----------------------------
The workspace host (``https://<workspace>.cloud.databricks.com``) and the PAT /
OAuth token are per-client secrets; the artifact path, the existing cluster id, and
the Unity Catalog catalog name differ per client. Baking any of them in would make
the module non-portable and risk committing a secret. They are therefore Terraform
``variable`` blocks (the token one ``sensitive = true``) the bootstrap step fills
from the local (git-ignored) config / CI secret store. The only values *derived
from the IR* are the project name (resource naming) and, where present, the cloud
region — both non-secret.

Determinism
------------------
HCL is assembled from fixed, ordered string templates — no timestamps, no random
suffixes, stable block order — so regenerating from the same IR is byte-identical.
"""

from __future__ import annotations

import re

#: Default Databricks runtime for the job's job-cluster — a pinned LTS version so
#: the module is reproducible and does not drift when a new runtime ships.
_DEFAULT_SPARK_VERSION = "15.4.x-scala2.12"

#: Default node type kept as a documented, non-secret constant (overridable var).
_DEFAULT_NODE_TYPE = "i3.xlarge"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 Databricks job / schema names are permissive, but we normalise to
 ``[a-z0-9-]`` for a deterministic, readable prefix across the generated
 resources. Lower-case, collapse any other run of characters to a single
 ``-``, and trim stray dashes.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def _sanitize_schema(raw: str) -> str:
    """Unity Catalog schema names use ``[a-z0-9_]`` — underscores, not dashes."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    return cleaned or "app"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/workspace-specific input (no literals).

 The workspace host + token are variables so the committed module carries no
 workspace URL and no secret; the token var is ``sensitive``. Artifact path,
 catalog, and cluster sizing are variables too so the module is portable.

 Args:
 region: the IR ``cloud.region`` — surfaced only as the ``cloud_region``
 *default* (a region is not a secret, R6). It is informational for the
 Databricks provider (host already encodes the workspace).
 """
    region_default = region if region else "us-east-1"
    header = (
        "# Inputs for the databricks backing infrastructure (generated — R6: no literal\n"
        "# workspace URL, token, or account specifics; the bootstrap step / CI secret\n"
        "# store fills these from local config).\n"
        "\n"
        'variable "databricks_host" {\n'
        '  description = "Databricks workspace URL (e.g. https://my-ws.cloud.databricks.com)."\n'
        "  type        = string\n"
        "}\n"
        "\n"
        'variable "databricks_token" {\n'
        '  description = "Databricks PAT / OAuth token used by Terraform (secret — by reference, R6)."\n'
        "  type        = string\n"
        "  sensitive   = true\n"
        "}\n"
        "\n"
        'variable "cloud_region" {\n'
        '  description = "Informational cloud region (non-secret)."\n'
        "  type        = string\n"
        f'  default     = "{region_default}"\n'
        "}\n"
    )
    return header + f"""
variable "project_name" {{
  description = "Project name used as the resource-name prefix."
  type        = string
}}

variable "artifact_path" {{
  description = "Workspace path to the uploaded artifact the job runs (DBFS or a Unity Catalog Volume, e.g. dbfs:/FileStore/artifacts/app.whl)."
  type        = string
}}

variable "entry_point" {{
  description = "Python wheel entry point the job task invokes."
  type        = string
  default     = "main"
}}

variable "package_name" {{
  description = "Installed wheel package name the job task runs (python_wheel_task.package_name)."
  type        = string
}}

variable "enable_unity_catalog" {{
  description = "Whether to create a Unity Catalog schema for the job output. Set false for workspaces without Unity Catalog (the schema + its output are then omitted)."
  type        = bool
  default     = true
}}

variable "unity_catalog" {{
  description = "Existing Unity Catalog catalog the job schema is created in (used only when enable_unity_catalog = true)."
  type        = string
  default     = "main"
}}

variable "job_schedule_cron" {{
  description = "Optional Quartz cron expression for the job schedule. Empty string (default) leaves the job unscheduled (run on demand via the deploy/verify verbs)."
  type        = string
  default     = ""
}}

variable "job_schedule_timezone" {{
  description = "IANA timezone id for the job schedule (used only when job_schedule_cron is non-empty)."
  type        = string
  default     = "UTC"
}}

variable "run_as_user_name" {{
  description = "Optional user/service-principal the job runs as. Empty string (default) runs as the job owner (the identity Terraform authenticates with)."
  type        = string
  default     = ""
}}

variable "spark_version" {{
  description = "Databricks runtime version for the job cluster."
  type        = string
  default     = "{_DEFAULT_SPARK_VERSION}"
}}

variable "node_type_id" {{
  description = "Worker node type for the job cluster."
  type        = string
  default     = "{_DEFAULT_NODE_TYPE}"
}}

variable "num_workers" {{
  description = "Number of workers for the job cluster."
  type        = number
  default     = 2
}}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — Unity Catalog schema + databricks_job for ``project_name``.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The workspace host + token are referenced via ``var.databricks_host`` /
 ``var.databricks_token`` (the latter ``sensitive``), so no workspace URL or
 token literal is committed. The job runs the uploaded wheel from
 ``var.artifact_path`` on a job cluster — there is deliberately NO container
 image, NO ECR, NO ECS here.
 """
    prefix = _sanitize_name(project_name)
    schema = _sanitize_schema(project_name)

    return f"""\
# databricks backing infrastructure (generated — design §8b: workspace, Unity
# Catalog, job). This is the shape MOST unlike aws-ecs: NO container image, NO ECR,
# NO ECS — the deployable is a Databricks JOB running an uploaded wheel/notebook.
#
# Scope (task 9.2): this module wires a JOB + an OPTIONAL Unity Catalog schema INTO
# an EXISTING workspace. Creating the workspace itself is an account-level step
# (account creds / cloud tenancy) kept out of scope on purpose (R7.1 — honest about
# limits); see README.md. The provider below targets the existing workspace by
# reference (host + token are variables, never literals — R6).
#
# Parameterised entirely by variables (see variables.tf). No literal workspace URL,
# token, or account specifics appear here (R6). Resource names derive from the IR
# project name ({project_name!r}) for a stable, readable layout (R5.3).

terraform {{
  required_version = ">= 1.5.0"
  required_providers {{
    databricks = {{
      source  = "databricks/databricks"
      version = ">= 1.40.0"
    }}
  }}
}}

# --- Workspace wiring -----------------------------------------------------------
# The provider authenticates against an EXISTING workspace. Host + token are
# referenced, never committed as literals (R6); the token var is sensitive.
provider "databricks" {{
  host  = var.databricks_host
  token = var.databricks_token
}}

locals {{
  name_prefix = "{prefix}"
}}

# --- Unity Catalog (OPTIONAL) ---------------------------------------------------
# A schema the job writes into, gated by var.enable_unity_catalog so workspaces
# WITHOUT Unity Catalog aren't forced into it (set the flag false -> no schema).
# Created in an EXISTING catalog (var.unity_catalog); this module does not create
# the catalog or metastore (account-level, out of scope — see README.md).
resource "databricks_schema" "app" {{
  count        = var.enable_unity_catalog ? 1 : 0
  catalog_name = var.unity_catalog
  name         = "{schema}"
  comment      = "Schema for ${{local.name_prefix}} job output (generated)."
}}

# --- The Databricks JOB the deploy verb creates/updates and verify runs ---------
# Runs the uploaded wheel (var.artifact_path) on an ephemeral job cluster. NO image.
# Schedule + run-as are optional (empty defaults) so nothing is imposed implicitly.
resource "databricks_job" "app" {{
  name = "${{local.name_prefix}}-job"

  # Optional run-as identity (omitted when var.run_as_user_name is empty).
  dynamic "run_as" {{
    for_each = var.run_as_user_name != "" ? [var.run_as_user_name] : []
    content {{
      user_name = run_as.value
    }}
  }}

  # Optional schedule (omitted when var.job_schedule_cron is empty — run on demand).
  dynamic "schedule" {{
    for_each = var.job_schedule_cron != "" ? [var.job_schedule_cron] : []
    content {{
      quartz_cron_expression = schedule.value
      timezone_id            = var.job_schedule_timezone
      pause_status           = "UNPAUSED"
    }}
  }}

  job_cluster {{
    job_cluster_key = "${{local.name_prefix}}-cluster"
    new_cluster {{
      spark_version = var.spark_version
      node_type_id  = var.node_type_id
      num_workers   = var.num_workers
    }}
  }}

  task {{
    task_key        = "${{local.name_prefix}}-task"
    job_cluster_key = "${{local.name_prefix}}-cluster"

    python_wheel_task {{
      package_name = var.package_name
      entry_point  = var.entry_point
    }}

    library {{
      whl = var.artifact_path
    }}
  }}
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream.

 These are the non-secret identifiers the verbs reference: the created job id
 (the ``deploy``/``verify`` verbs operate on it) and the Unity Catalog schema
 the job output lands in. Outputs make the module composable without
 re-deriving names elsewhere. No secret is output.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "databricks_job_id" {
  description = "Databricks job id (the deploy/verify verbs run / check this job)."
  value       = databricks_job.app.id
}

output "databricks_job_name" {
  description = "Databricks job name the deploy verb resets/updates."
  value       = databricks_job.app.name
}

# Unity Catalog schema output — null when Unity Catalog is disabled
# (var.enable_unity_catalog = false), so downstream can detect its absence.
output "unity_catalog_schema" {
  description = "Unity Catalog schema the job writes into (catalog.schema), or null when Unity Catalog is disabled."
  value       = var.enable_unity_catalog ? "${var.unity_catalog}.${databricks_schema.app[0].name}" : null
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never a real workspace URL, token, or account
 specific. The token is intentionally shown as a placeholder/ref only.
 """
    region_value = region if region else "us-east-1"
    prefix = _sanitize_name(project_name)
    package = _sanitize_schema(project_name)
    return f"""\
# Example tfvars for the databricks backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real workspace specifics belong in the committed example (R6).

# Workspace URL + token — supply from your secret store / env, NOT committed.
# databricks_host  = "https://your-workspace.cloud.databricks.com"
# databricks_token = "$DATABRICKS_TOKEN"   # by reference — never a literal (R6)

project_name = "{prefix}"
cloud_region = "{region_value}"

# Where the uploaded wheel lives (package verb target) — fill from your workspace.
artifact_path = "dbfs:/FileStore/artifacts/{package}.whl"
package_name  = "{package}"
entry_point   = "main"

# Unity Catalog — OPTIONAL. Set enable_unity_catalog = false for workspaces
# without Unity Catalog (the schema + its output are then omitted entirely).
enable_unity_catalog = true
unity_catalog        = "main"

# Optional job schedule — leave job_schedule_cron empty to run on demand (via the
# deploy/verify verbs). Set a Quartz cron to schedule the job.
# job_schedule_cron     = "0 0 6 * * ?"   # 06:00 daily, for example
# job_schedule_timezone = "UTC"

# Optional run-as identity — leave empty to run as the job owner.
# run_as_user_name = "svc-ci@example.com"

# Job cluster sizing (override as needed).
# spark_version = "{_DEFAULT_SPARK_VERSION}"
# node_type_id  = "{_DEFAULT_NODE_TYPE}"
# num_workers   = 2
"""


def render_readme(project_name: str) -> str:
    """Render a ``README.md`` documenting the Terraform-vs-CLI split + boundaries.

 asks the module to cover "Terraform/API": Terraform *provisions* the
 job + optional Unity Catalog schema, while the plugin's CLI verbs (``package``
 uploads the artifact, ``verify`` runs the job) operate it. This README makes
 that split explicit and is honest about the workspace boundary: creating
 the workspace / metastore is an account-level step this module does not own.

 Deterministic: fixed template, project name only.
 """
    prefix = _sanitize_name(project_name)
    return f"""\
# `iac/databricks` — generated Terraform for the Databricks target (task 9.2)

Generated, committed, reviewable backing infrastructure for project
`{project_name}` (target_platform: `databricks`). Produced by the `databricks`
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
(job name `{prefix}-job`, the artifact path variable) so they stay consistent.

| Concern                         | Terraform (this module) | CLI/API verbs (the plugin)          |
| ------------------------------- | ----------------------- | ----------------------------------- |
| Create / update the job         | ✅ `databricks_job`     | also `deploy` (`databricks jobs`)*  |
| Optional Unity Catalog schema   | ✅ `databricks_schema`  | —                                   |
| Upload the artifact (wheel)     | — (references the path) | ✅ `package` (`databricks fs cp`)   |
| Run the job + check run state   | —                       | ✅ `verify` (`run-now` / `get-run`) |

\\* The module and the `deploy` verb both converge the SAME job (same name +
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
"""
