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
