"""Terraform (HCL) the gcp-dataproc plugin supplies as its backing IaC.

 — the gcp-dataproc ``IaC`` cell is **Dataproc + WIF**. This module
renders that infrastructure as valid HCL, parameterised by the IR and by Terraform
*variables* — never by literal GCP project ids, secrets, or service account keys.

What the module provisions
--------------------------
* **GCS staging bucket** — holds the versioned PySpark script(s) + extra python
 libs/wheel the ``package`` verb uploads. Versioning is enabled so each upload is
 addressable by an immutable object GENERATION number (the promotable reference —
 lists Dataproc as promotion=yes).
* **Dataproc Serverless batch config** — a ``google_dataproc_batch`` PySpark batch
 (the data-job shape: NO container image, NO cluster to keep warm). The ``deploy``
 verb submits a batch out of band; the ``verify`` verb polls the batch state. A
 client may instead reference/manage a long-running cluster, but the Serverless
 batch is the default no-ops-overhead path.
* **Dataproc service account** — the identity the batch runs as, scoped to read the
 staging bucket and write worker/driver logs.
* **Deploy service account** — the identity CI impersonates to stage artifacts and
 submit the batch, scoped to exactly those roles.
* **Workload Identity Federation (WIF)** — a workload identity pool + an OIDC
 provider bound to the client's CI repository, so the pipeline authenticates
 *keylessly* by federating its CI OIDC token into the deploy service account (no
 long-lived JSON key — the GCP keyless-OIDC story).

Why variables, not literals
----------------------------
The GCP project id, the client CI repo (``org/name``), the OIDC issuer, and the
object keys differ per client and per run. Baking them in would make the module
non-portable and risk committing project-specific values. They are therefore
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

#: Pinned Dataproc Serverless runtime default (reproducible, non-"latest").
_DEFAULT_RUNTIME_VERSION = "2.2"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 GCS / service-account / Dataproc batch ids allow lower-case letters, digits and
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
        "# Inputs for the gcp-dataproc backing infrastructure (generated — R6: no\n"
        "# literal GCP project ids, secrets, or service account keys; the bootstrap\n"
        "# step fills these from local config).\n"
        "\n"
        'variable "project_id" {\n'
        '  description = "GCP project id to provision into."\n'
        "  type        = string\n"
        "}\n"
        "\n"
        'variable "region" {\n'
        '  description = "GCP region to provision into (Dataproc + GCS staging bucket)."\n'
        "  type        = string\n"
        f'  default     = "{region_default}"\n'
        "}\n"
    )
    return header + f"""
variable "project_name" {{
  description = "Project name used as the resource-name prefix."
  type        = string
}}

variable "main_python_file_key" {{
  description = "GCS object key of the main PySpark script the batch runs (uploaded by the package verb)."
  type        = string
  default     = "dataproc/scripts/main.py"
}}

variable "python_file_uris_key" {{
  description = "GCS object key of the extra python libs/wheel bundled for the batch (pythonFileUris)."
  type        = string
  default     = "dataproc/libs/extra-python-libs.zip"
}}

variable "runtime_version" {{
  description = "Dataproc Serverless runtime version for the batch."
  type        = string
  default     = "{_DEFAULT_RUNTIME_VERSION}"
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
    """Render ``main.tf`` — GCS staging + Dataproc batch + SAs + WIF for the project.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The project id and region are referenced via ``var.project_id`` / ``var.region``
 (whose default the IR drives in ``variables.tf``), so no project/region literal is
 hard-coded in a resource. No service account key is ever created or written — CI
 federates its OIDC token into the deploy service account via Workload Identity
 Federation, so the committed HCL carries no secret. There is NO container
 image anywhere — the deployable is a Dataproc PySpark BATCH.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# gcp-dataproc backing infrastructure (generated — design §11.9: Dataproc + WIF).
#
# The data-job shape (like aws-glue / databricks): NO container image — the
# deployable is a Dataproc Serverless PySpark BATCH running a script staged in GCS.
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
  name_prefix     = "{prefix}"
  staging_bucket  = "${{local.name_prefix}}-${{var.project_id}}-dataproc-staging"
  batch_id_prefix = "${{local.name_prefix}}-batch"
}}

# --- GCS: holds the versioned PySpark script + extra libs (package verb) -------
resource "google_storage_bucket" "staging" {{
  name                        = local.staging_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {{
    enabled = true
  }}
}}

# --- Dataproc service account: the identity the batch runs as -------------------
resource "google_service_account" "dataproc" {{
  account_id   = "${{local.name_prefix}}-dataproc"
  display_name = "Dataproc batch runtime SA for ${{local.name_prefix}}"
}}

# The batch SA reads the staging bucket and writes driver/worker logs.
resource "google_storage_bucket_iam_member" "dataproc_reader" {{
  bucket = google_storage_bucket.staging.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${{google_service_account.dataproc.email}}"
}}

resource "google_project_iam_member" "dataproc_worker" {{
  project = var.project_id
  role    = "roles/dataproc.worker"
  member  = "serviceAccount:${{google_service_account.dataproc.email}}"
}}

# --- Dataproc Serverless PySpark batch (deploy verb target) --------------------
# The main + extra python files are VARIABLE keys into the versioned staging bucket
# so no artifact value is committed; the deploy verb submits the batch + the verify
# verb polls its state.
resource "google_dataproc_batch" "app" {{
  batch_id = "${{local.batch_id_prefix}}-tf"
  location = var.region

  runtime_config {{
    version = var.runtime_version
  }}

  environment_config {{
    execution_config {{
      service_account = google_service_account.dataproc.email
      staging_bucket  = google_storage_bucket.staging.name
    }}
  }}

  pyspark_batch {{
    main_python_file_uri = "gs://${{google_storage_bucket.staging.name}}/${{var.main_python_file_key}}"
    python_file_uris     = ["gs://${{google_storage_bucket.staging.name}}/${{var.python_file_uris_key}}"]
  }}

  lifecycle {{
    # The deploy verb submits new batches out of band; ignore drift on the spec.
    ignore_changes = [pyspark_batch, runtime_config]
  }}
}}

# --- Deploy service account: the identity CI impersonates (no JSON key) --------
resource "google_service_account" "deploy" {{
  account_id   = "${{local.name_prefix}}-deploy"
  display_name = "CI deploy SA for the ${{local.name_prefix}} Dataproc batches"
}}

# Stage artifacts into the GCS staging bucket.
resource "google_storage_bucket_iam_member" "deploy_writer" {{
  bucket = google_storage_bucket.staging.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${{google_service_account.deploy.email}}"
}}

# Submit / manage Dataproc batches.
resource "google_project_iam_member" "deploy_dataproc_editor" {{
  project = var.project_id
  role    = "roles/dataproc.editor"
  member  = "serviceAccount:${{google_service_account.deploy.email}}"
}}

# Allow the deploy SA to act as the Dataproc runtime SA (submit under its identity).
resource "google_service_account_iam_member" "deploy_act_as" {{
  service_account_id = google_service_account.dataproc.name
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

 These are the non-secret identifiers the verbs reference (the GCS staging bucket
 the package verb uploads to, the Dataproc runtime SA, the deploy SA email, and
 the WIF provider resource name the CI OIDC step uses). Outputs make the module
 composable without re-deriving names elsewhere.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "staging_bucket" {
  description = "Versioned GCS bucket the package verb uploads the PySpark script/libs to."
  value       = google_storage_bucket.staging.name
}

output "dataproc_service_account_email" {
  description = "Service account the Dataproc batch runs as."
  value       = google_service_account.dataproc.email
}

output "deploy_service_account_email" {
  description = "Service account email CI impersonates (via WIF) to stage + submit batches."
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
# Example tfvars for the gcp-dataproc backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real project specifics belong in the committed example (R6).

project_id   = "REPLACE_ME_PROJECT_ID"
region       = "{region_value}"
project_name = "{prefix}"

# Where the uploaded PySpark script + extra libs live (package verb targets).
main_python_file_key = "dataproc/scripts/main.py"
python_file_uris_key = "dataproc/libs/extra-python-libs.zip"

# Dataproc Serverless runtime version (override as needed).
runtime_version = "{_DEFAULT_RUNTIME_VERSION}"

# CI OIDC trust — scope the WIF provider to your CI repository.
ci_repository   = "your-org/your-repo"
oidc_issuer_uri = "{_DEFAULT_OIDC_ISSUER}"
"""
