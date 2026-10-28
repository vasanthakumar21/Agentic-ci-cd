# Inputs for the azure-aks backing infrastructure (generated — R6: no literal
# subscription id, tenant, or secret; the bootstrap step fills these from local
# config).

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
  description = "Resource group the ACR and identities live in."
  type        = string
}

variable "aks_cluster_name" {
  description = "Name of the EXISTING AKS cluster the workload deploys to (referenced, not created)."
  type        = string
}

variable "aks_resource_group_name" {
  description = "Resource group of the existing AKS cluster."
  type        = string
}

variable "acr_sku" {
  description = "SKU for the Azure Container Registry (Basic/Standard/Premium)."
  type        = string
  default     = "Standard"
}

variable "service_account_namespace" {
  description = "Kubernetes namespace of the workload ServiceAccount (workload identity binding)."
  type        = string
  default     = "default"
}

variable "service_account_name" {
  description = "Kubernetes ServiceAccount name the workload identity is bound to."
  type        = string
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
