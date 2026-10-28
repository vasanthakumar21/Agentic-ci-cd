# Inputs for the azure-functions backing infrastructure (generated — R6: no
# literal subscription id, tenant, connection string, or secret; the bootstrap
# step fills these from local config).

variable "location" {
  description = "Azure region to provision into."
  type        = string
  default     = "eu-west-2"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group the Function App and its dependencies live in."
  type        = string
}

variable "runtime_name" {
  description = "Function App runtime stack (e.g. python, node, dotnet-isolated)."
  type        = string
  default     = "python"
}

variable "runtime_version" {
  description = "Function App runtime version (e.g. 3.11 for python)."
  type        = string
  default     = "3.11"
}

variable "service_plan_sku" {
  description = "App Service plan SKU for the Function App (Y1 = consumption)."
  type        = string
  default     = "Y1"
}

variable "storage_account_tier" {
  description = "Tier for the backing storage account."
  type        = string
  default     = "Standard"
}

variable "storage_replication_type" {
  description = "Replication type for the backing storage account."
  type        = string
  default     = "LRS"
}

variable "oidc_issuer_url" {
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}

variable "oidc_subject" {
  description = "OIDC subject the federated credential trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}

variable "oidc_audience" {
  description = "Allowed OIDC audience for the Azure AD federated credential."
  type        = string
  default     = "api://AzureADTokenExchange"
}
