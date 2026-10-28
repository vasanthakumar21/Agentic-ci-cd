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
