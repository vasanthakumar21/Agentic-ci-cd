# Inputs for the azure-data-factory backing infrastructure (generated — R6:
# no literal subscription id, tenant, connection string, or secret; the
# bootstrap step fills these from local config).

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
  description = "Resource group the Data Factory and its dependencies live in."
  type        = string
}

variable "artifact_filesystem" {
  description = "ADLS Gen2 filesystem (container) name for ADF definitions/artifacts."
  type        = string
  default     = "definitions"
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
