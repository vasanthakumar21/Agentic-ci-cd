# Non-secret handles the pipeline and bootstrap consume downstream.

output "databricks_job_id" {
  description = "Databricks job id (the deploy/verify verbs run / check this job)."
  value       = databricks_job.app.id
}

output "databricks_job_name" {
  description = "Databricks job name the deploy verb resets/updates."
  value       = databricks_job.app.name
}

# Unity Catalog schema output — null when Unity Catalog is disabled
# (var.enable_unity_catalog = false), so downstream can detect its absence.
output "unity_catalog_schema" {
  description = "Unity Catalog schema the job writes into (catalog.schema), or null when Unity Catalog is disabled."
  value       = var.enable_unity_catalog ? "${var.unity_catalog}.${databricks_schema.app[0].name}" : null
}
