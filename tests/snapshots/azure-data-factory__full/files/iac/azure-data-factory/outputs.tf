# Non-secret handles the pipeline and bootstrap consume downstream.

output "data_factory_name" {
  description = "Data Factory name the deploy/verify verbs target."
  value       = azurerm_data_factory.app.name
}

output "resource_group_name" {
  description = "Resource group the Data Factory lives in."
  value       = azurerm_resource_group.adf.name
}

output "storage_account_name" {
  description = "ADLS Gen2 account the package verb uploads ADF definitions to."
  value       = azurerm_storage_account.lake.name
}

output "ci_deploy_identity_client_id" {
  description = "User-assigned identity client id the CI pipeline logs in with via OIDC."
  value       = azurerm_user_assigned_identity.ci_deploy.client_id
}
