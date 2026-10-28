# Non-secret handles the pipeline and bootstrap consume downstream.

output "spark_pool_name" {
  description = "Synapse Spark pool name the Spark job definitions run on."
  value       = azurerm_synapse_spark_pool.app.name
}

output "storage_account_name" {
  description = "ADLS Gen2 account the package verb uploads job artifacts to."
  value       = azurerm_storage_account.lake.name
}

output "artifact_filesystem" {
  description = "ADLS Gen2 filesystem (container) that holds job artifacts/output."
  value       = azurerm_storage_data_lake_gen2_filesystem.artifacts.name
}

output "synapse_workspace_name" {
  description = "Name of the referenced Synapse workspace the Spark jobs belong to."
  value       = data.azurerm_synapse_workspace.ws.name
}

output "ci_deploy_identity_client_id" {
  description = "User-assigned identity client id the CI pipeline logs in with via OIDC."
  value       = azurerm_user_assigned_identity.ci_deploy.client_id
}
