# Inputs for the gcp-dataproc backing infrastructure (generated — R6: no
# literal GCP project ids, secrets, or service account keys; the bootstrap
# step fills these from local config).

variable "project_id" {
  description = "GCP project id to provision into."
  type        = string
}

variable "region" {
  description = "GCP region to provision into (Dataproc + GCS staging bucket)."
  type        = string
  default     = "us-central1"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "main_python_file_key" {
  description = "GCS object key of the main PySpark script the batch runs (uploaded by the package verb)."
  type        = string
  default     = "dataproc/scripts/main.py"
}

variable "python_file_uris_key" {
  description = "GCS object key of the extra python libs/wheel bundled for the batch (pythonFileUris)."
  type        = string
  default     = "dataproc/libs/extra-python-libs.zip"
}

variable "runtime_version" {
  description = "Dataproc Serverless runtime version for the batch."
  type        = string
  default     = "2.2"
}

variable "ci_repository" {
  description = "Client CI repository the WIF provider trusts (e.g. org/name)."
  type        = string
}

variable "oidc_issuer_uri" {
  description = "CI provider OIDC issuer URI (e.g. https://token.actions.githubusercontent.com)."
  type        = string
  default     = "https://token.actions.githubusercontent.com"
}
