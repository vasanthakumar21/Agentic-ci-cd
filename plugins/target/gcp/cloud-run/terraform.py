"""Terraform (HCL) the gcp-cloud-run plugin supplies as its backing IaC.

 — the gcp-cloud-run ``IaC`` cell is **Cloud Run + WIF/OIDC**. This
module renders that infrastructure as valid HCL, parameterised by the IR and by
Terraform *variables* — never by literal GCP project ids, secrets, or service
account keys.

What the module provisions
--------------------------
* **Artifact Registry** — a Docker repository to hold the image the pipeline builds
 and pushes (the ``package`` verb target).
* **Cloud Run** — a v2 Cloud Run service that runs the pushed image (the ``deploy``
 verb target).
* **Deploy service account** — the identity CI impersonates to push to Artifact
 Registry and deploy the Cloud Run service, scoped to exactly those roles.
* **Workload Identity Federation (WIF)** — a workload identity pool + an OIDC
 provider bound to the client's CI repository, so the pipeline authenticates
 *keylessly* by federating its CI OIDC token into the deploy service account (no
 long-lived JSON key — the GCP keyless-OIDC story). The repo is a variable so no
 org/repo literal is committed.

Why variables, not literals
----------------------------
The GCP project id, the client CI repo (``org/name``), the OIDC issuer, and the
image tag differ per client and per run. Baking them in would make the module
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

#: The OIDC audience GitHub's token carries when federated into GCP WIF. Kept as a
#: documented, non-secret default so the module applies without committing specifics.
_DEFAULT_OIDC_ISSUER = "https://token.actions.githubusercontent.com"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 Artifact Registry / Cloud Run / service-account ids allow lower-case letters,
 digits and ``-``; project names are user-defined. We lower-case, replace any
 other run of characters with a single ``-``, and trim stray dashes so the prefix
 is deterministic and valid across the GCP resources this module creates.
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
        "# Inputs for the gcp-cloud-run backing infrastructure (generated — R6: no\n"
        "# literal GCP project ids, secrets, or service account keys; the bootstrap\n"
        "# step fills these from local config).\n"
        "\n"
        'variable "project_id" {\n'
        '  description = "GCP project id to provision into."\n'
        "  type        = string\n"
        "}\n"
        "\n"
        'variable "region" {\n'
        '  description = "GCP region to provision into (Cloud Run + Artifact Registry)."\n'
        "  type        = string\n"
        f'  default     = "{region_default}"\n'
        "}\n"
    )
    return header + """
variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "image_tag" {
  description = "Container image tag the Cloud Run service runs (e.g. a git SHA)."
  type        = string
  default     = "latest"
}

variable "container_port" {
  description = "Port the container listens on (Cloud Run sends traffic here)."
  type        = number
  default     = 8080
}

variable "ci_repository" {
  description = "Client CI repository the WIF provider trusts (e.g. org/name)."
  type        = string
}

variable "oidc_issuer_uri" {
  description = "CI provider OIDC issuer URI (e.g. https://token.actions.githubusercontent.com)."
  type        = string
  default     = "https://token.actions.githubusercontent.com"
}

variable "allow_unauthenticated" {
  description = "Whether the Cloud Run service allows unauthenticated invocations."
  type        = bool
  default     = false
}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — Artifact Registry + Cloud Run + SA + WIF for the project.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The project id and region are referenced via ``var.project_id`` / ``var.region``
 (whose default the IR drives in ``variables.tf``), so no project/region literal is
 hard-coded in a resource. No service account key is ever created or written — CI
 federates its OIDC token into the deploy service account via Workload Identity
 Federation, so the committed HCL carries no secret.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# gcp-cloud-run backing infrastructure (generated — design §11.9: Cloud Run + WIF/OIDC).
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
  ar_repo_id      = "${{local.name_prefix}}-repo"
  service_name    = "${{local.name_prefix}}-service"
  image_reference = "${{var.region}}-docker.pkg.dev/${{var.project_id}}/${{google_artifact_registry_repository.app.repository_id}}/${{local.name_prefix}}:${{var.image_tag}}"
}}

# --- Artifact Registry: holds the image the pipeline builds + pushes (package) --
resource "google_artifact_registry_repository" "app" {{
  location      = var.region
  repository_id = local.ar_repo_id
  format        = "DOCKER"
  description   = "Images for the ${{local.name_prefix}} Cloud Run service."
}}

# --- Cloud Run v2 service: runs the pushed image (deploy verb target) ----------
resource "google_cloud_run_v2_service" "app" {{
  name     = local.service_name
  location = var.region

  template {{
    service_account = google_service_account.deploy.email

    containers {{
      image = local.image_reference

      ports {{
        container_port = var.container_port
      }}
    }}
  }}
}}

# Optionally expose the service publicly (off by default — least privilege).
resource "google_cloud_run_v2_service_iam_member" "invoker" {{
  count    = var.allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.app.name
  location = google_cloud_run_v2_service.app.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}}

# --- Deploy service account: the identity CI impersonates (no JSON key) --------
resource "google_service_account" "deploy" {{
  account_id   = "${{local.name_prefix}}-deploy"
  display_name = "CI deploy SA for the ${{local.name_prefix}} Cloud Run service"
}}

# Push images to Artifact Registry.
resource "google_project_iam_member" "deploy_ar_writer" {{
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${{google_service_account.deploy.email}}"
}}

# Deploy / update the Cloud Run service.
resource "google_project_iam_member" "deploy_run_admin" {{
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${{google_service_account.deploy.email}}"
}}

# Allow the service to act as the runtime service account.
resource "google_service_account_iam_member" "deploy_act_as" {{
  service_account_id = google_service_account.deploy.name
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

 These are the non-secret identifiers the verbs reference (the Artifact Registry
 repo + image path, the Cloud Run service name + region, the deploy service
 account email, and the WIF provider resource name the CI OIDC step uses).
 Outputs make the module composable without re-deriving names elsewhere.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "artifact_registry_repository" {
  description = "Artifact Registry repository id to tag/push images to (package verb)."
  value       = google_artifact_registry_repository.app.repository_id
}

output "image_repository" {
  description = "Fully-qualified Artifact Registry image repo path (region-docker.pkg.dev/...)."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}/${local.name_prefix}"
}

output "cloud_run_service_name" {
  description = "Cloud Run service name the deploy/verify verbs target."
  value       = google_cloud_run_v2_service.app.name
}

output "cloud_run_region" {
  description = "Region the Cloud Run service runs in (deploy/verify verbs)."
  value       = google_cloud_run_v2_service.app.location
}

output "deploy_service_account_email" {
  description = "Service account email CI impersonates (via WIF) to push + deploy."
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
# Example tfvars for the gcp-cloud-run backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real project specifics belong in the committed example (R6).

project_id   = "REPLACE_ME_PROJECT_ID"
region       = "{region_value}"
project_name = "{prefix}"
image_tag    = "latest"

# CI OIDC trust — scope the WIF provider to your CI repository.
ci_repository   = "your-org/your-repo"
oidc_issuer_uri = "{_DEFAULT_OIDC_ISSUER}"

# Expose the service publicly only if you really want unauthenticated access.
allow_unauthenticated = false
"""
