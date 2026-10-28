# gcp-dataflow backing infrastructure (generated — design §11.9: Dataflow + WIF).
#
# The data-job shape (like aws-glue / databricks): NO container image — the
# deployable is a Dataflow job launched from a staged TEMPLATE in GCS.
#
# Parameterised entirely by variables (see variables.tf). No literal GCP project
# ids, service account keys, or secrets appear here (R6). Resource names derive
# from the IR project name ('full-svc') for a stable, readable layout (R5.3).

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
  name_prefix      = "full-svc"
  dataflow_bucket  = "${local.name_prefix}-${var.project_id}-dataflow"
  job_name_prefix  = "${local.name_prefix}-job"
}

# --- GCS: holds staged templates + staging/temp locations (package verb) -------
resource "google_storage_bucket" "dataflow" {
  name                        = local.dataflow_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }
}

# --- Dataflow worker service account: the identity workers run as --------------
resource "google_service_account" "worker" {
  account_id   = "${local.name_prefix}-dataflow-worker"
  display_name = "Dataflow worker SA for ${local.name_prefix}"
}

resource "google_project_iam_member" "worker_dataflow_worker" {
  project = var.project_id
  role    = "roles/dataflow.worker"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_storage_bucket_iam_member" "worker_bucket_access" {
  bucket = google_storage_bucket.dataflow.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.worker.email}"
}

# --- Deploy service account: the identity CI impersonates (no JSON key) --------
resource "google_service_account" "deploy" {
  account_id   = "${local.name_prefix}-deploy"
  display_name = "CI deploy SA for the ${local.name_prefix} Dataflow jobs"
}

# Stage templates/artifacts into the GCS bucket.
resource "google_storage_bucket_iam_member" "deploy_writer" {
  bucket = google_storage_bucket.dataflow.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.deploy.email}"
}

# Launch / manage Dataflow jobs.
resource "google_project_iam_member" "deploy_dataflow_developer" {
  project = var.project_id
  role    = "roles/dataflow.developer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# Allow the deploy SA to act as the Dataflow worker SA (launch under its identity).
resource "google_service_account_iam_member" "deploy_act_as" {
  service_account_id = google_service_account.worker.name
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
