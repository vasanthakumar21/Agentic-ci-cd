# Inputs for the azure-container-apps backing infrastructure (generated — R6:
# no literal subscription id, tenant, or secret; the bootstrap step fills these
# from local config).

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
  description = "Resource group the Container App and its dependencies live in."
  type        = string
}

variable "acr_sku" {
  description = "SKU for the Azure Container Registry (Basic/Standard/Premium)."
  type        = string
  default     = "Standard"
}

variable "initial_image" {
  description = "Placeholder image the Container App starts with before the first deploy (the deploy verb overrides it)."
  type        = string
  default     = "mcr.microsoft.com/k8se/quickstart:latest"
}

variable "container_port" {
  description = "Ingress target port the container listens on."
  type        = number
  default     = 8080
}

variable "min_replicas" {
  description = "Minimum number of Container App replicas."
  type        = number
  default     = 0
}

variable "max_replicas" {
  description = "Maximum number of Container App replicas."
  type        = number
  default     = 2
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
