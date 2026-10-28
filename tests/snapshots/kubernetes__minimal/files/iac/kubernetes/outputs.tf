# Non-secret handles the pipeline and bootstrap consume downstream.

output "deployment_name" {
  description = "Deployment name the deploy/verify verbs target (rollout status)."
  value       = kubernetes_deployment.app.metadata[0].name
}

output "service_name" {
  description = "Service name fronting the Deployment pods."
  value       = kubernetes_service.app.metadata[0].name
}

output "namespace" {
  description = "Namespace the workload is deployed into."
  value       = var.namespace
}
