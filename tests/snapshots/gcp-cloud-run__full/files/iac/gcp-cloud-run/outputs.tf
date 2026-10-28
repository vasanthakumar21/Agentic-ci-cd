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
