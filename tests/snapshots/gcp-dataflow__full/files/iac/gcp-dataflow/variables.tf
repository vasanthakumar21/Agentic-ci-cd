# Inputs for the gcp-dataflow backing infrastructure (generated — R6: no
# literal GCP project ids, secrets, or service account keys; the bootstrap
# step fills these from local config).

variable "project_id" {
  description = "GCP project id to provision into."
  type        = string
}

variable "region" {
  description = "GCP region to provision into (Dataflow + GCS bucket)."
  type        = string
  default     = "eu-west-2"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "template_key" {
  description = "GCS object key of the staged Dataflow template the job launches from (uploaded by the package verb)."
  type        = string
  default     = "dataflow/templates/main.json"
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
