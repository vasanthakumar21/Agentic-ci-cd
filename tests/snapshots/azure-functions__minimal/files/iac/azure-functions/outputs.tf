# Non-secret handles the pipeline and bootstrap consume downstream.

output "function_app_name" {
  description = "Function App name the deploy/verify verbs target."
  value       = azurerm_linux_function_app.func.name
}

output "function_app_default_hostname" {
  description = "Function App default hostname (used by the verify smoke test)."
  value       = azurerm_linux_function_app.func.default_hostname
}

output "resource_group_name" {
  description = "Resource group the Function App lives in."
  value       = azurerm_resource_group.func.name
}

output "storage_account_name" {
  description = "Backing storage account name for the Function App."
  value       = azurerm_storage_account.func.name
}

output "ci_deploy_identity_client_id" {
  description = "User-assigned identity client id the CI pipeline logs in with via OIDC."
  value       = azurerm_user_assigned_identity.ci_deploy.client_id
}
