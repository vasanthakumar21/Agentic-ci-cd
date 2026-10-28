# gcp-dataproc backing infrastructure (generated — design §11.9: Dataproc + WIF).
#
# The data-job shape (like aws-glue / databricks): NO container image — the
# deployable is a Dataproc Serverless PySpark BATCH running a script staged in GCS.
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
  name_prefix     = "full-svc"
  staging_bucket  = "${local.name_prefix}-${var.project_id}-dataproc-staging"
  batch_id_prefix = "${local.name_prefix}-batch"
}

# --- GCS: holds the versioned PySpark script + extra libs (package verb) -------
resource "google_storage_bucket" "staging" {
  name                        = local.staging_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }
}

# --- Dataproc service account: the identity the batch runs as -------------------
resource "google_service_account" "dataproc" {
  account_id   = "${local.name_prefix}-dataproc"
  display_name = "Dataproc batch runtime SA for ${local.name_prefix}"
}

# The batch SA reads the staging bucket and writes driver/worker logs.
resource "google_storage_bucket_iam_member" "dataproc_reader" {
  bucket = google_storage_bucket.staging.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.dataproc.email}"
}

resource "google_project_iam_member" "dataproc_worker" {
  project = var.project_id
  role    = "roles/dataproc.worker"
  member  = "serviceAccount:${google_service_account.dataproc.email}"
}

# --- Dataproc Serverless PySpark batch (deploy verb target) --------------------
# The main + extra python files are VARIABLE keys into the versioned staging bucket
# so no artifact value is committed; the deploy verb submits the batch + the verify
# verb polls its state.
resource "google_dataproc_batch" "app" {
  batch_id = "${local.batch_id_prefix}-tf"
  location = var.region

  runtime_config {
    version = var.runtime_version
  }

  environment_config {
    execution_config {
      service_account = google_service_account.dataproc.email
      staging_bucket  = google_storage_bucket.staging.name
    }
  }

  pyspark_batch {
    main_python_file_uri = "gs://${google_storage_bucket.staging.name}/${var.main_python_file_key}"
    python_file_uris     = ["gs://${google_storage_bucket.staging.name}/${var.python_file_uris_key}"]
  }

  lifecycle {
    # The deploy verb submits new batches out of band; ignore drift on the spec.
    ignore_changes = [pyspark_batch, runtime_config]
  }
}

# --- Deploy service account: the identity CI impersonates (no JSON key) --------
resource "google_service_account" "deploy" {
  account_id   = "${local.name_prefix}-deploy"
  display_name = "CI deploy SA for the ${local.name_prefix} Dataproc batches"
}

# Stage artifacts into the GCS staging bucket.
resource "google_storage_bucket_iam_member" "deploy_writer" {
  bucket = google_storage_bucket.staging.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.deploy.email}"
}

# Submit / manage Dataproc batches.
resource "google_project_iam_member" "deploy_dataproc_editor" {
  project = var.project_id
  role    = "roles/dataproc.editor"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# Allow the deploy SA to act as the Dataproc runtime SA (submit under its identity).
resource "google_service_account_iam_member" "deploy_act_as" {
  service_account_id = google_service_account.dataproc.name
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
