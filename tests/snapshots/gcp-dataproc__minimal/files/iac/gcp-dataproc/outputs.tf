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
