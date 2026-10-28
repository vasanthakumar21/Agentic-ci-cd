# gcp-cloud-run backing infrastructure (generated — design §11.9: Cloud Run + WIF/OIDC).
#
# Parameterised entirely by variables (see variables.tf). No literal GCP project
# ids, service account keys, or secrets appear here (R6). Resource names derive
# from the IR project name ('minimal-svc') for a stable, readable layout (R5.3).

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

locals {
  name_prefix     = "minimal-svc"
  ar_repo_id      = "${local.name_prefix}-repo"
  service_name    = "${local.name_prefix}-service"
  image_reference = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}/${local.name_prefix}:${var.image_tag}"
}

# --- Artifact Registry: holds the image the pipeline builds + pushes (package) --
resource "google_artifact_registry_repository" "app" {
  location      = var.region
  repository_id = local.ar_repo_id
  format        = "DOCKER"
  description   = "Images for the ${local.name_prefix} Cloud Run service."
}

# --- Cloud Run v2 service: runs the pushed image (deploy verb target) ----------
resource "google_cloud_run_v2_service" "app" {
  name     = local.service_name
  location = var.region

  template {
    service_account = google_service_account.deploy.email

    containers {
      image = local.image_reference

      ports {
        container_port = var.container_port
      }
    }
  }
}

# Optionally expose the service publicly (off by default — least privilege).
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.app.name
  location = google_cloud_run_v2_service.app.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Deploy service account: the identity CI impersonates (no JSON key) --------
resource "google_service_account" "deploy" {
  account_id   = "${local.name_prefix}-deploy"
  display_name = "CI deploy SA for the ${local.name_prefix} Cloud Run service"
}

# Push images to Artifact Registry.
resource "google_project_iam_member" "deploy_ar_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# Deploy / update the Cloud Run service.
resource "google_project_iam_member" "deploy_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# Allow the service to act as the runtime service account.
resource "google_service_account_iam_member" "deploy_act_as" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
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
