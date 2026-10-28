"""Terraform (HCL) the gcp-gke plugin supplies as its backing IaC.

 — the gcp-gke ``IaC`` cell is **GKE refs + Workload Identity**. This
module renders that infrastructure as valid HCL, parameterised by the IR and by
Terraform *variables* — never by literal GCP project ids, secrets, or service
account keys.

What the module provisions / references
---------------------------------------
* **GKE reference** — the module does NOT create a cluster (clusters are long-lived,
 client-owned). It *references* an existing cluster by name/location via a
 ``google_container_cluster`` data source so the workload and the Workload Identity
 binding attach to the real cluster (no cluster is ever created here).
* **Artifact Registry** — a Docker repository to hold the image the pipeline builds
 and pushes (the ``package`` verb target), mirroring gcp-cloud-run.
* **Deploy service account** — the identity CI impersonates to push to Artifact
 Registry and run ``kubectl``/``helm`` against the cluster, scoped to exactly those
 roles (``artifactregistry.writer`` + ``container.developer``).
* **GKE Workload Identity** — a *runtime* Google service account for the workload
 plus the IAM binding that lets the Kubernetes ServiceAccount
 (``system:serviceaccount:<ns>:<ksa>``) impersonate it
 (``roles/iam.workloadIdentityUser`` on the member
 ``serviceAccount:<project>.svc.id.goog[<ns>/<ksa>]``). This is the GKE-native
 keyless way for a pod to act as a Google service account — the GCP analogue of
 EKS IRSA, and what makes this plugin GKE-specific.
* **Workload Identity Federation (WIF)** — a workload identity pool + an OIDC
 provider bound to the client's CI repository, so the pipeline authenticates
 *keylessly* by federating its CI OIDC token into the deploy service account (no
 long-lived JSON key).

Why variables, not literals
----------------------------
The GCP project id, the GKE cluster name/location, the client CI repo, the OIDC
issuer, the namespace, and the Kubernetes ServiceAccount name differ per client and
per run. Baking them in would make the module non-portable and risk committing
project-specific values. They are therefore Terraform ``variable`` blocks the
bootstrap step fills from the local (git-ignored) config. The only values *derived
from the IR* are the project name (resource naming) and the region (provider +
defaults), both non-secret.

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

 Artifact Registry / service-account ids allow lower-case letters, digits and
 ``-``; project names are user-defined. We lower-case, replace any other run of
 characters with a single ``-``, and trim stray dashes so the prefix is
 deterministic and valid across the GCP resources this module creates.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/project-specific input (no literals).

 Keeping these as variables (not hard-coded values) is what makes the module
 portable and keeps project ids / cluster names / CI repos / service account keys
 out of the committed files. The bootstrap step supplies them from the local
 git-ignored config.

 Args:
 region: the IR ``cloud.region`` — used only as the ``region`` *default* so
 the IR drives the region without hard-coding it in a resource. A region
 is not a secret, so surfacing it as a default is safe.
 """
    region_default = region if region else "us-central1"
    header = (
        "# Inputs for the gcp-gke backing infrastructure (generated — R6: no literal\n"
        "# GCP project ids, secrets, or service account keys; the bootstrap step fills\n"
        "# these from local config).\n"
        "\n"
        'variable "project_id" {\n'
        '  description = "GCP project id to provision into."\n'
        "  type        = string\n"
        "}\n"
        "\n"
        'variable "region" {\n'
        '  description = "GCP region to provision into (Artifact Registry + cluster lookup)."\n'
        "  type        = string\n"
        f'  default     = "{region_default}"\n'
        "}\n"
    )
    return header + """
variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "gke_cluster_name" {
  description = "Name of the existing GKE cluster the workload deploys into (referenced, not created)."
  type        = string
}

variable "gke_cluster_location" {
  description = "Location (region or zone) of the existing GKE cluster."
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace the workload (and its ServiceAccount) live in."
  type        = string
  default     = "default"
}

variable "kubernetes_service_account" {
  description = "Kubernetes ServiceAccount name Workload Identity binds the Google SA to."
  type        = string
  default     = "app"
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
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — GKE ref + Artifact Registry + SA + Workload Identity + WIF.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The project id and region are referenced via ``var.project_id`` / ``var.region``
 (whose default the IR drives in ``variables.tf``), so no project/region literal is
 hard-coded in a resource. No service account key is ever created or written — CI
 federates its OIDC token into the deploy service account via Workload Identity
 Federation, and the workload impersonates a Google service account via GKE
 Workload Identity, so the committed HCL carries no secret.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# gcp-gke backing infrastructure (generated — design §11.9: GKE refs + Workload Identity).
#
# Parameterised entirely by variables (see variables.tf). No literal GCP project
# ids, service account keys, or secrets appear here (R6). Resource names derive
# from the IR project name ({project_name!r}) for a stable, readable layout (R5.3).
# This module does NOT create a cluster — it references an existing one.

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

# --- GKE: reference the existing cluster (NOT created here) --------------------
data "google_container_cluster" "this" {{
  name     = var.gke_cluster_name
  location = var.gke_cluster_location
  project  = var.project_id
}}

locals {{
  name_prefix     = "{prefix}"
  ar_repo_id      = "${{local.name_prefix}}-repo"
  # The Workload Identity member for the Kubernetes ServiceAccount (KSA) that
  # impersonates the runtime GSA — the GKE-native keyless binding.
  wi_member = "serviceAccount:${{var.project_id}}.svc.id.goog[${{var.namespace}}/${{var.kubernetes_service_account}}]"
}}

# --- Artifact Registry: holds the image the pipeline builds + pushes (package) --
resource "google_artifact_registry_repository" "app" {{
  location      = var.region
  repository_id = local.ar_repo_id
  format        = "DOCKER"
  description   = "Images for the ${{local.name_prefix}} GKE workload."
}}

# --- Deploy service account: the identity CI impersonates (no JSON key) --------
resource "google_service_account" "deploy" {{
  account_id   = "${{local.name_prefix}}-deploy"
  display_name = "CI deploy SA for the ${{local.name_prefix}} GKE workload"
}}

# Push images to Artifact Registry.
resource "google_project_iam_member" "deploy_ar_writer" {{
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${{google_service_account.deploy.email}}"
}}

# Run kubectl/helm against the GKE cluster (get-credentials + apply).
resource "google_project_iam_member" "deploy_container_developer" {{
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${{google_service_account.deploy.email}}"
}}

# --- GKE Workload Identity: the runtime GSA the workload KSA impersonates -------
# This is the GKE-native keyless story (the GCP analogue of EKS IRSA): a Google
# service account for the workload, plus the IAM binding that lets the Kubernetes
# ServiceAccount impersonate it via the cluster's Workload Identity pool.
resource "google_service_account" "workload" {{
  account_id   = "${{local.name_prefix}}-workload"
  display_name = "Runtime GSA the ${{local.name_prefix}} GKE workload impersonates"
}}

resource "google_service_account_iam_member" "workload_identity_bind" {{
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.wi_member
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
 repo + image path, the referenced GKE cluster name/location, the deploy service
 account email, the runtime GSA email the KSA is annotated with, and the WIF
 provider resource name the CI OIDC step uses). Outputs make the module composable
 without re-deriving names elsewhere.
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

output "gke_cluster_name" {
  description = "Name of the GKE cluster the workload deploys into (deploy/verify verbs)."
  value       = data.google_container_cluster.this.name
}

output "gke_cluster_location" {
  description = "Location of the GKE cluster (get-credentials for deploy/verify)."
  value       = data.google_container_cluster.this.location
}

output "deploy_service_account_email" {
  description = "Service account email CI impersonates (via WIF) to push + deploy."
  value       = google_service_account.deploy.email
}

output "workload_service_account_email" {
  description = "Runtime GSA the Kubernetes ServiceAccount impersonates (GKE Workload Identity)."
  value       = google_service_account.workload.email
}

output "workload_identity_provider" {
  description = "WIF provider resource name the CI OIDC step federates against."
  value       = google_iam_workload_identity_pool_provider.ci.name
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never a real GCP project id, cluster name, CI repo,
 or key.
 """
    region_value = region if region else "us-central1"
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the gcp-gke backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real project specifics belong in the committed example (R6).

project_id   = "REPLACE_ME_PROJECT_ID"
region       = "{region_value}"
project_name = "{prefix}"

# The existing GKE cluster the workload deploys into (referenced, not created).
gke_cluster_name     = "REPLACE_ME-cluster"
gke_cluster_location = "{region_value}"

# Workload namespace + Kubernetes ServiceAccount bound to the runtime GSA (GKE WI).
namespace                  = "default"
kubernetes_service_account = "{prefix}"

# CI OIDC trust — scope the WIF provider to your CI repository.
ci_repository   = "your-org/your-repo"
oidc_issuer_uri = "{_DEFAULT_OIDC_ISSUER}"
"""
