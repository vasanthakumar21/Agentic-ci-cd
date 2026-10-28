# Inputs for the aws-batch backing infrastructure (generated — R6: no literal
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

variable "image_tag" {
  description = "Container image tag the Batch job definition runs (e.g. a git SHA)."
  type        = string
  default     = "latest"
}

variable "job_vcpu" {
  description = "vCPU allocated to each Batch job container."
  type        = string
  default     = "1"
}

variable "job_memory" {
  description = "Memory (MiB) allocated to each Batch job container."
  type        = string
  default     = "2048"
}

variable "max_vcpus" {
  description = "Maximum vCPU the Fargate compute environment may scale to."
  type        = number
  default     = 16
}

variable "subnet_ids" {
  description = "Subnets the Batch Fargate compute environment places jobs in (per-client networking)."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups attached to the Batch Fargate jobs."
  type        = list(string)
}

variable "oidc_provider_url" {
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}

variable "oidc_subject" {
  description = "OIDC subject the deploy role trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}

variable "oidc_client_id_list" {
  description = "Allowed OIDC audiences for the identity provider."
  type        = list(string)
  default     = ["sts.amazonaws.com"]
}
