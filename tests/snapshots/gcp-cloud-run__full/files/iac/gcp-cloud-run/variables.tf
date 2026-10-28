# Inputs for the gcp-cloud-run backing infrastructure (generated — R6: no
# literal GCP project ids, secrets, or service account keys; the bootstrap
# step fills these from local config).

variable "project_id" {
  description = "GCP project id to provision into."
  type        = string
}

variable "region" {
  description = "GCP region to provision into (Cloud Run + Artifact Registry)."
  type        = string
  default     = "eu-west-2"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "image_tag" {
  description = "Container image tag the Cloud Run service runs (e.g. a git SHA)."
  type        = string
  default     = "latest"
}

variable "container_port" {
  description = "Port the container listens on (Cloud Run sends traffic here)."
  type        = number
  default     = 8080
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

variable "allow_unauthenticated" {
  description = "Whether the Cloud Run service allows unauthenticated invocations."
  type        = bool
  default     = false
}
