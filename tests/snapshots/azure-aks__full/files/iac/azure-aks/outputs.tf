# Non-secret handles the pipeline and bootstrap consume downstream.

output "acr_login_server" {
  description = "ACR login server the package verb pushes images to (e.g. myacr.azurecr.io)."
  value       = azurerm_container_registry.app.login_server
}

output "acr_name" {
  description = "Azure Container Registry name."
  value       = azurerm_container_registry.app.name
}

output "workload_identity_client_id" {
  description = "User-assigned identity client id the workload ServiceAccount is annotated with."
  value       = azurerm_user_assigned_identity.workload.client_id
}

output "ci_deploy_identity_client_id" {
  description = "User-assigned identity client id the CI pipeline logs in with via OIDC."
  value       = azurerm_user_assigned_identity.ci_deploy.client_id
}

output "aks_cluster_name" {
  description = "Name of the referenced AKS cluster the workload deploys to."
  value       = data.azurerm_kubernetes_cluster.aks.name
}
