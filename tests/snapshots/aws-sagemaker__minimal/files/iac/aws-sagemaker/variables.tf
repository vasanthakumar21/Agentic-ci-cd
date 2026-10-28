# Inputs for the aws-sagemaker backing infrastructure (generated — R6: no literal
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
  description = "Container image tag the SageMaker model runs (e.g. a git SHA)."
  type        = string
  default     = "latest"
}

variable "model_artifact_key" {
  description = "S3 object key of the trained model artifact (model.tar.gz)."
  type        = string
  default     = "sagemaker/models/model.tar.gz"
}

variable "instance_type" {
  description = "SageMaker endpoint instance type."
  type        = string
  default     = "ml.m5.large"
}

variable "instance_count" {
  description = "Number of instances behind the SageMaker endpoint."
  type        = number
  default     = 1
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
