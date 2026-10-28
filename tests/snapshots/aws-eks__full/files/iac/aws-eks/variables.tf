# Inputs for the aws-eks backing infrastructure (generated — R6: no literal
# account specifics or secrets; the bootstrap step fills these from local config).

variable "aws_region" {
  description = "AWS region to provision into."
  type        = string
  default     = "eu-west-2"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "eks_cluster_name" {
  description = "Name of the existing EKS cluster the workload deploys into (referenced, not created)."
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace the workload (and its ServiceAccount) live in."
  type        = string
  default     = "default"
}

variable "service_account_name" {
  description = "Workload ServiceAccount name IRSA binds the workload IAM role to."
  type        = string
  default     = "app"
}

variable "oidc_provider_url" {
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}

variable "oidc_subject" {
  description = "CI OIDC subject the deploy role trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}

variable "oidc_client_id_list" {
  description = "Allowed OIDC audiences for the CI identity provider."
  type        = list(string)
  default     = ["sts.amazonaws.com"]
}
