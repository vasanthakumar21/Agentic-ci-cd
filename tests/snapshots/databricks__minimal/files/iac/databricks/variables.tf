# Inputs for the databricks backing infrastructure (generated — R6: no literal
# workspace URL, token, or account specifics; the bootstrap step / CI secret
# store fills these from local config).

variable "databricks_host" {
  description = "Databricks workspace URL (e.g. https://my-ws.cloud.databricks.com)."
  type        = string
}

variable "databricks_token" {
  description = "Databricks PAT / OAuth token used by Terraform (secret — by reference, R6)."
  type        = string
  sensitive   = true
}

variable "cloud_region" {
  description = "Informational cloud region (non-secret)."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "artifact_path" {
  description = "Workspace path to the uploaded artifact the job runs (DBFS or a Unity Catalog Volume, e.g. dbfs:/FileStore/artifacts/app.whl)."
  type        = string
}

variable "entry_point" {
  description = "Python wheel entry point the job task invokes."
  type        = string
  default     = "main"
}

variable "package_name" {
  description = "Installed wheel package name the job task runs (python_wheel_task.package_name)."
  type        = string
}

variable "enable_unity_catalog" {
  description = "Whether to create a Unity Catalog schema for the job output. Set false for workspaces without Unity Catalog (the schema + its output are then omitted)."
  type        = bool
  default     = true
}

variable "unity_catalog" {
  description = "Existing Unity Catalog catalog the job schema is created in (used only when enable_unity_catalog = true)."
  type        = string
  default     = "main"
}

variable "job_schedule_cron" {
  description = "Optional Quartz cron expression for the job schedule. Empty string (default) leaves the job unscheduled (run on demand via the deploy/verify verbs)."
  type        = string
  default     = ""
}

variable "job_schedule_timezone" {
  description = "IANA timezone id for the job schedule (used only when job_schedule_cron is non-empty)."
  type        = string
  default     = "UTC"
}

variable "run_as_user_name" {
  description = "Optional user/service-principal the job runs as. Empty string (default) runs as the job owner (the identity Terraform authenticates with)."
  type        = string
  default     = ""
}

variable "spark_version" {
  description = "Databricks runtime version for the job cluster."
  type        = string
  default     = "15.4.x-scala2.12"
}

variable "node_type_id" {
  description = "Worker node type for the job cluster."
  type        = string
  default     = "i3.xlarge"
}

variable "num_workers" {
  description = "Number of workers for the job cluster."
  type        = number
  default     = 2
}
