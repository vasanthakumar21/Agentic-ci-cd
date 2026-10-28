"""Terraform (HCL) the gcp-dataflow plugin supplies as its backing IaC.

 — the gcp-dataflow ``IaC`` cell is **Dataflow + WIF**. This module
renders that infrastructure as valid HCL, parameterised by the IR and by Terraform
*variables* — never by literal GCP project ids, secrets, or service account keys.

What the module provisions
--------------------------
* **GCS bucket** — holds the staged templates, the staging location and the temp
 location the ``package`` verb uploads to / the Dataflow job uses. Versioning is
 enabled so each template upload is addressable by an immutable object GENERATION
 number (the promotable reference — lists Dataflow as promotion=yes).
* **Dataflow worker service account** — the identity Dataflow workers run as,
 scoped to read the bucket, run as a Dataflow worker, and write logs.
* **Deploy service account** — the identity CI impersonates to stage templates and
 launch Dataflow jobs, scoped to exactly those roles.
* **Workload Identity Federation (WIF)** — a workload identity pool + an OIDC
 provider bound to the client's CI repository, so the pipeline authenticates
 *keylessly* by federating its CI OIDC token into the deploy service account (no
 long-lived JSON key — the GCP keyless-OIDC story).

There is deliberately NO container image / registry here: Dataflow launches from a
*staged template*, so the data-job shape holds (a flex-template image, if ever used,
is a by-reference knob the client supplies, never provisioned by this module).

Why variables, not literals
----------------------------
The GCP project id, the client CI repo (``org/name``), the OIDC issuer, and the
template/temp locations differ per client and per run. Baking them in would make the
module non-portable and risk committing project-specific values. They are therefore
Terraform ``variable`` blocks the bootstrap step fills from the local (git-ignored)
config. The only values *derived from the IR* are the project name (resource naming)
and the region (provider + defaults), both non-secret.

Determinism
------------------
HCL is assembled from fixed, ordered string templates — no timestamps, no random
suffixes, stable block order — so regenerating from the same IR is byte-identical.
"""

from __future__ import annotations

import re

#: The OIDC issuer GitHub's token carries when federated into GCP WIF. Kept as a
#: documented, non-secret default so the module applies without committing specifics.
_DEFAULT_OIDC_ISSUER = "https://token.actions.githubusercontent.com"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 GCS / service-account / Dataflow job ids allow lower-case letters, digits and
 ``-``; project names are user-defined. We lower-case, replace any other run of
 characters with a single ``-``, and trim stray dashes so the prefix is
 deterministic and valid across the GCP resources this module creates.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/project-specific input (no literals).

 Keeping these as variables (not hard-coded values) is what makes the module
 portable and keeps project ids / CI repos / service account keys out of the
 committed files. The bootstrap step supplies them from the local git-ignored
 config.

 Args:
 region: the IR ``cloud.region`` — used only as the ``region`` *default* so
 the IR drives the region without hard-coding it in a resource. A region
 is not a secret, so surfacing it as a default is safe.
 """
    region_default = region if region else "us-central1"
    header = (
        "# Inputs for the gcp-dataflow backing infrastructure (generated — R6: no\n"
        "# literal GCP project ids, secrets, or service account keys; the bootstrap\n"
        "# step fills these from local config).\n"
        "\n"
        'variable "project_id" {\n'
        '  description = "GCP project id to provision into."\n'
        "  type        = string\n"
        "}\n"
        "\n"
        'variable "region" {\n'
        '  description = "GCP region to provision into (Dataflow + GCS bucket)."\n'
        "  type        = string\n"
        f'  default     = "{region_default}"\n'
        "}\n"
    )
    return header + f"""
variable "project_name" {{
  description = "Project name used as the resource-name prefix."
  type        = string
}}

variable "template_key" {{
  description = "GCS object key of the staged Dataflow template the job launches from (uploaded by the package verb)."
  type        = string
  default     = "dataflow/templates/main.json"
}}

variable "ci_repository" {{
  description = "Client CI repository the WIF provider trusts (e.g. org/name)."
  type        = string
}}

variable "oidc_issuer_uri" {{
  description = "CI provider OIDC issuer URI (e.g. https://token.actions.githubusercontent.com)."
  type        = string
  default     = "{_DEFAULT_OIDC_ISSUER}"
}}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — GCS bucket + Dataflow worker SA + deploy SA + WIF.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The project id and region are referenced via ``var.project_id`` / ``var.region``
 (whose default the IR drives in ``variables.tf``), so no project/region literal is
 hard-coded in a resource. No service account key is ever created or written — CI
 federates its OIDC token into the deploy service account via Workload Identity
 Federation, so the committed HCL carries no secret. There is NO container
 image anywhere — the deployable is a Dataflow job launched from a staged TEMPLATE.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# gcp-dataflow backing infrastructure (generated — design §11.9: Dataflow + WIF).
#
# The data-job shape (like aws-glue / databricks): NO container image — the
# deployable is a Dataflow job launched from a staged TEMPLATE in GCS.
#
# Parameterised entirely by variables (see variables.tf). No literal GCP project
# ids, service account keys, or secrets appear here (R6). Resource names derive
# from the IR project name ({project_name!r}) for a stable, readable layout (R5.3).

terraform {{
  required_version = ">= 1.5.0"
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = ">= 5.0"
    }}
  }}
}}

provider "google" {{
  project = var.project_id
  region  = var.region
}}

# Project number is resolved at apply time — never committed as a literal (R6).
data "google_project" "current" {{
  project_id = var.project_id
}}

locals {{
  name_prefix      = "{prefix}"
  dataflow_bucket  = "${{local.name_prefix}}-${{var.project_id}}-dataflow"
  job_name_prefix  = "${{local.name_prefix}}-job"
}}

# --- GCS: holds staged templates + staging/temp locations (package verb) -------
resource "google_storage_bucket" "dataflow" {{
  name                        = local.dataflow_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {{
    enabled = true
  }}
}}

# --- Dataflow worker service account: the identity workers run as --------------
resource "google_service_account" "worker" {{
  account_id   = "${{local.name_prefix}}-dataflow-worker"
  display_name = "Dataflow worker SA for ${{local.name_prefix}}"
}}

resource "google_project_iam_member" "worker_dataflow_worker" {{
  project = var.project_id
  role    = "roles/dataflow.worker"
  member  = "serviceAccount:${{google_service_account.worker.email}}"
}}

resource "google_storage_bucket_iam_member" "worker_bucket_access" {{
  bucket = google_storage_bucket.dataflow.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${{google_service_account.worker.email}}"
}}

# --- Deploy service account: the identity CI impersonates (no JSON key) --------
resource "google_service_account" "deploy" {{
  account_id   = "${{local.name_prefix}}-deploy"
  display_name = "CI deploy SA for the ${{local.name_prefix}} Dataflow jobs"
}}

# Stage templates/artifacts into the GCS bucket.
resource "google_storage_bucket_iam_member" "deploy_writer" {{
  bucket = google_storage_bucket.dataflow.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${{google_service_account.deploy.email}}"
}}

# Launch / manage Dataflow jobs.
resource "google_project_iam_member" "deploy_dataflow_developer" {{
  project = var.project_id
  role    = "roles/dataflow.developer"
  member  = "serviceAccount:${{google_service_account.deploy.email}}"
}}

# Allow the deploy SA to act as the Dataflow worker SA (launch under its identity).
resource "google_service_account_iam_member" "deploy_act_as" {{
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${{google_service_account.deploy.email}}"
}}

# --- Workload Identity Federation: keyless CI auth (no JSON key) ---------------
resource "google_iam_workload_identity_pool" "ci" {{
  workload_identity_pool_id = "${{local.name_prefix}}-ci-pool"
  display_name              = "CI pool for ${{local.name_prefix}}"
}}

resource "google_iam_workload_identity_pool_provider" "ci" {{
  workload_identity_pool_id          = google_iam_workload_identity_pool.ci.workload_identity_pool_id
  workload_identity_pool_provider_id = "${{local.name_prefix}}-ci-provider"
  display_name                       = "CI OIDC provider for ${{local.name_prefix}}"

  attribute_mapping = {{
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }}

  # Only tokens from the client's CI repository may federate (scoped trust).
  attribute_condition = "assertion.repository == \\"${{var.ci_repository}}\\""

  oidc {{
    issuer_uri = var.oidc_issuer_uri
  }}
}}

# Bind the CI repo's federated identity to the deploy service account (keyless).
resource "google_service_account_iam_member" "wif_bind" {{
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${{google_iam_workload_identity_pool.ci.name}}/attribute.repository/${{var.ci_repository}}"
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream.

 These are the non-secret identifiers the verbs reference (the GCS bucket the
 package verb stages templates to, the Dataflow worker SA, the deploy SA email,
 and the WIF provider resource name the CI OIDC step uses). Outputs make the
 module composable without re-deriving names elsewhere.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "dataflow_bucket" {
  description = "Versioned GCS bucket the package verb stages templates/artifacts to."
  value       = google_storage_bucket.dataflow.name
}

output "worker_service_account_email" {
  description = "Service account the Dataflow workers run as."
  value       = google_service_account.worker.email
}

output "deploy_service_account_email" {
  description = "Service account email CI impersonates (via WIF) to stage + launch jobs."
  value       = google_service_account.deploy.email
}

output "workload_identity_provider" {
  description = "WIF provider resource name the CI OIDC step federates against."
  value       = google_iam_workload_identity_pool_provider.ci.name
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never a real GCP project id, CI repo, or key.
 """
    region_value = region if region else "us-central1"
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the gcp-dataflow backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real project specifics belong in the committed example (R6).

project_id   = "REPLACE_ME_PROJECT_ID"
region       = "{region_value}"
project_name = "{prefix}"

# Where the uploaded Dataflow template lives (package verb targets).
template_key = "dataflow/templates/main.json"

# CI OIDC trust — scope the WIF provider to your CI repository.
ci_repository   = "your-org/your-repo"
oidc_issuer_uri = "{_DEFAULT_OIDC_ISSUER}"
"""
