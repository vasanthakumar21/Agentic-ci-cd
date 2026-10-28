# Inputs for the azure-synapse backing infrastructure (generated — R6: no
# literal subscription id, tenant, storage key, or secret; the bootstrap step
# fills these from local config).

variable "location" {
  description = "Azure region to provision into."
  type        = string
  default     = "uksouth"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group the Spark pool, storage, and identities live in."
  type        = string
}

variable "synapse_workspace_name" {
  description = "Name of the EXISTING Synapse workspace the Spark pool + jobs belong to (referenced, not created)."
  type        = string
}

variable "synapse_resource_group_name" {
  description = "Resource group of the existing Synapse workspace."
  type        = string
}

variable "spark_version" {
  description = "Apache Spark version for the Synapse Spark pool."
  type        = string
  default     = "3.4"
}

variable "node_size" {
  description = "Node size for the Synapse Spark pool (Small/Medium/Large)."
  type        = string
  default     = "Small"
}

variable "node_count" {
  description = "Number of nodes for the Synapse Spark pool (used when autoscale is off)."
  type        = number
  default     = 3
}

variable "artifact_filesystem" {
  description = "ADLS Gen2 filesystem (container) name for job artifacts/output."
  type        = string
  default     = "artifacts"
}

variable "oidc_issuer_url" {
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}

variable "oidc_subject" {
  description = "OIDC subject the CI federated credential trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}

variable "oidc_audience" {
  description = "Allowed OIDC audience for the Azure AD federated credential."
  type        = string
  default     = "api://AzureADTokenExchange"
}
