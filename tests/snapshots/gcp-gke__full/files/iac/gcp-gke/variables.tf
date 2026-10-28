# Inputs for the gcp-gke backing infrastructure (generated — R6: no literal
# GCP project ids, secrets, or service account keys; the bootstrap step fills
# these from local config).

variable "project_id" {
  description = "GCP project id to provision into."
  type        = string
}

variable "region" {
  description = "GCP region to provision into (Artifact Registry + cluster lookup)."
  type        = string
  default     = "eu-west-2"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "gke_cluster_name" {
  description = "Name of the existing GKE cluster the workload deploys into (referenced, not created)."
  type        = string
}

variable "gke_cluster_location" {
  description = "Location (region or zone) of the existing GKE cluster."
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace the workload (and its ServiceAccount) live in."
  type        = string
  default     = "default"
}

variable "kubernetes_service_account" {
  description = "Kubernetes ServiceAccount name Workload Identity binds the Google SA to."
  type        = string
  default     = "app"
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
