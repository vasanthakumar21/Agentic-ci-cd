# gcp-gke backing infrastructure (generated — design §11.9: GKE refs + Workload Identity).
#
# Parameterised entirely by variables (see variables.tf). No literal GCP project
# ids, service account keys, or secrets appear here (R6). Resource names derive
# from the IR project name ('minimal-svc') for a stable, readable layout (R5.3).
# This module does NOT create a cluster — it references an existing one.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Project number is resolved at apply time — never committed as a literal (R6).
data "google_project" "current" {
  project_id = var.project_id
}

# --- GKE: reference the existing cluster (NOT created here) --------------------
data "google_container_cluster" "this" {
  name     = var.gke_cluster_name
  location = var.gke_cluster_location
  project  = var.project_id
}

locals {
  name_prefix     = "minimal-svc"
  ar_repo_id      = "${local.name_prefix}-repo"
  # The Workload Identity member for the Kubernetes ServiceAccount (KSA) that
  # impersonates the runtime GSA — the GKE-native keyless binding.
  wi_member = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.kubernetes_service_account}]"
}

# --- Artifact Registry: holds the image the pipeline builds + pushes (package) --
resource "google_artifact_registry_repository" "app" {
  location      = var.region
  repository_id = local.ar_repo_id
  format        = "DOCKER"
  description   = "Images for the ${local.name_prefix} GKE workload."
}

# --- Deploy service account: the identity CI impersonates (no JSON key) --------
resource "google_service_account" "deploy" {
  account_id   = "${local.name_prefix}-deploy"
  display_name = "CI deploy SA for the ${local.name_prefix} GKE workload"
}

# Push images to Artifact Registry.
resource "google_project_iam_member" "deploy_ar_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# Run kubectl/helm against the GKE cluster (get-credentials + apply).
resource "google_project_iam_member" "deploy_container_developer" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# --- GKE Workload Identity: the runtime GSA the workload KSA impersonates -------
# This is the GKE-native keyless story (the GCP analogue of EKS IRSA): a Google
# service account for the workload, plus the IAM binding that lets the Kubernetes
# ServiceAccount impersonate it via the cluster's Workload Identity pool.
resource "google_service_account" "workload" {
  account_id   = "${local.name_prefix}-workload"
  display_name = "Runtime GSA the ${local.name_prefix} GKE workload impersonates"
}

resource "google_service_account_iam_member" "workload_identity_bind" {
  service_account_id = google_service_account.workload.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.wi_member
}

# --- Workload Identity Federation: keyless CI auth (no JSON key) ---------------
resource "google_iam_workload_identity_pool" "ci" {
  workload_identity_pool_id = "${local.name_prefix}-ci-pool"
  display_name              = "CI pool for ${local.name_prefix}"
}

resource "google_iam_workload_identity_pool_provider" "ci" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.ci.workload_identity_pool_id
  workload_identity_pool_provider_id = "${local.name_prefix}-ci-provider"
  display_name                       = "CI OIDC provider for ${local.name_prefix}"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  # Only tokens from the client's CI repository may federate (scoped trust).
  attribute_condition = "assertion.repository == \"${var.ci_repository}\""

  oidc {
    issuer_uri = var.oidc_issuer_uri
  }
}

# Bind the CI repo's federated identity to the deploy service account (keyless).
resource "google_service_account_iam_member" "wif_bind" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.ci.name}/attribute.repository/${var.ci_repository}"
}
