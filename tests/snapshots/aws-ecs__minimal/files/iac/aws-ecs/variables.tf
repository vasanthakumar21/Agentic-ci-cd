# Inputs for the aws-ecs backing infrastructure (generated — R6: no literal
# account specifics or secrets; the bootstrap step fills these from local config).

variable "aws_region" {
  description = "AWS region to provision into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "image_tag" {
  description = "Container image tag the ECS task definition runs (e.g. a git SHA)."
  type        = string
  default     = "latest"
}

variable "container_port" {
  description = "Port the container listens on."
  type        = number
  default     = 8080
}

variable "desired_count" {
  description = "Number of ECS service tasks to run."
  type        = number
  default     = 1
}

variable "task_cpu" {
  description = "Fargate task CPU units."
  type        = string
  default     = "256"
}

variable "task_memory" {
  description = "Fargate task memory (MiB)."
  type        = string
  default     = "512"
}

variable "subnet_ids" {
  description = "Subnets the ECS service places tasks in (per-client networking)."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups attached to the ECS service tasks."
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
