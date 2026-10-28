# Non-secret handles the pipeline and bootstrap consume downstream.

output "acr_login_server" {
  description = "ACR login server the package verb pushes images to (e.g. myacr.azurecr.io)."
  value       = azurerm_container_registry.app.login_server
}

output "acr_name" {
  description = "Azure Container Registry name."
  value       = azurerm_container_registry.app.name
}

output "container_app_name" {
  description = "Container App name the deploy/verify verbs target."
  value       = azurerm_container_app.app.name
}

output "resource_group_name" {
  description = "Resource group the Container App lives in."
  value       = azurerm_resource_group.app.name
}

output "container_app_fqdn" {
  description = "Container App ingress FQDN (used by the verify revision check)."
  value       = azurerm_container_app.app.latest_revision_fqdn
}

output "ci_deploy_identity_client_id" {
  description = "User-assigned identity client id the CI pipeline logs in with via OIDC."
  value       = azurerm_user_assigned_identity.ci_deploy.client_id
}
